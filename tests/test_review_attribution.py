"""Tests para la atribución de review --run-id al registro con éxito (TASK-712)."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from hefesto import evidence, state
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear suma
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""


class ReviewAttributionTests(HefestoTestCase):
    providers = "gemini,codex"

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.scenario_path = self.tmp / "scenario.json"
        self.calls_path = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario_path)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls_path)
        self.init_project(self.providers)
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def scenario(self, **entries: object) -> None:
        self.scenario_path.write_text(json.dumps(entries), encoding="utf-8")

    def run_json(self, role: str = "runner", *extra: str) -> tuple[int, dict[str, object]]:
        code, out, err = self.cli("run", role, "instrucción", *extra)
        return code, (json.loads(out) if out.strip().startswith("{") else {"stderr": err, "stdout": out})

    def test_reviewer_fallback_attributes_to_successful_provider(self) -> None:
        """(a) Reviewer con fallback: registra al proveedor que respondió con éxito y en evidencia."""
        self.cli("start", "TASK-001")
        self.scenario(
            gemini={"mode": "ok", "write": {"src/a.py": "a = 1\n"}},
            claude={"mode": "quota"},
            codex={"mode": "ok", "summary": "VEREDICTO: pass\nbuen código"},
        )
        code, runner = self.run_json("runner")
        self.assertEqual(code, 0, runner)
        self.assertEqual(runner["provider"], "gemini")

        code, reviewer = self.run_json("reviewer")
        self.assertEqual(code, 0, reviewer)
        self.assertEqual(reviewer["provider"], "codex")

        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--run-id", str(reviewer["run_id"]))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(state.load(self.project)["review"]["reviewer"], "codex:default")

        code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 0, out + err)

        ev = evidence.show(self.project, "TASK-001")
        self.assertEqual(ev["review"]["reviewer"], "codex:default")

    def test_reviewer_run_all_failed_rejected(self) -> None:
        """(b) Run de reviewer en el que todos fallan: review sale con 1."""
        self.cli("start", "TASK-001")
        self.scenario(**{"*": {"mode": "rate_limit"}})
        code, reviewer = self.run_json("reviewer")
        self.assertEqual(code, 3)
        run_id = str(reviewer.get("run_id"))
        self.assertTrue(run_id)

        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--run-id", run_id)
        self.assertEqual(code, 1)
        self.assertIn("ningún registro con éxito", err + out)


if __name__ == "__main__":
    unittest.main()
