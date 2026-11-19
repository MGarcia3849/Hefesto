"""Hermes como motor subordinado: salida tratada como datos y perfiles sin secretos.

El perfil de lectura reduce el riesgo; no es un aislamiento del sistema.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, guard, project, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError

MODES = ("lectura", "accion")
PROFILES = {"lectura": "hefesto-lectura", "accion": "hefesto-accion"}
INSTALL_HINT = (
    "Instala Hermes Agent con el script oficial del repositorio NousResearch/hermes-agent. "
    "Después inicia sesión con tu suscripción de ChatGPT: hermes auth add openai-codex."
)


@dataclass
class Result:
    ok: bool
    text: str
    files: list[Path]
    exit_code: int


def _config() -> config.Config:
    try:
        root = project.find_project()
    except HefestoError:
        root = None
    return config.load(root)


def find_hermes() -> str | None:
    binary = shutil.which("hermes")
    if binary:
        return binary
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        folder = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "bin"
        for name in ("hermes.exe", "hermes.cmd", "hermes"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return None


def office_dir() -> Path:
    configured = _config().get("hermes", "office_dir", default="")
    folder = Path(configured).expanduser() if configured else Path.home() / "Documents" / "Hefesto Oficina"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HefestoError("No se pudo crear la carpeta Oficina.", EXIT_INPUT) from exc
    return folder.resolve()


def profile_settings(mode: str) -> dict:
    if mode not in MODES:
        raise HefestoError("Modo de Hermes desconocido.", EXIT_INPUT)
    irreversible = [
        "git push*", "rm *", "del *", "Remove-Item*", "rmdir*", "format*", "mkfs*", "diskpart*",
    ]
    reading = ["*send*", "himalaya message send*", "*smtp*", "pip install*", "npm install*",
               "winget*", "choco*", "*install*"]
    return {"approvals": {"single_query_mode": "deny" if mode == "lectura" else "approve",
                          "deny": irreversible + (reading if mode == "lectura" else [])}}


def _profiles_dir() -> Path:
    if os.environ.get("HERMES_HOME"):
        home = Path(os.environ["HERMES_HOME"]).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        home = Path(os.environ["LOCALAPPDATA"]) / "hermes"
    else:
        home = Path.home() / ".hermes"
    return home / "profiles"


def _hermes_cli(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run([binary, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, shell=False, env=_child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HefestoError("No se pudo ejecutar Hermes para configurar el perfil.", EXIT_UNAVAILABLE) from exc


def apply_profile(name: str, settings: dict) -> None:
    """Crea o actualiza el perfil con la CLI oficial de Hermes (verificado con Hermes v0.23.2).

    `hermes profile create NOMBRE --clone --no-alias` copia la configuración y las skills del perfil activo (sin
    tokens de bots ni allowlists de mensajería) y comparte los inicios de sesión del perfil `default`; después
    `hermes -p NOMBRE config set` fija solo los ajustes de aprobación de este puente. No se escriben secretos.
    """
    mode = next((mode for mode, profile in PROFILES.items() if profile == name), None)
    if mode is None or settings != profile_settings(mode):
        raise HefestoError("Perfil de Hermes o ajustes no admitidos.", EXIT_INPUT)
    binary = find_hermes()
    if not binary:
        raise HefestoError(INSTALL_HINT, EXIT_UNAVAILABLE)
    listed = _hermes_cli(binary, "profile", "list")
    if re.search(rf"(?m)^\W*{re.escape(name)}\b", listed.stdout or "") is None:
        created = _hermes_cli(binary, "profile", "create", name, "--clone", "--no-alias",
                              "--description", f"Perfil de Hefesto ({mode})")
        if created.returncode != 0:
            raise HefestoError(f"Hermes no pudo crear el perfil {name}.", EXIT_UNAVAILABLE)
    approvals = settings["approvals"]
    for key, value in (("approvals.single_query_mode", approvals["single_query_mode"]),
                       ("approvals.deny", json.dumps(approvals["deny"], ensure_ascii=False))):
        result = _hermes_cli(binary, "-p", name, "config", "set", key, value)
        if result.returncode != 0:
            raise HefestoError(f"Hermes no pudo fijar {key} en {name}.", EXIT_UNAVAILABLE)


def setup() -> list[str]:
    util.require_tty("hefesto hermes setup")
    for mode, name in PROFILES.items():
        apply_profile(name, profile_settings(mode))
    return list(PROFILES.values())


def _child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key.upper() not in {"HEFESTO_ROLE", "HEFESTO_TASK_ID"}}


def _snapshot(folder: Path) -> dict[Path, tuple[int, int]]:
    files: dict[Path, tuple[int, int]] = {}
    for path in folder.rglob("*"):
        try:
            if not path.is_symlink() and path.is_file():
                info = path.stat()
                files[path] = (info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            continue
    return files


def _authorize(instruction: str, approved_operation: str | None) -> None:
    from hefesto import telegram_ops

    operation = None
    if approved_operation:
        try:
            operation = telegram_ops.Store().get(approved_operation)
        except HefestoError:
            pass
    if (not isinstance(operation, dict) or operation.get("status") != "running"
            or operation.get("action") != "assist" or operation.get("instruction") != instruction):
        raise HefestoError("Hermes accion exige una operación assist aprobada y en curso para esta instrucción.",
                           EXIT_BLOCKED)


def ask(instruction: str, mode: str = "lectura", workdir: Path | None = None,
        approved_operation: str | None = None, timeout: float | None = None) -> Result:
    if mode not in MODES:
        raise HefestoError("Modo de Hermes desconocido.", EXIT_INPUT)
    if mode == "accion":
        _authorize(instruction, approved_operation)
    binary = find_hermes()
    if binary is None:
        raise HefestoError(INSTALL_HINT, EXIT_UNAVAILABLE)
    folder = Path(workdir).resolve() if workdir is not None else office_dir()
    limit = timeout if timeout is not None else _config().get("hermes", "timeout_seconds", default=900)
    before = _snapshot(folder)
    try:
        completed = subprocess.run(
            [binary, "-p", PROFILES[mode], "chat", "--oneshot", "--query-file", "-", "--format", "stream-json"],
            input=instruction, cwd=folder, timeout=limit, env=_child_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
        )
    except subprocess.TimeoutExpired:
        after = _snapshot(folder)
        return Result(False, "Hermes agotó el tiempo de espera.",
                      sorted(path for path, info in after.items() if before.get(path) != info), 124)
    except OSError as exc:
        raise HefestoError("No se pudo ejecutar Hermes. " + INSTALL_HINT, EXIT_UNAVAILABLE) from exc
    after = _snapshot(folder)
    files = sorted(path for path, info in after.items() if before.get(path) != info)
    result = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    if result is None:
        return Result(False, guard.redact(completed.stderr or "Hermes no devolvió un evento result."),
                      files, completed.returncode or 1)
    code = result.get("exit_code")
    if type(code) is not int:
        code = 1
    text = result.get("text", "")
    return Result(completed.returncode == 0 and code == 0, guard.redact(text if isinstance(text, str) else ""),
                  files, code or completed.returncode)


def status() -> str:
    binary = find_hermes()
    lines = ["Hermes instalado: " + (binary if binary else "no")]
    if binary:
        try:
            version = subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=10, env=_child_env(), shell=False)
            lines.append(guard.redact(version.stdout.strip()) if version.returncode == 0 else "Versión no disponible.")
        except (OSError, subprocess.TimeoutExpired):
            lines.append("Versión no disponible.")
    else:
        lines.append(INSTALL_HINT)
    for name in PROFILES.values():
        present = (_profiles_dir() / name / "config.yaml").is_file()
        lines.append(f"Perfil {name}: {'presente' if present else 'pendiente de setup'}")
    lines.append(f"Oficina: {office_dir()}")
    return "\n".join(lines)
