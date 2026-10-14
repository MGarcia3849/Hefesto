"""Fase 3: rama por fase, aislamiento, checkpoint con guard, review, done, evidencias y finish."""

from __future__ import annotations

import json
from pathlib import Path

from hefesto import config, evidence, gitops, roadmap, state
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear suma
  - verify: `test -f src/suma.py`
  - files: src/
  - role: runner
  - size: S
- [ ] TASK-002: Crear resta
  - verify: `test -f src/resta.py`
  - files: src/
  - role: runner
  - depends_on: TASK-001
  - size: S
"""

CONTRACT = """# ROADMAP

## Fase 1 — Contrato
- [ ] TASK-001: Contrato de suma
  - verify: `test -f tests/test_suma.py`
  - files: tests/
  - role: core
  - size: S
  - contract: tests/test_suma.py
- [ ] TASK-002: Implementar suma
  - verify: `true`
  - files: src/, tests/
  - role: runner
  - depends_on: TASK-001
  - size: M
"""

BRANCH = "hefesto/fase-1-nucleo"


class GitFlowCase(HefestoTestCase):
    roadmap_text = ROADMAP

    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        self.set_roadmap(self.roadmap_text)

    def set_roadmap(self, text: str, trust_it: bool = True) -> None:
        (self.project / ".hefesto" / "ROADMAP.md").write_text(text, encoding="utf-8")
        git(self.project, "add", ".hefesto/ROADMAP.md")
        git(self.project, "commit", "-q", "-m", "roadmap")
        if trust_it:
            code, out, err = self.cli("trust", tty_value=True, answers=["s"])
            self.assertEqual(code, 0, out + err)

    def workdir(self) -> Path:
        return Path(state.load(self.project).get("workdir") or self.project)

    def write(self, rel: str, content: str = "x = 1\n") -> None:
        path = self.workdir() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def complete(self, task: str, rel: str) -> None:
        self.assertEqual(self.cli("start", task)[0], 0)
        self.write(rel)
        code, out, err = self.cli("review", task, "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("done", task)
        self.assertEqual(code, 0, out + err)


class DoneTests(GitFlowCase):
    def test_full_task_on_phase_branch(self) -> None:
        self.complete("TASK-001", "src/suma.py")
        self.assertEqual(gitops.current_branch(self.project), BRANCH)
        log = git(self.project, "log", "--format=%s", "-2").splitlines()
        self.assertEqual(log, ["hefesto(TASK-001): evidence", "hefesto(TASK-001): Crear suma [verified]"])
        self.assertIn("- [x] TASK-001", git(self.project, "show", "HEAD:.hefesto/ROADMAP.md"))
        ev = evidence.show(self.project, "TASK-001")
        self.assertEqual(ev["verify"]["exit_code"], 0)
        self.assertEqual(ev["review"]["verdict"], "pass")
        self.assertEqual(ev["files"][0]["path"], "src/suma.py")
        self.assertIsNone(ev["prev_sha256"])
        data = state.load(self.project)
        self.assertEqual((data["status"], data["last_checkpoint"]), ("IDLE", ev["commit"]))
        self.assertEqual(gitops.dirty_paths(self.project, include_control=True), [])

    def test_phase_mode_puts_consecutive_commits_on_one_branch(self) -> None:
        self.complete("TASK-001", "src/suma.py")
        self.complete("TASK-002", "src/resta.py")
        subjects = git(self.project, "log", "--format=%s", "-4").splitlines()
        self.assertEqual(subjects[1], "hefesto(TASK-002): Crear resta [verified]")
        self.assertEqual(subjects[3], "hefesto(TASK-001): Crear suma [verified]")
        self.assertEqual(state.load(self.project)["status"], "COMPLETED")
        self.assertEqual(evidence.show(self.project, "TASK-002")["seq"], 2)

    def test_done_requires_trusted_verify(self) -> None:
        self.set_roadmap(ROADMAP.replace("test -f src/suma.py", "test -f src/suma.py && true"), trust_it=False)
        self.cli("start", "TASK-001")
        code, _, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 2)
        self.assertIn("hefesto trust", err)
        self.assertEqual(state.load(self.project)["attempts"], 0)

    def test_red_verify_counts_as_failure(self) -> None:
        self.cli("start", "TASK-001")
        code, _, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 1)
        self.assertEqual(state.load(self.project)["attempts"], 1)
        logs = list((self.project / ".hefesto" / "logs" / "verify").glob("TASK-001-*.log"))
        self.assertEqual(len(logs), 1)

    def test_done_requires_review_pass_when_code_changed(self) -> None:
        self.cli("start", "TASK-001")
        self.write("src/suma.py")
        code, _, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 2)
        self.assertIn("revisión `pass`", err)

    def test_checkpoint_blocks_secret_by_content(self) -> None:
        self.cli("start", "TASK-001")
        self.write("src/suma.py", 'API_KEY = "sk-ant-' + "a" * 30 + '"\n')
        self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        before = git(self.project, "rev-parse", "HEAD")
        code, _, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 2)
        self.assertIn("secreto", err)
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.project, "diff", "--cached", "--name-only"), "")
        self.assertIn("- [~] TASK-001", (self.project / ".hefesto" / "ROADMAP.md").read_text(encoding="utf-8"))

    def test_checkpoint_blocks_paths_outside_files(self) -> None:
        self.cli("start", "TASK-001")
        self.write("src/suma.py")
        self.write("otro/fuera.py")
        git(self.project, "add", "otro/fuera.py")
        self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        code, _, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 2)
        self.assertIn("otro/fuera.py: fuera de `files`", err)

    def test_checkpoint_refuses_non_hefesto_branch(self) -> None:
        cfg = config.load(self.project)
        task = roadmap.load_valid(self.project).get("TASK-001")
        with self.assertRaises(HefestoError) as ctx:
            gitops.checkpoint(self.project, self.project, cfg, task, "x")
        self.assertEqual(ctx.exception.code, 2)


class IsolationTests(GitFlowCase):
    def test_auto_uses_worktree_and_never_touches_user_changes(self) -> None:
        (self.project / "mio.txt").write_text("trabajo del usuario\n", encoding="utf-8")
        main = gitops.current_branch(self.project)
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        data = state.load(self.project)
        self.assertEqual(data["isolation"], "worktree")
        self.assertEqual(gitops.current_branch(self.project), main)
        self.assertEqual((self.project / "mio.txt").read_text(encoding="utf-8"), "trabajo del usuario\n")
        self.write("src/suma.py")
        self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.project / "src" / "suma.py").exists())
        self.assertIn("mio.txt", gitops.dirty_paths(self.project))
        self.complete("TASK-002", "src/resta.py")
        worktrees = [l for l in git(self.project, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
        self.assertEqual(len(worktrees), 2)

    def test_auto_uses_branch_when_clean(self) -> None:
        self.cli("start", "TASK-001")
        self.assertEqual(state.load(self.project)["isolation"], "branch")
        self.assertEqual(gitops.current_branch(self.project), BRANCH)

    def test_branch_isolation_refuses_dirty_tree(self) -> None:
        (self.project / ".hefesto" / "hefesto.toml").write_text('[execution]\nisolation = "branch"\n', encoding="utf-8")
        git(self.project, "add", ".hefesto/hefesto.toml")
        git(self.project, "commit", "-q", "-m", "cfg")
        (self.project / "mio.txt").write_text("x", encoding="utf-8")
        code, _, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 2)
        self.assertIn("mio.txt", err)
        self.assertEqual(state.load(self.project)["status"], "IDLE")


class ReviewTests(GitFlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.cli("start", "TASK-001")
        self.write("src/suma.py")

    def test_rejects_same_model(self) -> None:
        code, _, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "claude:sonnet",
                                "--reviewer", "claude:sonnet")
        self.assertEqual(code, 2)
        self.assertIn("mismo modelo", err)

    def test_rejects_same_provider_when_another_is_available(self) -> None:
        self.use_fakes("gemini")
        code, _, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "claude:haiku",
                                "--reviewer", "claude:sonnet")
        self.assertEqual(code, 2)
        self.assertIn("otro proveedor disponible", err)

    def test_same_provider_allowed_only_if_superior_and_noted(self) -> None:
        code, _, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "claude:sonnet",
                                "--reviewer", "claude:haiku")
        self.assertEqual(code, 2)
        self.assertIn("superior", err)
        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "claude:haiku",
                                  "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, err)
        self.assertIn("excepción", out)
        self.cli("done", "TASK-001")
        self.assertIn("mismo proveedor", evidence.show(self.project, "TASK-001")["review"]["exception"])

    def test_requires_author_when_unknown(self) -> None:
        code, _, err = self.cli("review", "TASK-001", "--verdict", "pass", "--reviewer", "claude:sonnet")
        self.assertEqual(code, 1)
        self.assertIn("--author", err)

    def test_negative_review_counts_as_attempt(self) -> None:
        code, out, err = self.cli("review", "TASK-001", "--verdict", "fail", "--notes", "falta manejo de errores",
                                  "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertEqual(code, 1)
        self.assertEqual(state.load(self.project)["attempts"], 1)


class ContractTests(GitFlowCase):
    roadmap_text = CONTRACT

    def test_modifying_contract_tests_is_automatic_negative_review(self) -> None:
        self.cli("start", "TASK-001")
        self.write("tests/test_suma.py", "def test_suma():\n    assert False\n")
        self.cli("review", "TASK-001", "--verdict", "pass", "--author", "claude:opus", "--reviewer", "codex")
        self.assertEqual(self.cli("done", "TASK-001")[0], 0)
        self.cli("start", "TASK-002")
        self.write("src/suma.py", "def suma(a, b):\n    return a + b\n")
        self.write("tests/test_suma.py", "def test_suma():\n    assert True\n")
        code, _, err = self.cli("review", "TASK-002", "--verdict", "pass", "--author", "gemini",
                                "--reviewer", "claude:sonnet")
        self.assertNotEqual(code, 0)
        self.assertEqual(state.load(self.project)["attempts"], 1)
        reviews = (self.project / ".hefesto" / "logs" / "reviews.jsonl").read_text(encoding="utf-8")
        self.assertIn("modificó tests de contrato", reviews)


class EvidenceChainTests(GitFlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.complete("TASK-001", "src/suma.py")
        self.complete("TASK-002", "src/resta.py")

    def test_chain_is_valid(self) -> None:
        code, out, _ = self.cli("evidence", "verify")
        self.assertEqual(code, 0)
        self.assertIn("2 evidencias", out)

    def test_editing_old_evidence_breaks_chain(self) -> None:
        path = self.project / ".hefesto" / "evidence" / "TASK-001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["verify"]["exit_code"] = 0
        data["review"]["notes"] = "editado"
        path.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = self.cli("evidence", "verify")
        self.assertEqual(code, 2)
        self.assertIn("TASK-001 fue editada o borrada", err)

    def test_deleting_old_or_last_evidence_breaks_chain(self) -> None:
        (self.project / ".hefesto" / "evidence" / "TASK-002.json").unlink()
        code, _, err = self.cli("evidence", "verify")
        self.assertEqual(code, 2)
        self.assertIn("se borró", err)


class FinishTests(GitFlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.main = gitops.current_branch(self.project)
        self.complete("TASK-001", "src/suma.py")
        self.complete("TASK-002", "src/resta.py")

    def test_finish_requires_tty(self) -> None:
        code, _, err = self.cli("finish")
        self.assertEqual(code, 2)
        self.assertTrue(gitops.branch_exists(self.project, BRANCH))

    def test_finish_squash_leaves_single_commit(self) -> None:
        before = int(git(self.project, "rev-list", "--count", self.main))
        code, out, err = self.cli("finish", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(gitops.current_branch(self.project), self.main)
        self.assertEqual(int(git(self.project, "rev-list", "--count", self.main)), before + 1)
        self.assertFalse(gitops.branch_exists(self.project, BRANCH))
        self.assertTrue((self.project / "src" / "resta.py").exists())
        self.assertEqual(self.cli("evidence", "verify")[0], 0)

    def test_finish_cancelled_keeps_branch(self) -> None:
        code, out, _ = self.cli("finish", tty_value=True, answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("Cancelado", out)
        self.assertTrue(gitops.branch_exists(self.project, BRANCH))

    def test_finish_stops_on_conflict(self) -> None:
        git(self.project, "checkout", "-q", self.main)
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "suma.py").write_text("conflicto = True\n", encoding="utf-8")
        git(self.project, "add", "src/suma.py")
        git(self.project, "commit", "-q", "-m", "cambio en main")
        git(self.project, "checkout", "-q", BRANCH)
        code, _, err = self.cli("finish", tty_value=True, answers=["s"])
        self.assertEqual(code, 2)
        self.assertIn("src/suma.py", err)
        self.assertTrue(gitops.branch_exists(self.project, BRANCH))
        self.assertFalse((self.project / ".git" / "rebase-merge").exists())

    def test_finish_does_not_integrate_with_red_tests(self) -> None:
        (self.project / ".hefesto" / "hefesto.toml").write_text('[execution]\ntest_command = "false"\n', encoding="utf-8")
        before = git(self.project, "rev-parse", self.main)
        code, _, err = self.cli("finish", tty_value=True, answers=["s"])
        self.assertEqual(code, 1)
        self.assertEqual(git(self.project, "rev-parse", self.main), before)

    def test_finish_pr_requires_config(self) -> None:
        code, _, err = self.cli("finish", "--pr", tty_value=True, answers=["s"])
        self.assertEqual(code, 2)
        self.assertIn("github.pr", err)
