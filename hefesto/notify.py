"""Avisos de escritorio opcionales cuando algo necesita a un humano."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path

from hefesto import config, guard

MAX_LEN = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

_WINDOWS_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; "
    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.BalloonTipTitle = $env:HEFESTO_NOTIFY_TITLE; "
    "$n.BalloonTipText = $env:HEFESTO_NOTIFY_MESSAGE; "
    "$n.Visible = $true; "
    "$n.ShowBalloonTip(5000); "
    "Start-Sleep -Milliseconds 200; "
    "$n.Dispose()"
)


def _clean(text: str, limit: int = MAX_LEN) -> str:
    redacted = guard.redact(str(text or ""))
    single_line = _CONTROL_RE.sub(" ", redacted).strip()
    return single_line[:limit]


def _apple_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send(cfg: config.Config, title: str, message: str) -> bool:
    """Envía un aviso de escritorio si `[notify] enabled = true`. Nunca lanza excepciones."""
    try:
        if not bool(cfg.get("notify", "enabled", default=False)):
            return False
        clean_title = _clean(title)
        clean_message = _clean(message)
        system = platform.system()
        env: dict[str, str] | None = None
        if system == "Windows":
            cmd = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_PS_SCRIPT,
            ]
            env = {
                **os.environ,
                "HEFESTO_NOTIFY_TITLE": clean_title,
                "HEFESTO_NOTIFY_MESSAGE": clean_message,
            }
        elif system == "Darwin":
            script = (
                f'display notification "{_apple_escape(clean_message)}" '
                f'with title "{_apple_escape(clean_title)}"'
            )
            cmd = ["osascript", "-e", script]
        else:
            cmd = ["notify-send", "--", clean_title, clean_message]
        kwargs: dict[str, object] = {
            "shell": False,
            "timeout": 10,
            "capture_output": True,
            "check": False,
        }
        if env is not None:
            kwargs["env"] = env
        proc = subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]
        return proc.returncode == 0
    except Exception:
        return False


def event(project: Path, kind: str, message: str) -> bool:
    """Carga la configuración del proyecto y envía un aviso con título `Hefesto · <kind>`."""
    try:
        cfg = config.load(project)
    except Exception:
        return False
    return send(cfg, f"Hefesto · {kind}", message)
