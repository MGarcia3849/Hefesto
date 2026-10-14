"""`hefesto status`: resumen compacto del proyecto."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hefesto import state


def current_branch(path: Path) -> str:
    result = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=path, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip() or "(detached)"


def render(project: Path) -> str:
    data = state.load(project)
    workdir = Path(data.get("workdir") or project)
    lines = [
        f"hefesto: {data['status']}",
        f"  tarea:     {data['current_task_id'] or '-'}  intentos: {data['attempts']}",
        f"  rol:       {data['active_role'] or '-'}",
        f"  rama:      {current_branch(workdir if workdir.exists() else project)}",
    ]
    if data.get("last_error"):
        lines.append(f"  error:     {str(data['last_error'])[:200]}")
    lines.append(f"  siguiente: {data['next_action']}")
    return "\n".join(lines)
