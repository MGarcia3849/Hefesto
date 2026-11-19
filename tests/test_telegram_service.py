"""Arranque automático: pruebas offline, nunca registran tareas reales."""

from __future__ import annotations

import io
import os
import subprocess
import xml.etree.ElementTree as ET
from unittest import mock

from hefesto import telegram, telegram_service as service, util
from tests.helpers import HefestoTestCase, tty

TOKEN = "123456:" + "S" * 35


class TelegramServiceTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "estado con espacios"
        self.launcher = self.tmp / "hefesto.cmd"
        self.launcher.write_text("@echo off\n", encoding="utf-8")
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "OK\n", ""))
        self.enterContext(mock.patch.dict(os.environ, {
            "HEFESTO_STATE_DIR": str(self.home), "USERNAME": "persona", "USERDOMAIN": "EQUIPO",
            telegram.TOKEN_ENV: TOKEN,
        }))
        for key in service.AGENT_ENV:
            os.environ.pop(key, None)
        self.enterContext(mock.patch.object(service.shutil, "which", return_value=str(self.launcher)))
        self.check = self.enterContext(mock.patch.object(telegram, "check", return_value=(True, "OK")))

    def call(self, action: str) -> tuple[int, str]:
        return service.autostart(action, windows=True, run=self.runner, directory=self.tmp)

    def enable(self) -> tuple[int, str]:
        with tty(True):
            return self.call("enable")

    def test_xml_and_launchers_have_desktop_restart_and_no_token(self) -> None:
        self.assertEqual(self.enable()[0], 0)
        raw = (self.home / "telegram-autostart.xml").read_bytes()
        self.assertIn(raw[:2], (b"\xff\xfe", b"\xfe\xff"))
        root = ET.fromstring(raw)
        self.assertEqual(root.attrib["version"], "1.2")
        ns = {"t": service.NS}
        expected = {
            "Triggers/LogonTrigger/UserId": "EQUIPO\\persona",
            "Triggers/LogonTrigger/Enabled": "true",
            "Principals/Principal/UserId": "EQUIPO\\persona",
            "Principals/Principal/LogonType": "InteractiveToken",
            "Principals/Principal/RunLevel": "LeastPrivilege",
            "Settings/MultipleInstancesPolicy": "IgnoreNew",
            "Settings/DisallowStartIfOnBatteries": "false",
            "Settings/StopIfGoingOnBatteries": "false",
            "Settings/ExecutionTimeLimit": "PT0S", "Settings/Hidden": "true",
            "Settings/RestartOnFailure/Interval": "PT1M", "Settings/RestartOnFailure/Count": "999",
            "Actions/Exec/WorkingDirectory": str(self.tmp.resolve()),
        }
        for path, value in expected.items():
            with self.subTest(path=path):
                self.assertEqual(root.findtext("/".join("t:" + part for part in path.split("/")), namespaces=ns), value)
        vbs = (self.home / "telegram-autostart.vbs").read_text(encoding="utf-16")
        self.assertIn("WScript.Quit sh.Run", vbs)
        self.assertIn(", 0, True)", vbs)
        worker = (self.home / "telegram-autostart.py").read_text(encoding="utf-8")
        compile(worker, "worker", "exec")
        self.assertIn("run_logged", worker)
        for content in (raw.decode("utf-16"), vbs, worker):
            self.assertNotIn(TOKEN, content)
            self.assertNotIn(telegram.TOKEN_ENV, content)
        self.assertEqual(self.runner.call_args.args[0], ["schtasks", "/Create", "/TN", service.TASK_NAME,
                                                        "/XML", str(self.home.resolve() / "telegram-autostart.xml"), "/F"])
        self.assertIs(self.runner.call_args.kwargs["shell"], False)

    def test_enable_requires_tty_and_refuses_agents(self) -> None:
        with tty(False), self.assertRaises(util.HefestoError) as ctx:
            self.call("enable")
        self.assertEqual(ctx.exception.code, util.EXIT_BLOCKED)
        for key in service.AGENT_ENV:
            with mock.patch.dict(os.environ, {key: ""}), tty(True):
                with self.assertRaises(util.HefestoError):
                    self.call("enable")
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_cmd_launcher_metacharacters_are_rejected(self) -> None:
        launcher = self.tmp / "hefesto&otra.cmd"
        launcher.touch()
        with mock.patch.object(service.shutil, "which", return_value=str(launcher)):
            with self.assertRaises(util.HefestoError):
                self.enable()
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_enable_requires_check_before_writing(self) -> None:
        self.check.return_value = False, "falta configuración"
        self.assertEqual(self.enable(), (util.EXIT_INPUT, "falta configuración"))
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_unsafe_paths_rejected_before_writing(self) -> None:
        for suffix in ('a"b', "a%b", "a!b", "a\nb"):
            with self.subTest(suffix=suffix), mock.patch.object(service.sys, "executable", suffix):
                with self.assertRaises(util.HefestoError):
                    self.enable()
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_repository_launcher_fallback(self) -> None:
        with mock.patch.object(service.shutil, "which", return_value=None):
            self.assertEqual(self.enable()[0], 0)
        self.assertIn("hefesto.cmd", (self.home / "telegram-autostart.py").read_text(encoding="utf-8"))

    def test_disable_start_and_status_exact_commands_without_check(self) -> None:
        expected = {"disable": ["/Delete", "/TN", service.TASK_NAME, "/F"],
                    "start": ["/Run", "/TN", service.TASK_NAME],
                    "status": ["/Query", "/TN", service.TASK_NAME, "/FO", "LIST", "/V"]}
        for action, args in expected.items():
            with self.subTest(action=action):
                self.assertEqual(self.call(action)[0], 0)
                self.assertEqual(self.runner.call_args.args[0], ["schtasks", *args])
                self.assertIs(self.runner.call_args.kwargs["shell"], False)
        self.check.assert_not_called()

    def test_status_shows_state_last_run_and_redacted_tail(self) -> None:
        self.home.mkdir()
        (self.home / "telegram-gateway.log").write_text("omitida\n" * 25 + TOKEN + "\n", encoding="utf-8")
        self.runner.return_value.stdout = "Estado: En ejecución\nÚltima ejecución: hoy\n" + TOKEN
        code, text = self.call("status")
        self.assertEqual(code, 0)
        self.assertIn("En ejecución", text)
        self.assertIn("Última ejecución", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(text.count("omitida"), 19)

    def test_failures_and_absent_task_are_reported(self) -> None:
        self.runner.return_value = subprocess.CompletedProcess([], 1, "", "No existe " + TOKEN)
        code, text = self.call("status")
        self.assertEqual(code, util.EXIT_INPUT)
        self.assertIn("puede no estar registrada", text)
        self.assertNotIn(TOKEN, text)
        for error in (OSError("fallo"), subprocess.TimeoutExpired("schtasks", 30)):
            self.runner.side_effect = error
            self.assertEqual(self.call("start")[0], util.EXIT_UNAVAILABLE)

    def test_other_platforms_do_not_run_or_write(self) -> None:
        for action in ("enable", "disable", "start", "status"):
            code, text = service.autostart(action, windows=False, run=self.runner)
            self.assertEqual(code, util.EXIT_UNAVAILABLE)
            self.assertIn("solo está implementado en Windows", text)
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_log_filters_child_output_and_propagates_failure(self) -> None:
        process = mock.MagicMock()
        process.stdout = io.StringIO("mensaje " + TOKEN + "\nerror\n")
        process.wait.return_value = 7
        process.__enter__.return_value = process
        log = self.tmp / "gateway.log"
        with mock.patch.object(service.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(service.run_logged(str(self.launcher), str(self.tmp), str(log)), 7)
        self.assertNotIn(TOKEN, log.read_text(encoding="utf-8"))
        self.assertIn("error", log.read_text(encoding="utf-8"))
        self.assertEqual(popen.call_args.args[0], [str(self.launcher), "telegram", "run"])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_cli_routes_all_actions_without_gateway_check(self) -> None:
        self.init_project()
        with mock.patch.object(service, "autostart", return_value=(0, "servicio")) as handler:
            for action in ("enable", "disable", "status", "start"):
                code, out, err = self.cli("telegram", "autostart", action)
                self.assertEqual(code, 0, out + err)
                self.assertIn("servicio", out)
                self.assertEqual(handler.call_args.args, (action,))
        self.check.assert_not_called()
