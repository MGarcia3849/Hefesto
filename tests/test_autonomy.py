"""Fase 6: note, resume, handoff, preflight de cuota, loop con relevo automático, STOP y pausa."""

from __future__ import annotations

import datetime as dt
import json
import os

from hefesto import config, progress, quota, roadmap, state
from hefesto.util import read_jsonl
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear a
  - verify: `test -f src/a.py`
  - files: src/
  - role: runner
  - size: S
- [ ] TASK-002: Crear b
  - verify: `test -f src/b.py`
  - files: src/
  - role: runner
  - size: S
"""

WORKERS = {
    "gemini": {"mode": "ok", "summary": "implementado", "write": {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}},
    "codex": {"mode": "ok", "summary": "implementado", "write": {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}},
    "claude": {"mode": "ok", "summary": "VEREDICTO: pass"},
}


class AutonomyCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini")
        self.scenario_path = self.tmp / "scenario.json"
        self.calls_path = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario_path)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls_path)
        for key in ("HEFESTO_ORCHESTRATOR", "CLAUDECODE", "GEMINI_CLI"):
            os.environ.pop(key, None)
        self.scenario()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)

    def scenario(self, **extra: object) -> None:
        self.scenario_path.write_text(json.dumps({**WORKERS, **extra}), encoding="utf-8")

    def calls(self, name: str) -> list[dict[str, object]]:
        return [c for c in read_jsonl(self.calls_path) if c["name"] == name]

    def loop(self, *args: str) -> tuple[int, str]:
        code, out, err = self.cli("loop", *args)
        return code, out + err


class ProgressTests(AutonomyCase):
    def test_note_resume_and_handoff(self) -> None:
        self.cli("start", "TASK-001")
        code, out, _ = self.cli("run", "runner", "implementa")
        runner = json.loads(out)
        os.environ["HEFESTO_ORCHESTRATOR"] = "claude"
        self.cli("note", "runner hecho; falta revisión")
        code, out, _ = self.cli("resume")
        self.assertEqual(code, 0)
        self.assertIn("TASK-001", out)
        self.assertIn("[claude] runner hecho; falta revisión", out)
        self.assertIn(runner["run_id"], out)
        self.assertIn("src/", out)
        code, out, _ = self.cli("handoff", "--reason", "quota")
        self.assertEqual(code, 0)
        self.assertIn('codex "$hefesto-next"', out)
        self.assertEqual(quota.provider_state("claude")["status"], "cooldown")
        info = json.loads(self.cli("resume", "--json")[1])
        self.assertIn("relevo (quota) de claude a codex", info["notes"][-1]["text"])
        self.assertTrue((self.project / "src" / "a.py").exists())

    def test_orchestrator_detection(self) -> None:
        os.environ["GEMINI_CLI"] = "1"
        self.assertEqual(progress.current_orchestrator(), "gemini")
        os.environ["HEFESTO_ORCHESTRATOR"] = "codex"
        self.assertEqual(progress.current_orchestrator(), "codex")


class PreflightTests(AutonomyCase):
    def test_check_without_probe_makes_no_calls(self) -> None:
        code, out, _ = self.cli("providers", "check")
        self.assertEqual(code, 0)
        self.assertIn("unknown", out)
        self.assertEqual(read_jsonl(self.calls_path), [])

    def test_probe_flag_and_cache(self) -> None:
        self.cli("providers", "check", "--probe")
        self.assertEqual(len(self.calls("gemini")), 1)
        config.set_setting(config.project_settings_path(self.project), ("quota", "probe"), True)
        self.cli("run", "runner", "x")
        self.cli("run", "runner", "x")
        gemini_calls = self.calls("gemini")
        self.assertEqual(len(gemini_calls), 3)  # 1 sonda manual + 2 delegaciones; la caché evita otra sonda
        self.assertTrue(all("Responde únicamente" not in str(c["stdin"]) for c in gemini_calls[1:]))

    def test_probe_detects_quota_and_skips(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("quota", "probe"), True)
        self.scenario(gemini={"mode": "quota"})
        code, out, _ = self.cli("run", "runner", "x")
        self.assertEqual(json.loads(out)["provider"], "codex")
        self.assertEqual(len(self.calls("gemini")), 1)
        self.assertEqual(quota.provider_state("gemini", model="default")["status"], "cooldown")

    def test_provider_in_cooldown_gets_no_calls(self) -> None:
        quota.mark_cooldown("gemini", dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1), "quota", "x")
        self.cli("providers", "check", "--probe")
        self.assertEqual(self.calls("gemini"), [])


class LoopTests(AutonomyCase):
    def complete_with(self, name: str) -> None:
        self.scenario(**{f"{name}@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", name, "--max-tasks", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("tareas completadas: 1", out)
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, "x")
        self.assertTrue(any("Modo autónomo" in str(c["stdin"]) for c in self.calls(name)))

    def test_claude_orchestrator_completes_a_task(self) -> None:
        self.complete_with("claude")

    def test_codex_orchestrator_completes_a_task(self) -> None:
        self.complete_with("codex")

    def test_gemini_orchestrator_completes_a_task(self) -> None:
        self.complete_with("gemini")

    def test_auto_hands_off_when_orchestrator_runs_out_of_quota(self) -> None:
        self.scenario(**{"claude@orchestrator": {"mode": "orchestrate", "quota_after": "runner"},
                         "codex@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", "auto", "--max-tasks", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, "x")
        runner_calls = [c for c in self.calls("gemini") if "Eres el **Runner**" in str(c["stdin"])]
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(quota.provider_state("claude", model="opus")["status"], "cooldown")
        ev = json.loads((self.project / ".hefesto" / "evidence" / "TASK-001.json").read_text(encoding="utf-8"))
        self.assertEqual(ev["attempts"], 1)
        notes = [n["text"] for n in read_jsonl(progress.progress_path(self.project))]
        self.assertTrue(any("relevo automático: claude" in n for n in notes))

    def test_resume_after_manual_handoff_does_not_repeat_delegations(self) -> None:
        self.cli("start", "TASK-001")
        self.cli("run", "runner", "implementa")
        self.cli("note", "runner hecho")
        self.cli("handoff", "--to", "gemini", "--from", "claude", "--reason", "quota")
        self.scenario(**{"gemini@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", "gemini", "--max-tasks", "1")
        self.assertEqual(code, 0, out)
        runner_calls = [c for c in self.calls("gemini") if "Eres el **Runner**" in str(c["stdin"])]
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, "x")

    def test_max_tasks_and_stop(self) -> None:
        self.scenario(**{"claude@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", "claude", "--max-tasks", "1")
        self.assertEqual(code, 0, out)
        rm = roadmap.load_valid(self.project)
        self.assertEqual((rm.get("TASK-001").marker, rm.get("TASK-002").marker), ("x", " "))
        self.assertEqual(self.cli("stop")[0], 0)
        before = len(self.calls("claude"))
        code, out = self.loop("--orchestrator", "claude")
        self.assertEqual(code, 0)
        self.assertIn("STOP", out)
        self.assertEqual(len(self.calls("claude")), before)

    def test_pauses_when_role_has_no_quota_without_blocking(self) -> None:
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        for name in ("gemini", "codex", "claude"):
            quota.mark_cooldown(name, later, "quota", "x")
        code, out = self.loop("--orchestrator", "auto")
        self.assertEqual(code, 3)
        self.assertIn("Pausa", out)
        data = state.load(self.project)
        self.assertEqual((data["status"], data["attempts"]), ("IDLE", 0))

    def test_wait_resumes_after_cooldown(self) -> None:
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        for name in ("gemini", "codex", "claude"):
            quota.mark_cooldown(name, soon, "quota", "x")
        self.scenario(**{"claude@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", "claude", "--max-tasks", "1", "--wait")
        self.assertEqual(code, 0, out)
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, "x")

    def test_loop_respects_call_caps(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("quota", "max_calls_per_provider_per_loop"),
                           {"claude": 1, "codex": 0, "gemini": 0})
        self.scenario(**{"claude@orchestrator": {"mode": "orchestrate"}})
        code, out = self.loop("--orchestrator", "claude", "--max-tasks", "2")
        self.assertEqual(code, 3, out)
        self.assertEqual(quota.load()["loop"]["id"], None)
