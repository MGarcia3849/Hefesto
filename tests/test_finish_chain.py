"""Integración única de fases abiertas contenidas en una rama posterior."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from hefesto import approval, config, evidence, gitops, security, state, util
from tests.helpers import HefestoTestCase, git


class FinishChainTests(HefestoTestCase):
    def prepare(self, count: int = 2, isolation: str = "branch", pending: bool = False,
                kinds: tuple[str, ...] = ()) -> None:
        self.init_project()
        config.set_setting(config.project_settings_path(self.project), ("execution", "isolation"), isolation)
        config.set_setting(config.project_settings_path(self.project), ("execution", "test_command"),
                           "python src/check.py")
        sections = ["# ROADMAP\n"]
        for index in range(1, count + 1):
            verify = "python src/check.py" if index == 1 else f"test -f src/phase{index}.txt"
            sections.append(f"\n## Fase {index} — Cadena {index}\n"
                            f"- [ ] TASK-00{index}: Paso {index}\n"
                            f"  - verify: `{verify}`\n  - files: src/\n  - role: runner\n  - size: S\n")
            if kinds:
                sections.append(f"  - kind: {kinds[index - 1]}\n")
            if index == 1 and pending:
                sections.append("- [ ] TASK-099: Pendiente\n  - verify: `true`\n"
                                "  - files: src/\n  - role: runner\n  - size: S\n")
        (self.project / ".hefesto" / "ROADMAP.md").write_text("".join(sections), encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "preparar cadena")
        self.main = gitops.main_branch(self.project, config.load(self.project))
        self.main_before = git(self.project, "rev-parse", self.main)
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        for index in range(1, count + 1):
            task = f"TASK-00{index}"
            code, out, err = self.cli("start", task)
            self.assertEqual(code, 0, out + err)
            workdir = Path(state.load(self.project)["workdir"])
            (workdir / "src").mkdir(exist_ok=True)
            if index == 1 or index == count:
                (workdir / "src/check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            if index > 1:
                (workdir / f"src/phase{index}.txt").write_text("hecho\n", encoding="utf-8")
            code, out, err = self.cli("review", task, "--verdict", "pass", "--author", "gemini",
                                      "--reviewer", "claude:sonnet")
            self.assertEqual(code, 0, out + err)
            code, out, err = self.cli("done", task)
            self.assertEqual(code, 0, out + err)
            if index == 1:
                # Regresión posterior al checkpoint; solo la última fase tiene el arreglo.
                (workdir / "src/check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
                git(workdir, "add", "src/check.py")
                git(workdir, "commit", "-q", "-m", "regresión arreglada en la última fase")
        self.phases = dict(state.load(self.project)["phases"])
        self.slugs = list(self.phases)
        self.last_slug = self.slugs[-1]

    def finish(self, answer: str = "s") -> tuple[int, str, str]:
        return self.cli("finish", self.last_slug, tty_value=True, answers=[answer])

    def assert_intact(self) -> None:
        self.assertEqual(git(self.project, "rev-parse", self.main), self.main_before)
        self.assertEqual(state.load(self.project)["phases"], self.phases)
        for info in self.phases.values():
            self.assertTrue(gitops.branch_exists(self.project, info["branch"]))

    def test_chain_integrates_with_one_confirmation_and_one_suite(self) -> None:
        self.prepare(count=3)
        first = self.phases[self.slugs[0]]
        self.assertIn("SystemExit(1)", git(self.project, "show", f"{first['branch']}:src/check.py"))
        with mock.patch("hefesto.util.confirm", wraps=util.confirm) as confirm, \
                mock.patch("hefesto.evidence.run_verify", wraps=evidence.run_verify) as verify:
            code, out, err = self.finish()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual([call.args[2] for call in verify.call_args_list],
                         ["suite", "TASK-001", "TASK-002", "TASK-003"])
        for slug, info in self.phases.items():
            self.assertIn(slug, out)
            self.assertFalse(gitops.branch_exists(self.project, info["branch"]))
        self.assertEqual(state.load(self.project)["phases"], {})
        self.assertIsNone(state.load(self.project)["branch"])
        self.assertIsNone(state.load(self.project)["workdir"])
        self.assertEqual(git(self.project, "rev-list", "--count", f"{self.main_before}..{self.main}"), "1")
        self.assertTrue((self.project / "src/phase2.txt").exists())
        self.assertTrue((self.project / "src/phase3.txt").exists())
        self.assertEqual(self.cli("evidence", "verify")[0], 0)
        events = (self.project / ".hefesto/logs/hefesto.log").read_text(encoding="utf-8")
        for info in self.phases.values():
            self.assertIn(info["branch"], events.splitlines()[-1])

    def test_worktrees_are_removed_for_every_included_phase(self) -> None:
        self.prepare(isolation="worktree")
        first_workdir = Path(self.phases[self.slugs[0]]["workdir"])
        self.assertNotEqual(evidence.run_verify(self.project, first_workdir, "probe", "python src/check.py", 10).exit_code, 0)
        code, out, err = self.finish()
        self.assertEqual(code, 0, out + err)
        for info in self.phases.values():
            self.assertFalse(Path(info["workdir"]).exists())
            self.assertFalse(gitops.branch_exists(self.project, info["branch"]))
        self.assertEqual(git(self.project, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(state.load(self.project)["phases"], {})

    def test_pending_task_in_previous_phase_blocks(self) -> None:
        self.prepare(pending=True)
        code, out, err = self.finish()
        self.assertEqual(code, util.EXIT_BLOCKED, out + err)
        self.assertIn("Tareas sin completar en la fase: TASK-099", err)
        self.assert_intact()

    def test_missing_evidence_in_previous_phase_blocks(self) -> None:
        self.prepare()
        (self.project / ".hefesto/evidence/TASK-001.json").unlink()
        code, out, err = self.finish()
        self.assertEqual(code, util.EXIT_BLOCKED, out + err)
        self.assertIn("falta TASK-001", err)
        self.assert_intact()

    def test_no_preserves_main_state_and_branches(self) -> None:
        self.prepare()
        code, out, err = self.finish("n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Cancelado", out)
        self.assert_intact()

    def test_previous_verify_is_checked_for_trust(self) -> None:
        self.prepare()
        path = self.project / ".hefesto/ROADMAP.md"
        path.write_text(path.read_text(encoding="utf-8").replace("`python src/check.py`", "`false`"), encoding="utf-8")
        code, out, err = self.finish()
        self.assertEqual(code, util.EXIT_BLOCKED, out + err)
        self.assertIn("verify de TASK-001", err)
        self.assert_intact()

    def test_previous_verify_failure_blocks_integration(self) -> None:
        self.prepare()
        (self.project / "src/check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        git(self.project, "add", "src/check.py")
        git(self.project, "commit", "-q", "-m", "arreglo incompleto")
        config.set_setting(config.project_settings_path(self.project), ("execution", "test_command"), "true")
        code, out, err = self.finish()
        self.assertEqual(code, util.EXIT_INPUT, out + err)
        self.assertIn("TASK-001", err)
        self.assert_intact()

    def test_gates_cover_the_common_base_and_all_kinds(self) -> None:
        self.prepare(kinds=("backend", "infra"))
        with mock.patch("hefesto.approval.gate", wraps=approval.gate) as motion_gate, \
                mock.patch("hefesto.security.gate", wraps=security.gate) as security_gate:
            code, out, err = self.finish()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(motion_gate.call_count, 2)
        self.assertEqual({call.args[2:] for call in motion_gate.call_args_list},
                         {(self.main_before, "backend", "finish"), (self.main_before, "infra", "finish")})
        self.assertEqual(security_gate.call_count, 1)
        self.assertEqual(security_gate.call_args.args[2:], (self.main_before, "finish"))

    def test_previous_phase_not_contained_is_left_open(self) -> None:
        self.prepare()
        first_branch = self.phases[self.slugs[0]]["branch"]
        git(self.project, "branch", "-f", first_branch, self.phases[self.last_slug]["branch"])
        # Un commit exclusivo convierte la anterior en una rama divergente.
        side = self.tmp / "side"
        git(self.project, "worktree", "add", "-q", str(side), first_branch)
        (side / "exclusive.txt").write_text("solo anterior\n", encoding="utf-8")
        git(side, "add", "exclusive.txt")
        git(side, "commit", "-q", "-m", "no contenida")
        gitops.remove_worktree(self.project, side)
        # Su verify no se incluye, y su contenido nunca se integra.
        with mock.patch("hefesto.evidence.run_verify", wraps=evidence.run_verify) as verify:
            code, out, err = self.finish()
        self.assertEqual(code, util.EXIT_BLOCKED, out + err)
        self.assertIn("Conflictos al rebasar", err)
        # El rebase individual conserva el conflicto previo al aplicar el arreglo sin el script heredado.
        self.assertNotIn("TASK-001", [call.args[2] for call in verify.call_args_list])
        self.assertTrue(gitops.branch_exists(self.project, first_branch))
        self.assertIn(self.slugs[0], state.load(self.project)["phases"])
        self.assertFalse((self.project / "exclusive.txt").exists())
        self.assert_intact()
