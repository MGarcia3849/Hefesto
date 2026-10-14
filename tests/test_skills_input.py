"""Regresiones de la lista blanca de find, con npx falso y registro de llamadas."""

from __future__ import annotations

from hefesto import skills
from hefesto.util import HefestoError
from tests.test_skills_ecosystem import SkillsCase


class FindInputTests(SkillsCase):
    def test_rejects_metacharacters_without_running_npx(self) -> None:
        for char in ('&', '|', '^', '%', '(', ')', '<', '>', '"', "'", '\n', '\r', '\t',
                     '\x00', '\x7f', '\u200b', '\u00a0', '\\', ';', '$', '`', '!', '*', '?', ':', '😀'):
            for field in ("term", "owner"):
                with self.subTest(char=char, field=field):
                    kwargs = {"term": "ui", field: f"ui{char}echo"}
                    with self.assertRaises(HefestoError) as ctx:
                        skills.find(**kwargs)
                    self.assertEqual(ctx.exception.code, 1)
                    self.assertEqual(self.calls_of("npx"), [])

    def test_rejects_empty_leading_hyphen_and_overlong_inputs(self) -> None:
        for value in ("", "   ", "-ui", "--help", "  -ui", "a" * 101, " " + "a" * 100):
            for field in ("term", "owner"):
                with self.subTest(value=value, field=field):
                    kwargs = {"term": "ui", field: value}
                    with self.assertRaises(HefestoError) as ctx:
                        skills.find(**kwargs)
                    self.assertEqual(ctx.exception.code, 1)
                    self.assertEqual(self.calls_of("npx"), [])

    def test_accepts_valid_terms_unchanged(self) -> None:
        terms = ("ui", "diseño ÁÉÍÓÚ áéíóú Ññ ü", "ui-kit_2.0", "@acme/ui-kit", " ui ", "a" * 100)
        for term in terms:
            with self.subTest(term=term):
                self.assertTrue(skills.find(term))
                self.assertEqual(self.calls_of("npx")[-1]["argv"], ["-y", "skills@1.7.2", "find", term])
        self.assertEqual(len(self.calls_of("npx")), len(terms))

    def test_accepts_valid_owner_and_keeps_filtering(self) -> None:
        owners = ("acme", "diseño ÁÉÍÓÚ Ññ ü", "owner-2_test.0/repo@skill", "a" * 100)
        for owner in owners:
            with self.subTest(owner=owner):
                results = skills.find("ui", owner=owner)
                self.assertEqual([r["owner"] for r in results], ["acme"] if owner == "acme" else [])
                self.assertEqual(self.calls_of("npx")[-1]["argv"], ["-y", "skills@1.7.2", "find", "ui"])
        self.assertEqual(len(self.calls_of("npx")), len(owners))

    def test_invalid_input_is_rejected_even_without_npx(self) -> None:
        self.use_fakes()
        for kwargs in ({"term": "ui&echo(x)"}, {"term": "ui", "owner": "acme&echo(x)"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HefestoError) as ctx:
                    skills.find(**kwargs)
                self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.calls_of("npx"), [])

    def test_cli_rejects_unsafe_term_and_owner_without_npx(self) -> None:
        for args in (("ui&echo(x)",), ("ui", "--owner", "acme&echo(x)")):
            with self.subTest(args=args):
                code, _, _ = self.run_cli("skill", "find", *args)
                self.assertEqual(code, 1)
                self.assertEqual(self.calls_of("npx"), [])
