"""Tablero de consola de solo lectura para el progreso y las delegaciones."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from hefesto import config, guard, lock, roadmap, state, util
from hefesto.util import EXIT_INPUT, HefestoError, read_jsonl

ROLES = ("core", "runner", "forge", "reviewer", "visual")
STATE_LABELS = {"idle": "en espera", "working": "trabajando", "done": "terminó", "failed": "falló"}
MAX_EVENTS = 8


def events_path(project: Path) -> Path:
    return project / ".hefesto" / "logs" / "delegations.jsonl"


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return guard.redact(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    return value


def snapshot(project: Path) -> dict[str, Any]:
    data = state.load(project)
    rm = roadmap.parse(project / ".hefesto" / "ROADMAP.md")
    cfg = config.load(project)
    counts = {"x": 0, "~": 0, "!": 0, " ": 0}
    for task in rm.tasks:
        if task.marker in counts:
            counts[task.marker] += 1
    total, done = len(rm.tasks), counts["x"]
    task_obj = next((t for t in rm.tasks if t.id == data.get("current_task_id")), None)
    task = ({"id": task_obj.id, "title": task_obj.title, "role": data.get("active_role") or task_obj.role,
             "attempts": data.get("attempts", 0), "phase": task_obj.phase} if task_obj else None)
    pending = next((t for t in rm.tasks if t.marker == " "), None)
    raw_events = read_jsonl(events_path(project))[-MAX_EVENTS:]
    agents = []
    for role in ROLES:
        providers = [entry.partition(":")[0] for entry in cfg.role_chain(role)]
        agents.append({"role": role, "providers": providers, "state": "idle", "provider": None,
                       "model_id": None, "cli": None, "run_id": None, "task_id": None})
    by_role = {a["role"]: a for a in agents}
    latest: dict[str, dict[str, Any]] = {}
    starts: dict[tuple[str, str], dict[str, Any]] = {}
    raw_events = [_clean(row) for row in raw_events]
    for row in raw_events:
        role = row.get("role")
        if role not in by_role:
            continue
        key = (str(row.get("run_id")), str(row.get("provider")))
        if row.get("event") == "start":
            starts[key] = row
            latest[role] = row
        elif row.get("event") == "end":
            starts.pop(key, None)
            latest[role] = row
    for role, row in latest.items():
        agent = by_role[role]
        for field in ("provider", "model_id", "cli", "run_id", "task_id"):
            agent[field] = row.get(field)
        if row.get("event") == "end":
            agent["state"] = "done" if row.get("success") else "failed"
        else:
            try:
                alive = lock.pid_alive(int(row.get("pid") or 0))
            except (TypeError, ValueError):
                alive = False
            if alive:
                agent["state"] = "working"
    return _clean({"project": project.name, "status": data["status"], "task": task,
                   "progress": {"done": done, "total": total, "in_progress": counts["~"],
                                "blocked": counts["!"], "pending": counts[" "], "remaining": total - done,
                                "percent": done * 100 // total if total else 0},
                   "next": {"id": pending.id, "title": pending.title} if pending else None,
                   "agents": agents, "events": raw_events})


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + ("…" if width else "")


def render(snapshot: dict[str, Any], frame: int = 0, width: int = 80, color: bool = False) -> str:
    width = max(1, width)
    lines: list[str] = []
    title = "HEFESTO"
    lines.append(" " * max(0, (width - len(title)) // 2) + title)
    lines.append(_fit(f"{snapshot['project']}  |  {snapshot['status']}", width))
    agents = snapshot["agents"]
    active = next((agent for agent in agents if agent["state"] == "working"), None)
    target = f" | {active['role'].title()}" if active else " | sin delegacion activa"
    lines.append(_fit(f"      [FORJA] plan -> trabajo -> revision -> entrega{target}", width))
    lines.append(_fit("      +-------------------------------------------+", width))
    lines.append(_fit("              |", width))
    for index, agent in enumerate(agents):
        name = agent["role"].title()
        label = STATE_LABELS.get(agent["state"], STATE_LABELS["idle"])
        pose = ("[o_O]", "[O_o]")[(frame + index) % 2] if agent["state"] == "working" else "[o_o]"
        attribution = (f" | {agent['provider']} {agent['model_id']} | {agent['cli']}"
                       if agent["provider"] else "")
        lines.append(_fit(f"{pose} {name:<8} {label}{attribution}", width))
    progress = snapshot["progress"]
    bar_width = max(3, min(18, width - 34))
    filled = round(bar_width * progress["percent"] / 100)
    bar = "=" * filled + "." * (bar_width - filled)
    lines.append(_fit(f"Progreso {bar} {progress['done']}/{progress['total']}  {progress['percent']}%  "
                      f"{progress['remaining']} restantes", width))
    current = snapshot.get("task")
    lines.append(_fit("Flujo", width))
    lines.append(_fit(f"En curso: {current['id']} | {current['title']}" if current else "En curso: ninguna tarea",
                      width))
    nxt = snapshot.get("next")
    lines.append(_fit(f"Siguiente: {nxt['id']} | {nxt['title']}" if nxt else "Siguiente: ninguna", width))
    lines.append(_fit("Eventos recientes", width))
    events = snapshot.get("events", [])[-2:]
    if not events:
        lines.append(_fit("Sin delegaciones registradas; los agentes estan en espera.", width))
    for event in events:
        role = str(event.get("role", "?")).title()
        status = STATE_LABELS["failed"] if event.get("event") == "end" and not event.get("success") else event.get("event", "")
        lines.append(_fit(f"{role} | {event.get('provider')} | {event.get('model_id')} | "
                          f"{event.get('cli')} | {status}", width))
    if color:
        lines = [f"\x1b[36m{line}\x1b[0m" if "HEFESTO" in line else line for line in lines]
    return "\n".join(lines)


def run(project: Path, once: bool = False, interval: float = 1.0, stream: TextIO | None = None,
        max_frames: int | None = None) -> int:
    if interval <= 0:
        raise HefestoError("El intervalo debe ser mayor que cero.", EXIT_INPUT)
    out = stream or sys.stdout
    width = shutil.get_terminal_size((80, 24)).columns
    if once or not util.is_tty():
        out.write(render(snapshot(project), width=width) + "\n")
        return 0
    out.write("\x1b[?25l")
    frame = 0
    try:
        while max_frames is None or frame < max_frames:
            out.write("\x1b[H" + render(snapshot(project), frame=frame, width=width, color=True))
            out.flush()
            frame += 1
            if max_frames is None or frame < max_frames:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h")
    return 0
