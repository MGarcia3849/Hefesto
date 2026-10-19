"""Pruebas adicionales de TASK-1606, separadas del contrato visual."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import state, util
from hefesto.util import HefestoError
from tests import test_visual


class DesignPassExtraTests(test_visual._VisualCase):
    _pass = test_visual.DesignPassTests._pass
    prepare_product = test_visual.DesignPassTests.prepare_product

    def test_visual_commands_receive_the_saved_diagnosis(self) -> None:
        from hefesto import visual

        self.prepare_product()
        workdir = self.tmp / "visual-worktree"
        workdir.mkdir()
        for diagnosis, shortcut in (("critique", None), ("audit", None), ("audit", "robustez")):
            with self.subTest(diagnosis=diagnosis, shortcut=shortcut):
                text = "Corrige la jerarquía con impeccable layout."
                relative = f".hefesto/design/reports/TASK-001-{diagnosis}.md"
                absolute = (self.project / relative).resolve()
                instructions: list[str] = []

                def delegate(role: str, instruction: str) -> tuple[bool, str]:
                    if role == "visual":
                        instructions.append(instruction)
                        self.assertIn(f"Lee el diagnóstico en {absolute}", instruction)
                        self.assertTrue(absolute.is_absolute())
                        self.assertFalse((workdir / relative).exists())
                        self.assertEqual((workdir / absolute).read_text(encoding="utf-8"), text)
                    return True, text

                report = visual.design_pass(self.project, self.cfg(), self.task(), delegate, lambda _: None,
                                            diagnosis=diagnosis, shortcut=shortcut)
                self.assertTrue(report.ok)
                self.assertTrue(instructions)
                self.assertIn("impeccable polish", instructions[-1])

    def test_diagnosis_is_written_atomically(self) -> None:
        self.prepare_product()
        text = "Jerarquía ilegible: impeccable typeset."
        with mock.patch("hefesto.util.atomic_write_text", wraps=util.atomic_write_text) as write:
            report, _, _ = self._pass(text)
        write.assert_called_once_with(report.report_path.resolve(), text)
        self.assertEqual(report.report_path.read_text(encoding="utf-8"), text)

    def test_reports_directory_cannot_escape_through_a_link(self) -> None:
        from hefesto import gitops

        self.prepare_product()
        outside = self.tmp / "outside"
        outside.mkdir()
        for relative in (".hefesto/design", ".hefesto/design/reports"):
            with self.subTest(link=relative):
                link = self.project / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                gitops._create_dir_link(outside, link)
                try:
                    with self.assertRaises(HefestoError) as ctx:
                        self._pass("impeccable layout")
                    self.assertEqual(ctx.exception.code, util.EXIT_INPUT)
                    self.assertIn("fuera del proyecto", str(ctx.exception))
                    self.assertEqual(self.events, ["reviewer"])
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    gitops._unlink_only(link)

    def test_cli_checkpoint_uses_workdir_only_for_the_requested_task(self) -> None:
        from hefesto import router
        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        workdir = str(self.tmp / "task-worktree")
        for status, task_id, directory in (("IDLE", None, workdir), ("IN_PROGRESS", "TASK-999", workdir),
                                           ("COMPLETED", "TASK-001", workdir),
                                           ("IN_PROGRESS", "TASK-001", workdir),
                                           ("IN_PROGRESS", "TASK-001", None)):
            with self.subTest(status=status, task_id=task_id, workdir=directory):
                current = dict(state.INITIAL, status=status, current_task_id=task_id,
                               workdir=directory)
                result = AdapterResult(True, "codex", "default", "hecho", None, "unknown", "")
                with mock.patch("hefesto.state.load", return_value=current), \
                        mock.patch("hefesto.router._task_context", wraps=router._task_context) as context, \
                        mock.patch("hefesto.router.run", return_value=RunOutcome(0, "r1", result)) as run, \
                        mock.patch("hefesto.gitops.checkpoint") as checkpoint:
                    code, out, err = self.cli("design", "pass", "TASK-001")
                    self.assertEqual(code, 0, out + err)
                    self.assertTrue(run.called)
                    checkpoint.assert_called_once()
                    context.assert_called_once_with(self.project, "TASK-001")
                    expected = (Path(directory)
                                if status == "IN_PROGRESS" and task_id == "TASK-001" and directory
                                else self.project)
                    self.assertEqual(checkpoint.call_args.args[1], expected)
                    run.reset_mock()
                    checkpoint.reset_mock()
                    code, out, err = self.cli("design", "pass", "TASK-001", "--dry-run")
                    self.assertEqual(code, 0, out + err)
                    run.assert_not_called()
                    checkpoint.assert_not_called()

    def test_cli_quota_exhaustion_is_unavailable_without_task_failure(self) -> None:
        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        good = RunOutcome(0, "r1", AdapterResult(True, "codex", "default", "impeccable layout",
                                               None, "unknown", ""))
        for failed_step, completed in (("critique", 0), ("layout", 1), ("polish", 2)):
            for has_result in (False, True):
                with self.subTest(failed_step=failed_step, has_result=has_result):
                    result = (AdapterResult(False, "codex", "default", "sin cuota", "quota", "unknown", "")
                              if has_result else None)
                    outcomes = [good] * completed + [RunOutcome(util.EXIT_UNAVAILABLE, "r2", result)]
                    before = state.load(self.project)
                    with mock.patch("hefesto.router.run", side_effect=outcomes) as run, \
                            mock.patch("hefesto.gitops.checkpoint") as checkpoint:
                        code, out, err = self.cli("design", "pass", "TASK-001")
                    self.assertEqual(code, util.EXIT_UNAVAILABLE, out + err)
                    self.assertIn("Sin proveedor con cuota", err)
                    self.assertIn("no es un fallo de la tarea", err)
                    self.assertIn(failed_step, err)
                    self.assertEqual(run.call_count, completed + 1)
                    self.assertEqual(checkpoint.call_count, max(0, completed - 1))
                    self.assertEqual(state.load(self.project), before)

    def test_cli_saves_full_final_text_without_raw_log_commands(self) -> None:
        from hefesto.adapters.base import SUMMARY_CHARS, Adapter, Call, validate_result
        from hefesto.router import RunOutcome

        self.prepare_product()
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        full = "Diagnóstico detallado. " * SUMMARY_CHARS + "impeccable layout"
        log_path = self.project / ".hefesto" / "logs" / "diagnosis.log"
        log_path.write_text('$ cli\n{"event": "impeccable bolder"}\n--- stderr ---\n', encoding="utf-8")
        call = Call("reviewer", "diagnostica", "TASK-001", "r1", "default", self.project, self.project)
        result = Adapter(self.cfg()).result(call, True, f"\n {full} \n", None, "unknown", str(log_path))
        self.assertGreater(len(full), 1500)
        self.assertEqual(result.text, full)
        self.assertEqual(result.summary, full[:SUMMARY_CHARS])
        self.assertNotIn("text", result.to_dict())
        self.assertEqual(validate_result(result.to_dict()), [])
        with mock.patch("hefesto.router.run", return_value=RunOutcome(0, "r1", result)), \
                mock.patch("hefesto.gitops.checkpoint") as checkpoint:
            code, out, err = self.cli("design", "pass", "TASK-001")
        self.assertEqual(code, 0, out + err)
        report = self.project / ".hefesto/design/reports/TASK-001-critique.md"
        self.assertEqual(report.read_text(encoding="utf-8"), full)
        self.assertIn("critique → layout → polish", out)
        self.assertNotIn("bolder", out)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual(checkpoint.call_args.args[1], self.workdir())

    def test_cli_falls_back_to_summary_without_reading_raw_log(self) -> None:
        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        log_path = self.project / ".hefesto" / "logs" / "diagnosis.log"
        log_path.write_text("impeccable bolder", encoding="utf-8")
        result = AdapterResult(True, "codex", "default", "resumen", None, "unknown", str(log_path))
        self.assertEqual(result.text, "")
        with mock.patch("hefesto.router.run", return_value=RunOutcome(0, "r1", result)), \
                mock.patch("hefesto.gitops.checkpoint") as checkpoint:
            code, out, err = self.cli("design", "pass", "TASK-001")
        self.assertEqual(code, 0, out + err)
        report = self.project / ".hefesto/design/reports/TASK-001-critique.md"
        self.assertEqual(report.read_text(encoding="utf-8"), "resumen")
        self.assertIn("critique → polish", out)
        checkpoint.assert_called_once()

    def test_cli_ignores_a_log_linked_outside_the_project(self) -> None:
        from hefesto import gitops
        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "diagnosis.log").write_text("impeccable bolder", encoding="utf-8")
        link = self.project / "linked-logs"
        gitops._create_dir_link(outside, link)
        try:
            result = AdapterResult(True, "codex", "default", "resumen", None, "unknown",
                                   "linked-logs/diagnosis.log")
            with mock.patch("hefesto.router.run", return_value=RunOutcome(0, "r1", result)), \
                    mock.patch("hefesto.gitops.checkpoint") as checkpoint:
                code, out, err = self.cli("design", "pass", "TASK-001")
            self.assertEqual(code, 0, out + err)
            report = self.project / ".hefesto/design/reports/TASK-001-critique.md"
            self.assertEqual(report.read_text(encoding="utf-8"), "resumen")
            checkpoint.assert_called_once()
        finally:
            gitops._unlink_only(link)

    def test_cli_rechecks_task_before_checkpoint(self) -> None:
        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        current = dict(state.INITIAL, status="IN_PROGRESS", current_task_id="TASK-001",
                       workdir=str(self.project))

        def delegate(*args: Any, **kwargs: Any) -> RunOutcome:
            current.update(current_task_id="TASK-999", workdir=str(self.tmp / "other-worktree"))
            return RunOutcome(0, "r1", AdapterResult(True, "codex", "default", "hecho", None, "unknown", ""))

        with mock.patch("hefesto.state.load", return_value=current), \
                mock.patch("hefesto.router.run", side_effect=delegate), \
                mock.patch("hefesto.gitops.checkpoint") as checkpoint:
            code, out, err = self.cli("design", "pass", "TASK-001", "--shortcut", "tipografia")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(checkpoint.call_count, 3)
        for call in checkpoint.call_args_list:
            self.assertEqual(call.args[1], self.project)
