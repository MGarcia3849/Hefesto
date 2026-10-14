"""Ciclo de vida de una tarea: next, start, fail, unblock (y done/review en gitops/evidence)."""

from __future__ import annotations

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
        "attempts": 0,
        "last_error": None,
        "error_history": [],
        "review": None,
        "next_action": next_action_for(task.role, task.id),
        **extra,
    })
    state.save(project, new)
    log.event(project, "start", f"{task.id} iniciada", role=task.role)
    return new


def start(project: Path, task_id: str) -> dict[str, Any]:
    """Arranca una tarea (sin aislamiento git; `gitops.start` lo añade)."""
    with project_lock(project):
        data = state.load(project)
        rm = roadmap.load_valid(project)
        task = rm.get(task_id)
        check_startable(project, rm, task, data)
        return mark_started(project, task, data, {})


def require_current(data: dict[str, Any], task_id: str) -> None:
    if data["status"] != "IN_PROGRESS" or data["current_task_id"] != task_id:
        raise HefestoError(f"{task_id} no es la tarea en curso (estado {data['status']}, "
                           f"tarea {data['current_task_id'] or '-'}).", EXIT_CONFLICT)


def register_failure(project: Path, task_id: str, reason: str) -> tuple[int, str]:
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
    data.update({"attempts": attempts, "last_error": reason[:2000], "error_history": history, "review": None})
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
        return register_failure(project, task_id, reason)


def unblock(project: Path, task_id: str, note: str) -> str:
    util.require_tty("hefesto unblock")
    with project_lock(project):
        data = state.load(project)
        if data["status"] != "BLOCKED_NEED_ARCHITECT_REVIEW" or data["current_task_id"] != task_id:
            raise HefestoError(f"{task_id} no está bloqueada.", EXIT_CONFLICT)
        roadmap.set_marker(roadmap.roadmap_path(project), task_id, " ")
        data = state.transition(data, "IDLE")
        data.update({"current_task_id": None, "active_role": None, "attempts": 0, "run_id": None,
                     "error_history": [], "next_action": "hefesto next"})
        state.save(project, data)
        append_decision(project, f"Desbloqueo de {task_id}", note)
        log.event(project, "unblock", f"{task_id} desbloqueada")
    return f"{task_id} desbloqueada; intentos a 0. Nota registrada en DECISIONS.md."


def append_decision(project: Path, title: str, body: str) -> None:
    path = project / ".hefesto" / "DECISIONS.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Decisiones (ADR)\n"
    entry = f"\n## {now_iso()[:10]} — {title}\n\n{body.strip()}\n"
    util.atomic_write_text(path, existing.rstrip("\n") + "\n" + entry)
