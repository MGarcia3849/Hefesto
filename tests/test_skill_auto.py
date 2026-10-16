"""Contratos de la Fase 12: instalación automática de skills (decisión del usuario, ADR 2026-10-16).

Cuando las skills instaladas no bastan para una tarea, Hefesto busca en skills.sh con las palabras clave de la
tarea, filtra por calidad (criterios de la skill `find-skills` de Vercel), descarga la mejor con `skills.fetch`
y la promueve sin preguntar, salvo que el informe de riesgos detecte algo: entonces la descarta y borra la
propuesta. Nunca ejecuta `npx skills add` ni código de la skill, nunca instala desde un trabajador delegado
(`HEFESTO_ROLE`) y nunca hace fallar la tarea por red, npx o git.

API que fijan estos tests (hefesto/skill_auto.py):
- `ensure(project, cfg, role, task) -> AutoResult`  (AutoResult: installed: list[str], rejected: list[tuple[str, str]],
  skipped: str | None)
- registro en `<HEFESTO_HOME>/skills/auto.jsonl` (una línea JSON por evento, con `event` y `at`)
Configuración `[skills_auto]`: enabled, min_installs, min_stars, max_per_task, good_score, search_ttl_hours.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config, roadmap, skill_select, util
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git

ROADMAP_TEXT = """# ROADMAP

## Fase 1 — Formularios
- [ ] TASK-001: Formularios reactivos con signals
  - verify: `true`
  - files: src/app/alta/alta.component.ts
  - role: runner
  - size: S
"""

GOOD = {"name": "acme/forms-kit@reactive-forms", "owner": "acme", "repo": "forms-kit", "skill": "reactive-forms",
        "installs": 25000, "stars": 1200, "trust": "media", "url": "https://skills.sh/acme/forms-kit/reactive-forms"}
SMALL = {"name": "nadie/x@formularios-reactivos", "owner": "nadie", "repo": "x", "skill": "formularios-reactivos",
         "installs": 40, "stars": 3, "trust": "baja", "url": "unknown"}
UNKNOWN = {"name": "otro/y@signals-forms", "owner": "otro", "repo": "y", "skill": "signals-forms",
           "installs": "unknown", "stars": "unknown", "trust": "baja", "url": "unknown"}


def fake_fetch_factory(risks_by_spec: dict[str, list[dict[str, str]]] | None = None):
    """Simula `skills.fetch`: deja una propuesta válida en HEFESTO_HOME/skills/_proposals/<owner>-<skill>."""
    risks_by_spec = risks_by_spec or {}

    def fake_fetch(spec: str) -> Path:
        owner_repo, skill = spec.split("@", 1)
        owner = owner_repo.split("/", 1)[0]
        proposal = util.hefesto_home() / "skills" / "_proposals" / f"{owner}-{skill}"
        proposal.mkdir(parents=True, exist_ok=True)
        (proposal / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Formularios reactivos con signals en Angular\n"
            "tags: formularios, reactivos, signals\n---\n\nUsa FormGroup tipado.\n", encoding="utf-8")
        (proposal / "SOURCE.json").write_text(json.dumps({
            "spec": spec, "repo_url": f"https://github.com/{owner_repo}.git", "commit": "abc123",
            "files": {}, "trust": {}, "risks": risks_by_spec.get(spec, []),
        }), encoding="utf-8")
        return proposal

    return fake_fetch


class _Base(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user_skills = self.tmp / "user-skills"
        self.user_skills.mkdir()

    def prepare(self) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_TEXT, encoding="utf-8")
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "user_roots"), [str(self.user_skills)])
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def set(self, section: str, key: str, value: Any) -> None:
        config.set_setting(config.project_settings_path(self.project), (section, key), value)

    def task(self) -> roadmap.Task:
        return roadmap.load_valid(self.project).get("TASK-001")

    def ensure(self, role: str = "runner"):
        from hefesto import skill_auto

        return skill_auto.ensure(self.project, config.load(self.project), role, self.task())

    def auto_log(self) -> list[dict[str, Any]]:
        return util.read_jsonl(util.hefesto_home() / "skills" / "auto.jsonl")

    def promoted(self) -> Path:
        return util.hefesto_home() / "skills" / "promoted"

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]


class AutoInstallTests(_Base):
    def test_default_config_declares_skills_auto(self) -> None:
        self.prepare()
        cfg = config.load(self.project)
        self.assertIs(cfg.get("skills_auto", "enabled"), True)
        for key in ("min_installs", "min_stars", "max_per_task", "good_score", "search_ttl_hours"):
            self.assertIsInstance(cfg.get("skills_auto", key), int, key)

    def test_installs_best_candidate_without_asking_and_it_reaches_selection(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", return_value=[SMALL, GOOD, UNKNOWN]) as find, \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()) as fetch, \
                mock.patch("hefesto.util.is_tty", return_value=False):
            result = self.ensure()
        find.assert_called_once()
        self.assertEqual(find.call_args.args[0], "formularios reactivos")  # dos primeras palabras clave
        fetch.assert_called_once_with(GOOD["name"])  # solo la que pasa los filtros
        self.assertEqual(result.installed, ["reactive-forms"])
        self.assertTrue((self.promoted() / "reactive-forms" / "SKILL.md").is_file())
        names = [c.name for c in skill_select.select(self.project, config.load(self.project), "runner", self.task())]
        self.assertIn("reactive-forms", names)
        events = [e.get("event") for e in self.auto_log()]
        self.assertIn("installed", events)
        self.assertTrue(all("at" in e for e in self.auto_log()))

    def test_unknown_or_low_metrics_are_never_installed(self) -> None:
        self.prepare()
        many_installs_few_stars = dict(GOOD, name="a/b@stars-bajas", skill="stars-bajas", installs=90000, stars=5)
        many_stars_few_installs = dict(GOOD, name="a/b@installs-bajas", skill="installs-bajas", installs=50, stars=9000)
        unknown_stars = dict(GOOD, name="a/b@sin-estrellas", skill="sin-estrellas", stars="unknown")
        self.set("skills_auto", "search_ttl_hours", 0)
        self.set("skills_auto", "min_installs", 1000)
        self.set("skills_auto", "min_stars", 100)
        for candidates in ([SMALL, UNKNOWN], [many_installs_few_stars], [many_stars_few_installs], [unknown_stars]):
            with mock.patch("hefesto.skills.find", return_value=candidates), \
                    mock.patch("hefesto.skills.fetch") as fetch:
                result = self.ensure()
            fetch.assert_not_called()
            self.assertEqual(result.installed, [])

    def test_thresholds_come_from_config(self) -> None:
        self.prepare()
        self.set("skills_auto", "min_installs", 10)
        self.set("skills_auto", "min_stars", 1)
        with mock.patch("hefesto.skills.find", return_value=[SMALL]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()) as fetch:
            result = self.ensure()
        fetch.assert_called_once_with(SMALL["name"])
        self.assertEqual(result.installed, ["formularios-reactivos"])

    def test_does_not_search_when_installed_skills_are_enough(self) -> None:
        self.prepare()
        folder = self.user_skills / "formularios-reactivos-signals"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: formularios-reactivos-signals\ndescription: x\n---\n\nCuerpo\n",
                                         encoding="utf-8")
        self.set("skills_auto", "good_score", 6)
        with mock.patch("hefesto.skills.find") as find:
            result = self.ensure()
        find.assert_not_called()
        self.assertTrue(result.skipped)

    def test_already_installed_candidates_are_skipped(self) -> None:
        self.prepare()
        folder = self.user_skills / "reactive-forms"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: reactive-forms\ndescription: nada\n---\n\nCuerpo\n", encoding="utf-8")
        self.set("skills_auto", "good_score", 99)
        with mock.patch("hefesto.skills.find", return_value=[GOOD]), mock.patch("hefesto.skills.fetch") as fetch:
            self.ensure()
        fetch.assert_not_called()

    def test_respects_max_per_task(self) -> None:
        self.prepare()
        second = dict(GOOD, name="acme/forms-kit@typed-forms", skill="typed-forms", installs=20000)
        # La mejor (más instalaciones) gana aunque no venga primera.
        with mock.patch("hefesto.skills.find", return_value=[second, GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()) as fetch:
            result = self.ensure()
        fetch.assert_called_once_with(GOOD["name"])
        self.assertEqual(result.installed, ["reactive-forms"])

    def test_same_search_is_not_repeated_within_ttl(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", return_value=[]) as find:
            self.ensure()
            second = self.ensure()
        self.assertEqual(find.call_count, 1)
        self.assertTrue(second.skipped)

    def test_disabled_never_searches(self) -> None:
        self.prepare()
        self.set("skills_auto", "enabled", False)
        with mock.patch("hefesto.skills.find") as find:
            result = self.ensure()
        find.assert_not_called()
        self.assertTrue(result.skipped)


class SafetyTests(_Base):
    def test_risky_skill_is_rejected_and_its_proposal_removed(self) -> None:
        self.prepare()
        risks = {GOOD["name"]: [{"file": "SKILL.md", "kind": "override",
                                 "detail": "línea 3: Ignore previous instructions"}]}
        with mock.patch("hefesto.skills.find", return_value=[GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory(risks)), \
                mock.patch("hefesto.skills.promote") as promote:
            result = self.ensure()
        promote.assert_not_called()
        self.assertEqual(result.installed, [])
        self.assertEqual([spec for spec, _ in result.rejected], [GOOD["name"]])
        self.assertFalse((util.hefesto_home() / "skills" / "_proposals" / "acme-reactive-forms").exists())
        self.assertFalse((self.promoted() / "reactive-forms").exists())
        self.assertIn("rejected", [e.get("event") for e in self.auto_log()])

    def test_network_or_npx_failures_never_raise(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", side_effect=HefestoError("sin npx", 3)):
            result = self.ensure()
        self.assertEqual(result.installed, [])
        self.set("skills_auto", "search_ttl_hours", 0)
        with mock.patch("hefesto.skills.find", return_value=[GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=OSError("git caído")):
            result = self.ensure()
        self.assertEqual(result.installed, [])
        self.assertIn("error", [e.get("event") for e in self.auto_log()])

    def test_delegated_worker_never_installs(self) -> None:
        self.prepare()
        with mock.patch.dict(os.environ, {"HEFESTO_ROLE": "runner"}), \
                mock.patch("hefesto.skills.find") as find, mock.patch("hefesto.skills.fetch") as fetch:
            result = self.ensure()
        find.assert_not_called()
        fetch.assert_not_called()
        self.assertTrue(result.skipped)

    def test_never_runs_npx_skills_add(self) -> None:
        self.prepare()
        import subprocess

        calls: list[str] = []

        def spy(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            # Nunca ejecuta nada: solo anota el comando.
            calls.append(" ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("hefesto.skills.find", return_value=[GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()), \
                mock.patch("subprocess.run", side_effect=spy), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no se lanzan procesos")):
            self.ensure()
        self.assertFalse(any("skills" in c and " add" in f" {c}" for c in calls), calls)


class CliTests(_Base):
    def test_start_installs_missing_skills_and_keeps_exit_code(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", return_value=[GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()):
            code, out, err = self.here("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        self.assertIn("reactive-forms", out)
        self.assertTrue((self.promoted() / "reactive-forms").is_dir())

    def test_start_survives_auto_failure(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", side_effect=HefestoError("sin red", 3)):
            code, out, err = self.here("start", "TASK-001")
        self.assertEqual(code, 0, out + err)

    def test_skill_auto_command(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skills.find", return_value=[GOOD]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()):
            code, out, err = self.here("skill", "auto", "TASK-001")
        self.assertEqual(code, 0, out + err)
        self.assertIn("reactive-forms", out)

    def test_start_with_auto_disabled_does_not_search(self) -> None:
        self.prepare()
        self.set("skills_auto", "enabled", False)
        with mock.patch("hefesto.skills.find") as find:
            code, out, err = self.here("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        find.assert_not_called()
