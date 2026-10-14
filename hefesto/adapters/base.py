"""Interfaz común de adaptadores: reciben rol, prompt, task_id y run_id; devuelven un resultado validado.

Una salida que no cumpla el esquema es un fallo (`invalid_output`), nunca un éxito.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hefesto import config, log
from hefesto.guard import redact
from hefesto.util import filtered_env

ERROR_TYPES = ("missing_binary", "auth", "quota", "rate_limit", "timeout", "invalid_output", "denied_by_policy")
READ_ONLY_ROLES = ("reviewer",)
SUMMARY_CHARS = 1500


@dataclass
class AdapterResult:
    success: bool
    provider: str
    model: str
    summary: str
    error_type: str | None
    usage: dict[str, Any] | str
    log_path: str
    reset_hint: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("reset_hint")
        return data


def validate_result(data: Any) -> list[str]:
    """Esquema mínimo del resultado de un adaptador."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["no es un objeto"]
    types = {"success": bool, "provider": str, "model": str, "summary": str, "log_path": str}
    for key, typ in types.items():
        if not isinstance(data.get(key), typ):
            errors.append(f"`{key}` debe ser {typ.__name__}")
    if data.get("error_type") is not None and data.get("error_type") not in ERROR_TYPES:
        errors.append(f"error_type desconocido {data.get('error_type')!r}")
    if data.get("success") and data.get("error_type"):
        errors.append("success con error_type")
    usage = data.get("usage")
    if not (usage == "unknown" or isinstance(usage, dict)):
        errors.append("usage debe ser un objeto o `unknown`")
    return errors


@dataclass
class Call:
    role: str
    prompt: str
    task_id: str | None
    run_id: str
    model: str
    workdir: Path
    project: Path
    packet_path: Path | None = None
    timeout: int = 600
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.role in READ_ONLY_ROLES


class Adapter:
    """Adaptador de CLI. Las subclases definen `command`, `parse` y los patrones de error."""

    provider = ""
    patterns: dict[str, re.Pattern[str]] = {}

    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg

    # -- a implementar por cada CLI --------------------------------------------------
    def binary(self) -> str | None:
        from hefesto.providers import binary_for

        return binary_for(self.cfg, self.provider)

    def command(self, binary: str, call: Call) -> list[str]:
        raise NotImplementedError

    def stdin_for(self, call: Call) -> str:
        return call.prompt

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        raise NotImplementedError

    # -- comunes -----------------------------------------------------------------------
    def classify(self, text: str) -> str | None:
        for error_type in ("auth", "quota", "rate_limit"):
            pattern = self.patterns.get(error_type)
            if pattern is not None and pattern.search(text):
                return error_type
        return None

    def result(self, call: Call, success: bool, summary: str, error_type: str | None,
               usage: dict[str, Any] | str, log_path: str, reset_hint: str = "") -> AdapterResult:
        return AdapterResult(success=success, provider=self.provider, model=call.model or "default",
                             summary=redact(summary.strip())[:SUMMARY_CHARS], error_type=error_type, usage=usage,
                             log_path=log_path, reset_hint=reset_hint)

    def run(self, call: Call) -> AdapterResult:
        rel_log = f"runs/{call.run_id}-{self.provider}.log"
        binary = self.binary()
        if not binary:
            path = log.write_text(call.project, rel_log, f"{self.provider}: binario no encontrado")
            return self.result(call, False, f"{self.provider} no está instalado", "missing_binary", "unknown",
                               _rel(call.project, path))
        argv = self.command(binary, call)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, input=self.stdin_for(call), cwd=call.workdir, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=call.timeout,
                                  env=filtered_env(strip_credentials=False))
            stdout, stderr, code = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired:
            path = log.write_text(call.project, rel_log, f"$ {' '.join(argv[:6])}\n[timeout {call.timeout}s]")
            return self.result(call, False, f"timeout tras {call.timeout}s", "timeout", "unknown", _rel(call.project, path))
        except OSError as exc:
            path = log.write_text(call.project, rel_log, f"$ {' '.join(argv[:6])}\n{exc}")
            return self.result(call, False, str(exc), "missing_binary", "unknown", _rel(call.project, path))
        duration = round(time.monotonic() - start, 3)
        path = log.write_text(call.project, rel_log,
                              f"$ {' '.join(argv)}\n[exit {code}, {duration}s]\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
        call.extra["log_path"] = _rel(call.project, path)
        result = self.parse(stdout, stderr, code, call)
        result.duration = duration
        if validate_result(result.to_dict()):
            return self.result(call, False, "salida del adaptador fuera de esquema", "invalid_output", "unknown",
                               result.log_path)
        return result

    def failure_from_text(self, call: Call, text: str, code: int) -> AdapterResult:
        error_type = self.classify(text)
        summary = text.strip().splitlines()[-1] if text.strip() else f"salida vacía (exit {code})"
        return self.result(call, False, summary, error_type or ("invalid_output" if code == 0 else None),
                           "unknown", call.extra.get("log_path", ""), reset_hint=text)


def _rel(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return str(path)


def loads_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def last_json_object(text: str) -> Any:
    """El último objeto JSON válido de la salida (algunas CLIs imprimen avisos antes)."""
    whole = loads_or_none(text.strip())
    if whole is not None:
        return whole
    for line in reversed(text.strip().splitlines()):
        parsed = loads_or_none(line.strip())
        if isinstance(parsed, dict):
            return parsed
    start = text.find("{")
    return loads_or_none(text[start:]) if start >= 0 else None
