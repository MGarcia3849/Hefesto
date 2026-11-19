"""Project scoped delivery authorization and idempotent pull request creation."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hefesto import acceptance, config, evidence, roadmap, state, guard, notify
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_OK, HefestoError, atomic_write_json, now_iso, append_jsonl

TARGET_FILE = ".hefesto/deploy.toml"
ACTIONS = ("pr", "deploy")


def _delivery_config(project: Path) -> config.Config:
    """Valida las capas, admitiendo el destino argv únicamente en settings del proyecto."""
    cfg = config.load(project, validate_layers=False)
    checked = copy.deepcopy(cfg)
    for layer in checked.layers:
        if layer.name == "project-settings":
            delivery = layer.data.get("delivery")
            if isinstance(delivery, dict):
                for key in ("command", "name", "environment", "healthcheck", "rollback", "url",
                            "idempotent", "timeout_seconds"):
                    delivery.pop(key, None)
    errors = config.validate(checked)
    effort = config.effort_errors(checked)
    errors = [error for error in errors if error not in effort]
    if errors:
        raise HefestoError("Configuración inválida:\n  - " + "\n  - ".join(errors), EXIT_INPUT)
    return cfg


@dataclass(frozen=True)
class DeployTarget:
    name: str
    environment: str
    command: tuple[str, ...]
    healthcheck: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()
    url: str | None = None
    idempotent: bool = False
    timeout_seconds: int = 600


@dataclass
class Closure:
    slug: str
    branch: str
    ready: bool
    pending: list[str]


@dataclass
class PrResult:
    number: int
    url: str
    created: bool


@dataclass
class DeployResult:
    ok: bool
    exit_code: int
    status: str


@dataclass
class DeliveryReport:
    code: int
    steps: dict[str, str]


def load_target(project: Path) -> DeployTarget | None:
    path = project / TARGET_FILE
    if not path.exists():
        # Optional project-local argv configuration; no global setting may select a target.
        local = _delivery_config(project)
        command = local.get("delivery", "command", default=None)
        if command is None:
            # A package's single `deploy` script is an unambiguous existing project destination.
            package = project / "package.json"
            try:
                scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
                script = scripts.get("deploy") if isinstance(scripts, dict) else None
                if isinstance(script, str) and script.strip():
                    # npm script values are shell text, so they are deliberately not executed implicitly.
                    return None
            except (OSError, ValueError, AttributeError):
                pass
            return None
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise HefestoError("delivery.command debe ser una lista de argumentos del proyecto.", EXIT_INPUT)
        name = local.get("delivery", "name", default=project.name)
        environment = local.get("delivery", "environment", default="production")
        return DeployTarget(str(name), str(environment), tuple(command),
                            tuple(local.get("delivery", "healthcheck", default=[])),
                            tuple(local.get("delivery", "rollback", default=[])),
                            local.get("delivery", "url", default=None),
                            bool(local.get("delivery", "idempotent", default=False)),
                            int(local.get("delivery", "timeout_seconds", default=600)))
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        t = data["target"]
        if not isinstance(t, dict): raise ValueError("[target] debe ser una tabla")
        allowed = {"name", "environment", "command", "healthcheck", "rollback", "url", "idempotent", "timeout_seconds"}
        if set(t) - allowed:
            raise ValueError("claves desconocidas")
        def argv(key: str, required: bool = False) -> tuple[str, ...]:
            val = t.get(key, [])
            if not isinstance(val, list) or (required and not val) or any(not isinstance(x, str) or not x for x in val):
                raise ValueError(f"{key} debe ser una lista válida")
            return tuple(val)
        name, env = t["name"], t["environment"]
        timeout = t.get("timeout_seconds", 600)
        if not isinstance(name, str) or not name or not isinstance(env, str) or not env or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError("target inválido")
        url, idem = t.get("url"), t.get("idempotent", False)
        if url is not None and not isinstance(url, str): raise ValueError("url debe ser texto")
        if not isinstance(idem, bool): raise ValueError("idempotent debe ser booleano")
        return DeployTarget(name, env, argv("command", True), argv("healthcheck"), argv("rollback"), url, idem, timeout)
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise HefestoError(f"Destino de despliegue inválido: {exc}", EXIT_INPUT) from exc


def target_fingerprint(target: DeployTarget) -> str:
    return hashlib.sha256(json.dumps(asdict(target), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorizations_path(project: Path) -> Path:
    return project / ".hefesto" / "delivery.json"


def _auth(project: Path) -> dict[str, Any]:
    try:
        return json.loads(authorizations_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def authorize(project: Path, action: str, yes: bool = False) -> dict[str, Any]:
    if action not in ACTIONS:
        raise HefestoError(f"Acción desconocida: {action}", EXIT_INPUT)
    if os.environ.get("HEFESTO_ROLE"):
        raise HefestoError("Un agente no puede autorizar entregas; hace falta una persona.", EXIT_BLOCKED)
    from hefesto import util
    if not yes:
        util.require_tty(f"autorizar {action}")
    entry: dict[str, Any] = {"approved_at": now_iso()}
    if action == "deploy":
        target = load_target(project)
        if target is None:
            raise HefestoError("No hay destino de despliegue del proyecto.", EXIT_INPUT)
        entry["fingerprint"] = target_fingerprint(target)
        # Rearm any open phase's failed/interrupted deploy.
        for p in (project / ".hefesto" / "delivery").glob("*.json") if (project / ".hefesto" / "delivery").exists() else []:
            try:
                record = json.loads(p.read_text(encoding="utf-8")); step = record["steps"]["deploy"]
                if step.get("status") in ("interrupted", "failed"):
                    step.update(status="pending", attempts=0); atomic_write_json(p, record)
            except (OSError, ValueError, KeyError):
                continue
    data = _auth(project); data[action] = entry; atomic_write_json(authorizations_path(project), data)
    return entry


def is_authorized(project: Path, action: str) -> bool:
    entry = _auth(project).get(action)
    if not isinstance(entry, dict): return False
    if action == "deploy":
        target = load_target(project)
        return bool(target and entry.get("fingerprint") == target_fingerprint(target))
    return action == "pr"


def revoke(project: Path, action: str) -> bool:
    if action not in ACTIONS: raise HefestoError(f"Acción desconocida: {action}", EXIT_INPUT)
    data = _auth(project)
    had = action in data
    data.pop(action, None); atomic_write_json(authorizations_path(project), data)
    return had


def _phase(project: Path, phase: str | None) -> tuple[str, dict[str, Any], list[roadmap.Task]]:
    rm = roadmap.load_valid(project); phases = state.load(project).get("phases") or {}
    if phase:
        slug = roadmap.slugify(phase)
        slug = next((s for s in phases if s == slug or s.startswith(slug)), slug)
    else:
        slug = next(iter(phases), "")
    tasks = [t for t in rm.tasks if t.phase_slug == slug or t.phase_slug.startswith(slug)]
    return slug, phases.get(slug, {}), tasks


def phase_closure(project: Path, phase: str | None = None) -> Closure:
    slug, info, tasks = _phase(project, phase)
    pending = [t.id for t in tasks if t.marker != "x" or evidence.latest_evidence(project, t.id) is None]
    branch = str(info.get("branch") or "")
    st = state.load(project)
    if st.get("status") == "IN_PROGRESS" and (not branch or st.get("branch") == branch):
        pending.append(str(st.get("current_task_id") or "tarea en curso"))
    return Closure(slug, branch, bool(tasks) and not pending, pending)


def ensure_pr(project: Path, phase: str | None = None) -> PrResult:
    closure = phase_closure(project, phase)
    if not closure.ready or not is_authorized(project, "pr"):
        raise HefestoError("Fase no cerrada o falta autorización persistida para PR.", EXIT_BLOCKED)
    cfg = config.load(project); base = str(cfg.get("execution", "main_branch", default="main"))
    if base == "auto":
        from hefesto import gitops
        base = gitops.main_branch(project, cfg)
    head = closure.branch
    def call(argv: list[str]) -> subprocess.CompletedProcess[str]:
        p = subprocess.run(argv, cwd=project, capture_output=True, text=True)
        if p.returncode: raise HefestoError(p.stderr or f"Falló {' '.join(argv[:2])}", EXIT_BLOCKED)
        return p
    existing = json.loads(call(["gh", "pr", "list", "--head", head, "--state", "open", "--json", "number,url"]).stdout or "[]")
    if existing:
        result = PrResult(int(existing[0]["number"]), str(existing[0]["url"]), False)
    else:
        call(["git", "push", "-u", "origin", head])
        title = f"Hefesto: {head}"
        p = call(["gh", "pr", "create", "--base", base, "--head", head, "--title", title, "--body", "Entrega automática Hefesto."])
        url = p.stdout.strip().splitlines()[-1]
        result = PrResult(int(url.rstrip("/").split("/")[-1]), url, True)
    # When adopting an already-open PR, leave the user's primary checkout untouched.
    from hefesto import gitops
    original = gitops.current_branch(project)
    if existing and original == head and base != head:
        call(["git", "checkout", "-q", base])
    return result


def max_deploy_attempts(project: Path) -> int:
    value = _delivery_config(project).get("delivery", "max_deploy_attempts", default=2)
    return max(1, int(value))


def record_path(project: Path, slug: str) -> Path:
    return project / ".hefesto" / "delivery" / f"{slug}.json"


def deploy(project: Path, phase: str | None = None, *, approved_operation: str | None = None) -> DeployResult:
    approved_channel = None
    if approved_operation is not None:
        # Solo una operación de Telegram aprobada (con PIN) y en curso sustituye a `hefesto authorize deploy`;
        # su alcance incluye la huella del destino, así que un destino cambiado invalida la aprobación.
        from hefesto import telegram_ops

        telegram_ops.verify_running(project, approved_operation, "deploy")
        approved_channel = "telegram"
        append_jsonl(project / ".hefesto/logs/approvals.jsonl",
                     {"at": now_iso(), "action": "deploy", "channel": "telegram", "operation": approved_operation})
    return _deploy(project, phase, project, approved_channel=approved_channel)


def _deploy(project: Path, phase: str | None, cwd: Path, *, approved_channel: str | None = None) -> DeployResult:
    closure = phase_closure(project, phase)
    target = load_target(project)
    if not closure.ready or target is None or not (approved_channel == "telegram" or is_authorized(project, "deploy")):
        raise HefestoError("Fase no cerrada, sin destino o falta autorización vigente para despliegue.", EXIT_BLOCKED)
    from hefesto import approval, gitops, security

    data = state.load(project)
    info = (data.get("phases") or {}).get(closure.slug) or {}
    cfg = _delivery_config(project)
    base = info.get("base") or gitops.out(project, "merge-base", gitops.main_branch(project, cfg), closure.branch)
    tasks = [t for t in roadmap.load_valid(project).tasks if t.phase_slug == closure.slug]
    for kind in {t.kind for t in tasks} or {"code"}:
        approval.gate(project, cwd, base, kind, "deploy")
    security_report = security.gate(project, cwd, base, "deploy")
    warnings = security.warning_text(security_report)
    if warnings:
        print(warnings)
    from hefesto import predeploy

    predeploy.gate(project, cwd)
    fp = target_fingerprint(target)
    path = record_path(project, closure.slug)
    record = _read_record(path, closure)
    step = record["steps"]["deploy"]
    attempts = int(step.get("attempts", 0))
    if attempts >= max_deploy_attempts(project):
        return DeployResult(False, EXIT_BLOCKED, "failed")
    step.update(status="running", attempts=attempts + 1, fingerprint=fp, at=now_iso())
    _save_record(path, record)
    outcome = _run_step(project, closure.slug, "deploy", target.command, target.timeout_seconds, fp, cwd=cwd)
    code = outcome["exit_code"]
    if code == 0 and target.healthcheck:
        health = _run_step(project, closure.slug, "healthcheck", target.healthcheck, target.timeout_seconds, fp,
                           cwd=cwd)
        if health["exit_code"] != 0: code = health["exit_code"] or 1
    if code == 0:
        step.update(status="done", exit_code=0, at=now_iso())
        _save_record(path, record)
        return DeployResult(True, 0, "done")
    if target.rollback:
        _run_step(project, closure.slug, "rollback", target.rollback, target.timeout_seconds, fp, cwd=cwd)
    step.update(status="failed", exit_code=code, at=now_iso())
    _save_record(path, record)
    notify.event(project, "deploy", f"Falló despliegue de {closure.slug} ({target.name}); código {code}.")
    return DeployResult(False, code, "failed")


def deliver(project: Path, phase: str | None = None) -> DeliveryReport:
    closure = phase_closure(project, phase)
    if not closure.ready: return DeliveryReport(EXIT_BLOCKED, {"pr": "pending", "deploy": "pending"})
    path = record_path(project, closure.slug)
    record = _read_record(path, closure)
    steps = record["steps"]
    if steps["pr"].get("status") != "done":
        steps["pr"].update(status="running", at=now_iso()); _save_record(path, record)
        try:
            result = ensure_pr(project, phase)
        except HefestoError as exc:
            steps["pr"].update(status="failed", exit_code=exc.code, at=now_iso()); _save_record(path, record)
            return DeliveryReport(exc.code, {k: v["status"] for k, v in steps.items()})
        steps["pr"].update(status="done", number=result.number, url=result.url, created=result.created, at=now_iso())
        _save_record(path, record)
    if load_target(project) is None:
        steps["deploy"].update(status="skipped", at=now_iso()); _save_record(path, record)
        return DeliveryReport(EXIT_OK, {k: v["status"] for k, v in steps.items()})
    ds = steps["deploy"]
    if ds.get("status") == "done": return DeliveryReport(EXIT_OK, {k: v["status"] for k, v in steps.items()})
    target = load_target(project)
    if ds.get("status") == "running" and target and not target.idempotent:
        ds.update(status="interrupted", at=now_iso()); _save_record(path, record)
        return DeliveryReport(EXIT_BLOCKED, {k: v["status"] for k, v in steps.items()})
    if ds.get("status") == "interrupted":
        return DeliveryReport(EXIT_BLOCKED, {k: v["status"] for k, v in steps.items()})
    if int(ds.get("attempts", 0)) >= max_deploy_attempts(project):
        return DeliveryReport(EXIT_BLOCKED, {k: v["status"] for k, v in steps.items()})
    try:
        workdir = _verified_phase_workdir(project, closure)
        result = _deploy(project, phase, workdir)
    except HefestoError as exc:
        return DeliveryReport(exc.code, {k: v["status"] for k, v in steps.items()})
    record = _read_record(path, closure)
    steps = record["steps"]
    return DeliveryReport(EXIT_OK if result.ok else EXIT_BLOCKED, {k: v["status"] for k, v in steps.items()})


def _read_record(path: Path, closure: Closure) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("steps"), dict): raise ValueError()
    except (OSError, ValueError, json.JSONDecodeError):
        data = {"phase": closure.slug, "branch": closure.branch, "steps": {}}
    for name in ("pr", "deploy"):
        step = data["steps"].setdefault(name, {})
        if not isinstance(step, dict): data["steps"][name] = step = {}
        step.setdefault("status", "pending")
        if name == "deploy": step.setdefault("attempts", 0)
    data.update(phase=closure.slug, branch=closure.branch)
    return data


def _save_record(path: Path, record: dict[str, Any]) -> None:
    atomic_write_json(path, record)


def _verified_phase_workdir(project: Path, closure: Closure) -> Path:
    """Return the completed phase checkout only when it is still on its verified branch and clean."""
    data = state.load(project)
    phases = data.get("phases") or {}
    info = phases.get(closure.slug) or {}
    raw = info.get("workdir") or (data.get("workdir") if data.get("branch") == closure.branch else None)
    if not raw:
        # Legacy/branch isolation stores its checkout in the project root.
        raw = str(project) if not info else None
    if not raw:
        raise HefestoError("No se puede localizar el worktree verificado de la fase.", EXIT_BLOCKED)
    workdir = Path(raw).resolve()
    from hefesto import gitops
    if gitops.current_branch(workdir) != closure.branch or gitops.dirty_paths(workdir):
        raise HefestoError("El worktree de la fase no coincide con la revisión verificada o tiene cambios.",
                           EXIT_BLOCKED)
    return workdir


def _run_step(project: Path, phase: str, name: str, argv: tuple[str, ...], timeout: int, fp: str,
              cwd: Path | None = None) -> dict[str, Any]:
    code, output = 1, ""
    try:
        proc = subprocess.run(list(argv), cwd=cwd or project, capture_output=True, text=True, timeout=timeout)
        code, output = int(proc.returncode), (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code = 124; output = str(exc.stdout or "") + str(exc.stderr or "") + "timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        output = str(exc)
    status = "done" if code == 0 else "failed"
    append_jsonl(project / ".hefesto" / "logs" / "deliveries.jsonl", {
        "phase": phase, "step": name, "status": status, "exit_code": code, "at": now_iso(),
        "fingerprint": fp, "output": guard.redact(output)[:4000]})
    return {"status": status, "exit_code": code}
