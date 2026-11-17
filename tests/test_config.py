"""Tests de validación de configuración TOML."""

from __future__ import annotations

from pathlib import Path

from hefesto import config
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase


class DeployAttemptsConfigTests(HefestoTestCase):
    def write_toml(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[delivery]\nmax_deploy_attempts = {value}\n", encoding="utf-8")

    def test_global_and_project_toml_accept_positive_integer(self) -> None:
        global_toml = config.global_toml_path()
        project_toml = config.project_toml_path(self.project)
        self.write_toml(global_toml, "3")
        self.write_toml(project_toml, "4")

        cfg = config.load(self.project)

        self.assertEqual(cfg.get("delivery", "max_deploy_attempts"), 4)
        self.assertEqual(config.validate(cfg), [])

    def test_global_and_project_toml_reject_non_positive_or_non_integer(self) -> None:
        for path in (config.global_toml_path(), config.project_toml_path(self.project)):
            for value in ("0", "-1", "true", '{ "attempts" = 2 }'):
                with self.subTest(path=path, value=value):
                    self.write_toml(path, value)
                    with self.assertRaises(HefestoError):
                        config.load(self.project)
            path.unlink()

    def test_other_free_table_limits_remain_unchanged(self) -> None:
        path = config.project_toml_path(self.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[delivery]\nmax_deploy_attempts = 3\n"
            "[quota.max_calls_per_provider_per_loop]\nclaude = 2\n",
            encoding="utf-8",
        )

        cfg = config.load(self.project)

        self.assertEqual(cfg.get("delivery", "max_deploy_attempts"), 3)
        self.assertEqual(cfg.get("quota", "max_calls_per_provider_per_loop", "claude"), 2)
        self.assertEqual(config.validate(cfg), [])
