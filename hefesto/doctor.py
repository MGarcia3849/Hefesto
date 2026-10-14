"""`hefesto doctor`: dependencias, adaptadores, allowlist, lock y estado. No gasta cuota."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, state
from hefesto.util import (
    EXIT_OK,
    EXIT_UNAVAILABLE,
    HefestoError,
    IS_WINDOWS,
    find_bash,
    present_api_keys,
    which,
)

OK, WARN, MISSING, INFO = "ok", "!!", "--", "··"


@dataclass
class Check:
    level: str
    name: str
    detail: str
    required: bool = False


def _version(binary: str, *args: str) -> str:
    try:
        result = subprocess.run([binary, *(args or ("--version",))], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"
    out = (result.stdout or result.stderr).strip().splitlines()
    return out[0].strip() if out else "sin salida"


def _binary(name: str, required: bool = False, purpose: str = "") -> Check:
    path = which(name)
    if not path:
        level = WARN if required else MISSING
        return Check(level, name, f"no encontrado{(' — ' + purpose) if purpose else ''}", required)
    return Check(OK, name, f"{_version(path)}  ({path})", required)


def auth_status(provider: str) -> str:
    """Autenticación comprobable sin gastar llamadas: `yes`, `no` o `unknown`."""
    if provider == "codex":
        path = which("codex")
        if not path:
            return "no"
        try:
            result = subprocess.run([path, "login", "status"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        text = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and "logged in" in text and "not logged in" not in text:
            return "yes"
        return "no" if "not logged in" in text else "unknown"
    if provider == "gemini":
        creds = Path.home() / ".gemini" / "oauth_creds.json"
        return "yes" if creds.exists() else "unknown"
    if provider == "claude":
        creds = Path.home() / ".claude" / ".credentials.json"
        return "yes" if creds.exists() else "unknown"
    return "unknown"


def run_checks(project: Path | None) -> list[Check]:
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(Check(OK if py_ok else WARN, "python", f"{platform.python_version()} ({sys.executable})", True))
    plat = f"{platform.system()} {platform.release()}"
    if IS_WINDOWS:
        plat += " — Windows nativo (soporte añadido por decisión del usuario, ver DECISIONS.md)"
    checks.append(Check(INFO, "plataforma", plat))
    git = _binary("git", required=True)
    checks.append(git)
    if git.level == OK:
        wt = subprocess.run(["git", "worktree", "-h"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        has_wt = "git worktree add" in (wt.stdout + wt.stderr)
        checks.append(Check(OK if has_wt else WARN, "git worktree", "disponible" if has_wt else "no disponible", True))
    bash = find_bash()
    checks.append(Check(OK if bash else WARN, "bash (verify)", bash or "no encontrado: los `verify` no podrán ejecutarse", True))
    checks.append(_binary("claude", purpose="orquestador y trabajador Claude"))
    checks.append(_binary("codex", purpose="trabajador Codex"))
    gem, agy = _binary("gemini"), _binary("agy")
    checks += [gem, agy]
    if gem.level != OK and agy.level != OK:
        checks.append(Check(WARN, "gemini/agy", "ninguna CLI de Google instalada"))
    for prov in ("claude", "codex", "gemini"):
        checks.append(Check(INFO, f"auth {prov}", auth_status(prov)))
    checks.append(_binary("node", purpose="opcional: skills externas"))
    checks.append(_binary("npx", purpose="opcional: `hefesto skill find`"))
    checks.append(_binary("shellcheck", purpose="opcional: lint de install.sh"))
    checks.append(_binary("graphify", purpose="opcional: pack context"))
    keys = present_api_keys()
    if keys:
        checks.append(Check(WARN, "API keys", "en el entorno: " + ", ".join(keys)
                            + ". Las CLIs podrían facturar por API; Hefesto las elimina del proceso hijo."))
    else:
        checks.append(Check(OK, "API keys", "ninguna en el entorno (uso por suscripción)"))
    if project is not None:
        checks += project_checks(project)
    return checks


def project_checks(project: Path) -> list[Check]:
    checks: list[Check] = [Check(INFO, "proyecto", str(project))]
    try:
        cfg = config.load(project)
        checks.append(Check(OK, "config", "válida (" + ", ".join(layer.name for layer in cfg.layers) + ")"))
        approved = cfg.approved_providers()
        checks.append(Check(INFO, "allowlist", ", ".join(approved)))
        from hefesto import __version__, distribution

        if distribution.needs_migration(cfg.settings):
            version = cfg.settings.get("hefesto_version")
            detail = (
                f"proyecto creado con {version}; Hefesto es {__version__}: puede requerir `hefesto migrate`"
                if version
                else f"proyecto sin versión registrada; Hefesto es {__version__}: puede requerir `hefesto migrate`"
            )
            checks.append(Check(WARN, "versión", detail))
    except HefestoError as exc:
        checks.append(Check(WARN, "config", str(exc), True))
    try:
        data = state.load(project)
        checks.append(Check(OK, "estado", f"{data['status']} tarea={data['current_task_id'] or '-'} intentos={data['attempts']}"))
    except HefestoError as exc:
        checks.append(Check(WARN, "estado", str(exc), True))
    lock_dir = project / ".hefesto" / ".lock"
    if lock_dir.exists():
        owner = lock_dir / "owner"
        try:
            info = json.loads(owner.read_text(encoding="utf-8"))
            detail = f"tomado por PID {info.get('pid')} en {info.get('host')} desde {info.get('created')}"
        except (OSError, json.JSONDecodeError):
            detail = "existe sin fichero owner válido"
        checks.append(Check(WARN, "lock", detail))
    else:
        checks.append(Check(OK, "lock", "libre"))
    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    return "\n".join(f"[{c.level}] {c.name.ljust(width)}  {c.detail}" for c in checks)


def exit_code(checks: list[Check]) -> int:
    return EXIT_UNAVAILABLE if any(c.required and c.level == WARN for c in checks) else EXIT_OK
