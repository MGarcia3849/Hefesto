"""Tests de política de packs y protección de settings.json (TASK-805).

(1) .hefesto/settings.json protegido frente a herramientas de edición y Bash.
(2) Fallo en cerrado de la política de packs ante settings/catálogo corrupto o regex inválida.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

from hefesto import config, packs, util
from tests.helpers import HefestoTestCase, git

TEST_CATALOG_WITH_INVALID_REGEX = """
[packs.broken]
kind = "tool"
source = "acme/broken"
license = "MIT"
default = "off"
extra_cost = false
requires = []
binary = "broken"
version = "1.0.0"
install = "install-broken"
policy_block = ["(?invalid regex", "--forbidden-flag"]

[packs.valid]
kind = "tool"
source = "acme/valid"
license = "MIT"
default = "off"
extra_cost = false
requires = []
binary = "valid"
version = "1.0.0"
install = "install-valid"
policy_block = ["\\\\bvalid\\\\s+block\\\\b"]
"""


class PacksPolicyTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]

    def test_settings_json_blocked_from_edit_and_write_tools(self) -> None:
        """Herramientas de edición no pueden modificar .hefesto/settings.json."""
        for tool, key in (("Edit", "file_path"), ("Write", "path"), ("MultiEdit", "file_path")):
            payload = json.dumps({
                "tool_name": tool,
                "cwd": str(self.project),
                "tool_input": {key: ".hefesto/settings.json"},
            })
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
            self.assertEqual(code, 2, f"{tool} debió ser bloqueada")
            self.assertIn(".hefesto/settings.json", err)
            self.assertIn("solo lo modifica la CLI", err)

    def test_settings_json_blocked_from_apply_patch(self) -> None:
        """ApplyPatch no puede modificar .hefesto/settings.json."""
        patch = "*** Update File: .hefesto/settings.json\n--- a\n+++ b\n"
        payload = json.dumps({
            "tool_name": "ApplyPatch",
            "cwd": str(self.project),
            "tool_input": {"patch": patch},
        })
        code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 2)
        self.assertIn(".hefesto/settings.json", err)

    def test_settings_json_blocked_from_bash_writes(self) -> None:
        """Comandos Bash que escriben o eliminan .hefesto/settings.json se bloquean."""
        for cmd in (
            "echo '{}' > .hefesto/settings.json",
            "rm .hefesto/settings.json",
            "sed -i 's/a/b/' .hefesto/settings.json",
            "cp /tmp/settings.json .hefesto/settings.json",
        ):
            payload = json.dumps({
                "tool_name": "Bash",
                "cwd": str(self.project),
                "tool_input": {"command": cmd},
            })
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
            self.assertEqual(code, 2, f"Comando '{cmd}' debió ser bloqueado")
            self.assertIn(".hefesto/settings.json", err)

        # Invocación directa de la CLI de Hefesto está permitida
        plain_payload = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "hefesto pack list"},
        })
        self.assertEqual(self.run_cli("hook", "pre-tool-use", stdin=plain_payload)[0], 0)

    def test_cli_can_still_write_settings_normally(self) -> None:
        """La CLI y funciones internas pueden seguir escribiendo settings.json."""
        settings_path = config.project_settings_path(self.project)
        settings = config.read_settings(settings_path)
        settings["custom_key"] = "custom_val"
        config.write_settings(settings_path, settings)
        self.assertEqual(config.read_settings(settings_path).get("custom_key"), "custom_val")

    def test_fail_closed_when_settings_corrupted(self) -> None:
        """Si settings.json está corrupto, bloquea comandos de cualquier policy_block del catálogo."""
        settings_path = config.project_settings_path(self.project)
        settings_path.write_text("{corrupt json", encoding="utf-8")

        # video pack policy_block contiene 'cloud render'
        payload = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "npx hyperframes cloud render intro"},
        })
        code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 2)
        self.assertIn("video", err)
        self.assertIn("aviso: no se pudo leer settings.json", err)

        # Un comando normal no coincidente pasa, pero emite el aviso
        safe_payload = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "echo safe"},
        })
        code, _, err = self.run_cli("hook", "pre-tool-use", stdin=safe_payload)
        self.assertEqual(code, 0)
        self.assertIn("aviso: no se pudo leer settings.json", err)

    def test_fail_closed_when_settings_missing(self) -> None:
        """Si settings.json no existe en el proyecto, falla en cerrado."""
        settings_path = config.project_settings_path(self.project)
        if settings_path.exists():
            settings_path.unlink()

        payload = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "npx hyperframes cloud render intro"},
        })
        code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 2)
        self.assertIn("video", err)
        self.assertIn("aviso: no se pudo leer settings.json", err)

    def test_fail_closed_when_policy_block_regex_invalid(self) -> None:
        """Si alguna regex de policy_block es inválida, falla en cerrado con las válidas."""
        cat_file = self.tmp / "broken_catalog.toml"
        cat_file.write_text(TEST_CATALOG_WITH_INVALID_REGEX, encoding="utf-8")

        payload_valid_match = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "valid block execution"},
        })
        payload_forbidden_flag = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "my-tool --forbidden-flag"},
        })

        with mock.patch("hefesto.packs.CATALOG_PATH", cat_file):
            # Sin packs activos, pero regex rota en catálogo -> falla en cerrado
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload_valid_match)
            self.assertEqual(code, 2)
            self.assertIn("valid", err)
            self.assertIn("aviso: expresión de policy_block inválida", err)

            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload_forbidden_flag)
            self.assertEqual(code, 2)
            self.assertIn("broken", err)

            # (c) Una regex inválida de un pack activo bloquea cualquier comando
            settings_path = config.project_settings_path(self.project)
            settings = config.read_settings(settings_path)
            settings.setdefault("packs", {})["broken"] = {"enabled": True}
            config.write_settings(settings_path, settings)

            safe_payload = json.dumps({
                "tool_name": "Bash",
                "cwd": str(self.project),
                "tool_input": {"command": "echo safe_command"},
            })
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=safe_payload)
            self.assertEqual(code, 2)
            self.assertIn("broken", err)
            self.assertIn("expresión regular inválida", err)

    def test_catalog_unreadable_lets_command_pass_with_stderr_warning(self) -> None:
        """Si el catálogo no se puede leer, deja pasar sin packs activos y bloquea con pack activo."""
        bad_cat = self.tmp / "no_existe.toml"
        payload = json.dumps({
            "tool_name": "Bash",
            "cwd": str(self.project),
            "tool_input": {"command": "npx hyperframes cloud render intro"},
        })
        with mock.patch("hefesto.packs.CATALOG_PATH", bad_cat):
            # Caso 1: sin packs activos, deja pasar con aviso en stderr
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
            self.assertEqual(code, 0)
            self.assertIn("aviso: no se pudo leer el catálogo de packs", err)

            # Caso 2: con pack activo, bloquea con código 2 y mensaje claro
            settings_path = config.project_settings_path(self.project)
            settings = config.read_settings(settings_path)
            settings.setdefault("packs", {})["video"] = {"enabled": True}
            config.write_settings(settings_path, settings)

            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
            self.assertEqual(code, 2)
            self.assertIn("aviso: no se pudo leer el catálogo de packs", err)
            self.assertIn("no se puede verificar la política", err)

    def test_pack_enable_and_disable_print_sync_summary(self) -> None:
        """pack enable y pack disable imprimen el resumen de sync."""
        self.use_fakes("graphify")
        code, out, _ = self.here("pack", "enable", "context", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("ficheros gestionados", out)

        code, out, _ = self.cli("pack", "disable", "context")
        self.assertEqual(code, 0)
        self.assertIn("ficheros gestionados", out)

    def test_pack_enable_and_disable_exit_2_on_sync_conflicts(self) -> None:
        """pack enable y disable devuelven código 2 si sync reporta conflictos."""
        self.use_fakes("graphify")
        plan_file = self.project / ".claude" / "skills" / "hefesto-plan" / "SKILL.md"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text("modificación manual para forzar conflicto\n", encoding="utf-8")

        manifest_path = self.project / ".hefesto" / "generated.json"
        manifest = util.read_json(manifest_path, default={"files": {}})
        manifest["files"][".claude/skills/hefesto-plan/SKILL.md"] = "old_hash_distinto"
        util.atomic_write_json(manifest_path, manifest)

        code, out, _ = self.here("pack", "enable", "context", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Conflictos", out)

        code, out, _ = self.cli("pack", "disable", "context")
        self.assertEqual(code, 2)
        self.assertIn("Conflictos", out)

    def test_sync_empty_dir_cleanup_boundaries(self) -> None:
        """La limpieza de carpetas vacías no borra las raíces de skills ni agentes."""
        skills_root = self.project / ".claude" / "skills"
        agents_root = self.project / ".claude" / "agents"
        self.assertTrue(skills_root.is_dir())
        self.assertTrue(agents_root.is_dir())

        # Creación de skill simulada gestionada
        sub = skills_root / "test-skill"
        sub.mkdir(parents=True, exist_ok=True)
        test_file = sub / "SKILL.md"
        test_file.write_text("---\nname: test-skill\n---\n", encoding="utf-8")

        manifest_path = self.project / ".hefesto" / "generated.json"
        manifest = util.read_json(manifest_path, default={"files": {}})
        h = hashlib.sha256(test_file.read_bytes()).hexdigest()
        manifest["files"][".claude/skills/test-skill/SKILL.md"] = h
        util.atomic_write_json(manifest_path, manifest)

        # Ejecución de sync: detecta que test-skill/SKILL.md ya no es target y lo elimina
        from hefesto import sync
        sync.run(self.project)

        # La subcarpeta fue eliminada porque quedó vacía
        self.assertFalse(sub.exists())
        # Pero las raíces NUNCA se eliminan
        self.assertTrue(skills_root.is_dir())
        self.assertTrue(agents_root.is_dir())
