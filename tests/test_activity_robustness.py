"""Regresiones de procesos, redacción y confinamiento de los logs de agentes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from hefesto import activity, config, lock
from hefesto.adapters.base import Adapter, AdapterResult, Call, _terminate_tree
from tests.helpers import HefestoTestCase


class PythonAdapter(Adapter):
    provider = "python"

    def binary(self) -> str:
        return sys.executable

    def command(self, binary: str, call: Call) -> list[str]:
        return [binary, "-u", str(call.workdir / "child.py")]

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        return self.result(call, returncode == 0, "terminado", None, "unknown", call.extra["log_path"])


class ProcessTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project.mkdir()
        self.adapter = PythonAdapter(config.load(self.project))
        self.call = Call("forge", "prompt", "TASK-817", "robust", "default", self.project, self.project, timeout=1)
        self.log = self.project / ".hefesto/logs/runs/robust-python.log"

    def script(self, text: str) -> None:
        (self.project / "child.py").write_text(text, encoding="utf-8")

    def wait_for(self, predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("tiempo de espera agotado")

    def alive(self, pid: int) -> bool:
        # Un zombi POSIX ya murió, aunque aún espere a que init lo recoja.
        stat = Path(f"/proc/{pid}/stat")
        if os.name != "nt" and stat.exists():
            try:
                if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
            except FileNotFoundError:
                return False
        return lock.pid_alive(pid)

    def assert_dead(self, pids: list[int]) -> None:
        self.wait_for(lambda: all(not self.alive(pid) for pid in pids))
        self.assertEqual(activity.list_active(self.project), [])
        self.assertEqual(list(activity.active_dir(self.project).glob("*.json")), [])

    def tree_script(self) -> Path:
        self.script("import os, subprocess, sys, time, json\nfrom pathlib import Path\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    "Path('pids.json').write_text(json.dumps([os.getpid(), child.pid]))\n"
                    "time.sleep(120)\n")
        return self.project / "pids.json"

    def test_timeout_includes_blocked_stdin(self) -> None:
        self.script("import os, time\nfrom pathlib import Path\n"
                    "Path('pid').write_text(str(os.getpid()))\ntime.sleep(120)\n")
        self.call.prompt = "x" * (4 * 1024 * 1024)
        start = time.monotonic()
        result = self.adapter.run(self.call)
        self.assertEqual(result.error_type, "timeout")
        self.assertLess(time.monotonic() - start, 8)
        self.assert_dead([int((self.project / "pid").read_text())])

    def test_timeout_kills_child_and_grandchild_before_unregistering(self) -> None:
        pid_file = self.tree_script()
        original_finish = activity.finish

        def finish(project: Path, run_id: str) -> None:
            self.assertTrue((activity.active_dir(project) / f"{run_id}.json").exists())
            self.wait_for(lambda: all(not self.alive(pid) for pid in json.loads(pid_file.read_text())))
            original_finish(project, run_id)

        with mock.patch("hefesto.activity.finish", side_effect=finish):
            self.assertEqual(self.adapter.run(self.call).error_type, "timeout")
        self.assert_dead(json.loads(pid_file.read_text()))

    def test_exception_and_keyboard_interrupt_kill_tree(self) -> None:
        for error in (RuntimeError("interrumpido"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                pid_file = self.tree_script()
                pid_file.unlink(missing_ok=True)
                original_register = activity.register

                def interrupt(*args, **kwargs):  # type: ignore[no-untyped-def]
                    original_register(*args, **kwargs)
                    self.wait_for(pid_file.exists)
                    raise error

                with mock.patch("hefesto.activity.register", side_effect=interrupt):
                    with self.assertRaises(type(error)):
                        self.adapter.run(self.call)
                self.assert_dead(json.loads(pid_file.read_text()))

    def test_log_open_error_does_not_launch_process(self) -> None:
        self.log.mkdir(parents=True)
        with mock.patch("hefesto.adapters.base.subprocess.Popen") as popen:
            with self.assertRaises(OSError):
                self.adapter.run(self.call)
        popen.assert_not_called()

    def test_keyboard_interrupt_while_waiting_kills_tree(self) -> None:
        pid_file = self.tree_script()
        original_wait = subprocess.Popen.wait
        interrupted = False

        def wait(proc, timeout=None):  # type: ignore[no-untyped-def]
            nonlocal interrupted
            if not interrupted and pid_file.exists():
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(proc, timeout=timeout)

        with mock.patch("subprocess.Popen.wait", new=wait):
            with self.assertRaises(KeyboardInterrupt):
                self.adapter.run(self.call)
        self.assert_dead(json.loads(pid_file.read_text()))

    def test_windows_taskkill_failure_still_kills_tree(self) -> None:
        if os.name != "nt":
            self.skipTest("fallback de Windows")
        pid_file = self.tree_script()
        denied = subprocess.CompletedProcess(["taskkill"], 1, b"", b"Access denied")
        with mock.patch("hefesto.adapters.base.subprocess.run", return_value=denied):
            self.assertEqual(self.adapter.run(self.call).error_type, "timeout")
        self.assert_dead(json.loads(pid_file.read_text()))

    def test_live_output_and_multiline_redaction_on_success_and_timeout(self) -> None:
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.log.unlink(missing_ok=True)
                self.script("import sys, time\n"
                            "print('PASO visible', flush=True)\n"
                            "print('password =\\n\\\"synthetic-value-only\\\"', flush=True)\n"
                            "print('client_secret:\\n\\\"synthetic-stderr-only\\\"', file=sys.stderr, flush=True)\n"
                            f"time.sleep({120 if timeout else 0.5})\n")
                results = []
                worker = threading.Thread(target=lambda: results.append(self.adapter.run(self.call)), daemon=True)
                worker.start()
                self.wait_for(lambda: self.log.exists() and "PASO visible" in self.log.read_text(encoding="utf-8"))
                self.assertTrue(worker.is_alive())
                worker.join(timeout=8)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0].error_type, "timeout" if timeout else None)
                content = self.log.read_text(encoding="utf-8")
                self.assertIn("PASO visible", content)
                self.assertIn("[REDACTED]", content)
                self.assertNotIn("synthetic-value-only", content)
                self.assertNotIn("synthetic-stderr-only", content)

    def test_platform_tree_termination_arguments(self) -> None:
        proc = mock.Mock(pid=123)
        if os.name == "nt":
            with mock.patch("hefesto.adapters.base.subprocess.run") as run:
                _terminate_tree(proc)
            self.assertEqual(run.call_args.args[0][1:], ["/T", "/F", "/PID", "123"])
            self.assertFalse(run.call_args.kwargs.get("shell", False))
        else:
            import signal

            with mock.patch("hefesto.adapters.base.os.killpg") as killpg:
                _terminate_tree(proc)
            killpg.assert_called_once_with(123, signal.SIGKILL)
        proc.wait.assert_called_once_with(timeout=5)


class LogConfinementTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        self.valid = self.project / ".hefesto/logs/runs/inside.log"
        self.valid.parent.mkdir(parents=True, exist_ok=True)
        self.valid.write_text("salida permitida\n", encoding="utf-8")
        self.outside = self.project / "outside.txt"
        self.outside.write_text("CONTENIDO EXTERIOR\n", encoding="utf-8")

    def register(self, path: str) -> Path:
        return activity.register(self.project, "entry", "worker", "forge", "python", "python:default",
                                 "TASK-817", path)

    def test_register_normalizes_absolute_and_relative_paths(self) -> None:
        for path in (str(self.valid), ".hefesto/logs/runs/../runs/inside.log"):
            entry = json.loads(self.register(path).read_text(encoding="utf-8"))
            self.assertEqual(entry["log_path"], ".hefesto/logs/runs/inside.log")

    def assert_rejected(self, path: str) -> None:
        with self.assertRaises(ValueError):
            self.register(path)
        # Un registro manipulado se valida de nuevo antes de leerlo en la CLI.
        entry_path = self.register(str(self.valid))
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry["log_path"] = path
        entry_path.write_text(json.dumps(entry), encoding="utf-8")
        for args in (("jobs",), ("jobs", "--json"), ("watch", "--once")):
            code, out, err = self.cli(*args)
            self.assertEqual(code, 0, err)
            self.assertIn("(ruta de log no válida)", out)
            self.assertNotIn("CONTENIDO EXTERIOR", out)

    def test_absolute_traversal_and_prefix_sibling_are_rejected(self) -> None:
        sibling = self.project / ".hefesto/logs-other/outside.txt"
        sibling.parent.mkdir()
        sibling.write_text("CONTENIDO EXTERIOR", encoding="utf-8")
        for path in (str(self.outside), ".hefesto/logs/../../outside.txt", str(sibling), "", None):
            with self.subTest(path=path):
                self.assert_rejected(path)

    def test_symlink_escape_is_rejected(self) -> None:
        link = self.valid.parent / "link.log"
        try:
            link.symlink_to(self.outside)
        except OSError as exc:
            self.skipTest(f"enlaces simbólicos no disponibles: {exc}")
        self.assert_rejected(str(link))
