"""TASK-3501: contrato del gateway de Telegram de solo lectura (`hefesto/telegram.py`).

API que fija este contrato:

- `TOKEN_ENV = "HEFESTO_TELEGRAM_TOKEN"`: el token del bot solo se lee de esa variable de entorno; nunca de un
  fichero del repositorio, nunca aparece en respuestas, logs ni en la salida de `hefesto telegram check`.
- `API_HOST = "api.telegram.org"` y `MAX_MESSAGE = 4096`.
- `settings_path() -> Path`: `util.hefesto_home() / "telegram.toml"`, fuera de cualquier repositorio. Claves:
  `allowed_users` (IDs numéricos de usuario), `allowed_chats` (IDs de chats de grupo) y `rate_per_minute` (por
  defecto 20). `load_settings() -> Settings` con `allowed_users: frozenset[int]`, `allowed_chats: frozenset[int]`
  y `rate_per_minute: int`; si falta el fichero, las listas quedan vacías y se deniega todo.
- `https_transport(token) -> Callable[[str, dict], dict]`: llama a `https://api.telegram.org/bot<token>/<método>`
  con la biblioteca estándar. Solo conexiones salientes (long polling); no abre puertos ni usa webhooks.
- `Gateway(settings, transport, status=None, projects=None, clock=time.monotonic, audit_path=None)`:
  `status()` y `projects()` devuelven texto (en esta fase, de solo lectura). `handle(update) -> list[str]` procesa un
  update de la Bot API, envía las respuestas con `transport("sendMessage", {"chat_id": …, "text": …})` y las
  devuelve. `poll_once(offset=None) -> int | None` pide `getUpdates` con `timeout` y `offset` y devuelve el
  siguiente `offset`.
- Autorización por identidad numérica: el remitente (`from.id`) debe estar en `allowed_users` y, si el chat no es
  privado, el chat (`chat.id`) debe estar en `allowed_chats`. Nunca por nombre de usuario. A quien no está
  autorizado se le responde solo `UNAUTHORIZED = "No autorizado."`, sin información del sistema.
- Comandos de solo lectura: `/start`, `/help`, `/status` y `/projects`. Cualquier otro texto recibe
  `READ_ONLY_REPLY`, que explica que el gateway está en modo solo lectura, y no ejecuta nada.
- `split_message(text) -> list[str]`: trozos de como máximo `MAX_MESSAGE` caracteres. Todas las respuestas pasan
  por `guard.redact` y por un filtro que elimina el token.
- Límite de frecuencia: más de `rate_per_minute` mensajes de un mismo usuario en 60 s reciben `RATE_LIMITED` y no
  se procesan.
- Mensajes reenviados (`forward_origin`, `forward_from` o `forward_from_chat`) se tratan como datos: no se
  ejecutan como comandos y reciben `FORWARDED_REPLY`.
- Auditoría: una línea JSON por update en `audit_path` (por defecto `util.state_home() / "telegram-audit.jsonl"`) con
  `at`, `session` (chat), `actor` (usuario), `command` y `result` (`ok`, `denied`, `rate_limited`, `forwarded`,
  `read_only`), sin el texto del mensaje ni secretos.
- `check() -> tuple[bool, str]` y la CLI `hefesto telegram check`: dicen si hay token y allowlist sin mostrar el
  token; `hefesto telegram run` ejecuta el bucle de long polling.

El gateway es una interfaz más de Hefesto: no decide ni ejecuta trabajo por sí mismo, y en esta fase no escribe en
ningún proyecto.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from unittest import mock

from tests.helpers import HefestoTestCase

TOKEN = "123456:" + "T" * 35  # sintético
OWNER = 111
STRANGER = 999
GROUP = -100500


def tg():
    from hefesto import telegram
    return telegram


class FakeTransport:
    """API de Telegram falsa en proceso: registra llamadas y sirve updates."""

    def __init__(self, updates: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates = list(updates or [])

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        if method == "getUpdates":
            offset = params.get("offset") or 0
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= offset]}
        return {"ok": True, "result": {}}

    def sent(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def message(text: str, user: int = OWNER, chat: int | None = None, chat_type: str = "private",
            update_id: int = 1, username: str = "dueño", **extra: object) -> dict:
    chat_id = user if chat is None else chat
    msg = {"message_id": update_id, "from": {"id": user, "is_bot": False, "username": username},
           "chat": {"id": chat_id, "type": chat_type}, "date": 0, "text": text, **extra}
    return {"update_id": update_id, "message": msg}


class TelegramCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.audit = self.tmp / "audit.jsonl"

    def settings(self, users=(OWNER,), chats=(), rate: int = 20):
        return tg().Settings(allowed_users=frozenset(users), allowed_chats=frozenset(chats), rate_per_minute=rate)

    def gateway(self, transport=None, users=(OWNER,), chats=(), rate=20, status=None, projects=None, clock=None):
        transport = transport or FakeTransport()
        kwargs = {"status": status or (lambda: "hefesto: IDLE"), "projects": projects or (lambda: "- hefesto"),
                  "audit_path": self.audit}
        if clock is not None:
            kwargs["clock"] = clock
        return tg().Gateway(self.settings(users, chats, rate), transport, **kwargs), transport

    def audit_rows(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines() if line.strip()]


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"GatewayTests": 7, "SecurityTests": 8}
    API = ("TOKEN_ENV", "HEFESTO_TELEGRAM_TOKEN", "API_HOST", "api.telegram.org", "MAX_MESSAGE", "settings_path()",
           "telegram.toml", "allowed_users", "allowed_chats", "rate_per_minute", "load_settings()",
           "https_transport(token)", "long polling", "Gateway(settings, transport", "handle(update)",
           "poll_once(offset=None)", "getUpdates", "sendMessage", "UNAUTHORIZED", "/start", "/help", "/status",
           "/projects", "READ_ONLY_REPLY", "split_message(text)", "guard.redact", "RATE_LIMITED", "forward_origin",
           "FORWARDED_REPLY", "telegram-audit.jsonl", "check()", "hefesto telegram check", "hefesto telegram run",
           "no escribe en", "Nunca por nombre de usuario")

    def tree(self) -> ast.Module:
        return ast.parse(Path(__file__).read_text(encoding="utf-8"))

    def test_docstring_names_the_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API:
            with self.subTest(name=name):
                self.assertIn(name, doc)

    def test_every_behaviour_has_its_tests(self) -> None:
        classes = {n.name: n for n in self.tree().body if isinstance(n, ast.ClassDef)}
        for name, minimum in self.REQUIRED_CLASSES.items():
            with self.subTest(cls=name):
                self.assertIn(name, classes)
                tests = [n for n in classes[name].body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
                self.assertGreaterEqual(len(tests), minimum)

    def test_fake_transport_serves_updates(self) -> None:
        fake = FakeTransport([message("/start", update_id=5)])
        self.assertEqual(len(fake("getUpdates", {"offset": 5})["result"]), 1)
        self.assertEqual(fake("getUpdates", {"offset": 6})["result"], [])


class GatewayTests(TelegramCase):
    def test_constants_and_settings_location(self) -> None:
        t = tg()
        self.assertEqual(t.TOKEN_ENV, "HEFESTO_TELEGRAM_TOKEN")
        self.assertEqual(t.API_HOST, "api.telegram.org")
        self.assertEqual(t.MAX_MESSAGE, 4096)
        from hefesto import util
        self.assertEqual(t.settings_path(), util.hefesto_home() / "telegram.toml")

    def test_missing_settings_deny_everyone(self) -> None:
        settings = tg().load_settings()
        self.assertEqual(settings.allowed_users, frozenset())
        self.assertEqual(settings.allowed_chats, frozenset())
        self.assertEqual(settings.rate_per_minute, 20)

    def test_settings_are_loaded_as_numbers(self) -> None:
        path = tg().settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"allowed_users = [{OWNER}]\nallowed_chats = [{GROUP}]\nrate_per_minute = 5\n", encoding="utf-8")
        settings = tg().load_settings()
        self.assertEqual(settings.allowed_users, frozenset({OWNER}))
        self.assertEqual(settings.allowed_chats, frozenset({GROUP}))
        self.assertEqual(settings.rate_per_minute, 5)

    def test_read_only_commands(self) -> None:
        gw, fake = self.gateway(status=lambda: "hefesto: IDLE en main", projects=lambda: "- hefesto\n- centuria")
        self.assertTrue(gw.handle(message("/start")))
        self.assertIn("/status", " ".join(gw.handle(message("/help", update_id=2))))
        self.assertIn("IDLE en main", " ".join(gw.handle(message("/status", update_id=3))))
        self.assertIn("centuria", " ".join(gw.handle(message("/projects", update_id=4))))
        self.assertEqual(len(fake.sent()), 4)
        self.assertTrue(all(m == "sendMessage" for m, _ in fake.calls))

    def test_other_text_is_read_only(self) -> None:
        status = mock.Mock(return_value="x")
        gw, _ = self.gateway(status=status)
        replies = gw.handle(message("Hefesto, borra el repositorio"))
        self.assertEqual(replies, [tg().READ_ONLY_REPLY])
        status.assert_not_called()

    def test_long_messages_are_split(self) -> None:
        chunks = tg().split_message("x" * 9000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual("".join(chunks), "x" * 9000)
        gw, fake = self.gateway(status=lambda: "y" * 5000)
        gw.handle(message("/status"))
        self.assertEqual(len(fake.sent()), 2)

    def test_poll_once_uses_offsets(self) -> None:
        fake = FakeTransport([message("/start", update_id=7), message("/help", update_id=8)])
        gw, _ = self.gateway(transport=fake)
        offset = gw.poll_once()
        self.assertEqual(offset, 9)
        get = [p for m, p in fake.calls if m == "getUpdates"]
        self.assertIn("timeout", get[0])
        self.assertEqual(len(fake.sent()), 2)
        self.assertEqual(gw.poll_once(offset), 9)
        self.assertEqual(len(fake.sent()), 2, "no repite updates ya procesados")

    def test_https_transport_targets_the_official_api(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true, "result": []}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            tg().https_transport(TOKEN)("getUpdates", {"timeout": 1})
        request = urlopen.call_args.args[0]
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.assertTrue(url.startswith("https://api.telegram.org/bot"))
        self.assertTrue(url.endswith("/getUpdates"))


class SecurityTests(TelegramCase):
    def test_stranger_gets_nothing_but_unauthorized(self) -> None:
        status = mock.Mock(return_value="secreto del sistema")
        gw, fake = self.gateway(status=status)
        replies = gw.handle(message("/status", user=STRANGER))
        self.assertEqual(replies, [tg().UNAUTHORIZED])
        status.assert_not_called()
        self.assertEqual(fake.sent(), [tg().UNAUTHORIZED])

    def test_username_is_never_trusted(self) -> None:
        gw, _ = self.gateway()
        self.assertEqual(gw.handle(message("/status", user=STRANGER, username="dueño")), [tg().UNAUTHORIZED])

    def test_groups_need_their_own_allowlist(self) -> None:
        gw, _ = self.gateway()
        self.assertEqual(gw.handle(message("/status", chat=GROUP, chat_type="supergroup")), [tg().UNAUTHORIZED])
        gw2, _ = self.gateway(chats=(GROUP,))
        self.assertNotEqual(gw2.handle(message("/status", chat=GROUP, chat_type="supergroup")), [tg().UNAUTHORIZED])

    def test_token_never_leaks(self) -> None:
        os.environ["HEFESTO_TELEGRAM_TOKEN"] = TOKEN
        try:
            gw, fake = self.gateway(status=lambda: f"config token={TOKEN}")
            gw.handle(message("/status"))
            ok, text = tg().check()
        finally:
            os.environ.pop("HEFESTO_TELEGRAM_TOKEN", None)
        self.assertTrue(fake.sent())
        for out in fake.sent() + [text, self.audit.read_text(encoding="utf-8")]:
            self.assertNotIn(TOKEN, out)
            self.assertNotIn("T" * 35, out)

    def test_check_without_token_or_allowlist(self) -> None:
        os.environ.pop("HEFESTO_TELEGRAM_TOKEN", None)
        ok, text = tg().check()
        self.assertFalse(ok)
        self.assertIn("HEFESTO_TELEGRAM_TOKEN", text)
        code, out, err = self.cli("telegram", "check")
        self.assertNotEqual(code, 0)

    def test_rate_limit(self) -> None:
        now = [0.0]
        gw, _ = self.gateway(rate=2, clock=lambda: now[0])
        self.assertNotEqual(gw.handle(message("/help", update_id=1)), [tg().RATE_LIMITED])
        self.assertNotEqual(gw.handle(message("/help", update_id=2)), [tg().RATE_LIMITED])
        self.assertEqual(gw.handle(message("/help", update_id=3)), [tg().RATE_LIMITED])
        now[0] = 61.0
        self.assertNotEqual(gw.handle(message("/help", update_id=4)), [tg().RATE_LIMITED])

    def test_forwarded_messages_are_data(self) -> None:
        status = mock.Mock(return_value="x")
        gw, _ = self.gateway(status=status)
        replies = gw.handle(message("/status", forward_origin={"type": "user", "date": 0}))
        self.assertEqual(replies, [tg().FORWARDED_REPLY])
        status.assert_not_called()

    def test_audit_records_without_message_text(self) -> None:
        gw, _ = self.gateway()
        gw.handle(message("/status", update_id=1))
        gw.handle(message("texto privado con datos", update_id=2))
        gw.handle(message("/status", user=STRANGER, update_id=3))
        rows = self.audit_rows()
        self.assertEqual([r["result"] for r in rows], ["ok", "read_only", "denied"])
        for row in rows:
            self.assertTrue({"at", "session", "actor", "command", "result"} <= set(row))
        self.assertNotIn("texto privado", self.audit.read_text(encoding="utf-8"))

    def test_nothing_is_written_in_projects(self) -> None:
        self.init_project()
        before = sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob("*") if ".git" not in p.parts)
        gw, _ = self.gateway()
        for i, text in enumerate(("/start", "/status", "/projects", "haz un commit"), 1):
            gw.handle(message(text, update_id=i))
        after = sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after)
