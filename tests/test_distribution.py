"""Contrato (TASK-701 → TASK-703/704): distribución de Hefesto (sección 11.3 de HEFESTO_V2_1.md).

Interfaz esperada en `hefesto/distribution.py` (solo librería estándar):

- `detect_mode(path: Path) -> str` → `"create"` | `"continue"` | `"improve"`.
  `continue` si existe `path/.hefesto/`; `create` si no hay ficheros de código (se ignoran `.git/`,
  `README*`, `LICENSE*`, `.gitignore`, `.gitattributes` y ficheros ocultos); si no, `improve`.
- `detect_stack(path: Path) -> dict` con claves `stacks` (lista ordenada), `test`, `lint`, `build`
  (texto o `None`). Señales: `pubspec.yaml` → flutter (`flutter test`, `flutter analyze`, `flutter build`);
  `package.json` → node (`npm test`/`npm run lint`/`npm run build`, solo si el script existe);
  `pyproject.toml`, `setup.py` o `requirements.txt` → python (`python -m pytest`); `Cargo.toml` → rust
  (`cargo test`, `cargo clippy`, `cargo build`); `go.mod` → go (`go test ./...`, `go vet ./...`, `go build ./...`).
- `available_tags(root: Path) -> list[str]`: tags `vX.Y.Z[-sufijo]` del repositorio de Hefesto en `root`, tras
  `git fetch --tags` de su `origin`, ordenados de menor a mayor versión.
- `self_update(root: Path, to: str | None = None, project: Path | None = None) -> str`: exige TTY
  (`util.require_tty`, código 2), rechaza una copia con cambios sin commitear (código 4), imprime el
  CHANGELOG.md de la versión destino **antes** de pedir confirmación (`util.confirm`) y solo entonces hace
  checkout del tag y escribe `hefesto_home()/VERSION`. No toca nada de `hefesto_home()` salvo `VERSION`
  (configuración global, `settings.json`, memoria). Si se pasa `project`, ejecuta `sync` en él al terminar.
- `migrate(project: Path) -> str`: si `settings.json` tiene una `hefesto_version` distinta de la actual,
  copia `.hefesto/settings.json`, `ROADMAP.md`, `DECISIONS.md` y `LEARNINGS.md` a
  `.hefesto/backups/<marca-de-tiempo>/`, aplica las migraciones pendientes, fija `hefesto_version` y ejecuta
  `sync`. Si ya está al día, no hace nada (ni copia de seguridad).

Nota: `tests/helpers.py::HefestoTestCase.init_project` inicializa en modo `--shared` (lo adapta TASK-706 al
introducir el modo local), porque estos tests hacen commits de ficheros de `.hefesto/`.

CLI: `hefesto bootstrap [dir] [--providers LISTA]`, `hefesto adopt [dir] [--providers LISTA]`,
`hefesto self-update [--to TAG]`, `hefesto migrate`. `doctor` avisa de que hace falta `hefesto migrate`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from hefesto import __version__, config, distribution, guard, util
from hefesto.util import HefestoError
from tests.helpers import GIT_ENV, ROOT, HefestoTestCase, git, tty

HEFESTO_GENERATED = (".hefesto", ".claude", ".agents", ".codex", ".gemini", ".git")
HEFESTO_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".gitignore")


def tree_hashes(path: Path) -> dict[str, str]:
    """Hash de todos los ficheros que no son de Hefesto (el código del usuario)."""
    out: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path).as_posix()
        if item.is_dir() or rel.split("/")[0] in HEFESTO_GENERATED or rel in HEFESTO_FILES:
            continue
        out[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
    return out


def write(path: Path, rel: str, text: str = "x\n") -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class ModeTests(HefestoTestCase):
    def test_empty_or_docs_only_is_create(self) -> None:
        self.project.mkdir()
        self.assertEqual(distribution.detect_mode(self.project), "create")
        git(self.project, "init", "-q")
        for name in ("README.md", "LICENSE", ".gitignore"):
            write(self.project, name)
        self.assertEqual(distribution.detect_mode(self.project), "create")

    def test_code_without_hefesto_is_improve(self) -> None:
        write(self.project, "src/main.py", "print(1)\n")
        self.assertEqual(distribution.detect_mode(self.project), "improve")

    def test_existing_hefesto_is_continue(self) -> None:
        self.init_project()
        write(self.project, "src/main.py", "print(1)\n")
        self.assertEqual(distribution.detect_mode(self.project), "continue")


class StackTests(HefestoTestCase):
    def test_node_uses_existing_scripts_only(self) -> None:
        write(self.project, "package.json", json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}))
        stack = distribution.detect_stack(self.project)
        self.assertEqual(stack["stacks"], ["node"])
        self.assertEqual((stack["test"], stack["lint"], stack["build"]), ("npm test", None, "npm run build"))

    def test_flutter_python_rust_go(self) -> None:
        cases = {
            "pubspec.yaml": ("flutter", "flutter test"),
            "pyproject.toml": ("python", "python -m pytest"),
            "Cargo.toml": ("rust", "cargo test"),
            "go.mod": ("go", "go test ./..."),
        }
        for marker, (name, test_cmd) in cases.items():
            with self.subTest(marker=marker):
                folder = self.tmp / name
                write(folder, marker)
                stack = distribution.detect_stack(folder)
                self.assertEqual(stack["stacks"], [name])
                self.assertEqual(stack["test"], test_cmd)

    def test_unknown_stack(self) -> None:
        self.project.mkdir()
        stack = distribution.detect_stack(self.project)
        self.assertEqual(stack, {"stacks": [], "test": None, "lint": None, "build": None})


class BootstrapTests(HefestoTestCase):
    def test_without_tty_only_reports(self) -> None:
        self.project.mkdir()
        code, out, _ = self.run_cli("bootstrap", str(self.project))
        self.assertEqual(code, 0)
        self.assertIn("crear", out.lower())
        self.assertFalse((self.project / ".hefesto").exists())

    def test_create_runs_init_after_confirmation(self) -> None:
        self.project.mkdir()
        code, out, err = self.run_cli("bootstrap", str(self.project), "--providers", "gemini,codex",
                                      tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.project / ".hefesto" / "state.json").exists())
        self.assertIn("hefesto-plan", out)

    def test_create_declined_does_nothing(self) -> None:
        self.project.mkdir()
        code, _, _ = self.run_cli("bootstrap", str(self.project), tty_value=True, answers=["n"])
        self.assertEqual(code, 0)
        self.assertFalse((self.project / ".hefesto").exists())

    def test_continue_shows_resume(self) -> None:
        self.init_project()
        code, out, _ = self.run_cli("bootstrap", str(self.project), tty_value=True, answers=["s"])
        self.assertEqual(code, 0)
        self.assertIn("continuar", out.lower())
        self.assertIn("hefesto resume", out)

    def test_improve_runs_adopt(self) -> None:
        write(self.project, "src/app.py", "print('hola')\n")
        before = tree_hashes(self.project)
        code, out, err = self.run_cli("bootstrap", str(self.project), "--providers", "gemini",
                                      tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("mejorar", out.lower())
        self.assertTrue((self.project / ".hefesto").exists())
        self.assertEqual(tree_hashes(self.project), before)


class AdoptTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        write(self.project, "package.json", json.dumps({"name": "app", "scripts": {"test": "jest", "lint": "eslint ."}}))
        write(self.project, "src/index.js", "module.exports = 1;\n")
        write(self.project, "README.md", "# App\n\nUsamos PostgreSQL y desplegamos en Fly.io.\n")
        git(self.project, "init", "-q")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "inicial")

    def test_adopt_never_modifies_code(self) -> None:
        before = tree_hashes(self.project)
        head = git(self.project, "rev-parse", "HEAD")
        code, out, err = self.run_cli("adopt", str(self.project), "--providers", "gemini")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tree_hashes(self.project), before)
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), head)

    def test_adopt_records_stack_and_proposes_roadmap(self) -> None:
        self.run_cli("adopt", str(self.project), "--providers", "gemini")
        decisions = (self.project / ".hefesto" / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn("node", decisions)
        self.assertIn("npm test", decisions)
        self.assertIn("npm run lint", decisions)
        code, out, _ = self.run_cli("-C", str(self.project), "next")
        self.assertEqual(code, 0, out)
        roadmap = (self.project / ".hefesto" / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertIn("verify: `npm test`", roadmap)
        self.assertFalse((self.project / ".hefesto" / "trust.json").exists())


class SelfUpdateTests(HefestoTestCase):
    """Un repositorio git local hace de «GitHub»; `root` es la copia instalada de Hefesto."""

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q")
        write(self.origin, "CHANGELOG.md", "# Changelog\n\n## v1.0.0\n- inicial\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "v1.0.0")
        git(self.origin, "tag", "v1.0.0")
        self.root = self.tmp / "instalado"
        subprocess.run(["git", "clone", "-q", "--branch", "v1.0.0", str(self.origin), str(self.root)],
                       check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))
        write(self.origin, "CHANGELOG.md", "# Changelog\n\n## v1.1.0\n- novedad importante\n\n## v1.0.0\n- inicial\n")
        git(self.origin, "commit", "-q", "-am", "v1.1.0")
        git(self.origin, "tag", "v1.1.0")

    def test_available_tags_sorted(self) -> None:
        self.assertEqual(distribution.available_tags(self.root), ["v1.0.0", "v1.1.0"])

    def test_without_tty_nothing_changes(self) -> None:
        head = git(self.root, "rev-parse", "HEAD")
        with tty(False):
            with self.assertRaises(HefestoError) as ctx:
                distribution.self_update(self.root)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

    def test_declined_confirmation_keeps_version(self) -> None:
        head = git(self.root, "rev-parse", "HEAD")
        with tty(True, ["n"]):
            distribution.self_update(self.root)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

    def test_confirmed_update_checks_out_tag_and_writes_version(self) -> None:
        home = util.hefesto_home()
        write(home, "hefesto.toml", "[limits]\nmax_attempts = 2\n")
        write(home, "settings.json", '{"config": {}}\n')
        write(home, "memory/preferencias/estilo.md", "prefiero tests pequeños\n")
        kept = {rel: (home / rel).read_bytes() for rel in ("hefesto.toml", "settings.json",
                                                            "memory/preferencias/estilo.md")}
        seen_before_confirm: list[str] = []

        def confirm(question: str, default: bool = False) -> bool:
            seen_before_confirm.append(printed.getvalue())
            return True

        import contextlib
        import io
        from unittest import mock

        printed = io.StringIO()
        with tty(True), mock.patch("hefesto.util.confirm", side_effect=confirm), contextlib.redirect_stdout(printed):
            message = distribution.self_update(self.root)
        self.assertIn("v1.1.0", message)
        self.assertIn("novedad importante", seen_before_confirm[0])
        self.assertEqual(git(self.root, "describe", "--tags", "--exact-match"), "v1.1.0")
        self.assertEqual((home / "VERSION").read_text(encoding="utf-8").strip(), "v1.1.0")
        for rel, content in kept.items():
            self.assertEqual((home / rel).read_bytes(), content, rel)

    def test_runs_sync_in_the_current_project(self) -> None:
        self.init_project()
        generated = self.project / ".claude" / "skills" / "hefesto-next" / "SKILL.md"
        generated.unlink()
        with tty(True, ["s"]):
            distribution.self_update(self.root, project=self.project)
        self.assertTrue(generated.exists())

    def test_explicit_target_and_dirty_copy(self) -> None:
        write(self.root, "CHANGELOG.md", "editado a mano\n")
        with tty(True, ["s"]):
            with self.assertRaises(HefestoError) as ctx:
                distribution.self_update(self.root, to="v1.1.0")
        self.assertEqual(ctx.exception.code, 4)


class MigrateTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        path = config.project_settings_path(self.project)
        data = config.read_settings(path)
        data["hefesto_version"] = "2.0.0"
        config.write_settings(path, data)
        self.originals: dict[str, str] = {}
        for name in ("ROADMAP.md", "DECISIONS.md", "LEARNINGS.md"):
            target = self.project / ".hefesto" / name
            target.write_text(f"# {name}\n\ncontenido previo de {name}\n", encoding="utf-8")
            self.originals[name] = target.read_text(encoding="utf-8")

    def test_doctor_warns_and_migrate_backs_up(self) -> None:
        _, out, _ = self.cli("doctor")
        self.assertIn("hefesto migrate", out)
        code, out, err = self.cli("migrate")
        self.assertEqual(code, 0, out + err)
        settings = config.read_settings(config.project_settings_path(self.project))
        self.assertEqual(settings["hefesto_version"], __version__)
        backups = list((self.project / ".hefesto" / "backups").glob("*/settings.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["hefesto_version"], "2.0.0")
        for name, content in self.originals.items():
            self.assertEqual((backups[0].parent / name).read_text(encoding="utf-8"), content, name)

    def test_migrate_runs_sync(self) -> None:
        generated = self.project / ".claude" / "skills" / "hefesto-next" / "SKILL.md"
        generated.unlink()
        self.assertEqual(self.cli("migrate")[0], 0)
        self.assertTrue(generated.exists())

    def test_up_to_date_does_nothing(self) -> None:
        self.cli("migrate")
        code, out, _ = self.cli("migrate")
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.project / ".hefesto" / "backups").iterdir())), 1)


class InstallTests(HefestoTestCase):
    def test_install_from_git_clone_is_idempotent(self) -> None:
        # «GitHub» local construido con la copia de trabajo actual (incluye cambios aún sin commitear).
        origin = self.tmp / "origin"
        shutil.copytree(ROOT, origin, ignore=shutil.ignore_patterns(
            ".git", ".hefesto", "__pycache__", "graphify-out", *guard.DEFAULT_PROTECTED))
        git(origin, "init", "-q")
        git(origin, "add", "-A")
        git(origin, "commit", "-q", "-m", "release")
        git(origin, "tag", "v9.9.9")
        clone = self.tmp / "clon"
        subprocess.run(["git", "clone", "-q", "--branch", "v9.9.9", str(origin), str(clone)], check=True,
                       capture_output=True, env=dict(os.environ, **GIT_ENV))
        bash = util.find_bash()
        self.assertIsNotNone(bash)
        env = dict(os.environ, HEFESTO_BIN_DIR=str(self.tmp / "bin"), HEFESTO_HOME=str(self.tmp / "home"))
        for _ in range(2):
            result = subprocess.run([str(bash), "install.sh"], cwd=clone, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.tmp / "home" / "VERSION").read_text(encoding="utf-8").strip(), "v9.9.9")
        launcher = self.tmp / "bin" / "hefesto"
        result = subprocess.run([str(bash), str(launcher), "--version"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hefesto", result.stdout)
        shutil.rmtree(clone, ignore_errors=True)
