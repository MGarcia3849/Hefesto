"""Contratos de la Fase 11: skills que aplican a cada tarea, dentro del paquete del trabajador.

La especificación (sección 5.3) pide que el paquete lleve «reglas del rol y las skills que aplican». Hefesto las
elige de forma determinista (sin LLM) entre las skills ya instaladas: framework, promovidas, proyecto
(`.claude/skills`, `.agents/skills`) y carpetas del usuario configurables (`skills_selection.user_roots`).
Buscar en skills.sh es solo una propuesta (`skill suggest --online`): descargar o promover sigue siendo humano.

API que fijan estos tests (hefesto/skill_select.py):
- `discover(roots: list[tuple[str, Path]]) -> list[SkillInfo]`  (SkillInfo: name, source, path, description, tags, body)
- `keywords(role: str, task: roadmap.Task) -> list[str]`
- `default_roots(project: Path, cfg: config.Config) -> list[tuple[str, Path]]`
- `select(project, cfg, role, task, roots=None) -> list[SkillChoice]`  (SkillChoice: name, source, path,
  description, score, reasons)
Orígenes: "proyecto" > "promovida" > "framework" > "usuario" (al deduplicar por nombre gana el primero).
Configuración `[skills_selection]`: enabled, max_skills, min_score, max_chars_per_skill, user_roots, exclude.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

from hefesto import config, packet, roadmap, state
from tests.helpers import HefestoTestCase, git

ROADMAP_TEXT = """# ROADMAP

## Fase 1 — Interfaz
- [ ] TASK-001: Animación del panel de avisos con signals
  - verify: `true`
  - files: src/app/avisos/aviso-panel.component.ts, src/app/avisos/aviso-panel.component.css
  - role: visual
  - size: S
  - accept: la transición respeta prefers-reduced-motion
"""

ROADMAP_PINNED = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Ajustar el cálculo de horas
  - verify: `true`
  - files: src/horas.py
  - role: runner
  - size: S
  - skills: rara-pero-fijada, otra-fijada
"""


def make_skill(root: Path, dirname: str, name: str | None = None, description: str = "", tags: str = "",
               body: str = "Cuerpo de la skill.") -> Path:
    folder = root / dirname
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or dirname}", f"description: {description}"]
    if tags:
        lines.append(f"tags: {tags}")
    lines.append("---")
    path = folder / "SKILL.md"
    path.write_text("\n".join(lines) + "\n\n" + body + "\n", encoding="utf-8")
    return path


def task_from(text: str, task_id: str = "TASK-001") -> roadmap.Task:
    return roadmap.parse_text(text).get(task_id)


class _Base(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user_skills = self.tmp / "user-skills"
        self.user_skills.mkdir()

    def prepare(self, roadmap_text: str = ROADMAP_TEXT) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
        settings = config.project_settings_path(self.project)
        # Nunca las carpetas reales del usuario: solo la temporal del test.
        config.set_setting(settings, ("skills_selection", "user_roots"), [str(self.user_skills)])
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]


class DiscoveryTests(_Base):
    def test_discover_reads_frontmatter_and_skips_folders_without_skill_md(self) -> None:
        from hefesto import skill_select

        root = self.tmp / "r"
        make_skill(root, "angular-dev", description="Buenas prácticas de Angular con signals", tags="angular, typescript",
                   body="Usa signals.")
        (root / "sin-skill").mkdir()
        infos = skill_select.discover([("usuario", root)])
        self.assertEqual([i.name for i in infos], ["angular-dev"])
        info = infos[0]
        self.assertEqual(info.source, "usuario")
        self.assertIn("signals", info.description)
        self.assertIn("angular", info.tags.lower())
        self.assertIn("Usa signals.", info.body)
        self.assertEqual(info.path, root / "angular-dev" / "SKILL.md")

    def test_discover_dedupes_by_name_keeping_first_root(self) -> None:
        from hefesto import skill_select

        proj, user = self.tmp / "p", self.tmp / "u"
        make_skill(proj, "hefesto-skill-frontend-taste", description="copia del proyecto")
        make_skill(user, "frontend-taste", name="hefesto-skill-frontend-taste", description="copia del usuario")
        infos = skill_select.discover([("proyecto", proj), ("usuario", user)])
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].source, "proyecto")

    def test_missing_roots_are_ignored(self) -> None:
        from hefesto import skill_select

        self.assertEqual(skill_select.discover([("usuario", self.tmp / "no-existe")]), [])

    def test_default_roots_order_and_user_roots_from_config(self) -> None:
        from hefesto import skill_select

        self.prepare()
        cfg = config.load(self.project)
        roots = skill_select.default_roots(self.project, cfg)
        sources = [s for s, _ in roots]
        # proyecto antes que promovidas, framework y usuario
        self.assertEqual(sources[0], "proyecto")
        self.assertLess(sources.index("promovida"), sources.index("framework"))
        self.assertLess(sources.index("framework"), sources.index("usuario"))
        paths = [p for _, p in roots]
        self.assertIn(self.project / ".claude" / "skills", paths)
        self.assertIn(self.project / ".agents" / "skills", paths)
        self.assertIn(self.user_skills, paths)

    def test_default_config_declares_skills_selection(self) -> None:
        self.prepare()
        cfg = config.load(self.project)
        self.assertIs(cfg.get("skills_selection", "enabled"), True)
        self.assertIsInstance(cfg.get("skills_selection", "max_skills"), int)
        self.assertIsInstance(cfg.get("skills_selection", "min_score"), int)
        self.assertIsInstance(cfg.get("skills_selection", "max_chars_per_skill"), int)
        self.assertIsInstance(cfg.get("skills_selection", "exclude"), list)


class SelectionTests(_Base):
    def test_keywords_from_title_accept_extensions_and_role(self) -> None:
        from hefesto import skill_select

        words = skill_select.keywords("visual", task_from(ROADMAP_TEXT))
        for expected in ("animacion", "signals", "angular", "typescript", "css", "visual", "frontend"):
            self.assertIn(expected, words)
        self.assertNotIn("para", words)  # palabra vacía
        self.assertNotIn("del", words)
        self.assertEqual(words, skill_select.keywords("visual", task_from(ROADMAP_TEXT)))  # determinista

    def test_select_ranks_by_score_and_explains_reasons(self) -> None:
        from hefesto import skill_select

        self.prepare()
        root = self.tmp / "r"
        make_skill(root, "angular-signals", description="Signals en Angular: computed, effect", tags="angular")
        make_skill(root, "motion", description="Animación accesible con prefers-reduced-motion")
        make_skill(root, "cocina", description="Recetas de cocina mediterránea")
        cfg = config.load(self.project)
        choices = skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT), roots=[("usuario", root)])
        names = [c.name for c in choices]
        self.assertEqual(names[0], "angular-signals")
        self.assertIn("motion", names)
        self.assertNotIn("cocina", names)
        self.assertTrue(all(c.score > 0 and c.reasons for c in choices))
        self.assertEqual(choices, skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT),
                                                      roots=[("usuario", root)]))

    def test_matching_is_by_whole_word_not_substring(self) -> None:
        from hefesto import skill_select

        self.prepare()
        root = self.tmp / "r"
        # «ui» (palabra clave del rol visual) no debe casar con «build» ni «guide»
        make_skill(root, "herramientas", description="build guide for tooling")
        cfg = config.load(self.project)
        choices = skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT), roots=[("usuario", root)])
        self.assertEqual(choices, [])

    def test_max_skills_and_min_score_from_config(self) -> None:
        from hefesto import skill_select

        self.prepare()
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "max_skills"), 1)
        root = self.tmp / "r"
        make_skill(root, "angular-signals", description="Signals en Angular", tags="angular, signals")
        make_skill(root, "solo-css", description="css")
        cfg = config.load(self.project)
        choices = skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT), roots=[("usuario", root)])
        self.assertEqual([c.name for c in choices], ["angular-signals"])

        config.set_setting(settings, ("skills_selection", "max_skills"), 5)
        config.set_setting(settings, ("skills_selection", "min_score"), 50)
        cfg = config.load(self.project)
        self.assertEqual(skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT),
                                             roots=[("usuario", root)]), [])

    def test_protocol_and_excluded_skills_are_never_selected(self) -> None:
        from hefesto import skill_select

        self.prepare()
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "exclude"), ["angular-vetada"])
        root = self.tmp / "r"
        for proto in ("hefesto-next", "hefesto-plan", "hefesto-retro"):
            make_skill(root, proto, description="angular signals animación css typescript visual frontend")
        make_skill(root, "angular-vetada", description="angular signals animación css typescript")
        cfg = config.load(self.project)
        self.assertEqual(skill_select.select(self.project, cfg, "visual", task_from(ROADMAP_TEXT),
                                             roots=[("usuario", root)]), [])

    def test_disabled_selection_returns_only_pinned(self) -> None:
        from hefesto import skill_select

        self.prepare(ROADMAP_PINNED)
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "enabled"), False)
        root = self.tmp / "r"
        make_skill(root, "rara-pero-fijada", description="nada que ver")
        make_skill(root, "horas-calculo", description="cálculo de horas", tags="horas, calculo")
        cfg = config.load(self.project)
        choices = skill_select.select(self.project, cfg, "runner", task_from(ROADMAP_PINNED), roots=[("usuario", root)])
        self.assertEqual([c.name for c in choices], ["rara-pero-fijada"])


class RoadmapSkillsTests(_Base):
    def test_skills_field_is_valid_and_parsed(self) -> None:
        rm = roadmap.parse_text(ROADMAP_PINNED)
        self.assertEqual(roadmap.validate(rm, self.tmp), [])
        self.assertEqual(rm.get("TASK-001").skills, ["rara-pero-fijada", "otra-fijada"])
        self.assertEqual(task_from(ROADMAP_TEXT).skills, [])

    def test_pinned_skills_always_selected_first_and_missing_ones_do_not_fail(self) -> None:
        from hefesto import skill_select

        self.prepare(ROADMAP_PINNED)
        root = self.tmp / "r"
        make_skill(root, "rara-pero-fijada", description="nada que ver con la tarea")
        make_skill(root, "horas-calculo", description="Cálculo de horas", tags="horas, calculo")
        cfg = config.load(self.project)
        choices = skill_select.select(self.project, cfg, "runner", task_from(ROADMAP_PINNED), roots=[("usuario", root)])
        names = [c.name for c in choices]
        self.assertEqual(names[0], "rara-pero-fijada")  # fijada aunque no puntúe
        self.assertIn("horas-calculo", names)
        self.assertNotIn("otra-fijada", names)  # no instalada: se ignora sin error
        self.assertTrue(any("ROADMAP" in r for r in choices[0].reasons))


class PacketSkillsTests(_Base):
    def build(self, role: str = "visual") -> packet.Packet:
        cfg = config.load(self.project)
        rm = roadmap.load_valid(self.project)
        task = rm.get("TASK-001")
        return packet.build(self.project, cfg, role, "instrucción", task, rm, state.load(self.project),
                            "claude:sonnet", self.project)

    def test_packet_includes_selected_skill_text(self) -> None:
        self.prepare()
        make_skill(self.user_skills, "angular-signals", description="Signals en Angular", tags="angular, signals",
                   body="REGLA-ÚNICA: usa computed() para estado derivado.")
        pkt = self.build()
        self.assertIn("## Skills que aplican (síguelas)", pkt.text)
        self.assertIn("angular-signals", pkt.text)
        self.assertIn("REGLA-ÚNICA: usa computed()", pkt.text)
        self.assertIn("usuario", pkt.text)

    def test_skill_text_is_redacted_sanitized_and_truncated(self) -> None:
        self.prepare()
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "max_chars_per_skill"), 400)
        prefix = "sk-" + "proj-"
        secret = prefix + ("c" * 40)
        make_skill(self.user_skills, "angular-signals", description="Signals en Angular", tags="angular, signals",
                   body=f"clave {secret} \x1b[31mrojo\x00 " + ("relleno " * 300) + "FINAL-NO-VISIBLE")
        pkt = self.build()
        self.assertNotIn(prefix, pkt.text)
        self.assertIn("[REDACTED]", pkt.text)
        self.assertNotIn("\x00", pkt.text)
        self.assertNotIn("FINAL-NO-VISIBLE", pkt.text)
        self.assertIn("[skill recortada]", pkt.text)

    def test_no_section_without_selected_skills(self) -> None:
        self.prepare()
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("skills_selection", "min_score"), 999)
        # Desde la Fase 15 el catálogo real aporta entradas a las tareas visuales: aquí se aísla de él.
        with mock.patch("hefesto.skill_catalog.load", return_value={}):
            self.assertNotIn("Skills que aplican", self.build().text)

    def test_skills_are_trimmed_after_memory_and_before_task_files(self) -> None:
        self.assertGreater(packet.P_SKILLS, packet.P_FILES)
        self.assertLessEqual(packet.P_SKILLS, packet.P_MEMORY)
        self.prepare()
        (self.project / ".hefesto" / "LEARNINGS.md").write_text(
            "# LEARNINGS\n\n- src/app/avisos: lección sobre avisos\n", encoding="utf-8")
        make_skill(self.user_skills, "angular-signals", description="Signals en Angular", tags="angular, signals")
        text = self.build().text
        self.assertLess(text.index("## Skills que aplican"), text.index("## Decisiones y lecciones"))


class SuggestTests(_Base):
    def test_suggest_lists_local_choices_with_score(self) -> None:
        self.prepare()
        make_skill(self.user_skills, "angular-signals", description="Signals en Angular", tags="angular, signals")
        code, out, err = self.here("skill", "suggest", "TASK-001")
        self.assertEqual(code, 0, out + err)
        self.assertIn("angular-signals", out)
        self.assertIn("usuario", out)

    def test_suggest_online_only_proposes_never_fetches(self) -> None:
        self.prepare()
        found = [{"name": "acme/angular-kit@angular-forms", "owner": "acme", "repo": "angular-kit", "skill": "angular-forms",
                  "installs": 5000, "stars": 900, "trust": "media",
                  "url": "https://skills.sh/acme/angular-kit/angular-forms"}]
        with mock.patch("hefesto.skills.find", return_value=found) as find, \
                mock.patch("hefesto.skills.fetch") as fetch, mock.patch("hefesto.skills.promote") as promote:
            code, out, err = self.here("skill", "suggest", "TASK-001", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(find.called)
        fetch.assert_not_called()
        promote.assert_not_called()
        self.assertIn("hefesto skill fetch acme/angular-kit@angular-forms", out)

    def test_suggest_unknown_task_fails(self) -> None:
        self.prepare()
        code, _out, _err = self.here("skill", "suggest", "TASK-999")
        self.assertNotEqual(code, 0)

    def test_route_shows_packet_skills(self) -> None:
        self.prepare()
        make_skill(self.user_skills, "angular-signals", description="Signals en Angular", tags="angular, signals")
        code, out, err = self.here("route", "TASK-001", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("skills", out.lower())
        self.assertIn("angular-signals", out)
