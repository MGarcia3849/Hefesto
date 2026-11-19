"""Regresiones adversariales de movimiento y autoridad fuera del checkout."""

from __future__ import annotations

import json
from unittest import mock

from hefesto import approval, guard
from hefesto.util import EXIT_BLOCKED, HefestoError
from tests.helpers import git, tty
from tests.test_addon_approval import ApprovalCase, binary_diff, diff_for


class DetectionBypassTests(ApprovalCase):
    def test_resources_and_libraries(self) -> None:
        samples = (
            ("a.svg", '<animate attributeName="x"/>', "motion-graphic"),
            ("a.astro", '<video src="a.webm"/>', "video"),
            ("a.mjs", "gsap.to(el, {x: 5});", "animation"),
            ("a.cjs", 'require("motion");', "animation"),
            ("a.gltf", '{"asset":{"version":"2.0"},"meshes":[]}', "3d"),
            ("a.json", '{"fr":30,"ip":0,"op":60,"layers":[]}', "motion-graphic"),
            ("bundle.unknown", 'requestAnimationFrame(draw);', "animation"),
            ("a.css", 'a { animation: none, spin 1s; }', "animation"),
            ("a.css", 'a { transition: none, opacity 1s; }', "animation"),
            ("a.css", 'a { animation:\n spin 1s; }', "animation"),
        )
        for path, line, effect in samples:
            with self.subTest(path=path, line=line):
                findings = approval.detect_motion(diff_for(path, added=line.splitlines()))
                self.assertIn(effect, [f["effect"] for f in findings])

    def test_ambiguous_binary_resources_fail_closed(self) -> None:
        for path in ("a.gif", "a.webp", "a.lottie", "asset.unknown"):
            with self.subTest(path=path):
                self.assertTrue(approval.detect_motion(binary_diff(path)))

    def test_ignored_bundle_is_checked_at_every_stage(self) -> None:
        (self.project / ".gitignore").write_text("dist/\n", encoding="utf-8")
        git(self.project, "add", ".gitignore")
        git(self.project, "commit", "-qm", "ignore output")
        base = git(self.project, "rev-parse", "HEAD")
        output = self.project / "dist" / "bundle.mjs"
        output.parent.mkdir()
        output.write_text("gsap.to(el, {x: 5});", encoding="utf-8")
        for stage in approval.GATE_STAGES:
            with self.subTest(stage=stage):
                with self.assertRaises(HefestoError) as ctx:
                    approval.gate(self.project, self.project, base, "web", stage)
                self.assertEqual(ctx.exception.code, EXIT_BLOCKED)

    def test_moved_motion_and_static_multiline_remain_allowed(self) -> None:
        line = "a { animation: spin 1s; }"
        self.assertEqual(approval.detect_motion(diff_for("a.css", added=[line], removed=[line])), [])
        self.assertEqual(approval.detect_motion(diff_for("a.css", added=["a { animation:", "none; }"])), [])

    def test_existing_multiline_declaration_cannot_hide_new_value(self) -> None:
        path = self.project / "a.css"
        path.write_text("a { animation:\n none; }\n", encoding="utf-8")
        git(self.project, "add", "a.css")
        git(self.project, "commit", "-qm", "static")
        base = git(self.project, "rev-parse", "HEAD")
        path.write_text("a { animation:\n spin 1s; }\n", encoding="utf-8")
        with self.assertRaises(HefestoError):
            approval.gate(self.project, self.project, base, "web", "checkpoint")

    def test_binary_approval_covers_only_exact_content(self) -> None:
        base = git(self.project, "rev-parse", "HEAD")
        path = self.project / "a.webp"
        path.write_bytes(b"RIFF\x00animated WEBP one")
        proposal = approval.propose(self.project, "a.webp", "web", "motion-graphic", "Asset animation")
        with tty(True, ["s"]):
            approval.approve(self.project, proposal["id"])
        approval.gate(self.project, self.project, base, "web", "checkpoint", consume=True)
        git(self.project, "add", "a.webp")
        git(self.project, "commit", "-qm", "asset")
        self.assertTrue(approval.gate(self.project, self.project, base, "web", "finish"))
        path.write_bytes(b"RIFF\x00animated WEBP two")
        with self.assertRaises(HefestoError):
            approval.gate(self.project, self.project, base, "web", "deploy")

    def test_staged_motion_is_blocked_even_when_worktree_is_static(self) -> None:
        base = git(self.project, "rev-parse", "HEAD")
        path = self.project / "a.mjs"
        path.write_text("gsap.to(el, {x: 1});", encoding="utf-8")
        git(self.project, "add", "a.mjs")
        path.write_text("const x = 1;", encoding="utf-8")
        with self.assertRaises(HefestoError):
            approval.gate(self.project, self.project, base, "web", "checkpoint")


class AuthorityBypassTests(ApprovalCase):
    def test_complete_public_record_cannot_authorize(self) -> None:
        proposal = self.propose()
        forged = {"id": "APR-forged", "proposal": proposal["id"],
                  **{k: proposal[k] for k in ("project", "page", "kind", "effect", "sha256")},
                  "approved_at": "2026-11-18", "channel": "tty", "revoked_at": None, "used_for": []}
        approval.approvals_path(self.project).write_text(json.dumps({"approvals": [forged]}), encoding="utf-8")
        self.assertEqual(approval.active(self.project), [])

    def test_edit_and_rollback_of_local_record_fail_closed(self) -> None:
        record = self.approved()
        path = approval.approvals_path(self.project)
        before = path.read_text(encoding="utf-8")
        approval.consume(self.project, record["id"], ["used"])
        path.write_text(before, encoding="utf-8")
        self.assertEqual(approval.active(self.project), [])

    def test_missing_or_corrupt_independent_record_fails_closed(self) -> None:
        self.approved()
        external = approval._authority_path(self.project)
        self.assertFalse(external.is_relative_to(self.project))
        external.write_text("broken", encoding="utf-8")
        self.assertEqual(approval.active(self.project), [])
        external.unlink()
        self.assertEqual(approval.active(self.project), [])

    def test_authority_inside_checkout_is_rejected(self) -> None:
        with mock.patch("hefesto.util.hefesto_home", return_value=self.project / "home"):
            with self.assertRaises(HefestoError):
                self.approved()

    def test_python_hooks_reject_indirect_writers_allow_normal_verification(self) -> None:
        commands = (
            'python -c "from hefesto import approval as a; a._save(p, [])"',
            'python -c "from pathlib import Path; Path(\'.hefesto/\' + \'approvals.json\').write_text(\'{}\')"',
            "python writer.py", "python -m arbitrary_writer",
            '\"C:/Python/python.exe\" -c "exec(payload)"',
            "python -m unittest tests.test_addon_approval && python writer.py",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(guard.check_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}))
        for command in ("python -m unittest tests.test_addon_approval.VisualGateTests", "python tools/dev.py lint",
                        "python -m unittest tests.test_addon_approval.VisualGateTests && python tools/dev.py lint"):
            self.assertIsNone(guard.check_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}))
