"""Regresiones de revisión de TASK-905, separadas del contrato inmutable."""

from __future__ import annotations

import concurrent.futures
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config, evidence, gitops, loop, providers, quota, roadmap, router, scheduler
from hefesto.adapters.base import AdapterResult
from tests import test_parallel as contract
from tests.helpers import HefestoTestCase, git


def response(mid: str, summary: str = "VEREDICTO: pass") -> router.RunOutcome:
    result = AdapterResult(True, mid.split(":")[0], mid, summary, None, {}, "")
    return router.RunOutcome(0, "revision-test", result)


class QuotaThreadTests(HefestoTestCase):
    def test_parallel_calls_serialize_file_lock_access_and_preserve_counts(self) -> None:
        quota.start_loop("parallel-test")
        counter_lock = threading.Lock()
        barrier = threading.Barrier(8, timeout=10)
        active = 0
        peak = 0

        class CountingLock(quota.Lock):
            def __enter__(self) -> Any:
                nonlocal active, peak
                with counter_lock:
                    active += 1
                    peak = max(peak, active)
                entered = super().__enter__()
                time.sleep(0.01)
                return entered

            def __exit__(self, *args: Any) -> None:
                nonlocal active
                try:
                    super().__exit__(*args)
                finally:
                    with counter_lock:
                        active -= 1

        def calls(_index: int) -> None:
            barrier.wait()
            for _ in range(5):
                quota.record_call("claude")

        with mock.patch.object(quota, "Lock", CountingLock), \
                concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(calls, range(8)))
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)
        self.assertEqual(quota.calls_in_loop("claude"), 40)
        self.assertFalse((quota.providers_path().parent / ".lock").exists())


class ExclusiveSelectionTests(unittest.TestCase):
    def test_running_exclusive_blocks_disjoint_tasks_from_ids_and_markers(self) -> None:
        for options in ({"parallel": False}, {"size": "L"}):
            for marker, running in ((" ", ["TASK-001"]), ("~", [])):
                with self.subTest(options=options, marker=marker):
                    rm = roadmap.parse_text(contract.roadmap_text(
                        contract.task_block("TASK-001", "src/a.py", marker=marker, **options),
                        contract.task_block("TASK-002", "docs/b.md"),
                    ))
                    self.assertEqual(scheduler.select_batch(rm, running, 4), [])


class DefaultWorkerTests(contract.ParallelBase):
    def job(self) -> scheduler.Job:
        task = roadmap.parse_text(contract.simple(1)).tasks[0]
        return scheduler.Job(task, self.project, "claude:haiku", "claude:haiku", "runner", 1)

    def test_missing_result_preserves_rate_limit_and_quota_hops(self) -> None:
        for error in ("rate_limit", "quota"):
            with self.subTest(error=error):
                routed = router.RunOutcome(3, "limited", None, [
                    router.Hop("claude:haiku", "claude:haiku", f"{error}: cooldown hasta mañana"),
                ])
                with mock.patch.object(router, "run", return_value=routed), \
                        mock.patch.object(evidence, "run_verify") as verify:
                    outcome = scheduler.default_worker(self.project, config.load(self.project), self.job(), "task-run")
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_type, error)
                self.assertEqual(outcome.model_id, "claude:haiku")
                verify.assert_not_called()

    def test_default_worker_capacity_failure_requeues_without_spending_attempt(self) -> None:
        self.write_roadmap(contract.simple(1))
        roles: list[str] = []

        def run(_project: Path, role: str, _instruction: str, **kw: Any) -> router.RunOutcome:
            roles.append(role)
            entry, = kw["entries"]
            mid = providers.model_id(config.load(self.project), entry)
            if len(roles) == 1:
                return router.RunOutcome(3, "limited", None, [router.Hop(entry, mid, "rate_limit: cooldown")])
            if role != "reviewer":
                target = kw["workdir"] / "src/m1.py"
                target.parent.mkdir(exist_ok=True)
                target.write_text("fixed = True\n", encoding="utf-8")
            return response(mid)

        conc = scheduler.Concurrency({"claude:haiku": 4})
        with mock.patch.object(router, "run", side_effect=run):
            results = self.run_parallel(concurrency=conc)
        self.assertEqual(roles, ["runner", "runner", "reviewer"])
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-001"].attempts, 1)
        self.assertIn(("claude:haiku", 2), conc.history)
        self.assert_clean_finish()

    def test_review_checks_previous_authors_and_actual_reviewer_identity(self) -> None:
        for previous in ([], [{"role": "runner", "success": True, "model_id": "claude:sonnet"}]):
            with self.subTest(previous=previous):
                # Se reserva gemini, pero un resultado incoherente atribuye la revisión a un autor.
                reviewer = "claude:sonnet" if previous else "claude:haiku"
                with mock.patch.object(router, "run", side_effect=[response("claude:haiku"), response(reviewer)]), \
                        mock.patch.object(evidence, "run_verify", return_value=evidence.VerifyResult(0, 0, "", "")), \
                        mock.patch("hefesto.acceptance.runs_for_task", return_value=previous):
                    outcome = scheduler.default_worker(self.project, config.load(self.project), self.job(), "task-run")
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.review["verdict"], "fail")
                self.assertIn("mismo modelo", outcome.summary)

    def test_same_model_reviewer_is_not_accepted_or_deadlocked(self) -> None:
        self.configure({("roles", "reviewer", "use"): ["claude:haiku"]})
        with mock.patch.object(router, "run", return_value=response("claude:haiku")) as run, \
                mock.patch.object(evidence, "run_verify", return_value=evidence.VerifyResult(0, 0, "", "")):
            outcome = scheduler.default_worker(self.project, config.load(self.project), self.job(), "task-run")
        self.assertFalse(outcome.ok)
        self.assertIn("independiente", outcome.summary)
        self.assertEqual(run.call_count, 1)

    def test_reviews_share_capacity_with_each_other_and_authors(self) -> None:
        self.configure({
            ("limits", "max_parallel_per_model"): {"claude:haiku": 1, "gemini:default": 1},
            ("roles", "reviewer", "use"): ["gemini", "claude:sonnet"],
        })
        self.write_roadmap(contract.simple(4))
        active: Counter[str] = Counter()
        peaks: Counter[str] = Counter()
        roles: dict[str, set[str]] = {}
        lock = threading.Lock()

        def run(_project: Path, role: str, _instruction: str, **kw: Any) -> router.RunOutcome:
            entry, = kw["entries"]  # El router no puede hacer fallback a un modelo sin reserva.
            mid = providers.model_id(config.load(self.project), entry)
            with lock:
                active[mid] += 1
                peaks[mid] = max(peaks[mid], active[mid])
                roles.setdefault(mid, set()).add(role)
            try:
                if role != "reviewer":
                    number = int(kw["task_id"].split("-")[1])
                    target = kw["workdir"] / f"src/m{number}.py"
                    target.parent.mkdir(exist_ok=True)
                    target.write_text("ok = True\n", encoding="utf-8")
                time.sleep(0.4)
                return response(mid)
            finally:
                with lock:
                    active[mid] -= 1

        with mock.patch.object(router, "run", side_effect=run):
            results = self.run_parallel()
        self.assertEqual({r.status for r in results.values()}, {"integrated"}, results)
        self.assertEqual(roles["gemini:default"], {"runner", "reviewer"})
        self.assertTrue(all(peak == 1 for peak in peaks.values()), peaks)
        self.assertEqual(sum(active.values()), 0)
        self.assert_clean_finish()

    def test_simultaneous_reviews_obey_single_reviewer_slot(self) -> None:
        self.configure({("limits", "max_parallel_per_model"): {"claude:haiku": 3, "gemini:default": 1}})
        self.write_roadmap(contract.simple(3))
        barrier = threading.Barrier(3, timeout=10)
        lock = threading.Lock()
        active = 0
        peak = 0
        reviews = 0

        def run(_project: Path, role: str, _instruction: str, **kw: Any) -> router.RunOutcome:
            nonlocal active, peak, reviews
            entry, = kw["entries"]
            mid = providers.model_id(config.load(self.project), entry)
            if role != "reviewer":
                number = int(kw["task_id"].split("-")[1])
                target = kw["workdir"] / f"src/m{number}.py"
                target.parent.mkdir(exist_ok=True)
                target.write_text("ok = True\n", encoding="utf-8")
                barrier.wait()
            else:
                with lock:
                    active += 1
                    reviews += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.2)
                finally:
                    with lock:
                        active -= 1
            return response(mid)

        with mock.patch.object(router, "run", side_effect=run):
            results = self.run_parallel()
        self.assertEqual({r.status for r in results.values()}, {"integrated"}, results)
        self.assertEqual(reviews, 3)
        self.assertEqual(peak, 1)
        self.assert_clean_finish()

    def test_reviewer_quota_reduces_reviewer_capacity_without_counting_attempt(self) -> None:
        self.write_roadmap(contract.simple(1))
        reviews = 0

        def run(_project: Path, role: str, _instruction: str, **kw: Any) -> router.RunOutcome:
            nonlocal reviews
            entry, = kw["entries"]
            mid = providers.model_id(config.load(self.project), entry)
            if role == "reviewer":
                reviews += 1
                if reviews == 1:
                    return router.RunOutcome(3, "limited", None, [router.Hop(entry, mid, "quota: cooldown")])
            else:
                target = kw["workdir"] / "src/m1.py"
                target.parent.mkdir(exist_ok=True)
                target.write_text("ok = True\n", encoding="utf-8")
            return response(mid)

        conc = scheduler.Concurrency({"claude:haiku": 4, "gemini:default": 4})
        with mock.patch.object(router, "run", side_effect=run):
            results = self.run_parallel(concurrency=conc)
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-001"].attempts, 1)
        self.assertEqual(reviews, 2)
        self.assertEqual(conc.history, [("gemini:default", 2)])
        self.assert_clean_finish()


class LifecycleTests(contract.ParallelBase):
    def test_execution_stops_at_initial_phase_boundary(self) -> None:
        self.write_roadmap(contract.roadmap_text(
            contract.task_block("TASK-001", "src/m1.py"),
            extra_phase=contract.task_block("TASK-002", "src/m2.py"),
        ))
        rec = contract.Recorder(delay=0)
        results = self.run_parallel(worker=rec)
        self.assertEqual(list(results), ["TASK-001"])
        self.assertEqual([job.task.id for job in rec.jobs], ["TASK-001"])
        self.assertEqual(self.markers(), {"TASK-001": "x", "TASK-002": " "})
        self.assertNotIn("src/m2.py", git(self.project, "ls-tree", "-r", "--name-only", contract.PHASE_BRANCH))
        self.assert_clean_finish()

    def test_stop_between_submissions_stops_remaining_batch(self) -> None:
        self.write_roadmap(contract.simple(3))
        stopped = threading.Event()

        def behaviour(_job: Any, _rec: Any) -> None:
            loop.request_stop(self.project)
            stopped.set()

        submit = concurrent.futures.ThreadPoolExecutor.submit

        def submit_and_wait(pool: Any, *args: Any, **kw: Any) -> Any:
            future = submit(pool, *args, **kw)
            self.assertTrue(stopped.wait(10))
            return future

        rec = contract.Recorder(delay=0, behaviour=behaviour)
        with mock.patch.object(concurrent.futures.ThreadPoolExecutor, "submit", submit_and_wait):
            results = self.run_parallel(worker=rec)
        self.assertEqual([j.task.id for j in rec.jobs], ["TASK-001"])
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(self.markers(), {"TASK-001": "x", "TASK-002": " ", "TASK-003": " "})
        self.assert_clean_finish()

    def test_stop_during_worktree_preparation_launches_no_worker(self) -> None:
        self.write_roadmap(contract.simple(2))
        real_git = gitops.git

        def prepare_then_stop(project: Path, *args: str, **kw: Any) -> Any:
            result = real_git(project, *args, **kw)
            if args[:2] == ("worktree", "add") and any("--TASK-001" in arg for arg in args):
                loop.request_stop(self.project)
            return result

        rec = contract.Recorder(delay=0)
        with mock.patch.object(gitops, "git", side_effect=prepare_then_stop):
            self.run_parallel(worker=rec)
        self.assertEqual(rec.jobs, [])
        self.assertEqual(set(self.markers().values()), {" "})
        self.assert_clean_finish()

    def test_verify_after_rebase_retries_escalates_and_preserves_commits(self) -> None:
        self.write_roadmap(contract.simple(1))
        commits: list[str] = []
        real_verify = evidence.run_verify

        def verify(project: Path, workdir: Path, *args: Any, **kw: Any) -> evidence.VerifyResult:
            commits.append(git(workdir, "rev-parse", "HEAD"))
            if len(commits) < 3:
                return evidence.VerifyResult(1, 0, f"incompatible con fase: {len(commits)}", "")
            return real_verify(project, workdir, *args, **kw)

        def behaviour(job: Any, _rec: Any) -> None:
            if job.attempt > 1:
                self.assertIn(f"incompatible con fase: {job.attempt - 1}", job.last_error)
                self.assertEqual(gitops.dirty_paths(job.workdir, include_control=True), [])
                for commit in commits:
                    git(job.workdir, "merge-base", "--is-ancestor", commit, "HEAD")
                self.assertIn(f"intento {job.attempt - 1}", (job.workdir / "src/m1.py").read_text())

        rec = contract.Recorder(delay=0, behaviour=behaviour)
        with mock.patch.object(evidence, "run_verify", side_effect=verify):
            results = self.run_parallel(worker=rec)
        self.assertEqual([j.role for j in rec.jobs], ["runner", "runner", "forge"])
        self.assertEqual([j.attempt for j in rec.jobs], [1, 2, 3])
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-001"].attempts, 3)
        for commit in commits:
            git(self.project, "merge-base", "--is-ancestor", commit, contract.PHASE_BRANCH)
        self.assertEqual(evidence.latest_evidence(self.project, "TASK-001")[1]["attempts"], 3)
        self.assert_clean_finish()

    def test_verify_after_rebase_exhausts_only_failed_task_and_keeps_branch(self) -> None:
        self.write_roadmap(contract.simple(2))
        real_verify = evidence.run_verify

        def verify(project: Path, workdir: Path, task_id: str, *args: Any, **kw: Any) -> evidence.VerifyResult:
            if task_id == "TASK-001":
                return evidence.VerifyResult(1, 0, "incompatible", "")
            return real_verify(project, workdir, task_id, *args, **kw)

        rec = contract.Recorder(delay=0)
        with mock.patch.object(evidence, "run_verify", side_effect=verify):
            results = self.run_parallel(worker=rec)
        self.assertEqual(results["TASK-001"].status, "failed")
        self.assertEqual(results["TASK-001"].attempts, 3)
        self.assertEqual(results["TASK-002"].status, "integrated")
        branch = scheduler.task_branch(contract.PHASE_BRANCH, "TASK-001")
        self.assertIn("intento 3", git(self.project, "show", f"{branch}:src/m1.py"))
        self.assertNotIn("src/m1.py", git(self.project, "ls-tree", "-r", "--name-only", contract.PHASE_BRANCH))
        self.assertEqual(self.markers(), {"TASK-001": "!", "TASK-002": "x"})
        self.assert_clean_finish()


if __name__ == "__main__":
    unittest.main()
