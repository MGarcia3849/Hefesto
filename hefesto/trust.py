"""Confianza de los comandos `verify`: hash SHA-256 aprobado por un humano, por tarea."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from hefesto import roadmap, util
from hefesto.util import atomic_write_json, now_iso, read_json


def trust_path(project: Path) -> Path:
    return project / ".hefesto" / "trust.json"


def verify_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def load(project: Path) -> dict[str, Any]:
    data = read_json(trust_path(project), default=None) or {}
    data.setdefault("schema_version", 1)
    data.setdefault("verify", {})
    return data


def status(project: Path, task: roadmap.Task) -> str:
    """`trusted`, `new` o `changed`."""
    entry = load(project)["verify"].get(task.id)
    if not entry:
        return "new"
    return "trusted" if entry.get("sha256") == verify_hash(task.verify) else "changed"


def approve_interactive(project: Path, rm: roadmap.Roadmap) -> tuple[int, list[str]]:
    """Muestra todos los `verify` y guarda los hashes si el humano confirma. Exige TTY."""
    util.require_tty("hefesto trust")
    data = load(project)
    lines: list[str] = []
    pending: list[roadmap.Task] = []
    for task in rm.tasks:
        if not task.verify:
            continue
        state = status(project, task)
        lines.append(f"  [{state:>7}] {task.id}: {task.verify}")
        if state != "trusted":
            pending.append(task)
    print("Comandos `verify` del ROADMAP (se ejecutarán en tu máquina con bash):")
    print("\n".join(lines) if lines else "  (ninguno)")
    if not pending:
        return 0, ["Todos los verify ya estaban aprobados."]
    if not util.confirm(f"¿Apruebas {len(pending)} comando(s) nuevo(s) o modificado(s)?", default=False):
        return 0, ["No se ha aprobado nada."]
    for task in pending:
        data["verify"][task.id] = {"sha256": verify_hash(task.verify), "approved_at": now_iso()}
    atomic_write_json(trust_path(project), data)
    return len(pending), [f"Aprobados: {', '.join(t.id for t in pending)}"]
