"""Fase 5: protocolo único materializado para Claude, Codex y Gemini; `sync`, `prompt` y hooks equivalentes."""

from __future__ import annotations

import json
import tomllib

from hefesto import config, guard, sync
from tests.helpers import HefestoTestCase


def skill_body(text: str) -> str:
    return text.split(f"<!-- {sync.NOTICE} -->\n\n", 1)[1]


class SyncTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def read(self, rel: str) -> str:
        return (self.project / rel).read_text(encoding="utf-8")

    def test_same_protocol_steps_for_three_clis(self) -> None:
        for step in sync.STEPS:
            body = sync.render_prompt(step)
            self.assertIn(f"Hefesto · {step}", body)
            self.assertEqual(skill_body(self.read(f".claude/skills/hefesto-{step}/SKILL.md")), body)
            self.assertEqual(skill_body(self.read(f".agents/skills/hefesto-{step}/SKILL.md")), body)
            command = tomllib.loads(self.read(f".gemini/commands/hefesto/{step}.toml"))
            self.assertEqual(command["prompt"], body)
            self.assertTrue(command["description"])

    def test_prompt_command_prints_step(self) -> None:
        code, out, _ = self.cli("prompt", "next")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), sync.render_prompt("next").strip())

    def test_subagents_follow_config(self) -> None:
        reviewer = self.read(".claude/agents/hefesto-reviewer.md")
        self.assertIn("tools: Read, Grep, Glob\n", reviewer)
        self.assertIn("model: sonnet", reviewer)
        self.assertIn("model: sonnet", self.read(".claude/agents/hefesto-forge.md"))
        config.set_setting(config.project_settings_path(self.project), ("roles", "runner", "use"), ["claude:opus"])
        code, out, _ = self.cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("update", out)
        self.assertIn("model: opus", self.read(".claude/agents/hefesto-runner.md"))

    def test_hand_edits_are_conflicts_not_overwritten(self) -> None:
        path = self.project / ".claude" / "skills" / "hefesto-next" / "SKILL.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nmi cambio\n", encoding="utf-8")
        code, out, _ = self.cli("sync")
        self.assertEqual(code, 2)
        self.assertIn("conflict  .claude/skills/hefesto-next/SKILL.md", out)
        self.assertIn("mi cambio", path.read_text(encoding="utf-8"))

    def test_check_reports_pending_without_writing(self) -> None:
        self.assertEqual(self.cli("sync", "--check")[0], 0)
        target = self.project / ".agents" / "skills" / "hefesto-plan" / "SKILL.md"
        target.unlink()
        code, out, _ = self.cli("sync", "--check")
        self.assertEqual(code, 1)
        self.assertIn("create    .agents/skills/hefesto-plan/SKILL.md", out)
        self.assertFalse(target.exists())

    def test_existing_user_file_is_a_conflict(self) -> None:
        settings = self.project / ".gemini" / "settings.json"
        settings.write_text('{"theme": "mío"}\n', encoding="utf-8")
        (self.project / ".hefesto" / "generated.json").unlink()
        code, out, _ = self.cli("sync")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"theme": "mío"})

    def test_hooks_for_codex_and_gemini(self) -> None:
        codex = json.loads(self.read(".codex/hooks.json"))["hooks"]
        gemini = json.loads(self.read(".gemini/settings.json"))["hooks"]
        self.assertEqual(codex["SessionStart"][0]["hooks"][0]["command"], "hefesto status")
        self.assertEqual(codex["PreToolUse"][0]["hooks"][0]["command"], "hefesto hook pre-tool-use")
        self.assertEqual(gemini["BeforeTool"][0]["hooks"][0]["command"], "hefesto hook pre-tool-use")
        codex_cfg = tomllib.loads(self.read(".codex/config.toml"))
        self.assertEqual(codex_cfg["sandbox_mode"], "workspace-write")
        self.assertFalse(codex_cfg["sandbox_workspace_write"]["network_access"])


class OtherOrchestratorGuardTests(HefestoTestCase):
    def check(self, tool: str, **tool_input: object) -> str | None:
        return guard.check_tool_use({"tool_name": tool, "tool_input": tool_input})

    def test_gemini_tool_names(self) -> None:
        self.assertIsNotNone(self.check("run_shell_command", command="git push origin main"))
        self.assertIsNotNone(self.check("read_file", absolute_path="/p/.env"))
        self.assertIsNotNone(self.check("write_file", file_path="/p/.hefesto/state.json"))
        self.assertIsNotNone(self.check("read_many_files", paths=["src/a.py", "keys/id_rsa"]))
        self.assertIsNone(self.check("read_file", absolute_path="/p/src/a.py"))

    def test_codex_tool_names(self) -> None:
        self.assertIsNotNone(self.check("Bash", command=["bash", "-lc", "rm -rf build"]))
        patch = "*** Begin Patch\n*** Update File: config/.env\n@@\n-A=1\n+A=2\n*** End Patch"
        self.assertIsNotNone(self.check("apply_patch", command=patch))
        ok = "*** Begin Patch\n*** Add File: src/nuevo.py\n+x = 1\n*** End Patch"
        self.assertIsNone(self.check("apply_patch", command=ok))
