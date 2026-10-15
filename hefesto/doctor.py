"""`hefesto doctor`: dependencias, adaptadores, allowlist, lock y estado. No gasta cuota."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, state, util
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


def _node_check() -> Check:
    path = which("node")
    if not path:
        return Check(MISSING, "node", "no encontrado — opcional: skills externas")
    ver = _version(path)
    match = re.search(r"v?(\d+)\.(\d+)", ver)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        if (major, minor) < (22, 20):
            return Check(WARN, "node", f"{ver}  ({path}) — anterior a 22.20 (se recomienda Node >= 22.20)")
    return Check(OK, "node", f"{ver}  ({path})")


def _npx_check() -> Check:
    path = util.which("npx")
    if not path:
        return Check(MISSING, "npx", "no encontrado — opcional: `hefesto skill find`")
    return Check(OK, "npx", path)


def _graphify_version() -> str:
    try:
        from hefesto import packs

        catalog = packs.load_catalog()
        for entry in catalog.values():
            if entry.get("binary") == "graphify" and "version" in entry:
                return str(entry["version"])
        if "context" in catalog and "version" in catalog["context"]:
            return str(catalog["context"]["version"])
    except Exception:
        pass
    return "0.9.13"


def _graphify_check() -> Check:
    path = util.which("graphify")
    if not path:
        return Check(MISSING, "graphify", "no encontrado — opcional: pack context")
    ver = _graphify_version()
    return Check(OK, "graphify", f"{path} (versión fijada {ver})")



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


def cpu_count() -> int | None:
    try:
        return os.cpu_count()
    except Exception:
        return None


def available_ram() -> int | None:
    try:
        if sys.platform == "win32" or platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if hasattr(ctypes, "windll") and ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys)
        elif sys.platform.startswith("linux") or platform.system() == "Linux":
            meminfo = Path("/proc/meminfo")
            if meminfo.is_file():
                for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        return int(parts[1]) * 1024
        elif sys.platform == "darwin" or platform.system() == "Darwin":
            page_size = 4096
            vm_proc = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            if vm_proc.returncode == 0:
                free_pages = 0
                inactive_pages = 0
                found = False
                for line in vm_proc.stdout.splitlines():
                    if "page size of" in line:
                        m = re.search(r"page size of (\d+) bytes", line)
                        if m:
                            page_size = int(m.group(1))
                    elif line.startswith("Pages free:"):
                        m = re.search(r":\s*(\d+)", line)
                        if m:
                            free_pages = int(m.group(1))
                            found = True
                    elif line.startswith("Pages inactive:"):
                        m = re.search(r":\s*(\d+)", line)
                        if m:
                            inactive_pages = int(m.group(1))
                            found = True
                if found:
                    return (free_pages + inactive_pages) * page_size
    except Exception:
        pass
    return None


def check_resources(cfg: config.Config | None = None) -> Check:
    cpus = cpu_count()
    ram_bytes = available_ram()

    cpu_part = f"{cpus} CPU" if cpus else "CPU desconocida"
    ram_part = f"{ram_bytes / (1024**3):.1f} GB RAM disponible" if ram_bytes else "RAM disponible desconocida"

    raw_val = cfg.get("limits", "max_parallel_tasks", default=4) if cfg else 4
    max_parallel_invalid = False
    try:
        if isinstance(raw_val, bool):
            raise ValueError("booleano no permitido")
        max_parallel = int(raw_val)
        if max_parallel <= 0:
            raise ValueError("debe ser positivo")
    except (ValueError, TypeError):
        max_parallel_invalid = True
        max_parallel = 4

    if max_parallel_invalid:
        detail = (f"{cpu_part}, {ram_part} — valor inválido de limits.max_parallel_tasks ({raw_val!r})")
        return Check(WARN, "recursos", detail)

    rec_cpu = cpus if (cpus and cpus > 0) else None
    rec_ram = int(ram_bytes / (1024**3)) if (ram_bytes and ram_bytes > 0) else None

    recommended: int | None = None
    if rec_cpu is not None and rec_ram is not None:
        recommended = max(1, min(rec_cpu, rec_ram))
    elif rec_cpu is not None:
        recommended = max(1, rec_cpu)
    elif rec_ram is not None:
        recommended = max(1, rec_ram)

    if recommended is None:
        return Check(INFO, "recursos", f"{cpu_part}, {ram_part}")

    if max_parallel > recommended:
        detail = (f"{cpu_part}, {ram_part} — max_parallel_tasks ({max_parallel}) supera lo recomendado "
                  f"({recommended} agentes, 1 CPU y 1 GB por agente)")
        return Check(WARN, "recursos", detail)

    detail = (f"{cpu_part}, {ram_part} (recomendado hasta {recommended} agentes en paralelo; "
              f"max_parallel_tasks={max_parallel})")
    return Check(OK, "recursos", detail)


def run_checks(project: Path | None) -> list[Check]:
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(Check(OK if py_ok else WARN, "python", f"{platform.python_version()} ({sys.executable})", True))
    plat = f"{platform.system()} {platform.release()}"
    if IS_WINDOWS:
        plat += " — Windows nativo (soporte añadido por decisión del usuario, ver DECISIONS.md)"
    checks.append(Check(INFO, "plataforma", plat))
    try:
        cfg = config.load(project, validate_layers=False)
    except Exception:
        cfg = None
    checks.append(check_resources(cfg))
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
    from hefesto import limits

    for prov in ("claude", "codex", "gemini"):
        checks.append(Check(INFO, f"límites {prov}", limits.limits_summary(prov)))
    checks.append(_node_check())
    checks.append(_npx_check())
    npm_ver = cfg.get("skills_sources", "npm_version", default="1.7.2") if cfg else "1.7.2"
    checks.append(Check(INFO, "skills (npm)", f"versión fijada {npm_ver}"))
    checks.append(_binary("shellcheck", purpose="opcional: lint de install.sh"))
    checks.append(_graphify_check())
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

    try:
        from hefesto import packs

        listed = packs.list_packs(project)
        active = [p for p in listed if p["enabled"]]
        if active:
            for p in active:
                if p["missing"]:
                    checks.append(Check(WARN, f"pack {p['name']}", f"activo — falta: {', '.join(p['missing'])}"))
                else:
                    checks.append(Check(OK, f"pack {p['name']}", "activo"))
        else:
            checks.append(Check(INFO, "packs", "ninguno activo"))
    except Exception as exc:
        checks.append(Check(WARN, "packs", str(exc)))

    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    return "\n".join(f"[{c.level}] {c.name.ljust(width)}  {c.detail}" for c in checks)


def exit_code(checks: list[Check]) -> int:
    return EXIT_UNAVAILABLE if any(c.required and c.level == WARN for c in checks) else EXIT_OK
