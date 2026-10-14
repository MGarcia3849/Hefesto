"""`hefesto loop`: ciclo headless conducido por un orquestador (Claude, Codex o Gemini) con relevo automático.

Cada iteración entrega al orquestador el paso `next` del protocolo; el orquestador trabaja solo a través
de la CLI `hefesto`, así que si se queda sin cuota a mitad, la siguiente iteración la retoma otro
(`--orchestrator auto`) desde `hefesto resume`, sin perder estado ni sumar intentos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from hefesto import adapters, config, log, progress, providers, quota, roadmap, router, state, sync
from hefesto.adapters.base import Call
from hefesto.util import (
    EXIT_BLOCKED,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    HefestoError,
    now_iso,
    parse_iso,
    read_jsonl,
    utcnow,
)
from hefesto.workflow import new_run_id

AUTONOMY = """

---
**Modo autónomo (`hefesto loop`).** No hay ningún humano en esta sesión:
- Completa el ciclo de **una sola tarea** y termina. No empieces otra.
- No ejecutes `hefesto trust`, `unblock`, `finish` ni nada que pida confirmación; si hace falta, deja una nota y termina.
- Si `hefesto run` sale con 3 (sin cuota en el rol), `hefesto note "pausa: sin cuota"` y termina.
"""
WAIT_POLL_SECONDS = 5.0
STALL_LIMIT = 2


@dataclass
class LoopResult:
    code: int
    message: str
    completed: int
    iterations: int


def stop_path(project: Path) -> Path:
    return project / ".hefesto" / "STOP"


def request_stop(project: Path) -> Path:
    path = stop_path(project)
    path.write_text(f"STOP solicitado {now_iso()}\n", encoding="utf-8")
    return path


def _done_count(project: Path) -> int:
    return sum(1 for t in roadmap.load_valid(project).tasks if t.marker == "x")


def _fingerprint(project: Path) -> tuple[object, ...]:
    data = state.load(project)
    notes = len(read_jsonl(progress.progress_path(project)))
    return (data["status"], data.get("current_task_id"), data["attempts"], data.get("review") is not None,
            _done_count(project), notes)


def _pending_role(project: Path) -> tuple[str | None, str]:
    """Rol que toca ahora (o None si no hay trabajo) y el motivo."""
    data = state.load(project)
    if data["status"] == "BLOCKED_NEED_ARCHITECT_REVIEW":
        return None, f"{data['current_task_id']} bloqueada: se necesita `hefesto unblock`"
    if data["status"] == "IN_PROGRESS":
        return str(data.get("active_role") or "runner"), ""
    task, reason = roadmap.next_task(roadmap.load_valid(project))
    return (task.role, "") if task else (None, reason)


def _choose(cfg: config.Config, requested: str) -> str | None:
    if requested != "auto":
        return requested if providers.available(cfg, requested)[0] else None
    return progress.next_orchestrator(cfg)


def _sleep_until(project: Path, until: str | None) -> bool:
    """Duerme hasta `until` comprobando STOP. False si no hay hora conocida o se pidió parar."""
    if not until:
        return False
    target = parse_iso(until)
    while utcnow() < target:
        if stop_path(project).exists():
            return False
        time.sleep(min(WAIT_POLL_SECONDS, max(0.05, (target - utcnow()).total_seconds())))
    return True


def _earliest_orchestrator_cooldown(cfg: config.Config, requested: str) -> str | None:
    names = [providers.split_entry(e)[0] for e in cfg.get("orchestrator", "use", default=[])] if requested == "auto" \
        else [requested]
    times = [quota.provider_state(n).get("cooldown_until") for n in names]
    valid = [t for t in times if t]
    return min(valid) if valid else None


def run(project: Path, orchestrator: str | None, max_tasks: int | None, max_turns: int | None,
        wait: bool) -> LoopResult:
    cfg = config.load(project)
    requested = orchestrator or str(cfg.get("orchestrator", "default"))
    max_tasks = max_tasks or int(cfg.get("limits", "max_tasks_per_loop"))
    max_turns = max_turns or int(cfg.get("limits", "max_turns_per_task"))
    max_iterations = max_tasks * (int(cfg.get("limits", "max_attempts")) + 3)
    loop_id = new_run_id()
    quota.start_loop(loop_id)
    completed = iterations = stalls = 0
    prompt = sync.render_prompt("next") + AUTONOMY
    log.event(project, "loop", f"loop {loop_id} iniciado", orchestrator=requested, max_tasks=max_tasks)
    try:
        while completed < max_tasks:
            if stop_path(project).exists():
                return LoopResult(EXIT_OK, "Parado por .hefesto/STOP (bórralo para volver a lanzar el loop).",
                                  completed, iterations)
            cfg = config.load(project)
            role, reason = _pending_role(project)
            if role is None:
                code = EXIT_BLOCKED if "bloqueada" in reason else EXIT_OK
                return LoopResult(code, f"Sin trabajo: {reason}.", completed, iterations)
            if not any(h.outcome == "elegido" for h in router.plan_route(cfg, role)):
                when = router.earliest_cooldown(cfg, role)
                if wait and _sleep_until(project, when):
                    continue
                return LoopResult(EXIT_UNAVAILABLE, f"Pausa: ningún proveedor del rol `{role}` tiene cuota. "
                                  f"Vuelve a haber hacia {when or 'desconocido'}.", completed, iterations)
            name = _choose(cfg, requested)
            if name is None:
                when = _earliest_orchestrator_cooldown(cfg, requested)
                if wait and _sleep_until(project, when):
                    continue
                return LoopResult(EXIT_UNAVAILABLE, f"Pausa: ningún orquestador disponible ({requested}). "
                                  f"Vuelve a haber hacia {when or 'desconocido'}.", completed, iterations)
            iterations += 1
            if iterations > max_iterations:
                return LoopResult(EXIT_INPUT, "Tope de iteraciones alcanzado sin completar las tareas.",
                                  completed, iterations)
            before_done, before = _done_count(project), _fingerprint(project)
            entry = next((e for e in cfg.get("orchestrator", "use", default=[]) if providers.split_entry(e)[0] == name),
                         name)
            run_id = new_run_id()
            call = Call(role="core", prompt=prompt, task_id=None, run_id=run_id,
                        model=providers.split_entry(entry)[1], workdir=project, project=project,
                        timeout=int(cfg.get("limits", "orchestrator_timeout_seconds")), orchestrator=True,
                        max_turns=max_turns, env={"HEFESTO_ORCHESTRATOR": name, "HEFESTO_LOOP_ID": loop_id})
            result = adapters.get(cfg, name).run(call)
            quota.record_call(name)
            log.record(project, "loop", {"loop_id": loop_id, "iteration": iterations, "orchestrator": name,
                                         "run_id": run_id, "success": result.success, "error_type": result.error_type,
                                         "summary": result.summary[:300]})
            if result.error_type in ("quota", "rate_limit", "auth"):
                if result.error_type == "auth":
                    quota.mark_auth_error(name, result.summary)
                else:
                    until = quota.cooldown_until(result.reset_hint or result.summary,
                                                 int(cfg.get("quota", "cooldown_default_minutes")))
                    quota.mark_cooldown(name, until, result.error_type, result.summary)
                progress.note(project, f"relevo automático: {name} sin cuota ({result.error_type}); el estado queda "
                                       "para el siguiente orquestador", orchestrator=name)
                if requested != "auto" and not wait:
                    return LoopResult(EXIT_UNAVAILABLE, f"{name} se quedó sin cuota; estado guardado para "
                                      "`hefesto resume`.", completed, iterations)
                continue
            completed += max(0, _done_count(project) - before_done)
            after = _fingerprint(project)
            if state.load(project)["status"] == "BLOCKED_NEED_ARCHITECT_REVIEW":
                return LoopResult(EXIT_BLOCKED, "Circuit breaker: tarea bloqueada; se necesita intervención humana.",
                                  completed, iterations)
            stalls = stalls + 1 if after == before else 0
            if stalls >= STALL_LIMIT:
                return LoopResult(EXIT_INPUT, f"El orquestador {name} no avanza ({stalls} iteraciones sin cambios). "
                                  f"Última respuesta: {result.summary[:200]}", completed, iterations)
        return LoopResult(EXIT_OK, f"Tope de {max_tasks} tareas alcanzado.", completed, iterations)
    except HefestoError as exc:
        return LoopResult(exc.code, str(exc), completed, iterations)
    finally:
        quota.end_loop()
        log.event(project, "loop", f"loop {loop_id} terminado", completed=completed, iterations=iterations)
