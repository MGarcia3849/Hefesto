"""TASK-2801: la versión publicada es 2.5.0 y la documentación y la migración la acompañan."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from hefesto import __version__, distribution

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTest(unittest.TestCase):
    def test_version_is_2_5_0(self) -> None:
        self.assertEqual(__version__, "2.5.0")

    def test_changelog_opens_with_the_release_section(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = re.findall(r"^## (v\d+\.\d+\.\d+)", text, flags=re.MULTILINE)
        self.assertEqual(releases[:2], ["v2.5.0", "v2.1.0"])
        before_release = text.split("## v2.5.0", 1)[0]
        self.assertIn("Keep a Changelog", before_release, "el preámbulo va antes de las versiones")
        self.assertNotRegex(text, r"\w\?\w", "sin caracteres corruptos")

    def test_readme_and_bootstrap_pin_the_release(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**v2.5.0", readme)
        self.assertIn("gh release create v2.5.0", readme)
        self.assertNotIn("git tag -a v2.1.0", readme)
        self.assertIn("`v2.5.0`", (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8"))

    def test_addon_defaults_migration_targets_2_5_0(self) -> None:
        steps = {fn: version for version, fn in distribution.MIGRATION_STEPS}
        self.assertEqual(steps[distribution._migration_addon_defaults], "2.5.0")
        self.assertTrue(distribution.needs_migration({"hefesto_version": "2.1.0-dev"}))
        self.assertFalse(distribution.needs_migration({"hefesto_version": "2.5.0"}))


if __name__ == "__main__":
    unittest.main()
