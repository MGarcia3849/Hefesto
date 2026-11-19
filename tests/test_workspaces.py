"""TASK-3601: contrato del registro de proyectos y de un tema de Telegram por proyecto (`hefesto/workspaces.py`).

API que fija este contrato:

- `registry_path() -> Path`: `util.hefesto_home() / "projects.toml"` (fuera de los repositorios), con tablas
  `[[project]]` de claves `id`, `name`, `path` (absoluta y canónica) y `capabilities` (subconjunto de
  `CAPABILITIES = ("read", "write")`).
- `Project`: `id`, `name`, `path: Path`, `capabilities: tuple[str, ...]`.
- `add_project(project_id, path, name=None, capabilities=("read", "write")) -> Project` y
  `remove_project(project_id)`: solo desde la terminal (`util.require_tty`); el `id` es `[a-z0-9][a-z0-9-]{0,31}`;
  la ruta debe existir, ser un directorio, no ser un enlace simbólico y guardarse resuelta. `list_projects()`
  devuelve los registrados en orden. Telegram nunca añade ni quita proyectos ni acepta rutas.
- `resolve(project_id) -> Project`: proyecto registrado y todavía válido; desconocido, desaparecido o convertido en
  enlace lanza `HefestoError` con `EXIT_INPUT`. `inside(project, rel) -> Path`: ruta dentro del proyecto; rechaza
  rutas absolutas, `..` y enlaces que escapan del proyecto con `HefestoError` (`EXIT_BLOCKED`) y lo registra en la
  auditoría de Telegram.
- Temas de Telegram (decisión del usuario): `bindings_path()` es `util.state_home() / "telegram-bindings.json"`.
  `bind(chat_id, thread_id, project_id)` vincula un tema (`message_thread_id`) de un grupo autorizado a un
  proyecto; `unbind(chat_id, thread_id)` lo desvincula; `select(chat_id, project_id)` fija el proyecto activo de un
  chat privado o del tema General (`thread_id=None`); `active(chat_id, thread_id) -> Project | None` devuelve el
  proyecto del tema o, si el tema no tiene, ninguno (un tema sin vincular nunca hereda el proyecto de otro). Los
  vínculos persisten en disco y sobreviven a un reinicio. `close(chat_id, thread_id)` desactiva la asociación sin
  matar procesos ni tocar ficheros.
- `project_status(project) -> str`: rama, número de cambios sin confirmar y estado de la tarea Hefesto en curso.
- `has_desktop() -> bool` y `open_in_vscode(project) -> tuple[bool, str]`: lanza `code <ruta>` sin shell solo si
  hay escritorio y la CLI `code` existe; si no, devuelve `False` y un aviso honesto, sin fallar la selección.
  Nunca cierra ventanas.
- `write_slot(project)`: contexto que garantiza una sola tarea de escritura por proyecto; si ya está ocupado lanza
  `HefestoError` con `EXIT_CONFLICT` sin esperar.
- Telegram (`hefesto/telegram.py`): `/projects` lista los ids; `/project ID` selecciona en privado o en General;
  `/bind ID` y `/unbind` dentro de un tema; `/status` responde con el estado del proyecto activo del tema; `/open`
  abre VS Code si es posible; `/close` desactiva la asociación. Las respuestas a un tema se envían con su
  `message_thread_id`. Ningún comando de esta fase escribe en un proyecto ni ejecuta git reset, clean o cambios de
  rama.
- CLI: `hefesto projects add ID RUTA [--name N] [--read-only]`, `hefesto projects list` y
  `hefesto projects remove ID`.
"""

from __future__ import annotations

import ast
import json
import os
import threading
from pathlib import Path
from unittest import mock

from tests.helpers import HefestoTestCase, git, tty
from tests.test_telegram import FakeTransport, OWNER, message

GROUP = -100777


def ws():
    from hefesto import workspaces
    return workspaces


def topic(text: str, thread: int | None, update_id: int = 1, user: int = OWNER) -> dict:
    extra = {"message_thread_id": thread, "is_topic_message": True} if thread is not None else {}
    return message(text, user=user, chat=GROUP, chat_type="supergroup", update_id=update_id, **extra)


class WorkspaceCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alpha = self.make_repo("alpha")
        self.beta = self.make_repo("beta")

    def make_repo(self, name: str) -> Path:
        path = self.tmp / "repos" / name
        path.mkdir(parents=True)
        git(path, "init", "-q")
        (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "inicial")
        return path

    def add(self, pid: str, path: Path, **kw):
        with tty(True, []):
            return ws().add_project(pid, path, **kw)

    def gateway(self, transport: FakeTransport | None = None):
        from hefesto import telegram
        transport = transport or FakeTransport()
        settings = telegram.Settings(allowed_users=frozenset({OWNER}), allowed_chats=frozenset({GROUP}),
                                     rate_per_minute=100)
        return telegram.Gateway(settings, transport, audit_path=self.tmp / "audit.jsonl"), transport


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"RegistryTests": 6, "BindingTests": 6, "SafetyTests": 5}
    API = ("registry_path()", "projects.toml", "CAPABILITIES", "Project", "add_project(project_id, path",
           "remove_project(project_id)", "util.require_tty", "list_projects()", "resolve(project_id)",
           "inside(project, rel)", "bindings_path()", "telegram-bindings.json", "bind(chat_id, thread_id, project_id)",
           "unbind(chat_id, thread_id)", "select(chat_id, project_id)", "active(chat_id, thread_id)",
           "close(chat_id, thread_id)", "project_status(project)", "has_desktop()", "open_in_vscode(project)",
           "write_slot(project)", "EXIT_CONFLICT", "/projects", "/project ID", "/bind ID", "/unbind", "/open",
           "/close", "message_thread_id", "hefesto projects add", "nunca hereda", "Nunca cierra ventanas")

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

    def test_topic_helper_marks_threads(self) -> None:
        self.assertEqual(topic("/x", 5)["message"]["message_thread_id"], 5)
        self.assertNotIn("message_thread_id", topic("/x", None)["message"])


class RegistryTests(WorkspaceCase):
    def test_registry_lives_outside_repositories(self) -> None:
        from hefesto import util
        self.assertEqual(ws().registry_path(), util.hefesto_home() / "projects.toml")
        self.assertEqual(ws().CAPABILITIES, ("read", "write"))

    def test_add_list_remove(self) -> None:
        project = self.add("alpha", self.alpha, name="Alpha")
        self.assertEqual((project.id, project.name, project.path), ("alpha", "Alpha", self.alpha.resolve()))
        self.add("beta", self.beta, capabilities=("read",))
        self.assertEqual([p.id for p in ws().list_projects()], ["alpha", "beta"])
        self.assertEqual(ws().resolve("beta").capabilities, ("read",))
        with tty(True, []):
            ws().remove_project("alpha")
        self.assertEqual([p.id for p in ws().list_projects()], ["beta"])

    def test_registry_edits_need_a_terminal(self) -> None:
        from hefesto.util import HefestoError
        with tty(False, []), self.assertRaises(HefestoError):
            ws().add_project("alpha", self.alpha)
        self.assertEqual(ws().list_projects(), [])

    def test_invalid_ids_and_paths_are_rejected(self) -> None:
        from hefesto.util import HefestoError
        for pid, path in (("Alpha!", self.alpha), ("../x", self.alpha), ("gamma", self.tmp / "no-existe"),
                          ("readme", self.alpha / "README.md")):
            with self.subTest(pid=pid), self.assertRaises(HefestoError):
                self.add(pid, path)

    def test_unknown_project_is_an_input_error(self) -> None:
        from hefesto.util import EXIT_INPUT, HefestoError
        with self.assertRaises(HefestoError) as ctx:
            ws().resolve("no-existe")
        self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_project_status_reports_branch_and_changes(self) -> None:
        project = self.add("alpha", self.alpha)
        (self.alpha / "nuevo.txt").write_text("x", encoding="utf-8")
        text = ws().project_status(project)
        self.assertIn(git(self.alpha, "branch", "--show-current"), text)
        self.assertIn("1", text)

    def test_cli(self) -> None:
        code, out, err = self.run_cli("projects", "add", "alpha", str(self.alpha), "--name", "Alpha", tty_value=True)
        self.assertEqual(code, 0, out + err)
        code, out, _ = self.run_cli("projects", "list")
        self.assertIn("alpha", out)
        code, _, _ = self.run_cli("projects", "remove", "alpha", tty_value=True)
        self.assertEqual(code, 0)


class BindingTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.add("alpha", self.alpha)
        self.add("beta", self.beta)

    def test_bind_topics_to_projects(self) -> None:
        ws().bind(GROUP, 10, "alpha")
        ws().bind(GROUP, 20, "beta")
        self.assertEqual(ws().active(GROUP, 10).id, "alpha")
        self.assertEqual(ws().active(GROUP, 20).id, "beta")

    def test_unbound_topic_never_inherits(self) -> None:
        ws().bind(GROUP, 10, "alpha")
        ws().select(GROUP, "beta")
        self.assertIsNone(ws().active(GROUP, 30))
        self.assertEqual(ws().active(GROUP, None).id, "beta")

    def test_bindings_survive_a_restart(self) -> None:
        ws().bind(GROUP, 10, "alpha")
        data = json.loads(ws().bindings_path().read_text(encoding="utf-8"))
        self.assertTrue(data)
        import importlib
        from hefesto import workspaces
        importlib.reload(workspaces)
        self.assertEqual(workspaces.active(GROUP, 10).id, "alpha")

    def test_close_and_unbind_touch_nothing(self) -> None:
        ws().bind(GROUP, 10, "alpha")
        before = git(self.alpha, "status", "--porcelain")
        ws().close(GROUP, 10)
        self.assertIsNone(ws().active(GROUP, 10))
        ws().bind(GROUP, 10, "alpha")
        ws().unbind(GROUP, 10)
        self.assertIsNone(ws().active(GROUP, 10))
        self.assertEqual(git(self.alpha, "status", "--porcelain"), before)

    def test_telegram_bind_and_status_in_a_topic(self) -> None:
        gw, fake = self.gateway()
        gw.handle(topic("/bind alpha", 10, update_id=1))
        replies = gw.handle(topic("/status", 10, update_id=2))
        self.assertIn(git(self.alpha, "branch", "--show-current"), " ".join(replies))
        sent = [p for m, p in fake.calls if m == "sendMessage"]
        self.assertTrue(all(p.get("message_thread_id") == 10 for p in sent))
        self.assertEqual(ws().active(GROUP, 10).id, "alpha")

    def test_telegram_projects_and_select(self) -> None:
        gw, _ = self.gateway()
        listed = " ".join(gw.handle(message("/projects", update_id=1)))
        self.assertIn("alpha", listed)
        self.assertIn("beta", listed)
        gw.handle(message("/project beta", update_id=2))
        self.assertEqual(ws().active(OWNER, None).id, "beta")

    def test_telegram_cannot_register_paths(self) -> None:
        gw, _ = self.gateway()
        gw.handle(message(f"/project {self.tmp}", update_id=1))
        gw.handle(topic(f"/bind {self.tmp / 'repos'}", 10, update_id=2))
        self.assertIsNone(ws().active(OWNER, None))
        self.assertIsNone(ws().active(GROUP, 10))
        self.assertEqual([p.id for p in ws().list_projects()], ["alpha", "beta"])


class SafetyTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.add("alpha", self.alpha)

    def test_inside_rejects_traversal_and_absolute(self) -> None:
        from hefesto.util import EXIT_BLOCKED, HefestoError
        self.assertEqual(ws().inside(self.project, "README.md"), (self.alpha / "README.md").resolve())
        for rel in ("../beta/README.md", str(self.beta / "README.md"), "sub/../../x"):
            with self.subTest(rel=rel), self.assertRaises(HefestoError) as ctx:
                ws().inside(self.project, rel)
            self.assertEqual(ctx.exception.code, EXIT_BLOCKED)

    def test_inside_rejects_escaping_symlinks(self) -> None:
        from hefesto.util import HefestoError
        link = self.alpha / "fuera"
        try:
            os.symlink(self.beta, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("el sistema no permite crear enlaces simbólicos")
        with self.assertRaises(HefestoError):
            ws().inside(self.project, "fuera/README.md")

    def test_moved_project_is_no_longer_valid(self) -> None:
        from hefesto.util import HefestoError
        self.alpha.rename(self.tmp / "repos" / "alpha-movido")
        with self.assertRaises(HefestoError):
            ws().resolve("alpha")

    def test_single_write_slot_per_project(self) -> None:
        from hefesto.util import EXIT_CONFLICT, HefestoError
        with ws().write_slot(self.project):
            with self.assertRaises(HefestoError) as ctx:
                with ws().write_slot(self.project):
                    pass
            self.assertEqual(ctx.exception.code, EXIT_CONFLICT)
        with ws().write_slot(self.project):
            pass

    def test_write_slot_is_exclusive_across_threads(self) -> None:
        from hefesto.util import HefestoError
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            try:
                with ws().write_slot(self.project):
                    results.append("dentro")
                    threading.Event().wait(0.3)
            except HefestoError:
                results.append("ocupado")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["dentro", "ocupado"])

    def test_vscode_only_with_desktop_and_cli(self) -> None:
        with mock.patch.object(ws(), "has_desktop", return_value=False), \
                mock.patch("subprocess.Popen") as popen:
            opened, text = ws().open_in_vscode(self.project)
        self.assertFalse(opened)
        self.assertTrue(text.strip())
        popen.assert_not_called()
        with mock.patch.object(ws(), "has_desktop", return_value=True), \
                mock.patch("shutil.which", return_value="C:/code.cmd"), \
                mock.patch("subprocess.Popen") as popen:
            opened, _ = ws().open_in_vscode(self.project)
        self.assertTrue(opened)
        args = popen.call_args.args[0]
        self.assertEqual(Path(args[-1]), self.alpha.resolve())
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
