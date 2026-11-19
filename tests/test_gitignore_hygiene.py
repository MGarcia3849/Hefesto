"""TASK-2901: las skills personales promovidas y las copias de `migrate` no aparecen como pendientes."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ignored(rel: str) -> bool:
    proc = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=ROOT, capture_output=True)
    return proc.returncode == 0


class GitignoreHygieneTest(unittest.TestCase):
    def test_migrate_backups_are_ignored(self) -> None:
        self.assertTrue(ignored(".hefesto/backups/20261119_000000/settings.json"))

    def test_personal_promoted_skills_are_ignored(self) -> None:
        for root in (".claude/skills", ".agents/skills"):
            self.assertTrue(ignored(f"{root}/lark-calendar/SKILL.md"), root)
            self.assertTrue(ignored(f"{root}/wayfinder/agents/openai.yaml"), root)

    def test_generated_hefesto_skills_stay_versioned(self) -> None:
        for root in (".claude/skills", ".agents/skills"):
            self.assertFalse(ignored(f"{root}/hefesto-plan/SKILL.md"), root)
            self.assertFalse(ignored(f"{root}/hefesto-skill-retro/SKILL.md"), root)

    def test_already_tracked_skills_are_not_affected(self) -> None:
        tracked = subprocess.run(["git", "ls-files", "--", ".claude/skills/lark-doc"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.split()
        self.assertTrue(tracked, "lark-doc sigue versionada")

    def test_other_entries_are_not_ignored(self) -> None:
        self.assertFalse(ignored(".hefesto/ROADMAP.md"))
        self.assertFalse(ignored(".claude/settings.json"))


if __name__ == "__main__":
    unittest.main()
