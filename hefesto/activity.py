"""Registro de agentes activos y seguimiento de ejecuciones en directo."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

from hefesto import lock, util


def active_dir(project: Path) -> Path:
    """Directorio donde se registran los procesos de agentes activos."""
    return project / ".hefesto" / "logs" / "active"


def resolve_log_path(project: Path, log_path: str) -> Path:
    """Resuelve un log y rechaza escapes, incluidos enlaces simbólicos."""
    if not isinstance(log_path, str) or not log_path or "\x00" in log_path:
        raise ValueError("ruta de log no válida")
    root = (project / ".hefesto" / "logs").resolve()
    path = (project / log_path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("ruta de log no válida")
    return path


def register(
    project: Path,
    run_id: str,
    kind: str,
    role: str,
    provider: str,
    model_id: str,
    task_id: str | None,
    log_path: str,
    pid: int | None = None,
) -> Path:
    """Registra un agente activo de forma atómica en `.hefesto/logs/active/<run_id>.json`."""
    path = resolve_log_path(project, log_path)
    normalized = os.path.relpath(path, project.resolve()).replace(os.sep, "/")
    target = active_dir(project) / f"{run_id}.json"
    effective_pid = os.getpid() if pid is None else pid
    record: dict[str, Any] = {
        "run_id": run_id,
        "kind": kind,
        "pid": effective_pid,
        "role": role,
        "provider": provider,
        "model_id": model_id,
        "task_id": task_id,
        "started_at": util.now_iso(),
        "log_path": normalized,
    }
    try:
        util.atomic_write_json(target, record)
    except OSError:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            util.atomic_write_json(target, record)
        except OSError:
            pass
    return target


def finish(project: Path, run_id: str) -> None:
    """Elimina la entrada del agente activo si existe."""
    target = active_dir(project) / f"{run_id}.json"
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def list_active(project: Path) -> list[dict[str, Any]]:
    """Devuelve las entradas cuyos PIDs siguen vivos y elimina huérfanas o ilegibles."""
    directory = active_dir(project)
    try:
        if not directory.is_dir():
            return []
    except OSError:
        return []

    try:
        entry_files = sorted(directory.glob("*.json"))
    except OSError:
        return []

    active: list[dict[str, Any]] = []
    for entry_file in entry_files:
        try:
            raw = entry_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            try:
                entry_file.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        except FileNotFoundError:
            continue
        except OSError:
            continue

        if not isinstance(data, dict):
            try:
                entry_file.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or not lock.pid_alive(pid):
            try:
                entry_file.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        active.append(data)
    return active


@contextlib.contextmanager
def tracking(
    project: Path,
    run_id: str,
    kind: str,
    role: str,
    provider: str,
    model_id: str,
    task_id: str | None,
    log_path: str,
    pid: int | None = None,
) -> Iterator[Path]:
    """Context manager que registra la actividad al entrar y la elimina al salir."""
    entry_path = register(
        project,
        run_id,
        kind,
        role,
        provider,
        model_id,
        task_id,
        log_path,
        pid,
    )
    try:
        yield entry_path
    finally:
        finish(project, run_id)
