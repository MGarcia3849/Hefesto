"""Registro en `.hefesto/logs/`, siempre con secretos redactados."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hefesto.guard import redact
from hefesto.util import append_jsonl, now_iso


def logs_dir(project: Path) -> Path:
    return project / ".hefesto" / "logs"


def event(project: Path, kind: str, message: str, **fields: Any) -> None:
    """Añade una línea a `logs/hefesto.log` (texto) con el evento redactado."""
    path = logs_dir(project) / "hefesto.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = " ".join(f"{k}={redact(str(v))}" for k, v in fields.items())
    line = f"{now_iso()} [{kind}] {redact(message)}" + (f" {extra}" if extra else "")
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def record(project: Path, name: str, data: dict[str, Any]) -> None:
    """Añade un registro JSON a `logs/<name>.jsonl`, con los textos redactados."""
    clean = {k: (redact(v) if isinstance(v, str) else v) for k, v in data.items()}
    clean.setdefault("at", now_iso())
    append_jsonl(logs_dir(project) / f"{name}.jsonl", clean)


def write_text(project: Path, rel: str, text: str) -> Path:
    path = logs_dir(project) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text), encoding="utf-8", newline="\n")
    return path
