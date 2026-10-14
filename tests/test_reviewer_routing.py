"""Contrato (TASK-701 → TASK-702): `hefesto run reviewer` nunca se enruta a un proveedor autor del cambio.

Interfaz esperada en `hefesto/router.py`:
- Al delegar en el rol `reviewer` con una tarea en curso, los proveedores que figuran como autores de esa
  ejecución de la tarea (delegaciones con éxito de runner/forge/visual/core en `logs/runs.jsonl`) se saltan.
- El salto queda en `hops` con un `outcome` que contiene la palabra «autor».
- Si no queda ningún otro proveedor disponible, se usa igualmente el proveedor autor (la regla de
  independencia de `hefesto review` decide después si es aceptable).
"""

from __future__ import annotations

import json
import os

from hefesto import config
from hefesto.util import read_jsonl
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear a
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""


class ReviewerRoutingTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini")
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({"*": {"mode": "ok", "summary": "VEREDICTO: pass",
                                              "write": {"src/a.py": "a = 1\n"}}}), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(scenario)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.tmp / "calls.jsonl")
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        self.settings = config.project_settings_path(self.project)

    def run_role(self, role: str) -> dict[str, object]:
        code, out, err = self.cli("run", role, "instrucción")
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def test_reviewer_skips_the_author_provider(self) -> None:
        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["gemini", "codex", "claude:sonnet"])
        self.cli("start", "TASK-001")
        runner = self.run_role("runner")
        self.assertEqual(runner["provider"], "gemini")
        reviewer = self.run_role("reviewer")
        self.assertEqual(reviewer["provider"], "codex")
        skipped = [h for h in reviewer["hops"] if h["entry"] == "gemini"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("autor", skipped[0]["outcome"])
        reviewer_calls = [c for c in read_jsonl(self.tmp / "calls.jsonl")
                          if c["name"] == "gemini" and "Reviewer" in str(c["stdin"])]
        self.assertEqual(reviewer_calls, [])

    def test_author_is_used_when_no_other_reviewer_is_available(self) -> None:
        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["gemini"])
        self.cli("start", "TASK-001")
        self.run_role("runner")
        reviewer = self.run_role("reviewer")
        self.assertEqual(reviewer["provider"], "gemini")

    def test_without_task_in_progress_routing_is_unchanged(self) -> None:
        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["gemini", "codex"])
        reviewer = self.run_role("reviewer")
        self.assertEqual(reviewer["provider"], "gemini")
