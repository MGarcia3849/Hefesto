"""Evidencias de cada tarea (`.hefesto/evidence/TASK.json`) encadenadas por SHA-256, y ejecución de `verify`."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hefesto import log
from hefesto.guard import redact
from hefesto.util import EXIT_UNAVAILABLE, HefestoError, atomic_write_text, filtered_env, find_bash, now_iso

SUMMARY_LINES = 20
SUMMARY_CHARS = 2000


@dataclass
class VerifyResult:
    exit_code: int
    duration: float
    output: str
    log_path: str

    @property
    def summary(self) -> str:
        lines = redact(self.output).strip().splitlines()[-SUMMARY_LINES:]
        return "\n".join(lines)[-SUMMARY_CHARS:]


def run_verify(project: Path, workdir: Path, task_id: str, command: str, timeout: int, label: str = "") -> VerifyResult:
    """Ejecuta `bash -c` en el directorio de trabajo, con timeout y entorno sin credenciales. Sin eval."""
    bash = find_bash()
    if not bash:
        raise HefestoError("No se encuentra bash para ejecutar `verify` (en Windows: Git for Windows).", EXIT_UNAVAILABLE)
    start = time.monotonic()
    try:
        proc = subprocess.run([bash, "-c", command], cwd=workdir, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=filtered_env(strip_credentials=True))
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        partial = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        code, output = 124, partial + f"\n[hefesto] verify superó el timeout de {timeout}s"
    duration = round(time.monotonic() - start, 3)
    stamp = now_iso().replace(":", "")
    path = log.write_text(project, f"verify/{task_id}{('-' + label) if label else ''}-{stamp}.log",
                          f"$ {command}\n[exit {code}, {duration}s]\n{output}")
    return VerifyResult(code, duration, output, str(path.relative_to(project).as_posix()))


def evidence_dir(project: Path) -> Path:
    return project / ".hefesto" / "evidence"


def file_sha256(path: Path) -> str:
    """Hash con saltos de línea normalizados: estable aunque git convierta LF↔CRLF al clonar."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def load_all(project: Path) -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(evidence_dir(project).glob("TASK-*.json")):
        try:
            items.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            items.append((path, {}))
    return sorted(items, key=lambda item: item[1].get("seq", 0) if isinstance(item[1].get("seq"), int) else 0)


def write(project: Path, record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Añade la evidencia al final de la cadena: `seq` siguiente y hash de la anterior."""
    chain = load_all(project)
    previous = chain[-1] if chain else None
    record = dict(record)
    record["schema_version"] = 1
    record["seq"] = (previous[1].get("seq", 0) + 1) if previous else 1
    record["prev_sha256"] = file_sha256(previous[0]) if previous else None
    record["prev_task_id"] = previous[1].get("task_id") if previous else None
    record["created_at"] = now_iso()
    path = evidence_dir(project) / f"{record['task_id']}.json"
    if path.exists():
        raise HefestoError(f"Ya existe la evidencia {path.name}; las evidencias no se reescriben.", 4)
    atomic_write_text(path, _canonical(record))
    return path, {"seq": record["seq"], "sha256": file_sha256(path), "task_id": record["task_id"]}


def verify_chain(project: Path, head: dict[str, Any] | None = None) -> list[str]:
    """Comprueba la cadena completa. `head` (del estado local) detecta el borrado de la última."""
    errors: list[str] = []
    chain = load_all(project)
    prev_path: Path | None = None
    prev_data: dict[str, Any] | None = None
    for index, (path, data) in enumerate(chain, 1):
        if not data:
            errors.append(f"{path.name}: JSON ilegible")
            continue
        if data.get("seq") != index:
            errors.append(f"{path.name}: seq {data.get('seq')} donde se esperaba {index} (falta o sobra una evidencia)")
        expected = file_sha256(prev_path) if prev_path else None
        if data.get("prev_sha256") != expected:
            errors.append(f"{path.name}: el hash de la evidencia anterior no coincide "
                          f"({(prev_data or {}).get('task_id', 'inicio')} fue editada o borrada)")
        if path.stem != data.get("task_id"):
            errors.append(f"{path.name}: task_id {data.get('task_id')} no coincide con el nombre")
        prev_path, prev_data = path, data
    if head:
        if not chain or prev_path is None:
            errors.append(f"falta la evidencia {head.get('task_id')} (cabeza de la cadena)")
        elif (prev_data or {}).get("seq", 0) < head.get("seq", 0):
            errors.append(f"la cadena termina en seq {(prev_data or {}).get('seq')} pero la última registrada es "
                          f"{head.get('seq')} ({head.get('task_id')}): se borró una evidencia")
        elif (prev_data or {}).get("seq") == head.get("seq") and file_sha256(prev_path) != head.get("sha256"):
            errors.append(f"{prev_path.name}: editada después de registrarse")
    return errors


def show(project: Path, task_id: str) -> dict[str, Any]:
    path = evidence_dir(project) / f"{task_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HefestoError(f"No hay evidencia para {task_id}.", 1) from exc
