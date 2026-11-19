"""TASK-3702: finish y deploy solo aceptan Telegram como canal humano con una operación aprobada y en curso."""

from __future__ import annotations

from unittest import mock

from hefesto.util import EXIT_BLOCKED, HefestoError
from tests.test_telegram import OWNER
from tests.test_telegram_ops import OpsCase, ok_result
from tests.test_workspaces import GROUP


class TelegramBarrierTests(OpsCase):
    def test_finish_rejects_an_unknown_operation(self) -> None:
        from hefesto import acceptance
        with self.assertRaises(HefestoError) as ctx:
            acceptance.finish(self.project, None, pr=False, approved_operation="inventada")
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)

    def test_deploy_rejects_an_unknown_operation(self) -> None:
        from hefesto import delivery
        with self.assertRaises(HefestoError) as ctx:
            delivery.deploy(self.project, approved_operation="inventada")
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)

    def test_pending_operation_is_not_enough(self) -> None:
        from hefesto import acceptance
        op = self.create("finish", instruction="")
        with self.assertRaises(HefestoError):
            acceptance.finish(self.project, None, pr=False, approved_operation=op["id"])

    def test_operation_of_another_action_is_rejected(self) -> None:
        from hefesto import acceptance, telegram_ops

        def try_finish_with_plan(op: dict) -> dict:
            acceptance.finish(self.project, None, pr=False, approved_operation=op["id"])
            return ok_result()

        op = self.store.approve(self.create("plan")["id"], OWNER, GROUP, 10)
        result = self.store.execute(op, try_finish_with_plan)
        self.assertEqual(result["status"], "failed")
        self.assertIn("operación de Telegram", result["result"]["summary"])
        with mock.patch.object(telegram_ops.Store, "get", return_value=dict(op, status="running", action="deploy")):
            with self.assertRaises(HefestoError):
                acceptance.finish(self.project, None, pr=False, approved_operation=op["id"])
