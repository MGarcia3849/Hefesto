"""Read-only console dashboard for Hefesto.

The snapshot reads state.load, the ROADMAP, configuration and events_path; it never writes state,
launches processes, or reads secrets (.env*, *.pem, *.key or id_*). All displayed text passes through
guard.redact. render(snapshot, frame=0, width=80, color=False) produces a deterministic, width-bounded
view. run(project, once=False, interval=1.0, stream=None, max_frames=None) uses util.is_tty(),
shutil.get_terminal_size, time.sleep and KeyboardInterrupt handling for an animated TTY view.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from hefesto import config, guard, lock, roadmap, state, util
from hefesto.util import EXIT_INPUT, HefestoError, read_jsonl

ROLES = ("core", "runner", "forge", "reviewer")
STATE_LABELS = {"idle": "en espera", "working": "trabajando", "done": "terminó", "failed": "falló"}
MAX_EVENTS = 8
SNAPSHOT_KEYS = {"project", "status", "task", "progress", "next", "agents", "events"}
AGENT_KEYS = {"role", "providers", "state", "provider", "model_id", "cli", "run_id", "task_id"}
EVENT_KEYS = ("event", "run_id", "task_id", "role", "provider", "model_id", "cli", "pid",
              "orchestrator", "at")
_ROLE_NAMES = {"core": "Core", "runner": "Runner", "forge": "Forge", "reviewer": "Reviewer"}
_SPINNERS = ("·", "•", "●", "•")


def events_path(project: Path) -> Path:
    return Path(project) / ".hefesto" / "logs" / "delegations.jsonl"


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return guard.redact(value)
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {guard.redact(str(key)): _safe(item) for key, item in value.items()}
    return value


def _read_events(project: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(events_path(project))
    clean: list[dict[str, Any]] = []
    for row in rows[-MAX_EVENTS:]:
        if not isinstance(row, dict):
            continue
        event = {key: _safe(row.get(key)) for key in EVENT_KEYS}
        # Kept for status rendering; success/error text is redacted as well.
        if "success" in row:
            event["success"] = row["success"]
        if "error_type" in row:
            event["error_type"] = _safe(row["error_type"])
        clean.append(event)
    return clean


def snapshot(project: Path) -> dict[str, Any]:
    """Build a serializable, read-only view from project state and local logs."""
    project = Path(project)
    data = state.load(project)
    rm = roadmap.load_valid(project)
    cfg = config.load(project)
    tasks = list(rm.tasks)
    counts = {"x": 0, "~": 0, "!": 0, " ": 0}
    for item in tasks:
        if item.marker in counts:
            counts[item.marker] += 1
    total, done = len(tasks), counts["x"]
    current_id = data.get("current_task_id")
    current = next((item for item in tasks if item.id == current_id), None)
    task = None
    if current:
        task = {"id": _safe(current.id), "title": _safe(current.title),
                "role": _safe(data.get("active_role") or current.role),
                "attempts": data.get("attempts", 0), "phase": _safe(current.phase)}
    next_task = next((item for item in tasks if item.marker == " "), None)
    events = _read_events(project)
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        role = event.get("role")
        if role in ROLES:
            latest[role] = event
    agents = []
    for role in ROLES:
        event = latest.get(role)
        agent_state = "idle"
        if event:
            if event.get("event") == "end":
                agent_state = "done" if event.get("success") is True else "failed"
            elif event.get("event") == "start":
                try:
                    alive = lock.pid_alive(int(event.get("pid") or 0))
                except (TypeError, ValueError, OSError):
                    alive = False
                if alive:
                    agent_state = "working"
        agents.append({
            "role": role,
            "providers": _safe(cfg.role_chain(role)),
            "state": agent_state,
            "provider": event.get("provider") if event else None,
            "model_id": event.get("model_id") if event else None,
            "cli": event.get("cli") if event else None,
            "run_id": event.get("run_id") if event else None,
            "task_id": event.get("task_id") if event else None,
        })
    return _safe({
        "project": project.name,
        "status": data.get("status"),
        "task": task,
        "progress": {"done": done, "total": total, "in_progress": counts["~"], "blocked": counts["!"],
                     "pending": counts[" "], "remaining": total - done,
                     "percent": done * 100 // total if total else 0},
        "next": {"id": next_task.id, "title": next_task.title} if next_task else None,
        "agents": agents,
        "events": events,
    })


def _clip(value: Any, width: int) -> str:
    text = guard.redact(str(value))
    return text if len(text) <= width else (text[:max(0, width - 1)] + "…" if width else "")


def render(snapshot: dict[str, Any], frame: int = 0, width: int = 80, color: bool = False) -> str:
    """Render a pure, deterministic dashboard. Every plain line fits within width."""
    width = max(1, int(width))
    def line(text: str = "") -> str:
        return _clip(text, width)

    center = "HEFESTO"
    pad = max(0, (width - len(center)) // 2)
    rows = [line(" " * pad + center), line(f"{snapshot.get('project', 'proyecto')} · {snapshot.get('status', 'IDLE')}"),
            line("─" * min(width, 40))]
    agents = {a.get("role"): a for a in snapshot.get("agents", [])}
    for role in ROLES:
        agent = agents.get(role, {})
        state_key = agent.get("state", "idle")
        label = STATE_LABELS.get(state_key, STATE_LABELS["idle"])
        if state_key == "working":
            label = f"{_SPINNERS[frame % len(_SPINNERS)]} {label}"
        detail = ""
        if agent.get("provider") is not None:
            detail = " · ".join(str(agent.get(k)) for k in ("provider", "model_id", "cli") if agent.get(k))
        rows.append(line(f"{_ROLE_NAMES[role]:<9} {label}" + (f" · {detail}" if detail else "")))
    progress = snapshot.get("progress", {})
    rows.extend((line("─" * min(width, 40)),
                 line(f"Progreso: {progress.get('done', 0)}/{progress.get('total', 0)} · "
                      f"{progress.get('percent', 0)}% · {progress.get('remaining', 0)} restantes")))
    task = snapshot.get("task")
    rows.append(line(f"En curso: {task['id']} · {task['title']}" if task else "En curso: ninguna"))
    next_task = snapshot.get("next")
    rows.append(line(f"Siguiente: {next_task['id']} · {next_task['title']}" if next_task else "Siguiente: ninguna"))
    events = snapshot.get("events", [])
    if events:
        rows.append(line("Delegaciones recientes:"))
        for event in events:
            role = _ROLE_NAMES.get(str(event.get("role")), str(event.get("role") or "?"))
            label = (STATE_LABELS["failed"] if event.get("event") == "end" and event.get("success") is False
                     else STATE_LABELS["working"] if event.get("event") == "start" else STATE_LABELS["done"])
            rows.append(line(f"{role} · {event.get('provider') or '?'} · {event.get('cli') or '?'} · {label}"))
    plain = "\n".join(rows) + "\n"
    if not color:
        return plain
    return "\n".join(f"\x1b[36m{row}\x1b[0m" if "HEFESTO" in row else row for row in rows) + "\n"


def run(project: Path, once: bool = False, interval: float = 1.0, stream: TextIO | None = None,
        max_frames: int | None = None) -> int:
    """Print one static snapshot or refresh a live TTY, restoring its cursor on exit."""
    if interval <= 0:
        raise HefestoError("--interval debe ser mayor que cero.", EXIT_INPUT)
    stream = stream or sys.stdout
    width = shutil.get_terminal_size((80, 24)).columns
    if once or not util.is_tty():
        stream.write(render(snapshot(project), width=width, color=False))
        stream.flush()
        return 0
    stream.write("\x1b[?25l")
    stream.flush()
    frame = 0
    try:
        while max_frames is None or frame < max_frames:
            current = snapshot(project)
            stream.write("\x1b[H" + render(current, frame=frame, width=width, color=True))
            stream.flush()
            frame += 1
            if max_frames is None or frame < max_frames:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        stream.write("\x1b[?25h")
        stream.flush()
    return 0
