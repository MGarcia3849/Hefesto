from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from hefesto import delivery, loop
from tests.test_delivery_contract import DeliveryCase, GhFake


class DeliveryRuntimeTests(DeliveryCase):
    def test_loop_delivers_every_closed_phase_once(self) -> None:
        closures = {"fase-1": delivery.Closure("fase-1", "branch-1", True, []),
                    "fase-2": delivery.Closure("fase-2", "branch-2", True, [])}
        pending_record = {"steps": {"pr": {"status": "pending"}, "deploy": {"status": "skipped"}}}
        with mock.patch.object(loop, "_pending_role", return_value=(None, "sin trabajo")), \
                mock.patch.object(loop.state, "load", return_value={"phases": {"fase-1": {}, "fase-2": {}}}), \
                mock.patch.object(delivery, "phase_closure", side_effect=lambda _p, slug: closures[slug]), \
                mock.patch.object(delivery, "is_authorized", return_value=True), \
                mock.patch.object(delivery, "_read_record", return_value=pending_record), \
                mock.patch.object(delivery, "load_target", return_value=None), \
                mock.patch.object(delivery, "deliver", return_value=delivery.DeliveryReport(
                    0, {"pr": "done", "deploy": "skipped"})) as deliver, \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            result = loop.run(self.project, "auto", 3, 1, False)
        self.assertEqual(result.code, 0)
        self.assertEqual(deliver.call_args_list, [mock.call(self.project, "fase-1"),
                                                   mock.call(self.project, "fase-2")])

    def test_loop_processes_delivery_at_exact_task_limit(self) -> None:
        closure = delivery.Closure("fase-1", "branch-1", True, [])
        incomplete_record = {"steps": {"pr": {"status": "pending"}, "deploy": {"status": "skipped"}}}
        with mock.patch.object(loop, "_pending_role", return_value=(None, "sin trabajo")), \
                mock.patch.object(loop.state, "load", return_value={"phases": {"fase-1": {}}}), \
                mock.patch.object(delivery, "phase_closure", return_value=closure), \
                mock.patch.object(delivery, "is_authorized", return_value=True), \
                mock.patch.object(delivery, "_read_record", return_value=incomplete_record), \
                mock.patch.object(delivery, "load_target", return_value=None), \
                mock.patch.object(delivery, "deliver", return_value=delivery.DeliveryReport(
                    0, {"pr": "done", "deploy": "skipped"})) as deliver, \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            result = loop.run(self.project, "auto", 1, 1, False)
        self.assertEqual(result.code, 0)
        deliver.assert_called_once_with(self.project, "fase-1")

    def test_loop_delivers_closed_phase_before_pending_next_phase(self) -> None:
        self.close_task()
        closure = delivery.phase_closure(self.project)
        with mock.patch.object(loop, "_pending_role", return_value=(None, "sin trabajo")), \
                mock.patch.object(delivery, "phase_closure", return_value=closure), \
                mock.patch.object(delivery, "is_authorized", return_value=True), \
                mock.patch.object(delivery, "deliver", return_value=delivery.DeliveryReport(
                    0, {"pr": "done", "deploy": "skipped"})) as deliver, \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            result = loop.run(self.project, "auto", 1, 1, False)
        self.assertEqual(result.code, 0)
        deliver.assert_called_once_with(self.project, closure.slug)

    def test_loop_does_not_repeat_completed_delivery(self) -> None:
        self.close_task()
        closure = delivery.phase_closure(self.project)
        complete_record = {"steps": {"pr": {"status": "done"}, "deploy": {"status": "skipped"}}}
        with mock.patch.object(loop, "_pending_role", return_value=("runner", "")), \
                mock.patch.object(delivery, "phase_closure", return_value=closure), \
                mock.patch.object(delivery, "is_authorized", return_value=True), \
                mock.patch.object(delivery, "_read_record", return_value=complete_record), \
                mock.patch.object(delivery, "deliver") as deliver, \
                mock.patch.object(loop.router, "plan_route", return_value=[]), \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            loop.run(self.project, "auto", 1, 1, False)
        deliver.assert_not_called()

    def test_loop_stop_prevents_delivery(self) -> None:
        self.close_task()
        loop.stop_path(self.project).write_text("STOP\n", encoding="utf-8")
        with mock.patch.object(delivery, "deliver") as deliver, \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            result = loop.run(self.project, "auto", 1, 1, False)
        self.assertEqual(result.code, 0)
        deliver.assert_not_called()

    def test_loop_uses_full_delivery_for_closed_authorized_phase(self) -> None:
        self.close_task()
        closure = delivery.phase_closure(self.project)
        with mock.patch.object(loop, "_pending_role", return_value=(None, "")), \
                mock.patch.object(delivery, "phase_closure", return_value=closure), \
                mock.patch.object(delivery, "is_authorized", return_value=True), \
                mock.patch.object(delivery, "deliver", return_value=delivery.DeliveryReport(
                    0, {"pr": "done", "deploy": "done"})) as deliver, \
                mock.patch.object(loop.quota, "start_loop"), mock.patch.object(loop.quota, "end_loop"), \
                mock.patch.object(loop.log, "event"):
            result = loop.run(self.project, "auto", 1, 1, False)
        self.assertEqual(result.code, 0)
        deliver.assert_called_once_with(self.project, closure.slug)

    def test_delivery_deploys_from_phase_worktree_once(self) -> None:
        self.close_task()
        self.write_target()
        delivery.authorize(self.project, "pr", yes=True)
        delivery.authorize(self.project, "deploy", yes=True)
        phase_worktree = self.tmp / "verified-phase"
        phase_worktree.mkdir()
        record = delivery.record_path(self.project, self.slug())
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"phase": self.slug(), "branch": self.branch(), "steps": {
            "pr": {"status": "pending"}, "deploy": {"status": "pending", "attempts": 0}}}),
                          encoding="utf-8")
        calls: list[tuple[list[str], dict[str, object]]] = []
        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if argv[:3] == ["gh", "pr", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            if argv[:3] == ["git", "push", "-u"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["gh", "pr", "create"]:
                return mock.Mock(returncode=0, stdout="https://github.invalid/pull/1\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(delivery, "_verified_phase_workdir", return_value=phase_worktree), \
                mock.patch("subprocess.run", side_effect=fake_run):
            first = delivery.deliver(self.project)
            second = delivery.deliver(self.project)
        self.assertEqual(first.steps, {"pr": "done", "deploy": "done"})
        self.assertEqual(second.steps, first.steps)
        deploy_calls = [(argv, kw) for argv, kw in calls if argv == list(delivery.load_target(self.project).command)]
        self.assertEqual(len(deploy_calls), 1)
        self.assertEqual(Path(deploy_calls[0][1]["cwd"]), phase_worktree)

