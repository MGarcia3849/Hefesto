"""Regresiones adversarias de TASK-803; repositorios y procesos aislados, sin red."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from hefesto import config, skills, sync, util
from hefesto.util import HefestoError
from tests.helpers import git, tty
from tests.test_skills_ecosystem import SAFE_SKILL, SkillsCase


class SkillsSecurityTests(SkillsCase):
    def source(self, path: Path) -> dict:
        return json.loads((path / "SOURCE.json").read_text(encoding="utf-8"))

    def promote(self) -> Path:
        with tty(True, ["s"]), contextlib.redirect_stdout(io.StringIO()):
            return skills.promote("acme-pretty-ui")

    def stored_skill(self, folder: str, spec_name: str, declared: str = "pretty-ui") -> Path:
        path = util.hefesto_home() / "skills" / "promoted" / folder
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(SAFE_SKILL.replace("name: pretty-ui", f"name: {declared}"), encoding="utf-8")
        util.atomic_write_json(path / "SOURCE.json", {"spec": f"acme/ui-kit@{spec_name}", "commit": "origin"})
        return path

    def test_all_fetch_git_processes_discard_inherited_git_environment(self) -> None:
        repo = self.make_repo({"skills/pretty-ui/.gitattributes": "* filter=hostile\n"})
        expected_commit = git(repo, "rev-parse", "HEAD")
        hostile = {
            "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "protocol.ext.allow", "GIT_CONFIG_VALUE_0": "always",
            "GIT_CONFIG_KEY_1": "filter.hostile.smudge", "GIT_CONFIG_VALUE_1": "unexpected-command",
            "GIT_CONFIG_KEY_2": "filter.hostile.required", "GIT_CONFIG_VALUE_2": "true",
            "GIT_CONFIG_PARAMETERS": "'protocol.ext.allow=always'", "GIT_DIR": str(self.tmp / "missing"),
            "GIT_WORK_TREE": str(self.tmp / "elsewhere"), "GIT_TEMPLATE_DIR": str(self.tmp / "templates"),
            "GIT_SSH_COMMAND": "unexpected-command", "GIT_ALLOW_PROTOCOL": "ext",
        }
        run = subprocess.run
        with mock.patch.dict(os.environ, hostile), mock.patch("hefesto.skills.subprocess.run", wraps=run) as calls:
            path = skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(self.source(path)["commit"], expected_commit)
        self.assertGreater(len(calls.call_args_list), 3)
        for call in calls.call_args_list:
            git_env = {k: v for k, v in call.kwargs["env"].items() if k.upper().startswith("GIT_")}
            self.assertEqual(set(git_env), {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                                            "GIT_LFS_SKIP_SMUDGE", "GIT_TERMINAL_PROMPT"})
            self.assertEqual(git_env["GIT_CONFIG_NOSYSTEM"], "1")
        clone = calls.call_args_list[0].args[0]
        for flag in ("--no-checkout", "protocol.allow=never", "protocol.file.allow=always", "protocol.https.allow=always"):
            self.assertIn(flag, clone)
        self.assertFalse(self.pwned.exists())

    def test_unsafe_git_locations_rejected_before_any_process(self) -> None:
        bad = ("ext::sh -c command", "fd::0", "ssh -oProxyCommand=command host", "ssh://host/repo",
               "git@host:repo", "http://host", "file:///tmp/repo", "relative", "--upload-pack=command",
               str(self.tmp / "ext::command"), "https:///missing-host", "https://host/path\ncommand",
               "https://host/path?redirect=ext::command", "https://host\\@other")
        for base in bad:
            with self.subTest(base=base):
                config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"), base)
                with mock.patch("hefesto.skills.subprocess.run") as run:
                    with self.assertRaises(HefestoError) as ctx:
                        skills.fetch("acme/ui-kit@pretty-ui")
                self.assertEqual(ctx.exception.code, 1)
                run.assert_not_called()

    def test_https_and_resulting_url_are_validated_without_network(self) -> None:
        config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"), "https://example.invalid/")
        with mock.patch("hefesto.skills._validate_git_location", wraps=skills._validate_git_location) as validate, \
                mock.patch("hefesto.skills.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "fake")) as run:
            with self.assertRaises(HefestoError):
                skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual([c.args[0] for c in validate.call_args_list],
                         ["https://example.invalid/", "https://example.invalid/acme/ui-kit.git"])
        self.assertIn("https://example.invalid/acme/ui-kit.git", run.call_args.args[0])

    def test_reserved_prefix_rejected_in_fetch_promote_and_update(self) -> None:
        for name in ("HEFESTO-next", "HeFeStO-plan", "hefesto-retro"):
            for operation, value in ((skills.fetch, f"acme/ui-kit@{name}"),
                                     (skills.promote, name), (skills.update, name)):
                with self.subTest(operation=operation.__name__, name=name), mock.patch("hefesto.skills.subprocess.run") as run:
                    with self.assertRaises(HefestoError) as ctx:
                        operation(value)
                    self.assertEqual(ctx.exception.code, 1)
                    run.assert_not_called()

    def test_reserved_spec_cannot_be_promoted_under_innocent_proposal_name(self) -> None:
        self.make_repo()
        path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)
        source["spec"] = "acme/ui-kit@HEFESTO-next"
        util.atomic_write_json(path / "SOURCE.json", source)
        with self.assertRaises(HefestoError) as ctx:
            self.promote()
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(path.exists())

    def test_frontmatter_name_rejects_reserved_and_non_whitelisted_values(self) -> None:
        repo = self.make_repo()
        for name in ("HEFESTO-next", "HeFeStO-plan", "../escape", "bad name", ".", "..", "", "name/child"):
            with self.subTest(name=name):
                (repo / "skills/pretty-ui/SKILL.md").write_text(
                    SAFE_SKILL.replace("name: pretty-ui", f"name: {name}"), encoding="utf-8")
                git(repo, "commit", "-q", "-am", "invalid-name")
                with self.assertRaises(HefestoError) as ctx:
                    skills.fetch("acme/ui-kit@pretty-ui")
                self.assertEqual(ctx.exception.code, 1)

    def test_frontmatter_mismatch_reports_override_and_spec_controls_active_name(self) -> None:
        repo = self.make_repo({"skills/pretty-ui/SKILL.md": SAFE_SKILL.replace("name: pretty-ui", "name: another-skill")})
        proposal = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(proposal)
        self.assertTrue(any(r["kind"] == "override" and "another-skill" in r["detail"] for r in source["risks"]))
        self.assertEqual(source["files"]["SKILL.md"], hashlib.sha256((proposal / "SKILL.md").read_bytes()).hexdigest())
        promoted = self.promote()
        self.assertIn("name: pretty-ui\n", (promoted / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(self.source(promoted), source)
        self.assertEqual(source["commit"], git(repo, "rev-parse", "HEAD"))
        # sync vuelve a aplicar la identidad incluso a una instalación previa sin normalizar.
        (promoted / "SKILL.md").write_text(SAFE_SKILL.replace("name: pretty-ui", "name: another-skill"), encoding="utf-8")
        targets = sync.targets(config.load())
        for root in (".agents/skills", ".claude/skills"):
            self.assertIn(b"name: pretty-ui\n", targets[f"{root}/pretty-ui/SKILL.md"])
            self.assertNotIn(f"{root}/another-skill/SKILL.md", targets)

    def test_frontmatter_case_difference_is_not_an_override(self) -> None:
        self.make_repo({"skills/pretty-ui/SKILL.md": SAFE_SKILL.replace("name: pretty-ui", "name: Pretty-UI")})
        path = skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(self.source(path)["risks"], [])
        self.assertIn("name: pretty-ui\n", (self.promote() / "SKILL.md").read_text(encoding="utf-8"))

    def test_promote_revalidates_frontmatter_before_confirmation(self) -> None:
        self.make_repo()
        path = skills.fetch("acme/ui-kit@pretty-ui")
        (path / "SKILL.md").write_text(SAFE_SKILL.replace("name: pretty-ui", "name: HEFESTO-next"), encoding="utf-8")
        with mock.patch("hefesto.util.confirm") as confirm, self.assertRaises(HefestoError) as ctx:
            skills.promote("acme-pretty-ui")
        self.assertEqual(ctx.exception.code, 1)
        confirm.assert_not_called()

    def test_duplicate_frontmatter_cannot_hide_reserved_name(self) -> None:
        self.make_repo({"skills/pretty-ui/SKILL.md": SAFE_SKILL.replace(
            "name: pretty-ui", "name: HEFESTO-next\nname: pretty-ui")})
        with self.assertRaises(HefestoError) as ctx:
            skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 1)

    def test_update_rejects_reserved_spec_from_existing_metadata(self) -> None:
        self.stored_skill("pretty-ui", "HEFESTO-next")
        with mock.patch("hefesto.skills.subprocess.run") as run, self.assertRaises(HefestoError) as ctx:
            skills.update("pretty-ui")
        self.assertEqual(ctx.exception.code, 1)
        run.assert_not_called()

    def test_sync_skips_reserved_directory_spec_and_frontmatter_with_warning(self) -> None:
        self.stored_skill("HEFESTO-next", "pretty-ui")
        self.stored_skill("innocent", "HeFeStO-next")
        self.stored_skill("safe-spec", "safe-spec", "HEFESTO-next")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            targets = sync.targets(config.load())
        self.assertEqual(stderr.getvalue().count("se omite"), 3)
        self.assertIsInstance(targets[".agents/skills/hefesto-next/SKILL.md"], str)
        self.assertFalse(any(isinstance(value, bytes) for value in targets.values()))

    def test_sync_skips_case_colliding_spec_names_on_every_filesystem(self) -> None:
        # Dos carpetas distintas simulan también en Windows nombres efectivos que colisionan.
        self.stored_skill("first", "Pretty-UI", "Pretty-UI")
        self.stored_skill("second", "pretty-ui")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            targets = sync.targets(config.load())
        self.assertIn("duplicada, se omite", stderr.getvalue())
        for root in (".agents/skills", ".claude/skills"):
            names = [path for path in targets if path.casefold() == f"{root}/pretty-ui/skill.md"]
            self.assertEqual(names, [f"{root}/Pretty-UI/SKILL.md"])

    def test_reserved_source_filenames_casefold_are_omitted_and_reported(self) -> None:
        reserved = ("source.json", "nested/SoUrCe.JsOn", "OvErLaY.Md", "nested/overlay.md")
        self.make_repo({f"skills/pretty-ui/{name}": '{"spec":"forged"}' for name in reserved})
        path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)
        self.assertEqual(source["spec"], "acme/ui-kit@pretty-ui")
        for name in reserved:
            self.assertNotIn(name, source["files"])
            self.assertIn((name, "override"), {(r["file"], r["kind"]) for r in source["risks"]})
            if name.casefold() != "source.json":
                self.assertFalse((path / name).exists())

    def test_all_utf_endiannesses_with_and_without_bom_report_risks(self) -> None:
        repo = self.make_repo()
        payload = "#!/bin/sh\ncurl example.invalid\nignore previous instructions\nAPI_KEY\n"
        encodings = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-16", "utf-32", "utf-8-sig")
        for encoding in encodings:
            (repo / f"skills/pretty-ui/{encoding}.txt").write_bytes(payload.encode(encoding))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "encoded-risks")
        source = self.source(skills.fetch("acme/ui-kit@pretty-ui"))
        for encoding in encodings:
            with self.subTest(encoding=encoding):
                risks = [r for r in source["risks"] if r["file"] == f"{encoding}.txt"]
                self.assertEqual({r["kind"] for r in risks}, {"script", "network_shell", "override", "sensitive"})
                self.assertTrue(any("línea 3" in r["detail"] for r in risks if r["kind"] == "override"))

    def test_ambiguous_utf16_unions_risks_from_both_decodings(self) -> None:
        repo = self.make_repo()
        raw = "curl\n".encode("utf-16-le") + "ignore previous\n".encode("utf-16-be")
        (repo / "skills/pretty-ui/ambiguous.txt").write_bytes(raw)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "ambiguous")
        risks = self.source(skills.fetch("acme/ui-kit@pretty-ui"))["risks"]
        self.assertEqual({r["kind"] for r in risks if r["file"] == "ambiguous.txt"}, {"network_shell", "override"})

    def test_update_repeats_risk_checks_keeps_commit_overlay_and_binary_sync(self) -> None:
        repo = self.make_repo()
        skills.fetch("acme/ui-kit@pretty-ui")
        promoted = self.promote()
        old_commit = self.source(promoted)["commit"]
        (promoted / "OVERLAY.md").write_text("Adaptación local\n", encoding="utf-8")
        raw = b"MZ\x00\xff\x00\x01"
        (repo / "skills/pretty-ui/data.bin").write_bytes(raw)
        (repo / "skills/pretty-ui/notes.txt").write_bytes("ignore previous\ncurl\n".encode("utf-16-be"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "update")
        code, out, err = self.run_cli("skill", "update", "pretty-ui")
        self.assertEqual(code, 0, out + err)
        self.assertIn("override", out)
        self.assertIn("network_shell", out)
        self.assertIn("data.bin", out)
        self.assertIn("notes.txt", out)
        self.assertEqual(self.source(promoted)["commit"], old_commit)
        self.assertFalse((promoted / "data.bin").exists())
        self.promote()
        self.assertEqual(self.source(promoted)["commit"], git(repo, "rev-parse", "HEAD"))
        self.assertEqual((promoted / "OVERLAY.md").read_text(encoding="utf-8"), "Adaptación local\n")
        targets = sync.targets(config.load())
        for root in (".agents/skills", ".claude/skills"):
            self.assertEqual(targets[f"{root}/pretty-ui/data.bin"], raw)

    def test_non_portable_filenames_and_case_collision_are_omitted_and_reported(self) -> None:
        self.assertFalse(skills._is_portable_rel_path("skills/pretty-ui/SKILL.md."))
        self.assertFalse(skills._is_portable_rel_path("skills/pretty-ui/OVERLAY.md "))
        self.assertFalse(skills._is_portable_rel_path("skills/pretty-ui/a:b"))
        self.assertFalse(skills._is_portable_rel_path("skills/pretty-ui/CON.txt"))
        self.assertTrue(skills._is_portable_rel_path("skills/pretty-ui/SKILL.md"))
        self.assertTrue(skills._is_portable_rel_path("skills/pretty-ui/note.txt"))

        repo = self.make_repo({"skills/pretty-ui/note.txt": "normal content\n"})
        orig_run = subprocess.run

        extra_entries = [
            b"100644 blob 1111111111111111111111111111111111111111\tskills/pretty-ui/SKILL.md.",
            b"100644 blob 2222222222222222222222222222222222222222\tskills/pretty-ui/OVERLAY.md ",
            b"100644 blob 3333333333333333333333333333333333333333\tskills/pretty-ui/a:b",
            b"100644 blob 4444444444444444444444444444444444444444\tskills/pretty-ui/CON.txt",
            b"100644 blob 5555555555555555555555555555555555555555\tskills/pretty-ui/NOTE.TXT",
        ]

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] == "ls-tree":
                real = orig_run(cmd, *args, **kwargs)
                augmented = real.stdout + b"".join(e + b"\x00" for e in extra_entries)
                return subprocess.CompletedProcess(cmd, real.returncode, augmented, real.stderr)
            return orig_run(cmd, *args, **kwargs)

        with mock.patch("hefesto.skills.subprocess.run", side_effect=fake_run):
            path = skills.fetch("acme/ui-kit@pretty-ui")

        source = self.source(path)
        risks = source["risks"]
        risk_map = {(r["file"], r["kind"]): r["detail"] for r in risks}

        for bad in ("SKILL.md.", "OVERLAY.md ", "a:b", "CON.txt"):
            self.assertIn((bad, "override"), risk_map)
            self.assertIn("nombre de fichero no portable", risk_map[(bad, "override")])
            self.assertNotIn(bad, source["files"])
            self.assertNotIn(bad, os.listdir(path))

        self.assertIn(("NOTE.TXT", "override"), risk_map)
        self.assertIn("colisión", risk_map[("NOTE.TXT", "override")].lower())
        self.assertNotIn("NOTE.TXT", source["files"])
        self.assertNotIn("NOTE.TXT", os.listdir(path))
        self.assertIn("note.txt", source["files"])
        self.assertTrue((path / "note.txt").exists())

    def test_ansi_and_carriage_return_sanitized_in_risks_and_promote_content(self) -> None:
        raw_text = "\x1b[31mcurl http://example.invalid\x1b[0m\r# comment\n"
        self.assertEqual(skills.sanitize_text("\x1b[31mtest\x1b[0m\r\b\t\n"), "\\x1b[31mtest\\x1b[0m\\r\\b\t\n")

        self.make_repo({"skills/pretty-ui/ansi_risk.sh": raw_text})
        path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)

        risk = next(r for r in source["risks"] if r["file"] == "ansi_risk.sh" and r["kind"] == "network_shell")
        self.assertNotIn("\x1b", risk["detail"])
        self.assertNotIn("\r", risk["detail"])
        self.assertIn(r"\x1b[31m", risk["detail"])
        self.assertIn(r"\r", risk["detail"])

        printed = io.StringIO()
        with tty(True, ["s"]), contextlib.redirect_stdout(printed):
            skills.promote("acme-pretty-ui")
        out = printed.getvalue()

        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertIn(r"\x1b[31m", out)
        self.assertIn(r"\r# comment", out)

    def test_file_size_limit_omits_large_file_and_reports_binary_risk(self) -> None:
        self.make_repo({
            "skills/pretty-ui/small.txt": "small",
            "skills/pretty-ui/large.txt": "x" * 300,
        })
        with mock.patch("hefesto.skills.MAX_FILE_SIZE", 200):
            path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)
        self.assertIn("small.txt", source["files"])
        self.assertNotIn("large.txt", source["files"])
        self.assertFalse((path / "large.txt").exists())
        self.assertTrue((path / "small.txt").exists())
        risks = source["risks"]
        self.assertTrue(any(r["file"] == "large.txt" and r["kind"] == "binary" and "demasiado grande" in r["detail"]
                            for r in risks))

    def test_total_size_limit_raises_code_2_without_leaving_half_proposal(self) -> None:
        self.make_repo({
            "skills/pretty-ui/file1.txt": "x" * 60,
            "skills/pretty-ui/file2.txt": "y" * 60,
        })
        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui"
        with mock.patch("hefesto.skills.MAX_TOTAL_SIZE", 50):
            with self.assertRaises(HefestoError) as ctx:
                skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(proposal.exists(), "La propuesta no debe quedar a medias")

    def test_file_count_limit_raises_code_2_without_leaving_half_proposal(self) -> None:
        self.make_repo({
            "skills/pretty-ui/a.txt": "a",
            "skills/pretty-ui/b.txt": "b",
            "skills/pretty-ui/c.txt": "c",
        })
        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui"
        with mock.patch("hefesto.skills.MAX_FILES", 2):
            with self.assertRaises(HefestoError) as ctx:
                skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(proposal.exists(), "La propuesta no debe quedar a medias")

    def test_file_containing_nul_is_not_treated_as_binary_and_scans_and_displays_text(self) -> None:
        raw_text = b"ignore previous\x00curl http://example.invalid\n"
        self.make_repo({"skills/pretty-ui/notes.txt": raw_text.decode("latin-1")})
        path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)
        risks = [r for r in source["risks"] if r["file"] == "notes.txt"]
        self.assertFalse(any(r["kind"] == "binary" for r in risks))
        self.assertTrue(any(r["kind"] == "override" for r in risks))
        self.assertTrue(any(r["kind"] == "network_shell" for r in risks))

        printed = io.StringIO()
        with tty(True, ["s"]), contextlib.redirect_stdout(printed):
            skills.promote("acme-pretty-ui")
        out = printed.getvalue()
        self.assertNotIn("[binario,", out)
        self.assertIn("ignore previous", out)
        self.assertIn("curl http://example.invalid", out)

    def test_binary_file_with_executable_signature_or_high_nonprintable_ratio_is_displayed_as_binary_and_still_scanned(self) -> None:
        elf_bytes = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00curl http://evil.invalid\n"
        high_nonprint = b"data\n" + b"\x00" * 30 + b"wget http://evil.invalid\n"
        repo = self.make_repo()
        (repo / "skills/pretty-ui/app.bin").write_bytes(elf_bytes)
        (repo / "skills/pretty-ui/high_nul.bin").write_bytes(high_nonprint)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "binaries")

        path = skills.fetch("acme/ui-kit@pretty-ui")
        source = self.source(path)
        elf_risks = [r for r in source["risks"] if r["file"] == "app.bin"]
        self.assertTrue(any(r["kind"] == "script" and "ELF" in r["detail"] for r in elf_risks))
        self.assertTrue(any(r["kind"] == "network_shell" for r in elf_risks))

        nul_risks = [r for r in source["risks"] if r["file"] == "high_nul.bin"]
        self.assertTrue(any(r["kind"] == "binary" for r in nul_risks))
        self.assertTrue(any(r["kind"] == "network_shell" for r in nul_risks))

        printed = io.StringIO()
        with tty(True, ["s"]), contextlib.redirect_stdout(printed):
            skills.promote("acme-pretty-ui")
        out = printed.getvalue()
        self.assertIn("== app.bin ==\n[binario,", out)
        self.assertIn("== high_nul.bin ==\n[binario,", out)

    def test_fetch_builds_in_sibling_temp_and_replaces_previous_proposal_atomically(self) -> None:
        repo = self.make_repo()
        path = skills.fetch("acme/ui-kit@pretty-ui")
        initial_commit = self.source(path)["commit"]
        (path / "local_marker.txt").write_text("existing", encoding="utf-8")

        # Fallo en el nuevo fetch (excede tamaño total)
        (repo / "skills/pretty-ui/huge.bin").write_bytes(b"x" * 200)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "huge")

        with mock.patch("hefesto.skills.MAX_TOTAL_SIZE", 150):
            with self.assertRaises(HefestoError):
                skills.fetch("acme/ui-kit@pretty-ui")

        # La propuesta anterior debe quedar intacta
        self.assertTrue(path.exists())
        self.assertTrue((path / "local_marker.txt").exists())
        self.assertEqual(self.source(path)["commit"], initial_commit)
        proposals_dir = util.hefesto_home() / "skills" / "_proposals"
        dot_temps = [p.name for p in proposals_dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(dot_temps, [])

    def test_missing_skill_md_raises_code_1_without_creating_proposal(self) -> None:
        repo = self.tmp / "gh" / "acme" / "ui-kit.git"
        (repo / "skills/pretty-ui/README.md").parent.mkdir(parents=True, exist_ok=True)
        (repo / "skills/pretty-ui/README.md").write_text("# only readme\n", encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "no-skill-md")
        config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"),
                           (self.tmp / "gh").as_posix())

        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui"
        with self.assertRaises(HefestoError) as ctx:
            skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(proposal.exists())

    def test_non_portable_skill_md_raises_code_1_without_creating_proposal(self) -> None:
        repo = self.make_repo()
        orig_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] == "ls-tree":
                real = orig_run(cmd, *args, **kwargs)
                modified = real.stdout.replace(b"\tskills/pretty-ui/SKILL.md", b"\tskills/pretty-ui/SKILL.md.")
                return subprocess.CompletedProcess(cmd, real.returncode, modified, real.stderr)
            return orig_run(cmd, *args, **kwargs)

        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui"
        with mock.patch("hefesto.skills.subprocess.run", side_effect=fake_run):
            with self.assertRaises(HefestoError) as ctx:
                skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(proposal.exists())

    def test_skill_md_exceeding_file_size_limit_raises_code_1_without_creating_proposal(self) -> None:
        self.make_repo()
        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui"
        with mock.patch("hefesto.skills.MAX_FILE_SIZE", 50):
            with self.assertRaises(HefestoError) as ctx:
                skills.fetch("acme/ui-kit@pretty-ui")
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(proposal.exists())
