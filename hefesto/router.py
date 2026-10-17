"""Enrutado de `hefesto run`: cadena `use` del rol, allowlist, cooldowns y fallback sin gastar intentos.

Cuota y rate limit no son fallos de la tarea: el proveedor pasa a cooldown y la misma petición va al
siguiente de la cadena. Solo un fallo real del trabajo cuenta como intento (y eso lo decide `fail`/`done`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

from hefesto import acceptance, adapters, config, guard, log, packet, preflight, providers, quota, roadmap, state
from hefesto.adapters.base import AdapterResult, Call
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_OK, EXIT_UNAVAILABLE, HefestoError, read_jsonl
from hefesto.workflow import new_run_id

ROLES = config.ROLES
CHALLENGE_ROLE_ORDER = ("reviewer", "core", "forge", "runner", "visual")


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
    challenge: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"run_id": self.run_id, "hops": [h.__dict__ for h in self.hops],
                                "packet": self.packet_path, "challenge": self.challenge}
        if self.result:
            data.update(self.result.to_dict())
        else:
            data.update({"success": False, "error_type": "unavailable",
                         "summary": "ningún proveedor de la cadena está disponible"})
        if self.next_available:
            data["next_available"] = self.next_available
        return data


def success_stats(project: Path, role: str) -> dict[str, dict[str, Any]]:
    """Por model_id, desde logs/runs.jsonl del rol: tasks, pass1 y passk."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in read_jsonl(log.logs_dir(project) / "runs.jsonl"):
        if row.get("role") != role:
            continue
        task_id = row.get("task_id")
        model_id = row.get("model_id")
        if not task_id or not model_id:
            continue
        grouped.setdefault(str(model_id), {}).setdefault(str(task_id), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for mid, by_task in grouped.items():
        total = len(by_task)
        if total == 0:
            continue
        first_ok = 0
        any_ok = 0
        for runs in by_task.values():
            ordered = sorted(runs, key=lambda r: str(r.get("at") or ""))
            if bool(ordered[0].get("success")):
                first_ok += 1
            if any(bool(r.get("success")) for r in ordered):
                any_ok += 1
        out[mid] = {
            "tasks": total,
            "pass1": first_ok / total,
            "passk": any_ok / total,
        }
    return out


def _ordered_chain(
    cfg: config.Config,
    role: str,
    project: Path | None,
    chain: list[str] | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    entries = list(chain if chain is not None else cfg.role_chain(role))
    if project is None or not bool(cfg.get("routing", "history", default=False)):
        return entries, {}
    min_samples = int(cfg.get("routing", "min_samples", default=5))
    raw_stats = success_stats(project, role)
    qualified = {
        mid: st for mid, st in raw_stats.items() if int(st.get("tasks", 0)) >= min_samples
    }
    if not qualified:
        return entries, {}
    positions = [i for i, e in enumerate(entries) if providers.model_id(cfg, e) in qualified]
    ranked = sorted(
        (entries[i] for i in positions),
        key=lambda e: (
            qualified[providers.model_id(cfg, e)]["passk"],
            qualified[providers.model_id(cfg, e)]["pass1"],
        ),
        reverse=True,
    )
    for pos, entry in zip(positions, ranked):
        entries[pos] = entry
    return entries, qualified


def _hop_suffix(mid: str, qualified: dict[str, dict[str, Any]]) -> str:
    st = qualified.get(mid)
    if not st:
        return ""
    p1 = round(float(st["pass1"]) * 100)
    pk = round(float(st["passk"]) * 100)
    return f" (pass@1 {p1}%, pass@k {pk}%, {st['tasks']} tareas)"


def plan_route(
    cfg: config.Config,
    role: str,
    author_providers: set[str] | None = None,
    project: Path | None = None,
) -> list[Hop]:
    """Qué haría el enrutador ahora mismo, sin llamar a nada."""
    hops: list[Hop] = []
    authors = author_providers or set()
    chain, qualified = _ordered_chain(cfg, role, project)
    other_available = any(
        providers.split_entry(e)[0] not in authors and providers.available(cfg, providers.split_entry(e)[0])[0]
        for e in chain
    ) if (authors and role == "reviewer") else False
    deferred_authors: list[str] = []
    for entry in chain:
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        suffix = _hop_suffix(mid, qualified)
        if role == "reviewer" and provider in authors and other_available:
            hops.append(Hop(entry, mid, f"saltado: autor del cambio{suffix}"))
            deferred_authors.append(entry)
            continue
        ok, reason = providers.available(cfg, provider)
        base = "elegido" if ok else f"saltado: {reason}"
        hops.append(Hop(entry, mid, f"{base}{suffix}"))
        if ok:
            return hops
    for entry in deferred_authors:
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        suffix = _hop_suffix(mid, qualified)
        ok, reason = providers.available(cfg, provider)
        base = "elegido" if ok else f"saltado: {reason}"
        hops.append(Hop(entry, mid, f"{base}{suffix}"))
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


def _pick_challenger(
    project: Path, cfg: config.Config, target_provider: str | None, role: str | None = None
) -> str | None:
    if not target_provider:
        return None
    seen: set[str] = set()
    role_order = (role, *CHALLENGE_ROLE_ORDER) if role else CHALLENGE_ROLE_ORDER
    for r in role_order:
        for entry in cfg.role_chain(r):
            if entry in seen:
                continue
            seen.add(entry)
            provider = providers.split_entry(entry)[0]
            if provider == target_provider:
                continue
            ok, _ = preflight.ensure(project, cfg, provider)
            if ok:
                return entry
    return None


def _challenge_prompt(task: roadmap.Task | None, instruction: str) -> str:
    lines = [
        "Revisión adversarial de la instrucción antes de delegar (rol: challenge).",
        "Señala ambigüedades, lagunas y riesgos de la instrucción frente a los criterios de la tarea. "
        "No modifiques ni escribas ficheros.",
    ]
    if task is not None:
        lines.append(f"- id: {task.id}")
        lines.append(f"- título: {task.title}")
        if task.accept:
            lines.append(f"- accept: {task.accept}")
        lines.append(f"- ficheros: {', '.join(task.files)}")
    lines.append(f"\nInstrucción:\n{guard.redact(instruction.strip())}")
    return "\n".join(lines) + "\n"


def run(project: Path, role: str, instruction: str, task_id: str | None = None, dry_run: bool = False,
        workdir: Path | None = None, entries: list[str] | None = None,
        task_run_id: str | None = None, challenge: bool = False) -> RunOutcome:
    if role not in ROLES:
        raise HefestoError(f"Rol desconocido `{role}` ({', '.join(ROLES)}).", EXIT_INPUT)
    cfg = config.load(project)
    run_id = new_run_id()
    if dry_run:
        authors: set[str] = set()
        try:
            task, rm, data, ctx_workdir, ctx_task_run_id = _task_context(project, task_id)
            eff_task_run_id = task_run_id if task_run_id is not None else ctx_task_run_id
            if role == "reviewer":
                authors = _author_providers(project, role, eff_task_run_id)
        except Exception:
            authors = set()
        hops = plan_route(cfg, role, authors, project=project)
        chosen = any(h.outcome.startswith("elegido") for h in hops)
        return RunOutcome(EXIT_OK if chosen else EXIT_UNAVAILABLE, run_id, None, hops)
    if guard.find_secrets(instruction):
        raise HefestoError("La instrucción contiene un secreto; no se envía a ningún proveedor.", EXIT_BLOCKED)
    task, rm, data, ctx_workdir, ctx_task_run_id = _task_context(project, task_id)
    eff_workdir = workdir if workdir is not None else ctx_workdir
    eff_task_run_id = task_run_id if task_run_id is not None else ctx_task_run_id
    authors = _author_providers(project, role, eff_task_run_id) if role == "reviewer" else set()
    hops: list[Hop] = []
    timeout = int(cfg.get("limits", "timeout_seconds"))
    default_cooldown = int(cfg.get("quota", "cooldown_default_minutes"))
    packet_path: Path | None = None
    challenge_info: dict[str, Any] | None = None

    if challenge:
        planned = plan_route(cfg, role, authors, project=project)
        chosen_hop = next((h for h in planned if h.outcome.startswith("elegido")), None)
        target_provider = providers.split_entry(chosen_hop.entry)[0] if chosen_hop else None
        critic_entry = _pick_challenger(project, cfg, target_provider, role=role)
        if critic_entry is None:
            print(
                "aviso: --challenge sin otro proveedor disponible; se delega con la instrucción original.",
                file=sys.stderr,
            )
        else:
            c_provider, c_model = providers.split_entry(critic_entry)
            c_mid = providers.model_id(cfg, critic_entry)
            c_call = Call(
                role="challenge",
                prompt=_challenge_prompt(task, instruction),
                task_id=task.id if task else None,
                run_id=run_id,
                model=c_model or str(cfg.get("adapters", c_provider, "model", default="") or ""),
                model_id=c_mid,
                workdir=eff_workdir,
                project=project,
                packet_path=None,
                timeout=timeout,
            )
            c_result = adapters.get(cfg, c_provider).run(c_call)
            quota.record_call(c_provider)
            _record(project, "challenge", task, eff_task_run_id, run_id, c_mid, c_result, None)
            if c_result.error_type in ("quota", "rate_limit"):
                until = quota.cooldown_until(c_result.reset_hint or c_result.summary, default_cooldown)
                quota.mark_cooldown(c_provider, until, c_result.error_type, c_result.summary)
            if c_result.success and c_result.summary.strip():
                quota.mark_ok(c_provider)
                critique = guard.redact(c_result.summary.strip())
                ch_path = log.write_text(project, f"challenges/{run_id}-{c_provider}.md", critique)
                ch_rel = _rel(project, ch_path)
                print(f"== Crítica previa ({c_provider}) ==\n{critique}", file=sys.stderr)
                expanded = f"{instruction.rstrip()}\n\n## Crítica previa (otro proveedor)\n\n{critique}"
                if guard.find_secrets(expanded):
                    print(
                        "aviso: la instrucción ampliada con la crítica contiene un secreto; "
                        "se delega con la instrucción original.",
                        file=sys.stderr,
                    )
                else:
                    instruction = expanded
                challenge_info = {"provider": c_provider, "path": ch_rel, "summary": critique}
            else:
                print(
                    f"aviso: la revisión adversarial ({c_provider}) falló: {c_result.summary}; "
                    "se delega con la instrucción original.",
                    file=sys.stderr,
                )

    chain = entries if entries is not None else _ordered_chain(cfg, role, project)[0]
    other_available = _other_provider_available(project, cfg, chain, authors) if authors else False

    def try_entry(entry: str) -> RunOutcome | None:
        nonlocal packet_path
        provider, model = providers.split_entry(entry)
        mid = providers.model_id(cfg, entry)
        ok, reason = preflight.ensure(project, cfg, provider)
        if not ok:
            hops.append(Hop(entry, mid, f"saltado: {reason}"))
            return None
        pkt = packet.build(project, cfg, role, instruction, task, rm, data, mid, eff_workdir)
        packet_path = packet.save(project, pkt, task.id if task else None, role)
        call = Call(role=role, prompt=pkt.text, task_id=task.id if task else None, run_id=run_id,
                    model=model or str(cfg.get("adapters", provider, "model", default="") or ""),
                    model_id=mid,
                    workdir=eff_workdir, project=project, packet_path=packet_path.resolve(), timeout=timeout)
        result = adapters.get(cfg, provider).run(call)
        quota.record_call(provider)
        _record(project, role, task, eff_task_run_id, run_id, mid, result, packet_path)
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
            return RunOutcome(EXIT_BLOCKED, run_id, result, hops, _rel(project, packet_path),
                              challenge=challenge_info)
        if result.success:
            quota.mark_ok(provider)
        return RunOutcome(EXIT_OK if result.success else EXIT_INPUT, run_id, result, hops,
                          _rel(project, packet_path), challenge=challenge_info)

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

    next_avail = earliest_cooldown(cfg, role)
    try:
        from hefesto import notify

        notify.event(
            project,
            "quota",
            f"Sin proveedor disponible para {role}" + (f" (hasta {next_avail})" if next_avail else ""),
        )
    except Exception:
        pass
    return RunOutcome(EXIT_UNAVAILABLE, run_id, None, hops, _rel(project, packet_path) if packet_path else None,
                      next_available=next_avail, challenge=challenge_info)


def earliest_cooldown(cfg: config.Config, role: str) -> str | None:
    times = []
    for entry in cfg.role_chain(role):
        current = quota.provider_state(providers.split_entry(entry)[0])
        if current.get("status") == "cooldown" and current.get("cooldown_until"):
            times.append(current["cooldown_until"])
    return min(times) if times else None


def _record(project: Path, role: str, task: roadmap.Task | None, task_run_id: str | None, run_id: str, mid: str,
            result: AdapterResult, packet_path: Path | None) -> None:
    usage = result.usage if isinstance(result.usage, dict) else {}
    log.record(project, "usage", {
        "run_id": run_id, "provider": result.provider, "model": mid, "role": role, "duration": result.duration,
        "result": "success" if result.success else (result.error_type or "failure"),
        "input_tokens": usage.get("input_tokens", "unknown"), "output_tokens": usage.get("output_tokens", "unknown"),
    })
    log.record(project, "runs", {
        "run_id": run_id, "task_id": task.id if task else None, "task_run_id": task_run_id, "role": role,
        "provider": result.provider, "model_id": mid, "success": result.success, "error_type": result.error_type,
        "summary": result.summary[:500], "log_path": result.log_path,
        "packet": _rel(project, packet_path) if packet_path else None,
    })


def _rel(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return str(path)
