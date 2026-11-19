"""TASK-3406: la documentación no contiene letras acentuadas sustituidas por «?» (texto mal codificado)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LETTER = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
INSIDE = re.compile(rf"[{LETTER}]\?[{LETTER}]")          # l?mites, t?cnicos
WORD_END = re.compile(rf"[{LETTER}]\?(?=[ ,.;:)])")       # qu? , est?  (fuera de una pregunta ¿…?)
START = re.compile(rf"(?:^|[\s(*])\?[{LETTER}]{{2,}}")    # ?nicamente
DASH = re.compile(r"\d \? [A-ZÁÉÍÓÚ]")                    # «2026-11-19 ? Ideas»: raya perdida


def documents() -> list[Path]:
    paths = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "BOOTSTRAP.md", ROOT / ".hefesto" / "DECISIONS.md"]
    paths += sorted((ROOT / "docs").glob("*.md"))
    return [p for p in paths if p.is_file()]


def corrupted(text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if "http" in line and "?" in line.split("http", 1)[1]:
            line = re.sub(r"https?://\S+", "", line)  # las URL pueden llevar «?» legítimos
        hits = INSIDE.findall(line) + START.findall(line) + DASH.findall(line)
        if "¿" not in line:
            hits += WORD_END.findall(line)
        found += [f"{number}: {hit}" for hit in hits]
    return found


class DocsEncodingTest(unittest.TestCase):
    def test_detector_catches_corruption_and_allows_questions(self) -> None:
        self.assertTrue(corrupted("sus l?mites legales"))
        self.assertTrue(corrupted("qu? funcionalidades"))
        self.assertTrue(corrupted("se adoptan ?nicamente"))
        self.assertTrue(corrupted("## 2026-11-19 ? Ideas de Craft Anything"))
        self.assertFalse(corrupted("¿Integrar la rama en main? [s/N]"))
        self.assertFalse(corrupted("ver https://example.com/a?b=c para más"))
        self.assertFalse(corrupted("límites técnicos y qué está"))

    def test_documents_have_no_corrupted_characters(self) -> None:
        for path in documents():
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertEqual(corrupted(path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
