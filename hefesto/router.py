"""Enrutado de `hefesto run`: cadena `use` del rol, allowlist, cooldowns y fallback sin gastar intentos.

Cuota y rate limit no son fallos de la tarea: el proveedor pasa a cooldown y la misma petición va al
siguiente de la cadena. Solo un fallo real del trabajo cuenta como intento (y eso lo decide `fail`/`done`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hefesto import acceptance, adapters, config, guard, log, packet, preflight, providers, quota, roadmap, state
from hefesto.adapters.base import AdapterResult, Call
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_OK, EXIT_UNAVAILABLE, HefestoError
from hefesto.workflow import new_run_id

ROLES = config.ROLES


@dataclass
class Hop:
    entry: str
    model_id: str
    outcome: str


@dataclass
class RunOutcome:
    code: int
    run_id: str
    result: AdapterResult | None
    hops: list[Hop] = field(default_factory=list)
    packet_path: str | None = None
    next_available: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"run_id": self.run_id, "hops": [h.__dict__ for h in self.hops],
                                "packet": self.packet_path}
        if self.result:
            data.update(self.result.to_dict())
        else:
            data.update({"success": False, "error_type": "unavailable",
                         "summary": "ningún proveedor de la cadena está disponible"})
        if self.next_available:
            data["next_available"] = self.next_available
        return data


def plan_route(cfg: config.Config, role: str, author_providers: set[str] | None = None) -> list[Hop]:
    """Qué haría el enrutador ahora mismo, sin llamar a nada."""
    hops: list[Hop] = []
    authors = author_providers or set()
    chain = cfg.role_chain(role)
    other_available = any(
        providers.split_entry(e)[0] not in authors and providers.available(cfg, providers.split_entry(e)[0])[0]
        for e in chain
    ) if (authors and role == "reviewer") else False
    deferred_authors: list[str] = []
    for entry in chain:
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        if role == "reviewer" and provider in authors and other_available:
            hops.append(Hop(entry, mid, "saltado: autor del cambio"))
            deferred_authors.append(entry)
            continue
        ok, reason = providers.available(cfg, provider)
        hops.append(Hop(entry, mid, "elegido" if ok else f"saltado: {reason}"))
        if ok:
            return hops
    for entry in deferred_authors:
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        ok, reason = providers.available(cfg, provider)
        hops.append(Hop(entry, mid, "elegido" if ok else f"saltado: {reason}"))
        if ok:
            return hops
    return hops


def _task_context(project: Path, task_id: str | None) -> tuple[roadmap.Task | None, roadmap.Roadmap | None,
                                                                dict[str, Any], Path, str | None]:
    data = state.load(project)
    if task_id is None and data["status"] == "IN_PROGRESS":
        task_id = data["current_task_id"]
    if task_id is None:
        return None, None, data, project, None
    rm = roadmap.load_valid(project)
    task = rm.get(task_id)
    current = data.get("current_task_id") == task_id and data["status"] == "IN_PROGRESS"
    workdir = Path(data["workdir"]) if current and data.get("workdir") else project
    return task, rm, data, workdir, data.get("run_id") if current else None


def _author_providers(project: Path, role: str, task_run_id: str | None) -> set[str]:
    if role != "reviewer" or not task_run_id:
        return set()
    return {
        str(r["provider"])
        for r in acceptance.runs_for_task(project, task_run_id)
        if r.get("role") in acceptance.AUTHOR_ROLES and r.get("success") and r.get("provider")
    }


def _other_provider_available(project: Path, cfg: config.Config, entries: list[str],
                              author_providers: set[str]) -> bool:
    for entry in entries:
        prov = providers.split_entry(entry)[0]
        if prov not in author_providers:
            ok, _ = preflight.ensure(project, cfg, prov)
            if ok:
                return True
    return False


def run(project: Path, role: str, instruction: str, task_id: str | None = None, dry_run: bool = False) -> RunOutcome:
    if role not in ROLES:
        raise HefestoError(f"Rol desconocido `{role}` ({', '.join(ROLES)}).", EXIT_INPUT)
    cfg = config.load(project)
    run_id = new_run_id()
    if dry_run:
        authors: set[str] = set()
        if role == "reviewer":
            try:
                _, _, _, _, task_run_id = _task_context(project, task_id)
                authors = _author_providers(project, role, task_run_id)
            except Exception:
                authors = set()
        hops = plan_route(cfg, role, authors)
        chosen = any(h.outcome == "elegido" for h in hops)
        return RunOutcome(EXIT_OK if chosen else EXIT_UNAVAILABLE, run_id, None, hops)
    if guard.find_secrets(instruction):
        raise HefestoError("La instrucción contiene un secreto; no se envía a ningún proveedor.", EXIT_BLOCKED)
    task, rm, data, workdir, task_run_id = _task_context(project, task_id)
    authors = _author_providers(project, role, task_run_id) if role == "reviewer" else set()
    hops: list[Hop] = []
    timeout = int(cfg.get("limits", "timeout_seconds"))
    default_cooldown = int(cfg.get("quota", "cooldown_default_minutes"))
    packet_path: Path | None = None
    chain = cfg.role_chain(role)
    other_available = _other_provider_available(project, cfg, chain, authors) if authors else False

    def try_entry(entry: str) -> RunOutcome | None:
        nonlocal packet_path
        provider, model = providers.split_entry(entry)
        mid = providers.model_id(cfg, entry)
        ok, reason = preflight.ensure(project, cfg, provider)
        if not ok:
            hops.append(Hop(entry, mid, f"saltado: {reason}"))
            return None
        pkt = packet.build(project, cfg, role, instruction, task, rm, data, mid, workdir)
        packet_path = packet.save(project, pkt, task.id if task else None, role)
        call = Call(role=role, prompt=pkt.text, task_id=task.id if task else None, run_id=run_id,
                    model=model or str(cfg.get("adapters", provider, "model", default="") or ""),
                    workdir=workdir, project=project, packet_path=packet_path.resolve(), timeout=timeout)
        result = adapters.get(cfg, provider).run(call)
        quota.record_call(provider)
        _record(project, role, task, task_run_id, run_id, mid, result, packet_path)
        if result.error_type in ("quota", "rate_limit"):
            until = quota.cooldown_until(result.reset_hint or result.summary, default_cooldown)
            quota.mark_cooldown(provider, until, result.error_type, result.summary)
            hops.append(Hop(entry, mid, f"{result.error_type}: cooldown hasta {until.isoformat()}"))
            return None
        if result.error_type == "auth":
            quota.mark_auth_error(provider, result.summary)
            hops.append(Hop(entry, mid, "auth: marcado y saltado"))
            return None
        if result.error_type == "missing_binary":
            hops.append(Hop(entry, mid, "binario no disponible"))
            return None
        hops.append(Hop(entry, mid, "respondió"))
        if result.error_type == "denied_by_policy":
            return RunOutcome(EXIT_BLOCKED, run_id, result, hops, _rel(project, packet_path))
        if result.success:
            quota.mark_ok(provider)
        return RunOutcome(EXIT_OK if result.success else EXIT_INPUT, run_id, result, hops, _rel(project, packet_path))

    deferred_authors: list[str] = []
    for entry in chain:
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        if role == "reviewer" and provider in authors and other_available:
            hops.append(Hop(entry, mid, "saltado: autor del cambio"))
            deferred_authors.append(entry)
            continue
        outcome = try_entry(entry)
        if outcome is not None:
            return outcome

    for entry in deferred_authors:
        outcome = try_entry(entry)
        if outcome is not None:
            return outcome

    return RunOutcome(EXIT_UNAVAILABLE, run_id, None, hops, _rel(project, packet_path) if packet_path else None,
                      next_available=earliest_cooldown(cfg, role))


def earliest_cooldown(cfg: config.Config, role: str) -> str | None:
    times = []
    for entry in cfg.role_chain(role):
        current = quota.provider_state(providers.split_entry(entry)[0])
        if current.get("status") == "cooldown" and current.get("cooldown_until"):
            times.append(current["cooldown_until"])
    return min(times) if times else None


def _record(project: Path, role: str, task: roadmap.Task | None, task_run_id: str | None, run_id: str, mid: str,
            result: AdapterResult, packet_path: Path) -> None:
    usage = result.usage if isinstance(result.usage, dict) else {}
    log.record(project, "usage", {
        "run_id": run_id, "provider": result.provider, "model": mid, "role": role, "duration": result.duration,
        "result": "success" if result.success else (result.error_type or "failure"),
        "input_tokens": usage.get("input_tokens", "unknown"), "output_tokens": usage.get("output_tokens", "unknown"),
    })
    log.record(project, "runs", {
        "run_id": run_id, "task_id": task.id if task else None, "task_run_id": task_run_id, "role": role,
        "provider": result.provider, "model_id": mid, "success": result.success, "error_type": result.error_type,
        "summary": result.summary[:500], "log_path": result.log_path, "packet": _rel(project, packet_path),
    })


def _rel(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return str(path)
