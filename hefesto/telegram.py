"""Gateway saliente de Telegram con identidad numérica y operaciones aprobables."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
import tomllib
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hefesto import guard, util, workspaces, telegram_ops, telegram_intent, hermes_bridge
from hefesto.util import EXIT_OK, HefestoError

TOKEN_ENV = "HEFESTO_TELEGRAM_TOKEN"
API_HOST = "api.telegram.org"
MAX_MESSAGE = 4096
UNAUTHORIZED = "No autorizado."
READ_ONLY_REPLY = ("Gateway en modo solo lectura. Usa /help para ver los comandos disponibles. "
                   "Indica qué proyecto quieres con «abre el proyecto NOMBRE».")
RATE_LIMITED = "Límite de frecuencia alcanzado. Espera un minuto."
FORWARDED_REPLY = "Los mensajes reenviados se tratan como datos, no como comandos."
VOICE_UNSUPPORTED = ("Las notas de voz no están disponibles hasta integrar una transcripción revisada. "
                     "Escribe el texto, por favor.")
HELP_REPLY = ("Hefesto: /start /help /status /projects /project ID /bind ID /unbind /open /close "
              "/clone URL /open [studio|vscode] /run /trust /finish /push /deploy "
              "/approve ID [PIN] /reject ID /diff /tasks /stop /open_file NOMBRE. "
              "El texto libre propone un plan que requiere aprobación.")
_BOT_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_COMMAND = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_]{0,31})(?:@[a-zA-Z0-9_]+)?(?:\s|$)")
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Settings:
    allowed_users: frozenset[int] = frozenset()
    allowed_chats: frozenset[int] = frozenset()
    rate_per_minute: int = 20


def settings_path() -> Path:
    return util.hefesto_home() / "telegram.toml"


def load_settings() -> Settings:
    try:
        with settings_path().open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError):
        raise HefestoError("No se puede leer la configuración de Telegram.") from None

    def ids(key: str) -> frozenset[int]:
        values = data.get(key, [])
        if not isinstance(values, list) or any(type(value) is not int for value in values):
            raise HefestoError(f"Telegram: {key} debe ser una lista de IDs numéricos.")
        return frozenset(values)

    rate = data.get("rate_per_minute", 20)
    if type(rate) is not int or rate < 1:
        raise HefestoError("Telegram: rate_per_minute debe ser un entero positivo.")
    return Settings(ids("allowed_users"), ids("allowed_chats"), rate)


def _safe_text(text: str) -> str:
    # El valor exacto se elimina antes de redactar: otro patrón podría partirlo.
    token = os.environ.get(TOKEN_ENV, "")
    if token:
        text = text.replace(token, "[REDACTED]")
    return guard.redact(_BOT_TOKEN.sub("[REDACTED]", text))


def check() -> tuple[bool, str]:
    try:
        settings = load_settings()
    except HefestoError as exc:
        return False, _safe_text(str(exc))
    if not os.environ.get(TOKEN_ENV, "").strip():
        return False, f"Falta {TOKEN_ENV}."
    if not settings.allowed_users:
        return False, "Telegram: configura allowed_users en telegram.toml (allowlist vacía)."
    return True, "Telegram: token presente y allowlist configurada; las operaciones requieren aprobación."


def https_transport(token: str) -> Transport:
    if _BOT_TOKEN.fullmatch(token) is None:
        raise HefestoError("Token de Telegram inválido.")

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in ("getUpdates", "sendMessage"):
            raise HefestoError("Método de Telegram no permitido.")
        try:
            request = urllib.request.Request(
                f"https://{API_HOST}/bot{token}/{method}",
                data=json.dumps(params).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.loads(response.read())
        except (OSError, ValueError):
            # Los errores de urllib pueden incluir la URL completa y el token.
            raise HefestoError("Error de comunicación con Telegram.") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HefestoError("Telegram rechazó la solicitud.")
        return result

    return call


def split_message(text: str) -> list[str]:
    return [text[index:index + MAX_MESSAGE] for index in range(0, len(text), MAX_MESSAGE)]


def _office_files(files: list[Path]) -> str:
    root = hermes_bridge.office_dir()
    lines = []
    for file in files:
        path = Path(file)
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            lines.append('Oficina: ' + str(path.relative_to(root)))
            if path.parent == root and path.suffix.lower() in ('.docx', '.xlsx', '.pptx', '.pdf'):
                lines.append('/open_file ' + path.name)
    return '\n'.join(lines)


def open_office_file(name: str) -> str:
    # Solo documentos: nunca scripts, ejecutables, enlaces, rutas o streams NTFS.
    if (not name or name in ('.', '..') or any(c in name for c in '/\\:<>|?*"')
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or name.endswith((' ', '.'))):
        raise HefestoError('Usa solo un nombre de fichero dentro de Oficina.')
    root = hermes_bridge.office_dir()
    target = root / name
    if (target.suffix.lower() not in ('.docx', '.xlsx', '.pptx', '.pdf') or target.is_symlink()
            or not target.is_file() or target.resolve().parent != root):
        raise HefestoError('Documento de Oficina no permitido o inexistente.')
    if not workspaces.has_desktop():
        return 'No hay escritorio disponible para abrir el documento de Oficina.'
    if os.name == 'nt':
        os.startfile(str(target))
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(target)], shell=False)
    return 'Abierto: ' + name


class Gateway:
    def __init__(self, settings: Settings, transport: Transport,
                 status: Callable[[], str] | None = None, projects: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic, audit_path: Path | None = None,
                 ops: telegram_ops.Store | None = None, executor: Callable[[dict], dict] | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.status = status or (lambda: "Estado no disponible.")
        self.projects = projects or self._list_projects
        self.clock = clock
        self.audit_path = audit_path if audit_path is not None else util.state_home() / "telegram-audit.jsonl"
        self._windows: dict[int, deque[float]] = {}
        self.ops = ops if ops is not None else telegram_ops.Store()
        self.executor = executor if executor is not None else telegram_ops.default_executor

    def _operation_command(self, command: str, argument: str, text: str, actor: int, session: int,
                           thread: int | None, chat_type: str) -> str:
        from hefesto import gitops, loop, roadmap

        project = workspaces.active(session, thread)
        general_room = thread is None and chat_type != 'private'
        if command == 'open_file':
            return open_office_file(argument)
        if general_room and command not in ('texto', 'approve', 'reject'):
            return READ_ONLY_REPLY
        if command == 'clone':
            return self.ops.describe(self.ops.create_clone(argument, actor, session, thread))
        if command == 'texto':
            intent = telegram_intent.parse(text, workspaces.list_projects())
            if intent.kind == 'unknown' and telegram_intent.is_project_request(text):
                intent = telegram_intent.parse(text, workspaces.list_projects(), workspaces.github_repos())
            if intent.kind == 'ambiguous':
                return '¿Qué proyecto quieres? Opciones: ' + ', '.join(intent.options)
            if intent.kind in ('general', 'general_action'):
                return self._general_request(intent, actor, session, thread)
            if general_room:
                return READ_ONLY_REPLY
            if intent.kind in ('clone', 'clone_and_work'):
                return self.ops.describe(self.ops.create_clone(intent.repo_url, actor, session, thread,
                                                               intent.instruction, intent.editor))
            if intent.project_id:
                project = workspaces.resolve(intent.project_id)
                workspaces.bind(session, thread, project.id)
                reply = f'Proyecto seleccionado: {project.id}.\n'
                if intent.kind in ('open', 'open_and_work'):
                    reply += workspaces.open_in_editor(project, intent.editor or 'vscode')[1] + '\n'
                if intent.kind in ('work', 'open_and_work'):
                    reply += self.ops.describe(self.ops.create('plan', project, actor, session, thread,
                                                              intent.instruction))
                return reply.strip()
            # Una referencia explícita desconocida nunca se aplica al proyecto activo por accidente.
            if telegram_intent.is_project_request(text):
                return 'No encuentro ese proyecto. ¿Qué proyecto quieres? Opciones: ' + self._list_projects()
            if project is None:
                return READ_ONLY_REPLY
        if command == "approve":
            parts = argument.split()
            if len(parts) not in (1, 2):
                raise HefestoError("Usa /approve ID [PIN].")
            saved = self.ops.get(parts[0])
            if general_room and saved['action'] != 'assist':
                return READ_ONLY_REPLY
            if saved['action'] not in ('clone', 'assist') and (project is None or saved["project"] != project.id):
                raise HefestoError("La operación pertenece a otro proyecto.")
            approved = self.ops.approve(parts[0], actor, session, thread, parts[1] if len(parts) == 2 else None)
            done = self.ops.execute(approved, self.executor)
            result = done["result"]
            if done['action'] == 'assist':
                return (f"{done['id']}: {'completada' if result['ok'] else 'falló'}\n" + result['summary']
                        + '\n' + _office_files([Path(p) for p in result['files']]))
            return (f"{done['id']}: {'completada' if done['status'] == 'done' else 'falló'}\n"
                    f"Rama: {result['branch']}\nFicheros: {', '.join(result['files'])[:1000]}\n"
                    f"Diff: {result['diff'][:1000]}\nPruebas: {result['tests'][-1200:]}\n"
                    f"{result['summary'][-1200:]}")
        if command == "reject":
            saved = self.ops.get(argument)
            if ((saved['chat'], saved['thread']) != (session, thread) or
                    (saved['action'] not in ('clone', 'assist') and (project is None or saved['project'] != project.id))):
                raise HefestoError("La operación pertenece a otro chat o proyecto.")
            return f"{self.ops.reject(argument, actor)['id']}: rechazada"
        if project is None:
            return READ_ONLY_REPLY
        if command == "diff":
            return gitops.out(project.path, "diff", "--stat")[:3500] or "Sin diferencias."
        if command == "tasks":
            return (workspaces.project_status(project) + "\n" + "\n".join(
                f"[{t.marker}] {t.id}: {t.title}" for t in roadmap.load_valid(project.path).tasks))[:3900]
        if command == "stop":
            if "write" not in project.capabilities:
                raise HefestoError("Proyecto sin permiso de escritura.")
            workspaces.inside(project, ".hefesto/STOP")
            loop.request_stop(project.path)
            return "STOP creado. El loop se detendrá antes de la siguiente iteración."
        action = "plan" if command == "texto" else command
        instruction = text if command == "texto" else argument
        return self.ops.describe(self.ops.create(action, project, actor, session, thread, instruction))

    def _general_request(self, intent: telegram_intent.Intent, actor: int, session: int,
                         thread: int | None) -> str:
        instruction = intent.instruction
        if intent.kind == 'general_action':
            instruction = ('Prepara únicamente un borrador o plan concreto, sin ejecutar efectos: no enviar, '
                           'borrar, mover, instalar, publicar, comprar ni ejecutar comandos. Detalla destinatarios, '
                           'contenido y rutas exactas; si faltan datos pide aclaración. Petición:\n' + instruction)
        result = hermes_bridge.ask(instruction, mode='lectura')
        reply = result.text + '\n' + _office_files(result.files)
        if intent.kind == 'general_action' and result.ok:
            approved_instruction = (intent.instruction + '\n\nBorrador o plan exacto aprobado:\n' + result.text
                                    + '\nEjecuta solo este alcance. Si falta información, solicita aclaración '
                                    'sin realizar efectos adicionales.')
            op = self.ops.create_assist(approved_instruction, actor, session, thread)
            reply += '\n' + self.ops.describe(op)
        return reply.strip()

    @staticmethod
    def _list_projects() -> str:
        return "\n".join(f"{p.id} — {p.name}" for p in workspaces.list_projects()) or "Sin proyectos registrados."

    def _project_command(self, command: str, argument: str, session: int,
                         thread: int | None, chat_type: str) -> str:
        if command in ("project", "bind"):
            if command == "project":
                if thread is not None:
                    raise HefestoError("Usa /project ID en privado o en General; /bind ID dentro de un tema.")
                workspaces.select(session, argument)
            else:
                if chat_type not in ("group", "supergroup") or thread is None:
                    raise HefestoError("Usa /bind ID dentro de un tema de un grupo autorizado.")
                workspaces.bind(session, thread, argument)
            return "Proyecto seleccionado."
        if command == 'open' and argument and argument not in workspaces.EDITORS:
            raise HefestoError('Usa /open [studio|vscode].')
        if argument and command != 'open':
            raise HefestoError("Este comando no acepta argumentos.")
        if command == "unbind":
            if chat_type not in ("group", "supergroup") or thread is None:
                raise HefestoError("Usa /unbind dentro de un tema de un grupo autorizado.")
            workspaces.unbind(session, thread)
            return "Tema desvinculado."
        if command == "close":
            workspaces.close(session, thread)
            return "Asociación desactivada."
        active = workspaces.active(session, thread)
        if command == "status":
            if active is not None:
                return workspaces.project_status(active) + "\n" + _provider_status(active.path)
            return self.status()
        if active is None:
            return "No hay proyecto activo. Usa /project ID o /bind ID."
        return (workspaces.open_in_vscode(active)[1] if not argument else
                workspaces.open_in_editor(active, argument)[1])

    @staticmethod
    def _voice_message(message: dict[str, Any]) -> bool:
        return any(key in message for key in ("voice", "audio", "video_note"))

    def handle(self, update: dict[str, Any]) -> list[str]:
        message = update.get("message")
        message = message if isinstance(message, dict) else {}
        sender, chat = message.get("from"), message.get("chat")
        sender = sender if isinstance(sender, dict) else {}
        chat = chat if isinstance(chat, dict) else {}
        actor, session = sender.get("id"), chat.get("id")
        actor = actor if type(actor) is int else None
        session = session if type(session) is int else None
        text = message.get("text", "")
        match = _COMMAND.match(text) if isinstance(text, str) else None
        voice_message = self._voice_message(message)
        command = match.group(1) if match else ("voice" if voice_message else "texto")
        argument = text[match.end():].strip() if match else ""
        thread = message.get("message_thread_id")
        thread = thread if type(thread) is int and thread > 0 else None
        authorized = (actor is not None and session is not None and actor in self.settings.allowed_users
                      and (chat.get("type") == "private" or session in self.settings.allowed_chats))
        if not authorized:
            reply, result = UNAUTHORIZED, "denied"
        else:
            now = self.clock()
            window = self._windows.setdefault(actor, deque())
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.settings.rate_per_minute:
                reply, result = RATE_LIMITED, "rate_limited"
            else:
                window.append(now)
                if any(key in message for key in ("forward_origin", "forward_from", "forward_from_chat")):
                    reply, result = FORWARDED_REPLY, "forwarded"
                elif voice_message:
                    reply, result = VOICE_UNSUPPORTED, "voice_unsupported"
                elif command in ("start", "help"):
                    reply, result = HELP_REPLY, "ok"
                elif command in ("status", "projects", "project", "bind", "unbind", "open", "close"):
                    try:
                        reply = (self.projects() if command == "projects" else
                                 self._project_command(command, argument, session, thread, chat.get("type", "")))
                        result = "ok"
                    except HefestoError as exc:
                        reply, result = str(exc), "denied"
                elif command in (*telegram_ops.ACTIONS, "clone", "approve", "reject", "diff", "tasks", "stop", "texto", "open_file"):
                    try:
                        reply = self._operation_command(command, argument, text, actor, session,
                                                        thread, chat.get("type", ""))
                        result = "read_only" if reply == READ_ONLY_REPLY else "ok"
                    except (HefestoError, OSError) as exc:
                        reply, result = str(exc), "denied"
                else:
                    reply, result = READ_ONLY_REPLY, "read_only"
        util.append_jsonl(self.audit_path, {
            "at": util.now_iso(), "session": session, "actor": actor,
            "command": _safe_text(command), "result": result,
        })
        replies = split_message(_safe_text(reply)) if session is not None else []
        for chunk in replies:
            params = {"chat_id": session, "text": chunk}
            if thread is not None:
                params["message_thread_id"] = thread
            self.transport("sendMessage", params)
        return replies


    def poll_once(self, offset: int | None = None) -> int | None:
        response = self.transport("getUpdates", {"timeout": 30, "offset": offset,
                                                  "allowed_updates": ["message"]})
        updates = response.get("result", [])
        if not isinstance(updates, list):
            raise HefestoError("Respuesta de Telegram inválida.")
        next_offset = offset
        for update in updates:
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise HefestoError("Update de Telegram inválido.")
            self.handle(update)
            candidate = update["update_id"] + 1
            next_offset = max(next_offset, candidate) if next_offset is not None else candidate
        return next_offset


def run(gateway: Gateway, *, max_failures: int = 5, stop_path: Path | None = None,
        log_path: Path | None = None, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        interrupted: Callable[[], bool] | None = None) -> int:
    """Ejecuta long polling con parada cooperativa y reintentos acotados."""
    if type(max_failures) is not int or max_failures < 1:
        raise HefestoError("El máximo de fallos de Telegram debe ser positivo.", util.EXIT_INPUT)
    stop_path = stop_path or (util.state_home() / "telegram-STOP")
    log_path = log_path or (util.state_home() / "telegram-gateway.jsonl")
    interrupted = interrupted or (lambda: False)
    token = os.environ.get(TOKEN_ENV, "")
    started = clock()

    def log(event: str, detail: str) -> None:
        util.append_jsonl(log_path, {"at": datetime.now(timezone.utc).isoformat(),
                                     "elapsed": max(0.0, clock() - started),
                                     "event": event, "detalle": _safe_text(detail)})

    recovered = gateway.ops.recover()
    log("recover", f"Operaciones interrumpidas: {len(recovered)}")
    print("Telegram: operaciones interrumpidas: " + (", ".join(recovered) if recovered else "ninguna"))
    offset: int | None = None
    failures = 0
    try:
        while True:
            if interrupted() or stop_path.exists():
                log("stopped", "Parada solicitada")
                print("Telegram: detenido.")
                return EXIT_OK
            try:
                offset = gateway.poll_once(offset)
                failures = 0
            except KeyboardInterrupt:
                log("stopped", "Interrumpido por teclado")
                print("Telegram: detenido.")
                return EXIT_OK
            except (HefestoError, OSError, ValueError) as exc:
                failures += 1
                detail = str(exc).replace(token, "[REDACTED]") if token else str(exc)
                log("network_error", detail)
                if failures >= max_failures:
                    log("failed", f"Máximo de fallos consecutivos alcanzado: {failures}")
                    print("Telegram: se alcanzó el máximo de fallos de red.", file=sys.stderr)
                    return 1
                delay = min(2 ** (failures - 1), 60)
                log("retry", f"Reintento en {delay} s")
                try:
                    sleep(delay)
                except KeyboardInterrupt:
                    log("stopped", "Interrumpido por teclado")
                    print("Telegram: detenido.")
                    return EXIT_OK
    finally:
        try:
            stop_path.unlink(missing_ok=True)
        except OSError:
            pass

def _provider_status(project: Path) -> str:
    """Describe role provider chains without exposing adapter configuration or credentials."""
    from hefesto import config

    cfg = config.load(project)
    lines = ["Proveedores por rol:"]
    for role in config.ROLES:
        providers = cfg.role_chain(role)
        lines.append(f"  {role}: {', '.join(providers) if providers else 'sin configurar'}")
    lines.append("ChatGPT: la suscripción se usa solo mediante la CLI oficial de Codex con su propio "
                 "'codex login'; no se reutilizan tokens ni sesiones de otras aplicaciones.")
    lines.append("Local: Ollama o compatible es una alternativa configurable con 'hefesto local enable'.")
    return "\n".join(lines)

