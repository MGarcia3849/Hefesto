"""Fase 1: config en capas, validate, init idempotente, doctor y hook PreToolUse."""

from __future__ import annotations

import json
import os

from hefesto import config, guard
from tests.helpers import HefestoTestCase, git


class ConfigTests(HefestoTestCase):
    def write_project_toml(self, text: str) -> None:
        path = self.project / ".hefesto" / "hefesto.toml"
        path.write_text(text, encoding="utf-8")

    def test_defaults_are_valid(self) -> None:
        cfg = config.load(None)
        self.assertEqual(config.validate(cfg), [])
        self.assertEqual(cfg.role_chain("runner")[0], "gemini")

    def test_rejects_broken_toml(self) -> None:
        self.init_project()
        self.write_project_toml("[limits\nmax_attempts = 3")
        code, out, err = self.cli("config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("TOML mal formado", err)

    def test_rejects_unknown_keys_bad_roles_and_negative_limits(self) -> None:
        self.init_project()
        self.write_project_toml(
            "[limits]\nmax_attempts = -1\nfoo = 1\n[roles.runner]\nuse = [\"openai\"]\n")
        code, out, _ = self.cli("config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("clave desconocida `limits.foo`", out)
        self.assertIn("proveedor desconocido `openai`", out)
        self.assertIn("no puede ser negativo", out)

    def test_local_enabled_requires_base_url(self) -> None:
        self.init_project()
        self.write_project_toml("[adapters.local]\nenabled = true\nmodel = \"m\"\n")
        code, out, _ = self.cli("config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("falta base_url", out)

    def test_settings_json_overrides_toml_and_toml_is_never_written(self) -> None:
        self.init_project()
        self.write_project_toml("[limits]\nmax_attempts = 2\n")
        before = (self.project / ".hefesto" / "hefesto.toml").read_bytes()
        config.set_setting(config.project_settings_path(self.project), ("limits", "max_attempts"), 5)
        cfg = config.load(self.project)
        self.assertEqual(cfg.get("limits", "max_attempts"), 5)
        self.assertEqual(cfg.origin("limits", "max_attempts"), "project-settings")
        self.assertEqual((self.project / ".hefesto" / "hefesto.toml").read_bytes(), before)
        code, out, _ = self.cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("limits.max_attempts = 5    # project-settings", out)


class InitTests(HefestoTestCase):
    def test_init_creates_structure_and_is_idempotent(self) -> None:
        self.init_project()
        hefesto_dir = self.project / ".hefesto"
        for rel in ("state.json", "settings.json", "ROADMAP.md", "DECISIONS.md", "LEARNINGS.md"):
            self.assertTrue((hefesto_dir / rel).exists(), rel)
        self.assertTrue((self.project / "AGENTS.md").exists())
        settings = json.loads((hefesto_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["approved_external_providers"], ["codex", "gemini"])
        (self.project / "AGENTS.md").write_text("mío", encoding="utf-8")
        gitignore_before = (self.project / ".gitignore").read_text(encoding="utf-8")
        code, out, err = self.run_cli("init", str(self.project), "--providers", "gemini,codex")
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.project / "AGENTS.md").read_text(encoding="utf-8"), "mío")
        self.assertEqual((self.project / ".gitignore").read_text(encoding="utf-8"), gitignore_before)
        self.assertEqual(gitignore_before.count(".hefesto/state.json"), 1)

    def test_init_with_providers_does_not_ask(self) -> None:
        self.project.mkdir()
        code, out, err = self.run_cli("init", str(self.project), "--providers", "gemini", tty_value=True,
                                      answers=["n", "n"])
        self.assertEqual(code, 0, err)
        settings = json.loads((self.project / ".hefesto" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["approved_external_providers"], ["gemini"])

    def test_init_asks_with_tty_default_yes(self) -> None:
        self.project.mkdir()
        code, out, err = self.run_cli("init", str(self.project), tty_value=True, answers=["", "n"])
        self.assertEqual(code, 0, err)
        settings = json.loads((self.project / ".hefesto" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["approved_external_providers"], ["gemini"])
        self.assertIn("Aviso: has rechazado codex", out)

    def test_init_rejects_local_provider(self) -> None:
        code, _, err = self.run_cli("init", str(self.project), "--providers", "local")
        self.assertEqual(code, 1)
        self.assertIn("hefesto local enable", err)

    def test_init_runs_git_init(self) -> None:
        self.init_project()
        self.assertTrue((self.project / ".git").exists())
        self.assertIn("inicial", git(self.project, "log", "--oneline"))

    def test_status_and_doctor(self) -> None:
        self.init_project()
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("hefesto: IDLE", out)
        code, out, _ = self.cli("doctor")
        self.assertIn("[ok] estado", out.replace("  ", " "))
        self.assertIn("allowlist", out)

    def test_doctor_warns_about_api_keys(self) -> None:
        os.environ["OPENAI_API_KEY"] = "x"
        try:
            _, out, _ = self.run_cli("doctor")
        finally:
            os.environ.pop("OPENAI_API_KEY")
        self.assertIn("OPENAI_API_KEY", out)


class HookTests(HefestoTestCase):
    def check(self, tool: str, **tool_input: str) -> str | None:
        return guard.check_tool_use({"tool_name": tool, "tool_input": tool_input})

    def test_blocks_dangerous_commands(self) -> None:
        for command in ("git push origin main", "git reset --hard HEAD~1", "git checkout -- .", "rm -rf build",
                        "git checkout main", "git switch -c feature/x", "cat .env", "claude --dangerously-skip-permissions"):
            self.assertIsNotNone(self.check("Bash", command=command), command)

    def test_allows_safe_commands(self) -> None:
        for command in ("git status", "git checkout hefesto/fase-1", "git checkout -- src/app.py", "pytest -q",
                        "hefesto done TASK-001"):
            self.assertIsNone(self.check("Bash", command=command), command)

    def test_blocks_protected_reads_and_state_edits(self) -> None:
        self.assertIsNotNone(self.check("Read", file_path="/repo/.env.local"))
        self.assertIsNotNone(self.check("Read", file_path="C:\\repo\\keys\\server.pem"))
        self.assertIsNotNone(self.check("Edit", file_path="/repo/.hefesto/state.json"))
        self.assertIsNone(self.check("Read", file_path="/repo/src/main.py"))

    def test_hook_command_exit_codes(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})
        code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 2)
        self.assertIn("git push", err)
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        code, _, _ = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 0)


class SecretTests(HefestoTestCase):
    def test_detects_and_redacts_secrets(self) -> None:
        samples = [
            "-----BEGIN RSA PRIVATE KEY-----",
            "AKIAABCDEFGHIJKLMNOP",
            "token = 'ghp_" + "a" * 36 + "'",
            "postgres://user:hunter22@db.example.com/app",
            'api_key = "abcdef123456"',
        ]
        for sample in samples:
            self.assertTrue(guard.find_secrets(sample), sample)
            self.assertIn("[REDACTED]", guard.redact(sample))
        self.assertEqual(guard.find_secrets("def suma(a, b):\n    return a + b\n"), [])
