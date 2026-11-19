"""TASK-2706: los verify no heredan la consola del usuario y `confirm` la deja en modo cocinado."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hefesto import evidence, util


class ChildConsoleKwargsTest(unittest.TestCase):
    def test_stdin_is_devnull(self) -> None:
        self.assertIs(util.child_console_kwargs()["stdin"], subprocess.DEVNULL)

    def test_windows_hides_child_console(self) -> None:
        with mock.patch.object(util.os, "name", "nt"):
            kwargs = util.child_console_kwargs()
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_posix_has_no_creationflags(self) -> None:
        with mock.patch.object(util.os, "name", "posix"):
            self.assertNotIn("creationflags", util.child_console_kwargs())


class RunVerifyIsolationTest(unittest.TestCase):
    def test_run_verify_does_not_inherit_console(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(evidence, "find_bash", return_value="bash"), \
                mock.patch.object(evidence.subprocess, "run", return_value=completed) as run:
            project = Path(tmp)
            evidence.run_verify(project, project, "TASK-X", "true", 10)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & 0x08000000)


class RestoreConsoleTest(unittest.TestCase):
    def test_noop_outside_windows(self) -> None:
        with mock.patch.object(util.os, "name", "posix"):
            util.restore_console()  # no debe tocar ctypes ni fallar

    def test_confirm_restores_console_before_asking(self) -> None:
        calls: list[str] = []
        with mock.patch.object(util, "restore_console", side_effect=lambda: calls.append("restore")), \
                mock.patch("builtins.input", side_effect=lambda _q: calls.append("input") or "s"):
            self.assertTrue(util.confirm("¿Seguro?"))
        self.assertEqual(calls, ["restore", "input"])

    @unittest.skipUnless(os.name == "nt", "solo Windows")
    def test_restore_sets_cooked_bits_on_raw_console(self) -> None:
        import ctypes

        modes = {-10: 0, -11: 0}
        set_calls: dict[int, int] = {}

        class FakeKernel:
            def GetStdHandle(self, std):  # noqa: N802
                return std

            def GetConsoleMode(self, handle, ref):  # noqa: N802
                ctypes.cast(ref, ctypes.POINTER(ctypes.c_ulong)).contents.value = modes[handle]
                return 1

            def SetConsoleMode(self, handle, mode):  # noqa: N802
                set_calls[handle] = mode
                return 1

        fake_windll = mock.Mock(kernel32=FakeKernel())
        with mock.patch.object(ctypes, "windll", fake_windll):
            util.restore_console()
        self.assertEqual(set_calls[-10] & 0x7, 0x7)
        self.assertEqual(set_calls[-11] & 0x3, 0x3)

    def test_restore_never_raises(self) -> None:
        with mock.patch.object(util.os, "name", "nt"), \
                mock.patch.dict("sys.modules", {"ctypes": None}):
            util.restore_console()


if __name__ == "__main__":
    unittest.main()
