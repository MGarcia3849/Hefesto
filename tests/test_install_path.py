"""Tests para install.sh --add-to-path (TASK-809)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hefesto import util

ROOT = Path(__file__).resolve().parent.parent


class InstallPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bash = util.find_bash()
        if not self.bash:
            self.skipTest("No se encontró bash en el sistema.")
        self._tmp = tempfile.TemporaryDirectory(prefix="hefesto-install-path-")
        self.tmp = Path(self._tmp.name)
        self.bin_dir = self.tmp / "bin"
        self.home_dir = self.tmp / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_install(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            HEFESTO_BIN_DIR=str(self.bin_dir),
            HEFESTO_HOME=str(self.home_dir),
            HEFESTO_PATH_DRY_RUN="1",
            PYTHONPATH=str(ROOT),
        )
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(self.bash), "install.sh", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def test_without_add_to_path_dry_run_not_present(self) -> None:
        res = self._run_install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("[dry-run]", res.stdout)
        self.assertNotIn("[dry-run]", res.stderr)

    def test_with_add_to_path_dry_run_present_with_directory(self) -> None:
        res = self._run_install("--add-to-path")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("[dry-run]", res.stdout)
        dry_run_lines = [line for line in res.stdout.splitlines() if line.startswith("[dry-run]")]
        self.assertTrue(dry_run_lines, f"No se encontró línea que empiece por [dry-run]:\n{res.stdout}")
        self.assertTrue(
            any(str(self.bin_dir) in line or self.bin_dir.as_posix() in line for line in dry_run_lines),
            f"El directorio {self.bin_dir} no aparece en la línea [dry-run]:\n{res.stdout}",
        )
        self.assertIn("terminal nueva", res.stdout.lower())

    def test_linux_profile_added_once_without_dry_run(self) -> None:
        if os.name == "nt":
            self.skipTest("Solo aplicable en entornos no-Windows (Linux/macOS/WSL).")
        profile_file = self.tmp / "test_profile"
        env_extra = {
            "HEFESTO_PROFILE": str(profile_file),
            "HEFESTO_PATH_DRY_RUN": "0",
        }
        for _ in range(2):
            res = self._run_install("--add-to-path", env_extra=env_extra)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(profile_file.exists())
        content = profile_file.read_text(encoding="utf-8")
        expected_line = f'export PATH="{self.bin_dir}:$PATH" # hefesto'
        self.assertEqual(content.count(expected_line), 1, f"La línea debe aparecer exactamente una vez:\n{content}")


if __name__ == "__main__":
    unittest.main()
