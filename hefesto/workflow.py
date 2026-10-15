"""Ciclo de vida de una tarea: next, start, fail, unblock (y done/review en gitops/evidence)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from hefesto import config, log, roadmap, state, util
from hefesto.lock import project_lock
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_INPUT, HefestoError, now_iso


def role_for_attempt(task_role: str, attempt: int, max_attempts: int) -> str:
    """Escalado: Runner → Runner con el error previo → Forge en el último intento."""
    if attempt >= max_attempts and task_role == "runner":
        return "forge"
    return task_role


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def next_action_for(role: str, task_id: str) -> str:
    return f'hefesto run {role} "<instrucción>" --task {task_id}'


def do_next(project: Path) -> tuple[roadmap.Task | None, str]:
    rm = roadmap.load_valid(project)
    data = state.load(project)
    if data["status"] == "IN_PROGRESS":
        return rm.get(data["current_task_id"]), "en curso"
    if data["status"] == "BLOCKED_NEED_ARCHITECT_REVIEW":
        raise HefestoError(f"{data['current_task_id']} está bloqueada: se necesita `hefesto unblock`.", EXIT_BLOCKED)
    return roadmap.next_task(rm)


def check_startable(project: Path, rm: roadmap.Roadmap, task: roadmap.Task, data: dict[str, Any]) -> None:
    if data["status"] == "IN_PROGRESS":
        raise HefestoError(f"Ya hay una tarea en curso: {data['current_task_id']}.", EXIT_CONFLICT)
    if data["status"] == "BLOCKED_NEED_ARCHITECT_REVIEW":
        raise HefestoError(f"{data['current_task_id']} está bloqueada; nada arranca hasta `hefesto unblock`.", EXIT_BLOCKED)
    if task.marker == "x":
        raise HefestoError(f"{task.id} ya está completada.", EXIT_INPUT)
    if task.marker == "!":
        raise HefestoError(f"{task.id} está bloqueada por el circuit breaker.", EXIT_BLOCKED)
    if task.marker == "~":
        raise HefestoError(f"{task.id} figura en curso en el ROADMAP pero no en el estado.", EXIT_CONFLICT)
    pending = roadmap.dependencies_done(rm, task)
    if pending:
        raise HefestoError(f"{task.id} depende de tareas sin completar: {', '.join(pending)}.", EXIT_INPUT)
    if task.size == "L":
        raise HefestoError(f"{task.id} es de tamaño L: el Core debe dividirla en tareas M/S antes de ejecutarla.",
                           EXIT_BLOCKED)


def mark_started(project: Path, task: roadmap.Task, data: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    roadmap.set_marker(roadmap.roadmap_path(project), task.id, "~")
    new = state.transition(data, "IN_PROGRESS")
    new.update({
        "current_task_id": task.id,
        "run_id": new_run_id(),
        "active_role": task.role,
        "started_role": task.role,
        "attempts": 0,
        "last_error": None,
        "error_history": [],
        "review": None,
        "last_failure": None,
        "next_action": next_action_for(task.role, task.id),
        **extra,
    })
    state.save(project, new)
    log.event(project, "start", f"{task.id} iniciada", role=task.role)
    return new


def start(project: Path, task_id: str) -> dict[str, Any]:
    """Arranca una tarea: comprueba, sitúa el trabajo en la rama de la fase y marca `[~]`."""
    from hefesto import gitops

    with project_lock(project):
        data = state.load(project)
        rm = roadmap.load_valid(project)
        task = rm.get(task_id)
        check_startable(project, rm, task, data)
        cfg = config.load(project)
        phases = dict(data.get("phases") or {})
        info = gitops.prepare_workdir(project, cfg, rm, task, phases)
        workdir = Path(info["workdir"])
        extra = {"phases": phases, "branch": info["branch"], "isolation": info["isolation"],
                 "workdir": str(workdir), "task_base": gitops.head(workdir)}
        return mark_started(project, task, data, extra)


def require_current(data: dict[str, Any], task_id: str) -> None:
    if data["status"] != "IN_PROGRESS" or data["current_task_id"] != task_id:
        raise HefestoError(f"{task_id} no es la tarea en curso (estado {data['status']}, "
                           f"tarea {data['current_task_id'] or '-'}).", EXIT_CONFLICT)


def _author_runs(project: Path, task_run_id: str | None) -> list[dict[str, Any]]:
    if not task_run_id:
        return []
    runs_file = project / ".hefesto" / "logs" / "runs.jsonl"
    if not runs_file.is_file():
        return []
    try:
        return [
            r for r in util.read_jsonl(runs_file)
            if r.get("task_run_id") == task_run_id
            and r.get("role") in ("runner", "forge", "visual", "core")
        ]
    except Exception:
        return []


def register_failure(project: Path, task_id: str, reason: str, source: str = "fail") -> tuple[int, str]:
    """Suma exactamente un intento. Devuelve (código, rol siguiente o mensaje). Llamar con el lock tomado."""
    cfg = config.load(project)
    max_attempts = int(cfg.get("limits", "max_attempts"))
    rm = roadmap.load_valid(project)
    task = rm.get(task_id)
    data = state.load(project)
    require_current(data, task_id)
    attempts = data["attempts"] + 1
    history = list(data.get("error_history") or []) + [{"attempt": attempts, "role": data.get("active_role"),
                                                         "error": reason[:2000], "at": now_iso()}]
    author_runs = _author_runs(project, data.get("run_id"))
    data.update({
        "attempts": attempts,
        "last_error": reason[:2000],
        "error_history": history,
        "review": None,
        "last_failure": {
            "source": source,
            "attempt": attempts,
            "at": now_iso(),
            "author_runs": len(author_runs),
            "author_run_ids": [r.get("run_id") for r in author_runs if r.get("run_id")],
        },
    })
    if attempts >= max_attempts:
        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!")
        data = state.transition(data, "BLOCKED_NEED_ARCHITECT_REVIEW")
        data["active_role"] = None
        data["next_action"] = f'intervención humana: revisar y `hefesto unblock {task_id} "nota"`'
        state.save(project, data)
        report = [f"== {now_iso()} circuit breaker: {task_id} — {task.title}", f"intentos: {attempts}"]
        report += [f"  intento {h['attempt']} ({h.get('role')}): {h['error']}" for h in history]
        log_path = project / ".hefesto" / "logs" / "circuit_breaker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        from hefesto.guard import redact

        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(redact("\n".join(report)) + "\n\n")
        log.event(project, "circuit_breaker", f"{task_id} bloqueada tras {attempts} intentos")
        return EXIT_BLOCKED, f"Circuit breaker: {task_id} bloqueada tras {attempts} intentos. Informe en logs/circuit_breaker.log"
    role = role_for_attempt(task.role, attempts + 1, max_attempts)
    data["active_role"] = role
    data["next_action"] = next_action_for(role, task_id)
    state.save(project, data)
    log.event(project, "fail", f"{task_id} intento {attempts} fallido", next_role=role)
    return util.EXIT_OK, role


def fail(project: Path, task_id: str, reason: str) -> tuple[int, str]:
    with project_lock(project):
        data = state.load(project)
        require_current(data, task_id)
        last_failure = data.get("last_failure")
        if isinstance(last_failure, dict) and last_failure.get("source") == "review":
            if last_failure.get("attempt") == data.get("attempts"):
                current_author_runs = _author_runs(project, data.get("run_id"))
                prev_runs_count = int(last_failure.get("author_runs", 0))
                prev_run_ids = set(last_failure.get("author_run_ids") or [])
                new_runs = [r for r in current_author_runs if r.get("run_id") not in prev_run_ids]
                if len(current_author_runs) <= prev_runs_count and not new_runs:
                    cfg = config.load(project)
                    max_attempts = int(cfg.get("limits", "max_attempts"))
                    rm = roadmap.load_valid(project)
                    task = rm.get(task_id)
                    next_role = data.get("active_role") or role_for_attempt(task.role, data["attempts"] + 1, max_attempts)
                    msg = f"el intento ya se contó con la revisión negativa. Rol del siguiente intento: {next_role}"
                    return util.EXIT_OK, msg
        return register_failure(project, task_id, reason, source="fail")


def unblock(project: Path, task_id: str, note: str, yes: bool = False) -> str:
    if yes:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    else:
        util.require_tty("hefesto unblock")
    with project_lock(project):
        data = state.load(project)
        if data["status"] != "BLOCKED_NEED_ARCHITECT_REVIEW" or data["current_task_id"] != task_id:
            raise HefestoError(f"{task_id} no está bloqueada.", EXIT_CONFLICT)
        roadmap.set_marker(roadmap.roadmap_path(project), task_id, " ")
        data = state.transition(data, "IDLE")
        data.update({"current_task_id": None, "active_role": None, "started_role": None, "attempts": 0, "run_id": None,
                     "error_history": [], "last_failure": None, "next_action": "hefesto next"})
        state.save(project, data)
        append_decision(project, f"Desbloqueo de {task_id}", note)
        log.event(project, "unblock", f"{task_id} desbloqueada")
    return f"{task_id} desbloqueada; intentos a 0. Nota registrada en DECISIONS.md."


def append_decision(project: Path, title: str, body: str) -> None:
    path = project / ".hefesto" / "DECISIONS.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Decisiones (ADR)\n"
    entry = f"\n## {now_iso()[:10]} — {title}\n\n{body.strip()}\n"
    util.atomic_write_text(path, existing.rstrip("\n") + "\n" + entry)
