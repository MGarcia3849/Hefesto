"""Pruebas offline del wrapper Modern Web Guidance."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from hefesto import config, web_guidance
from hefesto.util import EXIT_INPUT, HefestoError
from tests.helpers import HefestoTestCase


class WebGuidanceRuntimeTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def test_search_pins_binary_and_disables_telemetry(self) -> None:
        version = subprocess.CompletedProcess([], 0, "1.0.0", "")
        action = subprocess.CompletedProcess([], 0, "current guidance", "")
        with mock.patch("hefesto.web_guidance.util.which", return_value="/opt/modern-web-guidance"), \
                mock.patch("hefesto.web_guidance.subprocess.run", side_effect=[version, action]) as run:
            result = web_guidance.search(self.project, " container queries ")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "current guidance")
        self.assertEqual(run.call_args_list[1].args[0], ["/opt/modern-web-guidance", "search", "container queries"])
        self.assertEqual(run.call_args_list[1].kwargs["env"]["DISABLE_TELEMETRY"], "1")
        self.assertNotEqual(run.call_args_list[1].kwargs.get("shell"), True)

    def test_bad_version_never_runs_action(self) -> None:
        version = subprocess.CompletedProcess([], 0, "2.0.0", "")
        with mock.patch("hefesto.web_guidance.util.which", return_value="/bin/modern-web-guidance"), \
                mock.patch("hefesto.web_guidance.subprocess.run", return_value=version) as run:
            with self.assertRaises(HefestoError):
                web_guidance.search(self.project, "container queries")
        self.assertEqual(run.call_count, 1)

    def test_output_is_redacted_and_bounded(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("web_guidance", "max_output_chars"), 24)
        version = subprocess.CompletedProcess([], 0, "1.0.0", "")
        response = subprocess.CompletedProcess([], 2, "x" * 200, "external error")
        with mock.patch("hefesto.web_guidance.util.which", return_value="/bin/modern-web-guidance"), \
                mock.patch("hefesto.web_guidance.subprocess.run", side_effect=[version, response]):
            result = web_guidance.retrieve(self.project, "css/container")
        self.assertFalse(result.ok)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output), 24)

    def test_invalid_and_escaping_inputs_do_not_spawn(self) -> None:
        with mock.patch("hefesto.web_guidance.subprocess.run") as run:
            for action in (lambda: web_guidance.search(self.project, "--help"),
                           lambda: web_guidance.retrieve(self.project, "../secret"),
                           lambda: web_guidance.legacy_scan(self.project, "..")):
                with self.assertRaises(HefestoError) as ctx:
                    action()
                self.assertEqual(ctx.exception.code, EXIT_INPUT)
        run.assert_not_called()
