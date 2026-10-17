"""Piloto automático con el planificador del sistema (`schtasks` en Windows, `crontab` en POSIX)."""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

from hefesto import config, roadmap, router, state, trust, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError


def _record_path(project: Path) -> Path:
    return project / ".hefesto" / "logs" / "autopilot.json"


def _windows_script_path(project: Path) -> Path:
    return project / ".hefesto" / "logs" / "autopilot-loop.cmd"


def _cmd_line(argv: list[str]) -> str:
    return " ".join(f'"{arg.replace("%", "%%")}"' for arg in argv)


def _write_windows_script(project: Path) -> Path:
    script = _windows_script_path(project)
    loop_line = _cmd_line(_hefesto_argv(project, "loop"))
    cancel_line = _cmd_line(_hefesto_argv(project, "autopilot", "cancel"))
    content = f"@echo off\n{loop_line}\n{cancel_line}\n"
    util.atomic_write_text(script, content)
    return script


def task_name(project: Path) -> str:
    try:
        resolved = str(project.resolve())
    except OSError:
        resolved = str(project)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"hefesto-autopilot-{digest}"


def _hefesto_argv(project: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "hefesto", "-C", str(project), *args]


def _windows_date(local: dt.datetime) -> str:
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            buf = ctypes.create_unicode_buffer(64)
            # LOCALE_USER_DEFAULT = 0x0400, LOCALE_SSHORTDATE = 0x0000001F
            if windll.kernel32.GetLocaleInfoW(0x0400, 0x0000001F, buf, len(buf)):
                pattern = buf.value
                sep = "/"
                for ch in pattern:
                    if ch in ("/", "-", "."):
                        sep = ch
                        break
                tokens = [t.strip().lower() for t in re.split(r"[/\-.]", pattern) if t.strip()]
                parts: list[str] = []
                for tok in tokens:
                    if tok.startswith("y"):
                        parts.append(f"{local.year:04d}")
                    elif tok.startswith("m"):
                        parts.append(f"{local.month:02d}")
                    elif tok.startswith("d"):
                        parts.append(f"{local.day:02d}")
                if len(parts) == 3:
                    return sep.join(parts)
    except Exception:
        pass
    return local.strftime("%m/%d/%Y")


def windows_command(project: Path, when: dt.datetime) -> list[str]:
    if '"' in str(project) or '"' in sys.executable:
        raise HefestoError(
            "La ruta del proyecto o del ejecutable no puede contener comillas dobles.",
            EXIT_INPUT,
        )
    script = _windows_script_path(project)
    tr = f'"{script}"'
    if len(tr) > 261:
        raise HefestoError(
            f"El comando /TR de schtasks supera el límite de 261 caracteres ({len(tr)}).",
            EXIT_INPUT,
        )
    name = task_name(project)
    local = when.astimezone()
    return [
        "schtasks",
        "/Create",
        "/TN",
        name,
        "/TR",
        tr,
        "/SC",
        "ONCE",
        "/ST",
        local.strftime("%H:%M"),
        "/SD",
        _windows_date(local),
        "/F",
    ]


def crontab_line(project: Path, when: dt.datetime) -> str:
    local = when.astimezone()
    name = task_name(project)
    loop_cmd = " ".join(shlex.quote(arg) for arg in _hefesto_argv(project, "loop")).replace("%", r"\%")
    cancel_cmd = " ".join(shlex.quote(arg) for arg in _hefesto_argv(project, "autopilot", "cancel")).replace(
        "%", r"\%"
    )
    return (
        f"{local.minute} {local.hour} {local.day} {local.month} * "
        f"{loop_cmd} ; {cancel_cmd} # hefesto-autopilot {name}"
    )


def schedule(project: Path, now: dt.datetime | None = None) -> str:
    current_now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=dt.timezone.utc)
    st = state.load(project)
    if st.get("status") == "BLOCKED_NEED_ARCHITECT_REVIEW":
        raise HefestoError(
            "El proyecto está bloqueado (BLOCKED_NEED_ARCHITECT_REVIEW); no se programa autopilot.",
            EXIT_BLOCKED,
        )
    rm = roadmap.load_valid(project)
    if any(t.marker == "!" for t in rm.tasks):
        raise HefestoError(
            "Hay tareas bloqueadas en el ROADMAP; no se programa autopilot.",
            EXIT_BLOCKED,
        )
    next_t, _reason = roadmap.next_task(rm)
    if next_t is None and st.get("current_task_id"):
        next_t = rm.by_id().get(str(st["current_task_id"]))
    if next_t is not None and trust.status(project, next_t) != "trusted":
        raise HefestoError(
            f"El verify de {next_t.id} no está confiado; ejecuta `hefesto trust` antes de programar autopilot.",
            EXIT_BLOCKED,
        )
    cfg = config.load(project)
    role = next_t.role if next_t is not None else str(st.get("active_role") or "runner")
    cooldown_iso = router.earliest_cooldown(cfg, role)
    if not cooldown_iso:
        return "Sin cooldown activo: nada que programar."

    when = util.parse_iso(cooldown_iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    min_when = current_now + dt.timedelta(minutes=2)
    if when < min_when:
        when = min_when

    local_hhmm = when.astimezone().strftime("%H:%M")
    system = platform.system()
    name = task_name(project)

    if system == "Windows":
        argv = windows_command(project, when)
        script_path = _write_windows_script(project)
        try:
            proc = subprocess.run(argv, shell=False, check=False, capture_output=True, text=True)
        except Exception:
            script_path.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            script_path.unlink(missing_ok=True)
            err = (proc.stderr or proc.stdout or "").strip() or f"schtasks salió con {proc.returncode}"
            raise HefestoError(f"No se pudo programar en schtasks: {err}", EXIT_INPUT)
    else:
        marker = f"# hefesto-autopilot {name}"
        current = subprocess.run(
            ["crontab", "-l"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        existing = current.stdout if current.returncode == 0 and current.stdout else ""
        kept = [ln for ln in existing.splitlines() if marker not in ln]
        kept.append(crontab_line(project, when))
        new_crontab = "\n".join(kept) + "\n"
        proc = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            text=True,
            shell=False,
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"crontab salió con {proc.returncode}"
            raise HefestoError(f"No se pudo programar en crontab: {err}", EXIT_INPUT)

    util.atomic_write_json(
        _record_path(project),
        {
            "at": when.isoformat(),
            "local": local_hhmm,
            "system": system,
            "task_name": name,
        },
    )
    return f"Autopilot programado: `hefesto loop` a las {local_hhmm} ({name})."


def status(project: Path) -> str:
    rec = util.read_json(_record_path(project), default=None)
    if not isinstance(rec, dict) or not rec.get("local"):
        return "Autopilot: sin programación."
    name = rec.get("task_name") or task_name(project)
    system = rec.get("system") or platform.system()
    return f"Autopilot programado: `hefesto loop` a las {rec['local']} ({system}, {name})."


def cancel(project: Path) -> str:
    rec_path = _record_path(project)
    rec = util.read_json(rec_path, default=None)
    had_record = isinstance(rec, dict) and bool(rec.get("local"))
    name = (rec.get("task_name") if isinstance(rec, dict) else None) or task_name(project)
    system = platform.system()

    if system == "Windows":
        subprocess.run(
            ["schtasks", "/Delete", "/TN", str(name), "/F"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        marker = f"# hefesto-autopilot {name}"
        current = subprocess.run(
            ["crontab", "-l"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        if current.returncode == 0 and current.stdout is not None:
            kept = [ln for ln in current.stdout.splitlines() if marker not in ln]
            new_crontab = "\n".join(kept) + ("\n" if kept else "")
            subprocess.run(
                ["crontab", "-"],
                input=new_crontab,
                text=True,
                shell=False,
                check=False,
                capture_output=True,
            )

    rec_path.unlink(missing_ok=True)
    _windows_script_path(project).unlink(missing_ok=True)

    if not had_record:
        return "Autopilot: sin programación."
    return f"Autopilot cancelado ({name})."
