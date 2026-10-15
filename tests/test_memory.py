"""Tests del pack memory (TASK-906: memoria permanente entre proyectos)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from hefesto import config, memory, roadmap, util
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git, tty

ROADMAP_CONTENT = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-101: Modulo de autenticacion
  - verify: `python -c "print('ok')"`
  - files: hefesto/auth.py
  - role: runner
  - size: S
"""


class MemoryCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("HEFESTO_ROLE", None)

    def home(self) -> Path:
        return self.tmp / "home"

    def symlink(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"El sistema no permite crear enlaces simbólicos: {exc}")

    def enable_memory(self) -> Path:
        proj = self.init_project()
        path = proj / ".hefesto" / "settings.json"
        settings = config.read_settings(path)
        settings.setdefault("packs", {})["memory"] = {"enabled": True}
        config.write_settings(path, settings)
        return proj

    def setup_task_project(self) -> Path:
        self.init_project()
        rm_path = roadmap.roadmap_path(self.project)
        rm_path.write_text(ROADMAP_CONTENT, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("start", "TASK-101")
        self.assertEqual(code, 0, out + err)
        return self.project


class MemoryRememberTests(MemoryCase):
    def test_remember_scope_global_creates_candidate_in_pendientes_and_nothing_else(self) -> None:
        proj = self.init_project()
        initial_learnings = (proj / ".hefesto" / "LEARNINGS.md").read_text(encoding="utf-8")
        code, out, err = self.cli(
            "remember",
            "Evitar estados mutables globales",
            "--scope",
            "global",
            "--kind",
            "preferencias",
        )
        self.assertEqual(code, 0, out + err)

        pdir = self.home() / "memory" / "_pendientes"
        self.assertTrue(pdir.is_dir())
        files = list(pdir.glob("*.md"))
        self.assertEqual(len(files), 1)

        candidate = files[0]
        content = candidate.read_text(encoding="utf-8")
        self.assertIn("fecha:", content)
        self.assertIn(f"proyecto: {proj.name}", content)
        self.assertIn("tipo: preferencias", content)
        self.assertIn("Evitar estados mutables globales", content)

        # Nada fuera de _pendientes
        pref_dir = self.home() / "memory" / "preferencias"
        self.assertFalse(pref_dir.exists())
        self.assertFalse((self.home() / "LEARNINGS.md").exists())
        self.assertEqual((proj / ".hefesto" / "LEARNINGS.md").read_text(encoding="utf-8"), initial_learnings)

    def test_remember_scope_project_appends_to_project_learnings(self) -> None:
        proj = self.init_project()
        code, out, err = self.cli("remember", "Usar decimal para moneda", "--scope", "project")
        self.assertEqual(code, 0, out + err)

        learnings_path = proj / ".hefesto" / "LEARNINGS.md"
        self.assertTrue(learnings_path.is_file())
        text = learnings_path.read_text(encoding="utf-8")
        self.assertIn("Usar decimal para moneda", text)
        self.assertIn(util.now_iso()[:10], text)
        self.assertFalse((self.home() / "memory" / "_pendientes").exists())

    def test_remember_secret_blocked_with_code_2_and_no_writes(self) -> None:
        proj = self.init_project()
        initial_learnings = (proj / ".hefesto" / "LEARNINGS.md").read_text(encoding="utf-8")
        secret = "Clave privada: ghp_" + ("x" * 36)

        # Global
        code, out, err = self.cli("remember", secret, "--scope", "global")
        self.assertEqual(code, 2)
        self.assertIn("Secreto detectado", err)
        self.assertFalse((self.home() / "memory" / "_pendientes").exists())

        # Project
        code, out, err = self.cli("remember", secret, "--scope", "project")
        self.assertEqual(code, 2)
        self.assertIn("Secreto detectado", err)
        self.assertEqual((proj / ".hefesto" / "LEARNINGS.md").read_text(encoding="utf-8"), initial_learnings)

        # Direct API
        with self.assertRaises(HefestoError) as ctx:
            memory.remember(proj, secret)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual((proj / ".hefesto" / "LEARNINGS.md").read_text(encoding="utf-8"), initial_learnings)


class MemoryReviewTests(MemoryCase):
    def test_review_without_tty_fails_with_code_2(self) -> None:
        memory.remember(None, "Lección para review", scope="global")
        code, out, err = self.run_cli("memory", "review", tty_value=False)
        self.assertEqual(code, 2)
        self.assertEqual(len(memory.pending()), 1)

    def test_review_yes_with_hefesto_role_fails_with_code_2(self) -> None:
        memory.remember(None, "Lección con role", scope="global")
        os.environ["HEFESTO_ROLE"] = "runner"
        code, out, err = self.run_cli("memory", "review", "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(len(memory.pending()), 1)

    def test_review_interactive_approve_edit_discard_and_skip(self) -> None:
        p1 = memory.remember(None, "Nota para aprobar", scope="global")
        p2 = memory.remember(None, "Nota para editar", scope="global")
        p3 = memory.remember(None, "Nota para descartar", scope="global")
        p4 = memory.remember(None, "Nota para saltar", scope="global")

        replies = {p1: ["a"], p2: ["e", "Texto editado limpio"], p3: ["d"], p4: ["s"]}
        answers = [answer for path in memory.pending() for answer in replies[path]]
        code, out, err = self.run_cli("memory", "review", tty_value=True, answers=answers)
        self.assertEqual(code, 0, out + err)

        # p1 y p2 aprobadas (sin pack -> ~/hefesto/LEARNINGS.md)
        self.assertFalse(p1.exists())
        self.assertFalse(p2.exists())
        learnings = (self.home() / "LEARNINGS.md").read_text(encoding="utf-8")
        self.assertIn("Nota para aprobar", learnings)
        self.assertIn("Texto editado limpio", learnings)
        self.assertCountEqual(learnings.splitlines(), [
            f"- {util.now_iso()[:10]}: Nota para aprobar",
            f"- {util.now_iso()[:10]}: Texto editado limpio",
        ])
        self.assertNotIn("Nota para editar", learnings)
        self.assertNotIn("Nota para descartar", learnings)
        self.assertNotIn("Nota para saltar", learnings)

        # p3 descartada
        self.assertFalse(p3.exists())

        # p4 saltada: permanece en _pendientes
        self.assertTrue(p4.exists())
        self.assertEqual(len(memory.pending()), 1)

    def test_approve_without_pack_writes_to_root_learnings(self) -> None:
        p = memory.remember(None, "Nota independiente de proyectos", scope="global")
        self.assertTrue(p.is_file())

        msg = memory.review(None, yes=True)
        self.assertIn("1 aprobadas", msg)
        self.assertFalse(p.exists())

        root_learnings = self.home() / "LEARNINGS.md"
        self.assertTrue(root_learnings.is_file())
        self.assertIn("Nota independiente de proyectos", root_learnings.read_text(encoding="utf-8"))
        self.assertFalse((self.home() / "memory" / "errores-resueltos").exists())

    def test_approve_with_pack_active_writes_to_memory_kind_and_stacks(self) -> None:
        proj = self.init_project()
        # Activar pack memory en el proyecto
        settings_path = proj / ".hefesto" / "settings.json"
        settings = {"packs": {"memory": {"enabled": True}}}
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        p_err = memory.remember(proj, "Fallo de concurrencia", scope="global", kind="errores-resueltos")
        p_stack = memory.remember(proj, "Widgets const en build", scope="global", kind="stacks", stack="flutter")

        msg = memory.review(proj, yes=True)
        self.assertIn("2 aprobadas", msg)
        self.assertFalse(p_err.exists())
        self.assertFalse(p_stack.exists())

        err_file = self.home() / "memory" / "errores-resueltos" / p_err.name
        self.assertTrue(err_file.is_file())
        self.assertIn("Fallo de concurrencia", err_file.read_text(encoding="utf-8"))

        stack_file = self.home() / "memory" / "stacks" / "flutter" / p_stack.name
        self.assertTrue(stack_file.is_file())
        self.assertIn("Widgets const en build", stack_file.read_text(encoding="utf-8"))

        # No se tocó ~/hefesto/LEARNINGS.md
        self.assertFalse((self.home() / "LEARNINGS.md").exists())

    def test_review_skips_invalid_candidates_with_warning_and_reviews_valid(self) -> None:
        c1 = memory.remember(None, "Nota con tipo malo", scope="global")
        raw1 = memory.format_note({"tipo": "desconocido"}, "Nota con tipo malo")
        c1.write_text(raw1, encoding="utf-8")

        c2 = memory.remember(None, "Nota con cabecera rota", scope="global")
        raw2 = "---\nfecha: 2026-10-15\ntipo: errores-resueltos\nsin cierre de frontmatter"
        c2.write_text(raw2, encoding="utf-8")

        c3 = memory.remember(None, "Nota con secreto", scope="global")
        secret = "ghp_" + ("s" * 36)
        raw3 = memory.format_note({"tipo": "decisiones"}, f"Clave secreta {secret}")
        c3.write_text(raw3, encoding="utf-8")

        c4 = memory.remember(None, "Nota valida para aprobar", scope="global")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            msg = memory.review(None, yes=True)

        self.assertIn("1 aprobadas", msg)
        self.assertIn("3 saltadas", msg)
        err_output = err.getvalue()
        self.assertIn("Aviso", err_output)
        self.assertIn("Categoría no válida", err_output)
        self.assertIn("Cabecera rota", err_output)
        self.assertIn("Secreto detectado", err_output)

        self.assertTrue(c1.exists())
        self.assertEqual(c1.read_text(encoding="utf-8"), raw1)
        self.assertTrue(c2.exists())
        self.assertEqual(c2.read_text(encoding="utf-8"), raw2)
        self.assertTrue(c3.exists())
        self.assertEqual(c3.read_text(encoding="utf-8"), raw3)

        self.assertFalse(c4.exists())
        root_learnings = self.home() / "LEARNINGS.md"
        self.assertTrue(root_learnings.is_file())
        learnings_text = root_learnings.read_text(encoding="utf-8")
        self.assertIn("Nota valida para aprobar", learnings_text)
        self.assertNotIn("Nota con tipo malo", learnings_text)
        self.assertNotIn("Nota con cabecera rota", learnings_text)
        self.assertNotIn(secret, learnings_text)

    def test_review_skips_real_invalid_utf8_and_approves_valid_candidate(self) -> None:
        bad = memory.remember(None, "Nota con bytes invalidos", scope="global")
        raw = b"---\nfecha: 2026-10-15\ntipo: errores-resueltos\n---\n\nNota invalida: \xff\xfe\n"
        bad.write_bytes(raw)
        good = memory.remember(None, "Nota valida para aprobar", scope="global")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = memory.review(yes=True)

        self.assertIn("1 aprobadas", result)
        self.assertIn("1 saltadas", result)
        self.assertIn("Aviso", err.getvalue())
        self.assertIn("permanece en _pendientes", err.getvalue())
        self.assertEqual(bad.read_bytes(), raw)
        self.assertFalse(good.exists())
        learnings = (self.home() / "LEARNINGS.md").read_text(encoding="utf-8")
        self.assertEqual(learnings, f"- {util.now_iso()[:10]}: Nota valida para aprobar\n")

    def test_review_skips_unreadable_candidate_with_warning(self) -> None:
        for exc in (
            PermissionError("Permiso denegado"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "byte invalido"),
        ):
            with self.subTest(error=type(exc).__name__):
                bad = memory.remember(None, "Nota no legible", scope="global")
                good = memory.remember(None, "Nota legible para aprobar", scope="global")

                orig_read_text = Path.read_text

                def mock_read(path: Path, *args: object, **kwargs: object) -> str:
                    if path.name == bad.name:
                        raise exc
                    return orig_read_text(path, *args, **kwargs)

                err = io.StringIO()
                with mock.patch.object(Path, "read_text", new=mock_read), contextlib.redirect_stderr(err):
                    msg = memory.review(None, yes=True)

                self.assertIn("1 aprobadas", msg)
                self.assertIn("1 saltadas", msg)
                self.assertIn("Aviso", err.getvalue())
                self.assertTrue(bad.exists())
                self.assertFalse(good.exists())
                bad.unlink()
                (self.home() / "LEARNINGS.md").unlink(missing_ok=True)


class MemorySecurityTests(MemoryCase):
    def test_kind_is_validated_on_creation_and_approval(self) -> None:
        for kind in ("../outside", "", "other", str(self.tmp / "outside")):
            with self.subTest(kind=kind):
                with self.assertRaises(HefestoError) as ctx:
                    memory.remember(None, "Nota limpia", scope="global", kind=kind)
                self.assertEqual(ctx.exception.code, 1)
                self.assertFalse(memory.pending_dir().exists())
        candidate = memory.remember(None, "Nota limpia", scope="global")
        for kind in ("../../outside", "", "otro"):
            with self.subTest(edited_kind=kind):
                raw = memory.format_note({"tipo": kind}, "Nota limpia")
                candidate.write_text(raw, encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    res = memory.review(yes=True)
                self.assertIn("0 aprobadas", res)
                self.assertIn("1 saltadas", res)
                self.assertIn("Aviso", err.getvalue())
                self.assertEqual(candidate.read_text(encoding="utf-8"), raw)
                self.assertFalse((self.home() / "LEARNINGS.md").exists())

    def test_stack_is_normalized_at_creation_and_after_disk_edit(self) -> None:
        proj = self.enable_memory()
        for stack, expected in (("../../x", "x"), ("..\\..\\x", "x"), ("CON.txt", "general"),
                                ("NUL", "general"), ("LPT1", "general"), ("...", "general"),
                                ("Vue.JS / v3", "vue.js-v3")):
            with self.subTest(stack=stack):
                candidate = memory.remember(proj, "Nota de stack", scope="global", kind="stacks", stack=stack)
                meta, body = memory.parse_note_content(candidate.read_text(encoding="utf-8"))
                self.assertEqual(meta["stack"], expected)
                meta["stack"] = stack  # La aprobación tampoco debe confiar en el slug guardado.
                candidate.write_text(memory.format_note(meta, body), encoding="utf-8")
                memory.review(proj, yes=True)
                dest = memory.memory_dir() / "stacks" / expected / candidate.name
                self.assertTrue(dest.is_file())
                self.assertEqual(dest.resolve().relative_to(memory.memory_dir().resolve()).parts[0], "stacks")
                approved_meta, _ = memory.parse_note_content(dest.read_text(encoding="utf-8"))
                self.assertEqual(approved_meta["stack"], expected)
                self.assertFalse(candidate.exists())
        self.assertFalse((self.home() / "x").exists())
        self.assertFalse((self.tmp / "x").exists())

    def test_secrets_in_metadata_are_blocked_before_writes(self) -> None:
        secret = "ghp_" + "x" * 36
        for kwargs in ({"stack": secret}, {"kind": secret}, {"project": self.tmp / secret}):
            for scope in ("global", "project"):
                with self.subTest(metadata=list(kwargs), scope=scope):
                    args = {"project": self.project, "scope": scope, **kwargs}
                    with self.assertRaises(HefestoError) as ctx:
                        memory.remember(text="Nota limpia", **args)
                    self.assertEqual(ctx.exception.code, 2)
                    self.assertNotIn(secret, str(ctx.exception))
                    self.assertFalse(self.home().exists())
                    self.assertFalse(self.project.exists())

    def test_disk_edited_secret_is_retained_without_disclosure(self) -> None:
        secret = "ghp_" + "x" * 36
        candidate = memory.remember(None, "Nota limpia", scope="global")
        original = candidate.read_text(encoding="utf-8")
        for raw in (original + secret, original.replace("stack: general", f"stack: {secret}"),
                    original.replace("proyecto: ", f"proyecto: {secret}"),
                    original.replace("fecha:", f"ignorado: {secret}\nfecha:")):
            with self.subTest(raw_location=raw.find(secret)):
                candidate.write_text(raw, encoding="utf-8")
                code, output, err = self.run_cli("memory", "review", "--yes")
                self.assertEqual(code, 0, err)
                self.assertIn("Secreto detectado", err)
                self.assertNotIn(secret, output + err)
                self.assertEqual(candidate.read_text(encoding="utf-8"), raw)
                self.assertFalse((self.home() / "LEARNINGS.md").exists())

    def test_review_rechecks_disk_after_approve_and_edit_prompts(self) -> None:
        candidate = memory.remember(None, "Nota limpia", scope="global")
        original = candidate.read_text(encoding="utf-8")
        secret = "ghp_" + "x" * 36
        for action in ("a", "e"):
            with self.subTest(action=action):
                candidate.write_text(original, encoding="utf-8")
                replies = iter([action, "Edición limpia"])

                def answer(_prompt: str) -> str:
                    candidate.write_text(original + secret, encoding="utf-8")
                    return next(replies)

                err = io.StringIO()
                with tty(), mock.patch("builtins.input", side_effect=answer), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    result = memory.review()
                self.assertIn("0 aprobadas, 0 editadas", result)
                self.assertIn("Secreto detectado", err.getvalue())
                self.assertEqual(candidate.read_text(encoding="utf-8"), original + secret)
                self.assertFalse((self.home() / "LEARNINGS.md").exists())

    def test_secret_in_interactive_edit_is_not_saved(self) -> None:
        candidate = memory.remember(None, "Nota limpia", scope="global")
        original = candidate.read_text(encoding="utf-8")
        code, output, err = self.run_cli("memory", "review", tty_value=True,
                                         answers=["e", "ghp_" + "x" * 36])
        self.assertEqual(code, 0, err)
        self.assertIn("Secreto detectado", err)
        self.assertIn("0 editadas", output)
        self.assertEqual(candidate.read_text(encoding="utf-8"), original)
        self.assertFalse((self.home() / "LEARNINGS.md").exists())

    def test_pending_order_is_date_then_name(self) -> None:
        store = memory.pending_dir()
        store.mkdir(parents=True)
        for name, date in (("a.md", "2026-02-02"), ("z.md", "2026-01-01"), ("b.md", "2026-02-02")):
            (store / name).write_text(memory.format_note({"fecha": date}, name), encoding="utf-8")
        self.assertEqual([p.name for p in memory.pending()], ["z.md", "a.md", "b.md"])

    def test_pending_symlink_is_never_read_or_moved(self) -> None:
        store = memory.pending_dir()
        store.mkdir(parents=True)
        outside = self.tmp / "outside.md"
        outside.write_text("No leer ni mover", encoding="utf-8")
        link = store / "link.md"
        self.symlink(link, outside)
        read_text = Path.read_text

        def checked_read(path: Path, *args: object, **kwargs: object) -> str:
            self.assertNotEqual(path, link)
            self.assertNotEqual(path, outside)
            return read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", checked_read):
            code, _output, err = self.run_cli("memory", "review", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Aviso", err)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "No leer ni mover")

    def test_remember_rejects_linked_store_and_pending_parents(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        for component in ("memory", "memory/_pendientes"):
            with self.subTest(component=component):
                link = self.home() / component
                link.parent.mkdir(parents=True, exist_ok=True)
                self.symlink(link, outside)
                with self.assertRaises(HefestoError) as ctx:
                    memory.remember(None, "Nota limpia", scope="global")
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(list(outside.iterdir()), [])
                link.unlink()

    def test_project_and_fallback_learnings_symlinks_are_rejected(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("Conservar", encoding="utf-8")
        project_learnings = self.project / ".hefesto" / "LEARNINGS.md"
        project_learnings.parent.mkdir(parents=True)
        self.symlink(project_learnings, outside)
        with self.assertRaises(HefestoError) as ctx:
            memory.remember(self.project, "Nota local")
        self.assertEqual(ctx.exception.code, 2)
        candidate = memory.remember(None, "Nota global", scope="global")
        self.symlink(self.home() / "LEARNINGS.md", outside)
        with self.assertRaises(HefestoError) as ctx:
            memory.review(yes=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(candidate.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "Conservar")

    def test_project_control_directory_symlink_works(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        self.project.mkdir()
        self.symlink(self.project / ".hefesto", outside)
        path = memory.remember(self.project, "Nota local")
        self.assertTrue(path.is_file())
        learnings = outside / "LEARNINGS.md"
        self.assertTrue(learnings.is_file())
        self.assertIn("Nota local", learnings.read_text(encoding="utf-8"))

        leak_target = self.tmp / "leak.md"
        leak_target.write_text("Conservar", encoding="utf-8")
        learnings.unlink()
        self.symlink(learnings, leak_target)
        with self.assertRaises(HefestoError) as ctx:
            memory.remember(self.project, "Nota local con enlace interno")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(leak_target.read_text(encoding="utf-8"), "Conservar")

    test_project_control_directory_symlink_is_rejected = test_project_control_directory_symlink_works

    def test_remember_does_not_overwrite_a_candidate_symlink(self) -> None:
        candidate = memory.remember(None, "Nota limpia", scope="global")
        candidate.unlink()
        outside = self.tmp / "outside.md"
        outside.write_text("Conservar", encoding="utf-8")
        self.symlink(candidate, outside)
        with self.assertRaises(HefestoError) as ctx:
            memory.remember(None, "Nota limpia", scope="global")
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(candidate.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "Conservar")

    def test_delete_rechecks_links_after_confirmation(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("Conservar", encoding="utf-8")
        for action in ("review", "prune"):
            with self.subTest(action=action):
                if action == "review":
                    target = memory.remember(None, "Nota para descartar", scope="global")
                else:
                    target = memory.memory_dir() / "decisiones" / "old.md"
                    target.parent.mkdir(parents=True)
                    target.write_text(memory.format_note({"fecha": "2000-01-01"}, "Nota antigua"), encoding="utf-8")

                def answer(_prompt: str) -> str:
                    target.unlink()
                    self.symlink(target, outside)
                    return "d" if action == "review" else "s"

                with tty(), mock.patch("builtins.input", side_effect=answer), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(HefestoError) as ctx:
                        getattr(memory, action)()
                self.assertEqual(ctx.exception.code, 2)
                self.assertTrue(target.is_symlink())
                self.assertEqual(outside.read_text(encoding="utf-8"), "Conservar")
                target.unlink()

    def test_approval_rejects_linked_destination_and_parent(self) -> None:
        proj = self.enable_memory()
        candidate = memory.remember(proj, "Nota limpia", scope="global", kind="stacks", stack="python")
        outside = self.tmp / "outside"
        outside.mkdir()
        folder = memory.memory_dir() / "stacks" / "python"
        folder.parent.mkdir()
        self.symlink(folder, outside)
        with self.assertRaises(HefestoError) as ctx:
            memory.review(proj, yes=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(candidate.exists())
        self.assertEqual(list(outside.iterdir()), [])
        folder.unlink()
        folder.mkdir()
        target = outside / "note.md"
        target.write_text("Conservar", encoding="utf-8")
        self.symlink(folder / candidate.name, target)
        with self.assertRaises(HefestoError) as ctx:
            memory.review(proj, yes=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(candidate.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "Conservar")

    def test_search_relevant_and_prune_ignore_external_links(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        note = outside / "old.md"
        raw = memory.format_note({"fecha": "2000-01-01"}, "auth conservar")
        note.write_text(raw, encoding="utf-8")
        store = memory.memory_dir()
        store.mkdir(parents=True)
        self.symlink(store / "linked-dir", outside)
        self.symlink(store / "linked-file.md", note)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(memory.search("auth"), [])
            self.assertEqual(memory.relevant(None, task={"files": ["auth.py"]}), [])
            memory.prune(yes=True)
        self.assertEqual(note.read_text(encoding="utf-8"), raw)
        self.assertTrue((store / "linked-file.md").is_symlink())

    def test_linked_hefesto_home_works(self) -> None:
        real_home = self.tmp / "real_home"
        real_home.mkdir()
        linked_home = self.tmp / "linked_home"
        self.symlink(linked_home, real_home)
        with mock.patch.dict(os.environ, {"HEFESTO_HOME": str(linked_home)}):
            cand = memory.remember(None, "Nota con home enlazado", scope="global")
            self.assertTrue(cand.is_file())
            msg = memory.review(None, yes=True)
            self.assertIn("1 aprobadas", msg)
            root_learnings = real_home / "LEARNINGS.md"
            self.assertTrue(root_learnings.is_file())
            self.assertIn("Nota con home enlazado", root_learnings.read_text(encoding="utf-8"))


class MemorySearchTests(MemoryCase):
    def test_memory_search_finds_approved_notes_and_not_pending(self) -> None:
        # Pendiente (no debe aparecer)
        memory.remember(None, "TokenJWT pendiente en revision", scope="global")

        # Aprobada en memory
        dec_dir = self.home() / "memory" / "decisiones"
        dec_dir.mkdir(parents=True, exist_ok=True)
        (dec_dir / "nota1.md").write_text(
            "---\nfecha: 2026-10-15\ntipo: decisiones\n---\n\nDecisión sobre TokenJWT en cabeceras.\n",
            encoding="utf-8",
        )

        # En ~/hefesto/LEARNINGS.md
        root_learnings = self.home() / "LEARNINGS.md"
        root_learnings.parent.mkdir(parents=True, exist_ok=True)
        root_learnings.write_text("- 2026-10-15: Renovar TokenJWT cada 15 minutos.\n", encoding="utf-8")

        code, out, err = self.run_cli("memory", "search", "TokenJWT")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Decisión sobre TokenJWT", out)
        self.assertIn("Renovar TokenJWT cada 15 minutos", out)
        self.assertNotIn("pendiente en revision", out)

        results = memory.search("TokenJWT")
        self.assertEqual(len(results), 2)
        paths = [r["path"] for r in results]
        self.assertTrue(any("nota1.md" in p for p in paths))
        self.assertTrue(any("LEARNINGS.md" in p for p in paths))
        self.assertFalse(any("_pendientes" in p for p in paths))


class MemoryIntegrationTests(MemoryCase):
    def test_relevance_ignores_absolute_store_path_and_metadata(self) -> None:
        store = self.tmp / "hefesto" / "memory"
        store.mkdir(parents=True)
        (store / "cocina.md").write_text(
            memory.format_note({"proyecto": "hefesto"}, "Receta de paella valenciana."), encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"HEFESTO_HOME": str(store.parent)}):
            self.assertEqual(memory.relevant(None, task={"files": ["hefesto/auth.py"]}), [])
            (store / "auth.md").write_text("Verificar la firma.", encoding="utf-8")
            self.assertEqual(memory.relevant(None, task={"files": ["hefesto/auth.py"]}), ["Verificar la firma."])

    def test_configured_limits_apply_to_resume_and_packet(self) -> None:
        proj = self.setup_task_project()
        store = memory.memory_dir() / "errores-resueltos"
        store.mkdir(parents=True)
        bodies = [f"Regla de autenticacion {i}: verificar firma auth siempre." for i in range(7)]
        for i, body in enumerate(bodies):
            (store / f"auth_{i}.md").write_text(body, encoding="utf-8")
        settings = proj / ".hefesto" / "settings.json"
        for max_notes, max_chars, expected in (
            (2, 2000, bodies[:2]), (5, len(bodies[0]) + 1, bodies[:1]), (3, 17, [bodies[0][:17]]),
        ):
            with self.subTest(max_notes=max_notes, max_chars=max_chars):
                config.set_setting(settings, ("memory", "max_notes"), max_notes)
                config.set_setting(settings, ("memory", "max_chars"), max_chars)
                code, output, err = self.cli("resume", "--json")
                self.assertEqual(code, 0, err)
                notes = json.loads(output)["memory"]
                self.assertEqual(notes, expected)
                self.assertLessEqual(len(notes), max_notes)
                self.assertLessEqual(sum(map(len, notes)), max_chars)
                code, output, err = self.cli("resume")
                self.assertEqual(code, 0, err)
                rendered = output.split("memoria global:\n", 1)[1].split("siguiente acción:", 1)[0]
                self.assertEqual([line.removeprefix("  - ") for line in rendered.splitlines()], expected)
                code, output, err = self.cli("packet", "TASK-101")
                self.assertEqual(code, 0, err)
                parts = output.split("(memoria global)\n")[1:]
                packet_notes = [part.split("\n\n", 1)[0].rstrip() for part in parts]
                self.assertEqual(packet_notes, expected)
                self.assertLessEqual(sum(map(len, packet_notes)), max_chars)

    def test_resume_and_packet_include_up_to_max_notes_relevant_and_no_irrelevant(self) -> None:
        proj = self.setup_task_project()

        # Crear notas en el almacén global
        err_dir = self.home() / "memory" / "errores-resueltos"
        err_dir.mkdir(parents=True, exist_ok=True)

        # 7 notas relevantes sobre autenticacion
        for i in range(1, 8):
            (err_dir / f"auth_{i}.md").write_text(
                f"---\nfecha: 2026-10-15\n---\n\nRegla de autenticacion #{i}: verificar firma auth siempre.\n",
                encoding="utf-8",
            )

        # 1 nota irrelevante
        (err_dir / "cocina.md").write_text(
            "---\nfecha: 2026-10-15\n---\n\nReceta de paella valenciana con arroz bomba.\n",
            encoding="utf-8",
        )

        # Test hefesto resume
        code, out, err = self.cli("resume")
        self.assertEqual(code, 0, out + err)
        self.assertIn("memoria global:", out)
        self.assertIn("Regla de autenticacion", out)
        self.assertNotIn("paella", out)

        # Test resume --json
        code, out_json, _ = self.cli("resume", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out_json)
        self.assertIn("memory", data)
        self.assertLessEqual(len(data["memory"]), 5)
        for m in data["memory"]:
            self.assertIn("autenticacion", m.lower())
            self.assertNotIn("paella", m.lower())

        # Test hefesto packet TASK-101
        code, out_pkt, _ = self.cli("packet", "TASK-101")
        self.assertEqual(code, 0)
        self.assertIn("Decisiones y lecciones relevantes", out_pkt)
        self.assertIn("Regla de autenticacion", out_pkt)
        self.assertNotIn("paella", out_pkt)

    def test_resume_and_packet_work_with_symlinked_memory_dir(self) -> None:
        self.setup_task_project()
        outside = self.tmp / "outside_memory"
        outside.mkdir()
        note = outside / "note.md"
        note.write_text("Regla de autenticacion auth secreta.", encoding="utf-8")
        self.symlink(self.home() / "memory", outside)

        code, out, err = self.cli("resume")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Aviso", err)

        code, out_json, err = self.cli("resume", "--json")
        self.assertEqual(code, 0, out_json + err)
        data = json.loads(out_json)
        self.assertEqual(data.get("memory"), [])

        code, out_pkt, err = self.cli("packet", "TASK-101")
        self.assertEqual(code, 0, out_pkt + err)
        self.assertIn("# Paquete de tarea Hefesto", out_pkt)

    def test_resume_redacts_secret_in_memory_note(self) -> None:
        self.setup_task_project()
        secret = "ghp_" + ("x" * 36)
        err_dir = self.home() / "memory" / "errores-resueltos"
        err_dir.mkdir(parents=True, exist_ok=True)
        note_file = err_dir / "auth_secret.md"
        note_file.write_text(
            f"---\nfecha: 2026-10-15\n---\n\nRegla de autenticacion auth: clave {secret} para API.\n",
            encoding="utf-8",
        )

        code, out, err = self.cli("resume")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("Regla de autenticacion", out)

        code, out_json, _ = self.cli("resume", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(secret, out_json)
        data = json.loads(out_json)
        self.assertTrue(any("[REDACTED]" in m for m in data.get("memory", [])))
        for m in data.get("memory", []):
            self.assertNotIn(secret, m)

    def test_resume_and_packet_redact_before_truncating_secret(self) -> None:
        proj = self.setup_task_project()
        secret = "ghp_" + ("x" * 36)
        err_dir = self.home() / "memory" / "errores-resueltos"
        err_dir.mkdir(parents=True, exist_ok=True)
        note_file = err_dir / "auth_secret.md"
        note_file.write_text(
            f"---\nfecha: 2026-10-15\n---\n\nRegla de autenticacion auth: clave {secret} para API.\n",
            encoding="utf-8",
        )

        settings = proj / ".hefesto" / "settings.json"
        config.set_setting(settings, ("memory", "max_chars"), 44)

        code, out, err = self.cli("resume")
        self.assertEqual(code, 0, out + err)

        code, out_json, _ = self.cli("resume", "--json")
        self.assertEqual(code, 0)

        code, out_pkt, _ = self.cli("packet", "TASK-101")
        self.assertEqual(code, 0)

        for k in range(9, len(secret) + 1):
            prefix = secret[:k]
            self.assertNotIn(prefix, out)
            self.assertNotIn(prefix, out_json)
            self.assertNotIn(prefix, out_pkt)


class MemoryPruneTests(MemoryCase):
    def test_prune_preserves_real_invalid_utf8_and_deletes_valid_old_note(self) -> None:
        store = memory.memory_dir() / "decisiones"
        store.mkdir(parents=True)
        bad = store / "bad.md"
        raw = b"---\nfecha: 2000-01-01\n---\n\nNota antigua: \xff\xfe\n"
        bad.write_bytes(raw)
        good = store / "good.md"
        good.write_text(memory.format_note({"fecha": "2000-01-01"}, "Nota antigua"), encoding="utf-8")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = memory.prune(yes=True)

        self.assertIn("1 notas eliminadas", result)
        self.assertIn("Aviso", err.getvalue())
        self.assertEqual(bad.read_bytes(), raw)
        self.assertFalse(good.exists())

    def test_prune_real_store_deletes_only_confirmed_proposals(self) -> None:
        store = memory.memory_dir() / "decisiones"
        store.mkdir(parents=True)
        contents = {
            "a.md": memory.format_note({"fecha": util.now_iso()[:10]}, "Regla reciente"),
            "b.md": memory.format_note({"fecha": util.now_iso()[:10]}, "Regla reciente"),
            "c.md": memory.format_note({"fecha": "2000-01-01"}, "Regla antigua conservada"),
            "d.md": memory.format_note({"fecha": "2000-01-01"}, "Regla antigua eliminada"),
        }
        for name, content in contents.items():
            (store / name).write_text(content, encoding="utf-8")
        candidate = memory.remember(None, "Regla reciente", scope="global")
        fallback = self.home() / "LEARNINGS.md"
        fallback.write_text("- 2000-01-01: Conservar fuera de memory.", encoding="utf-8")
        code, output, err = self.run_cli("memory", "prune", tty_value=True, answers=["s", "n", "s"])
        self.assertEqual(code, 0, err)
        self.assertIn("2 notas eliminadas", output)
        self.assertEqual({p.name for p in store.glob("*.md")}, {"a.md", "c.md"})
        for name in ("a.md", "c.md"):
            self.assertEqual((store / name).read_text(encoding="utf-8"), contents[name])
        self.assertTrue(candidate.exists())
        self.assertEqual(fallback.read_text(encoding="utf-8"), "- 2000-01-01: Conservar fuera de memory.")

    def test_prune_without_tty_fails_with_code_2(self) -> None:
        code, out, err = self.run_cli("memory", "prune", tty_value=False)
        self.assertEqual(code, 2)

    def test_prune_yes_with_hefesto_role_fails_with_code_2(self) -> None:
        os.environ["HEFESTO_ROLE"] = "runner"
        code, out, err = self.run_cli("memory", "prune", "--yes")
        self.assertEqual(code, 2)

    def test_prune_proposes_duplicate_and_deletes_when_confirmed(self) -> None:
        err_dir = self.home() / "memory" / "errores-resueltos"
        err_dir.mkdir(parents=True, exist_ok=True)
        n1 = err_dir / "n1.md"
        n2 = err_dir / "n2.md"
        content = "---\nfecha: 2026-10-15\n---\n\nEvitar bucles infinitos en recursion.\n"
        n1.write_text(content, encoding="utf-8")
        n2.write_text(content, encoding="utf-8")

        code, out, err = self.run_cli("memory", "prune", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Limpieza completada", out)

        self.assertTrue(n1.exists())
        self.assertFalse(n2.exists())

    def test_prune_never_deletes_outside_memory_store(self) -> None:
        root_learnings = self.home() / "LEARNINGS.md"
        root_learnings.parent.mkdir(parents=True, exist_ok=True)
        root_learnings.write_text("- 2020-01-01: Nota muy antigua fuera de memory.\n", encoding="utf-8")

        msg = memory.prune(None, yes=True)
        self.assertTrue(root_learnings.is_file())
        self.assertIn("Nota muy antigua fuera de memory", root_learnings.read_text(encoding="utf-8"))
