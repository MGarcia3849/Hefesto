"""Arranque del gateway en la sesión de escritorio del usuario de Windows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Callable

from hefesto import telegram, util

TASK_NAME = "Hefesto Telegram"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
AGENT_ENV = ("HEFESTO_ROLE", "CLAUDECODE", "GEMINI_CLI", "CODEX_THREAD_ID")


def _safe_path(value: str | Path) -> str:
    text = str(value)
    if not text or any(c in text for c in '\"%!?\r\n\x00') or any(ord(c) < 32 for c in text):
        raise util.HefestoError("Autostart: ruta no segura (comillas, expansión o controles).")
    return text


def task_xml(user: str, script: Path, directory: Path, wscript: Path) -> bytes:
    """XML UTF-16 sin credenciales; wscript espera y propaga el fallo del hijo."""
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}Task", version="1.2")

    def node(parent: ET.Element, name: str, text: str | None = None, **attrs: str) -> ET.Element:
        element = ET.SubElement(parent, f"{{{NS}}}{name}", attrs)
        element.text = text
        return element

    trigger = node(node(root, "Triggers"), "LogonTrigger")
    node(trigger, "Enabled", "true")
    node(trigger, "UserId", user)
    principal = node(node(root, "Principals"), "Principal", id="Author")
    node(principal, "UserId", user)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(root, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S")):
        node(settings, key, value)
    restart = node(settings, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "999")
    action = node(node(root, "Actions", Context="Author"), "Exec")
    node(action, "Command", _safe_path(wscript))
    node(action, "Arguments", f'//B //Nologo "{_safe_path(script)}"')
    node(action, "WorkingDirectory", _safe_path(directory))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def run_logged(command: str, directory: str, log: str) -> int:
    """Filtra stdout y stderr antes de escribir: nunca redirige salida cruda al log."""
    try:
        with Path(log).open("a", encoding="utf-8") as stream:
            with subprocess.Popen([command, "telegram", "run"], cwd=directory, shell=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace",
                                  env=dict(os.environ, PYTHONUTF8="1")) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    stream.write(telegram._safe_text(line))
                    stream.flush()
                return process.wait()
    except OSError:
        # No volcar excepciones que puedan contener datos del entorno.
        return util.EXIT_INPUT


def autostart(action: str, *, windows: bool | None = None,
              run: Callable = subprocess.run, directory: Path | None = None) -> tuple[int, str]:
    """Gestiona la tarea; plataforma y subprocess sustituibles en pruebas offline."""
    if action not in ("enable", "disable", "status", "start"):
        raise util.HefestoError("Acción autostart desconocida.")
    if not (util.IS_WINDOWS if windows is None else windows):
        return util.EXIT_UNAVAILABLE, "Autostart solo está implementado en Windows; usa hefesto telegram run."
    home = util.state_home().resolve()
    log = home / "telegram-gateway.log"
    if action == "enable":
        util.require_tty("hefesto telegram autostart enable")
        if any(key in os.environ for key in AGENT_ENV):
            raise util.HefestoError("Autostart enable exige una terminal humana sin agente.", util.EXIT_BLOCKED)
        ok, message = telegram.check()
        if not ok:
            return util.EXIT_INPUT, telegram._safe_text(message)
        username = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        if not username or not domain:
            raise util.HefestoError("No se puede identificar el usuario Windows actual.")
        launcher = shutil.which("hefesto") or str(util.FRAMEWORK_ROOT / "bin" / "hefesto.cmd")
        if not Path(launcher).is_file():
            raise util.HefestoError("No se encuentra el lanzador de Hefesto.")
        # Windows puede interpretar un .cmd incluso con shell=False.
        if any(c in launcher for c in "&|<>^()"):
            raise util.HefestoError("Autostart: el lanzador contiene metacaracteres de cmd.")
        directory = (directory or util.FRAMEWORK_ROOT).resolve()
        script = home / "telegram-autostart.vbs"
        worker = home / "telegram-autostart.py"
        xml = home / "telegram-autostart.xml"
        wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
        for path in (home, launcher, directory, script, worker, xml, log, wscript, sys.executable, util.FRAMEWORK_ROOT):
            _safe_path(path)
        worker_text = ("import sys\n" + f"sys.path.insert(0, {json.dumps(str(util.FRAMEWORK_ROOT))})\n"
                       "from hefesto.telegram_service import run_logged\n"
                       f"sys.exit(run_logged({json.dumps(launcher)}, {json.dumps(str(directory))}, "
                       f"{json.dumps(str(log))}))\n")
        command = f'"{sys.executable}" "{worker}"'
        vbs = ('Set sh = CreateObject("WScript.Shell")\n'
               f'WScript.Quit sh.Run("{command.replace(chr(34), chr(34) * 2)}", 0, True)\n')
        home.mkdir(parents=True, exist_ok=True)
        worker.write_text(worker_text, encoding="utf-8")
        script.write_text(vbs, encoding="utf-16")
        xml.write_bytes(task_xml(f"{domain}\\{username}", script, directory, wscript))
        args = ["/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"]
    elif action == "disable":
        args = ["/Delete", "/TN", TASK_NAME, "/F"]
    elif action == "start":
        args = ["/Run", "/TN", TASK_NAME]
    else:
        args = ["/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"]
    try:
        result = run(["schtasks", *args], shell=False, capture_output=True, text=True,
                     errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return util.EXIT_UNAVAILABLE, "No se pudo ejecutar schtasks; comprueba el Programador de tareas."
    output = telegram._safe_text((result.stdout or "") + (result.stderr or ""))
    if action == "status":
        output = ("Tarea registrada (estado y última ejecución):\n" if result.returncode == 0 else
                  "No se pudo consultar la tarea; puede no estar registrada:\n") + output
        try:
            with log.open(encoding="utf-8", errors="replace") as stream:
                tail = "".join(deque(stream, maxlen=20))
            output += "\nÚltimas líneas del log:\n" + telegram._safe_text(tail)
        except OSError:
            output += "\nSin log disponible."
    elif result.returncode == 0:
        output += {"enable": f"\nAutostart registrado. Log: {log}",
                   "disable": "\nAutostart eliminado; una instancia activa puede seguir hasta telegram stop.",
                   "start": "\nArranque solicitado; comprueba autostart status."}[action]
    return (util.EXIT_OK if result.returncode == 0 else util.EXIT_INPUT), telegram._safe_text(output)
