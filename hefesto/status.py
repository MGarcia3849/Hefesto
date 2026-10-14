"""`hefesto status`: resumen compacto del proyecto."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hefesto import roadmap, state


def current_branch(path: Path) -> str:
    result = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=path, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip() or "(detached)"


def protocol_text(project: Path) -> str:
    local_proto = project / ".hefesto" / "AGENTS.md"
    if local_proto.exists():
        return local_proto.read_text(encoding="utf-8")
    root_proto = project / "AGENTS.md"
    if root_proto.exists():
        return root_proto.read_text(encoding="utf-8")
    return ""


def render(project: Path, protocol: bool = False) -> str:
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
    rm = roadmap.parse(roadmap.roadmap_path(project)) if roadmap.roadmap_path(project).exists() else None
    if rm is not None and rm.tasks:
        counts = {m: sum(1 for t in rm.tasks if t.marker == m) for m in roadmap.MARKERS}
        lines.append(f"  roadmap:   {counts['x']}/{len(rm.tasks)} hechas, {counts['~']} en curso, "
                     f"{counts['!']} bloqueadas, {counts[' ']} pendientes")
        errors = roadmap.validate(rm, project)
        if errors:
            lines.append(f"  aviso:     ROADMAP inválido ({len(errors)} errores): {errors[0]}")
        elif data["status"] in ("IDLE", "COMPLETED"):
            task, reason = roadmap.next_task(rm)
            lines.append(f"  próxima:   {task.id + ': ' + task.title if task else reason}")
    lines.append(f"  siguiente: {data['next_action']}")
    text = "\n".join(lines)
    if protocol:
        proto = protocol_text(project)
        if proto:
            return f"{text}\n\n{proto.rstrip()}\n"
    return text
