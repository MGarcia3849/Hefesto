"""Utilidades comunes: rutas, códigos de salida, escritura atómica, TTY y procesos."""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# Códigos de salida (sección 7).
EXIT_OK = 0
EXIT_INPUT = 1
EXIT_BLOCKED = 2
EXIT_UNAVAILABLE = 3
EXIT_CONFLICT = 4

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

# Variables que harían que una CLI facturase por API en lugar de por suscripción.
API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
_CREDENTIAL_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY")


class HefestoError(Exception):
    """Error controlado: se muestra al usuario y termina con `code`."""

    def __init__(self, message: str, code: int = EXIT_INPUT) -> None:
        super().__init__(message)
        self.code = code


def hefesto_home() -> Path:
    """Directorio global del usuario (`~/hefesto`): configuración y memoria global."""
    return Path(os.environ.get("HEFESTO_HOME") or Path.home() / "hefesto")


def state_home() -> Path:
    """Directorio de estado global (`~/.hefesto`): proveedores y cachés."""
    return Path(os.environ.get("HEFESTO_STATE_DIR") or Path.home() / ".hefesto")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def is_tty() -> bool:
    """True solo si stdin y stdout son terminales. Los tests lo sustituyen en proceso."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def require_tty(action: str) -> None:
    if not is_tty():
        raise HefestoError(f"`{action}` exige una terminal interactiva (humana).", EXIT_BLOCKED)


def confirm(question: str, default: bool = False) -> bool:
    suffix = " [S/n] " if default else " [s/N] "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("s", "si", "sí", "y", "yes")


def atomic_write_text(path: Path, text: str) -> None:
    """Escribe en un temporal del mismo directorio y lo mueve encima (atómico)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def which(name: str) -> str | None:
    """Resuelve un ejecutable; en Windows también encuentra los `.cmd` de npm."""
    return shutil.which(name)


def find_bash() -> str | None:
    """Bash para ejecutar `verify`. En Windows usa el de Git, nunca el lanzador de WSL."""
    if not IS_WINDOWS:
        return which("bash")
    git = which("git")
    if git:
        git_path = Path(git).resolve()
        for parent in git_path.parents:
            for candidate in (parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
            if parent.name.lower() == "git":
                break
    found = which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def filtered_env(extra: dict[str, str] | None = None, strip_credentials: bool = True) -> dict[str, str]:
    """Entorno para procesos hijo sin claves de API ni variables con aspecto de credencial."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in API_KEY_VARS or upper.endswith("_API_KEY"):
            continue
        if strip_credentials and any(marker in upper for marker in _CREDENTIAL_MARKERS):
            continue
        env[key] = value
    if extra:
        env.update(extra)
    return env


def present_api_keys() -> list[str]:
    return sorted(k for k in os.environ if k.upper() in API_KEY_VARS or k.upper().endswith("_API_KEY"))


def rel_inside(root: Path, rel: str) -> bool:
    """True si `rel` es una ruta relativa que queda dentro de `root`."""
    if not rel or rel.startswith(("/", "\\")) or (len(rel) > 1 and rel[1] == ":"):
        return False
    parts = Path(rel.replace("\\", "/")).parts
    if ".." in parts:
        return False
    try:
        (root / rel).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
