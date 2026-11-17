"""Modern Web Guidance v1.0.0 wrapper.

`BINARY == "modern-web-guidance"`, `PINNED_VERSION == "1.0.0"`, and
`ACTIONS == ("search", "retrieve", "legacy-scan")`. `ensure_cli` requires a
preinstalled binary and returns EXIT_UNAVAILABLE (3) if absent or unpinned.
Every invocation uses a bounded timeout, `util.filtered_env` with
`DISABLE_TELEMETRY`, no shell, and project cwd. `search`, `retrieve`, and
`legacy_scan` return immutable `GuidanceResult` values. Inputs are validated,
legacy scan paths stay inside the project, and external output is treated as
untrusted data: redacted and bounded by `max_output_chars`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, guard, util
from hefesto.util import EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError

BINARY = "modern-web-guidance"
PINNED_VERSION = "1.0.0"
ACTIONS = ("search", "retrieve", "legacy-scan")
DISABLE_TELEMETRY = "DISABLE_TELEMETRY"
QUERY_MAX_CHARS = 200
GUIDE_ID_RE = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}"
MAX_OUTPUT_CHARS = 20000
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class GuidanceResult:
    action: str
    ok: bool
    exit_code: int
    output: str
    truncated: bool


def timeout_seconds(project: Path) -> int:
    return config.load(project).get("web_guidance", "timeout_seconds", default=60)


def max_output_chars(project: Path) -> int:
    value = config.load(project).get("web_guidance", "max_output_chars", default=MAX_OUTPUT_CHARS)
    return max(1, value) if isinstance(value, int) else MAX_OUTPUT_CHARS


def ensure_cli() -> str:
    binary = util.which(BINARY)
    if not binary:
        raise HefestoError(f"No está instalado {BINARY} {PINNED_VERSION}; instálalo explícitamente.",
                           EXIT_UNAVAILABLE)
    try:
        result = subprocess.run([binary, "--version"], shell=False, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                env=util.filtered_env({DISABLE_TELEMETRY: "1", "PYTHONIOENCODING": "utf-8"}))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HefestoError(f"No se pudo comprobar {BINARY} {PINNED_VERSION}: {exc}", EXIT_UNAVAILABLE) from exc
    version = (result.stdout + result.stderr).strip()
    if result.returncode or version != PINNED_VERSION:
        raise HefestoError(f"Se requiere {BINARY} {PINNED_VERSION}; detectado {version or 'desconocido'}.",
                           EXIT_UNAVAILABLE)
    return binary


def _run(project: Path, action: str, value: str) -> GuidanceResult:
    binary = ensure_cli()
    try:
        result = subprocess.run([binary, action, value], cwd=project, shell=False, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout_seconds(project),
                                env=util.filtered_env({DISABLE_TELEMETRY: "1", "PYTHONIOENCODING": "utf-8"}))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HefestoError(f"{BINARY} {action} no pudo ejecutarse: {exc}", EXIT_UNAVAILABLE) from exc
    output = result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    output = guard.redact(output)
    limit = max_output_chars(project)
    truncated = len(output) > limit
    if truncated:
        marker = "… [salida recortada]"
        output = output[:max(0, limit - len(marker))] + marker[:limit]
    return GuidanceResult(action, result.returncode == 0, result.returncode, output, truncated)


def search(project: Path, query: str) -> GuidanceResult:
    query = query.strip()
    if not query or len(query) > QUERY_MAX_CHARS or query.startswith("-") or _CONTROL_RE.search(query):
        raise HefestoError("Consulta inválida (1–200 caracteres, sin controles ni opciones).", EXIT_INPUT)
    return _run(project, "search", query)


def retrieve(project: Path, guide_id: str) -> GuidanceResult:
    if (not isinstance(guide_id, str) or not re.fullmatch(GUIDE_ID_RE, guide_id) or
            any(part == ".." for part in guide_id.split("/")) or "\\" in guide_id):
        raise HefestoError("ID de guía inválido.", EXIT_INPUT)
    return _run(project, "retrieve", guide_id)


def legacy_scan(project: Path, path: str = ".") -> GuidanceResult:
    root = project.resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        rel = resolved.relative_to(root).as_posix() or "."
    except ValueError as exc:
        raise HefestoError("La ruta de legacy-scan debe permanecer dentro del proyecto.", EXIT_INPUT) from exc
    if not resolved.exists():
        raise HefestoError(f"No existe la ruta de legacy-scan: {path}.", EXIT_INPUT)
    return _run(project, "legacy-scan", rel)
