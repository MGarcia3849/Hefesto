"""`.hefesto/state.json`: esquema, validación y escritura atómica."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hefesto.util import EXIT_CONFLICT, HefestoError, atomic_write_json, now_iso

STATUSES = ("IDLE", "IN_PROGRESS", "BLOCKED_NEED_ARCHITECT_REVIEW", "COMPLETED")

INITIAL: dict[str, Any] = {
    "schema_version": 1,
    "status": "IDLE",
    "run_id": None,
    "current_task_id": None,
    "active_role": None,
    "attempts": 0,
    "last_checkpoint": None,
    "last_error": None,
    "next_action": "hefesto next",
    "updated_at": None,
}


TRANSITIONS: dict[str, tuple[str, ...]] = {
    "IDLE": ("IDLE", "IN_PROGRESS", "COMPLETED"),
    "IN_PROGRESS": ("IN_PROGRESS", "IDLE", "BLOCKED_NEED_ARCHITECT_REVIEW", "COMPLETED"),
    "BLOCKED_NEED_ARCHITECT_REVIEW": ("IDLE",),
    "COMPLETED": ("COMPLETED", "IDLE", "IN_PROGRESS"),
}


def transition(data: dict[str, Any], new_status: str) -> dict[str, Any]:
    """Devuelve una copia con el nuevo estado; rechaza transiciones ilegales."""
    current = data.get("status")
    if new_status not in STATUSES:
        raise HefestoError(f"Estado desconocido {new_status!r}", EXIT_CONFLICT)
    if new_status not in TRANSITIONS.get(str(current), ()):
        raise HefestoError(f"Transición ilegal {current} → {new_status}", EXIT_CONFLICT)
    out = dict(data)
    out["status"] = new_status
    return out


def state_path(project: Path) -> Path:
    return project / ".hefesto" / "state.json"


def validate(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["state.json no es un objeto"]
    for key in INITIAL:
        if key not in data:
            errors.append(f"falta `{key}`")
    if data.get("schema_version") != 1:
        errors.append("schema_version debe ser 1")
    if data.get("status") not in STATUSES:
        errors.append(f"estado desconocido {data.get('status')!r}")
    attempts = data.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        errors.append("attempts debe ser un entero ≥ 0")
    if data.get("status") in ("IN_PROGRESS", "BLOCKED_NEED_ARCHITECT_REVIEW") and not data.get("current_task_id"):
        errors.append(f"{data.get('status')} sin current_task_id")
    return errors


def load(project: Path) -> dict[str, Any]:
    path = state_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HefestoError("Falta .hefesto/state.json. Ejecuta `hefesto init`.", EXIT_CONFLICT) from exc
    except json.JSONDecodeError as exc:
        raise HefestoError(f"state.json corrupto: {exc}", EXIT_CONFLICT) from exc
    errors = validate(data)
    if errors:
        raise HefestoError("state.json inválido: " + "; ".join(errors), EXIT_CONFLICT)
    return data


def save(project: Path, data: dict[str, Any]) -> None:
    """Valida y escribe de forma atómica. Llamar siempre con el lock tomado."""
    data = dict(data)
    data["updated_at"] = now_iso()
    errors = validate(data)
    if errors:
        raise HefestoError("Estado resultante inválido: " + "; ".join(errors), EXIT_CONFLICT)
    atomic_write_json(state_path(project), data)


def write_initial(project: Path) -> None:
    data = dict(INITIAL)
    data["updated_at"] = now_iso()
    atomic_write_json(state_path(project), data)
