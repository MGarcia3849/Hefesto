"""Contratos de la Fase 14: aprendizaje (ADR 2026-10-15 y 2026-10-17).

API que fijan estos tests:

hefesto/learning.py
- `workflow.register_failure` añade a `.hefesto/logs/failures.jsonl` cada fallo: `task_id`, `attempt`, `source`
  (`fail`, `verify`, `review`), `role`, `error` (redactado) y `at`.
- `extract(project) -> list[Path]`: lee `.hefesto/evidence/*.json`, `logs/reviews.jsonl` y `logs/failures.jsonl`
  y, sin LLM, escribe o actualiza candidatas en `<HEFESTO_HOME>/memory/_pendientes/` para tareas con
  `attempts > 1` o con alguna revisión `fail`. El cuerpo lleva el motivo (notas de revisión y errores
  registrados), la solución (la tarea que acabó en verde) y «N intentos». Frontmatter: `confianza` (0.3 al nacer, +0.1 por cada fuente nueva, tope 0.9),
  `alcance` (`proyecto`), `proyecto`, `stack`, `tipo` (`errores-resueltos`), `tarea`, `fuentes`
  (`<proyecto>:<TASK>` separados por comas) y `clave` (hash estable de la lección). Una fuente ya contada no
  vuelve a subir la confianza. Nunca toca notas aprobadas. Redacta secretos.
- `global_candidates() -> list[Path]`: notas aprobadas de alcance `proyecto` con la misma `clave` en 2 o más
  proyectos → una candidata `alcance: global` en `_pendientes/` (nunca aprobada sin humano).
- `skill_drafts(min_repeats=3, min_confidence=0.7) -> list[Path]`: agrupa notas aprobadas por `stack` y término
  clave compartido; con `min_repeats` notas de confianza suficiente escribe
  `<HEFESTO_HOME>/skills/_proposals/<nombre>/SKILL.md` (frontmatter `name` y `description`); nunca reescribe un
  borrador existente ni promueve.
- CLI: `hefesto learn extract`, `hefesto learn skills`.

hefesto/packet.py
- `_memory_entries(project, task)`: puntúa bloques de DECISIONS/LEARNINGS por rutas, términos del título y
  etiquetas `#etiqueta`; devuelve los mejores primero y nunca más de `memory.max_chars` caracteres en total.
- `stack_rules(project, cfg) -> list[tuple[str, str]]`: con `[packet] rules_roots` incluye los `.md` de la
  subcarpeta `common` y de las que coincidan con el stack (stacks de `distribution.detect_stack` y
  dependencias de package.json: `@angular/core` → `angular`, `react` → `react`, `typescript` → `typescript`),
  dentro de `[packet] rules_max_chars`; `build` las añade al paquete.

hefesto/router.py
- `success_stats(project, role) -> dict[str, dict]`: por `model_id`, desde `logs/runs.jsonl` de ese rol:
  `tasks` (tareas distintas), `pass1` (fracción de tareas cuya primera ejecución del modelo tuvo éxito) y
  `passk` (fracción con alguna ejecución con éxito).
- `plan_route(cfg, role, author_providers=None, project=None)`: con `[routing] history = true` y `project`,
  los modelos con `tasks >= routing.min_samples` se reordenan por (passk, pass1) descendente dentro de las
  posiciones que ya ocupaban; el resto no se mueve. Cada `Hop` de un modelo con historial menciona `pass@1`.
- `hefesto route TASK --dry-run` imprime esas cifras cuando el historial está activo.
- `hefesto run ROL "…" --challenge`: antes de delegar, un proveedor distinto del elegido (con `HEFESTO_ROLE`
  = `challenge`) critica la instrucción; la crítica se guarda en `logs/challenges/` y el JSON de salida lleva
  `challenge: {provider, path, summary}`; la crítica se imprime en stderr y se añade a la instrucción del
  trabajador (bajo «Crítica previa») antes de delegar; sin otro proveedor
  `challenge: null`, y si la crítica falla se delega igual. No suma intentos.

hefesto/notify.py
- `send(cfg, title, message) -> bool`: `False` sin llamar a nada si `[notify] enabled = false`; con él activo
  usa el mecanismo del sistema y devuelve `False` (sin lanzar) si falla.
- `event(project, kind, message) -> bool`: carga la config del proyecto y llama a `send`.
- `notify.event(project, kind, …)` con `kind`: `blocked` (circuit breaker en `workflow.register_failure`),
  `trust` (`done` con verify sin confiar), `finish` (`done` de la última tarea) y `quota` (`run` sale con 3).
- CLI: `hefesto notify test`.

hefesto/autopilot.py
- `windows_command(project, when) -> list[str]`: `schtasks /Create … /SC ONCE …` con `/TN hefesto-autopilot-…`.
- `windows_command` usa la hora local en `/ST` y rechaza rutas con comillas dobles.
- `crontab_line(project, when) -> str`: `min hora día mes *` en hora local, ruta citada con `shlex.quote`,
  `hefesto … loop` seguido de `hefesto … autopilot cancel` (una sola ejecución) y marca `# hefesto-autopilot`.
- `autopilot status` muestra la programación vigente (hora local HH:MM); `schedule` sustituye a la anterior;
  `cancel` la retira del planificador del sistema y del registro.
- `schedule(project, now=None) -> str`: programa una ejecución de `hefesto loop` a la hora de `earliest_cooldown`
  con `subprocess.run` y una lista de argumentos (nunca `shell=True`); se niega (HefestoError, EXIT_BLOCKED)
  con una tarea bloqueada o con el verify de la siguiente tarea sin confiar; sin cooldown no programa nada.
- CLI: `hefesto autopilot schedule | status | cancel`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from unittest import mock

from hefesto import config, roadmap, state, trust, util
from hefesto.util import EXIT_BLOCKED, HefestoError, read_jsonl
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Formularios
- [ ] TASK-001: Formulario de alta con signals
  - verify: `true`
  - files: src/app/alta/alta.component.ts
  - role: runner
  - size: S
"""


def _frontmatter(path: Path) -> dict[str, str]:
    from hefesto import memory

    meta, _body = memory.parse_note_content(path.read_text(encoding="utf-8"))
    return meta


class LearningCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def write_evidence(self, task_id: str, attempts: int, title: str = "Formulario de alta con signals",
                       project: Path | None = None) -> None:
        proj = project or self.project
        folder = proj / ".hefesto" / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{task_id}-{attempts}.json").write_text(json.dumps({
            "task_id": task_id, "title": title, "phase": "Fase 1 — Formularios", "attempts": attempts,
            "review": {"verdict": "pass", "notes": "ok"}, "providers": ["gemini:default"], "runs": [],
            "files": [{"path": "src/app/alta/alta.component.ts", "sha256": "0"}],
        }), encoding="utf-8")

    def write_review(self, task_id: str, verdict: str, notes: str, project: Path | None = None) -> None:
        proj = project or self.project
        util.append_jsonl(proj / ".hefesto" / "logs" / "reviews.jsonl", {
            "task_id": task_id, "verdict": verdict, "notes": notes, "run_id": "r1",
            "reviewer": "codex:default", "authors": ["gemini:default"], "attempt": 1, "at": util.now_iso()})

    def pending(self) -> list[Path]:
        folder = util.hefesto_home() / "memory" / "_pendientes"
        return sorted(folder.glob("*.md")) if folder.exists() else []

    def approved_note(self, name: str, project: str, stack: str, body: str, confianza: str = "0.8",
                      alcance: str = "proyecto", clave: str = "") -> Path:
        from hefesto import memory

        folder = util.hefesto_home() / "memory" / "errores-resueltos"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.md"
        meta = {"fecha": "2026-10-17", "proyecto": project, "stack": stack, "tipo": "errores-resueltos",
                "confianza": confianza, "alcance": alcance, "clave": clave or name}
        path.write_text(memory.format_note(meta, body), encoding="utf-8")
        return path


class ExtractTests(LearningCase):
    def test_retried_task_becomes_a_candidate_with_initial_confidence(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=2)
        self.write_review("TASK-001", "fail", "El formulario no valida el DNI vacío")
        created = learning.extract(self.project)
        self.assertEqual(len(created), 1)
        meta = _frontmatter(created[0])
        self.assertEqual(meta["confianza"], "0.3")
        self.assertEqual(meta["alcance"], "proyecto")
        self.assertEqual(meta["tarea"], "TASK-001")
        self.assertEqual(meta["tipo"], "errores-resueltos")
        self.assertEqual(meta["proyecto"], self.project.name)
        self.assertIn("clave", meta)
        self.assertIn("DNI", created[0].read_text(encoding="utf-8"))

    def test_failed_review_alone_is_enough_and_records_stack_and_solution(self) -> None:
        from hefesto import learning

        (self.project / "package.json").write_text("{}", encoding="utf-8")
        self.write_evidence("TASK-001", attempts=1)
        self.write_review("TASK-001", "fail", "Faltaba el estado de carga")
        created = learning.extract(self.project)
        self.assertEqual(len(created), 1)
        meta = _frontmatter(created[0])
        self.assertEqual(meta["stack"], "node")
        body = created[0].read_text(encoding="utf-8")
        self.assertIn("Faltaba el estado de carga", body)        # motivo
        self.assertIn("Formulario de alta con signals", body)    # qué se resolvió (la tarea que acabó en verde)

    def test_failures_are_logged_with_their_source(self) -> None:
        self.cli("start", "TASK-001")
        code, out, err = self.cli("fail", "TASK-001", "TypeError en alta.component.ts")
        self.assertEqual(code, 0, out + err)
        rows = read_jsonl(self.project / ".hefesto" / "logs" / "failures.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "TASK-001")
        self.assertEqual(rows[0]["source"], "fail")
        self.assertEqual(rows[0]["attempt"], 1)
        self.assertIn("TypeError", rows[0]["error"])

    def test_failed_verify_gives_the_reason_and_the_solution(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=3)
        util.append_jsonl(self.project / ".hefesto" / "logs" / "failures.jsonl", {
            "task_id": "TASK-001", "attempt": 1, "source": "verify", "role": "runner",
            "error": "verify salió con 1: TypeError en alta.component.ts", "at": util.now_iso()})
        created = learning.extract(self.project)
        self.assertEqual(len(created), 1)
        body = created[0].read_text(encoding="utf-8")
        self.assertIn("TypeError en alta.component.ts", body)      # motivo, desde failures.jsonl
        self.assertIn("Formulario de alta con signals", body)      # solución: la tarea que acabó en verde
        self.assertRegex(body, r"\b3 intentos\b")

    def test_first_try_success_without_failed_review_is_ignored(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=1)
        self.write_review("TASK-001", "pass", "todo bien")
        self.assertEqual(learning.extract(self.project), [])
        self.assertEqual(self.pending(), [])

    def test_running_twice_does_not_duplicate_nor_raise_confidence(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=2)
        self.write_review("TASK-001", "fail", "El formulario no valida el DNI vacío")
        learning.extract(self.project)
        learning.extract(self.project)
        notes = self.pending()
        self.assertEqual(len(notes), 1)
        self.assertEqual(_frontmatter(notes[0])["confianza"], "0.3")

    def test_same_lesson_from_another_project_raises_confidence(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=2)
        self.write_review("TASK-001", "fail", "El formulario no valida el DNI vacío")
        learning.extract(self.project)
        other = self.tmp / "otro"
        (other / ".hefesto" / "logs").mkdir(parents=True)
        self.write_evidence("TASK-009", attempts=3, project=other)
        self.write_review("TASK-009", "fail", "El formulario no valida el DNI vacío", project=other)
        learning.extract(other)
        notes = self.pending()
        self.assertEqual(len(notes), 1)
        meta = _frontmatter(notes[0])
        self.assertEqual(meta["confianza"], "0.4")
        self.assertIn(f"{self.project.name}:TASK-001", meta["fuentes"])
        self.assertIn("otro:TASK-009", meta["fuentes"])

    def test_confidence_is_capped(self) -> None:
        from hefesto import learning

        for i in range(10):
            proj = self.tmp / f"p{i}"
            (proj / ".hefesto" / "logs").mkdir(parents=True)
            self.write_evidence(f"TASK-{i:03d}", attempts=2, project=proj)
            self.write_review(f"TASK-{i:03d}", "fail", "Falta el índice compuesto en producción", project=proj)
            learning.extract(proj)
        self.assertEqual(_frontmatter(self.pending()[0])["confianza"], "0.9")

    def test_approved_notes_are_never_modified(self) -> None:
        from hefesto import learning

        note = self.approved_note("indice", self.project.name, "node", "Falta el índice compuesto en producción")
        before = note.read_bytes()
        self.write_evidence("TASK-001", attempts=2)
        self.write_review("TASK-001", "fail", "Falta el índice compuesto en producción")
        learning.extract(self.project)
        self.assertEqual(note.read_bytes(), before)

    def test_secrets_are_redacted(self) -> None:
        from hefesto import learning

        self.write_evidence("TASK-001", attempts=2)
        fake_key = "sk-" + "ant-" + "api03-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # nunca literal: lo bloquea el guard
        self.write_review("TASK-001", "fail", f"La clave {fake_key} quedó en el log")
        created = learning.extract(self.project)
        self.assertTrue(created)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", created[0].read_text(encoding="utf-8"))

    def test_cli_learn_extract(self) -> None:
        self.write_evidence("TASK-001", attempts=2)
        self.write_review("TASK-001", "fail", "El formulario no valida el DNI vacío")
        code, out, err = self.cli("learn", "extract")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.pending()), 1)


class SkillDraftTests(LearningCase):
    def test_repeated_confident_lessons_produce_a_draft(self) -> None:
        from hefesto import learning

        for i in range(3):
            self.approved_note(f"n{i}", f"p{i}", "angular", f"Usa signals con resilientCollection en el caso {i}")
        drafts = learning.skill_drafts(min_repeats=3)
        self.assertEqual(len(drafts), 1)
        skill = drafts[0]
        self.assertEqual(skill.name, "SKILL.md")
        self.assertTrue(skill.parent.parent.samefile(util.hefesto_home() / "skills" / "_proposals"))
        meta = _frontmatter(skill)
        self.assertTrue(meta.get("name"))
        self.assertTrue(meta.get("description"))

    def test_too_few_or_low_confidence_lessons_produce_nothing(self) -> None:
        from hefesto import learning

        self.approved_note("a", "p1", "angular", "Usa resilientCollection con signals")
        self.approved_note("b", "p2", "angular", "Usa resilientCollection con signals")
        self.approved_note("c", "p3", "angular", "Usa resilientCollection con signals", confianza="0.3")
        self.assertEqual(learning.skill_drafts(min_repeats=3, min_confidence=0.7), [])

    def test_existing_draft_is_not_rewritten(self) -> None:
        from hefesto import learning

        for i in range(3):
            self.approved_note(f"n{i}", f"p{i}", "angular", "Usa resilientCollection con signals")
        first = learning.skill_drafts(min_repeats=3)
        first[0].write_text("---\nname: editado\ndescription: a mano\n---\n", encoding="utf-8")
        self.assertEqual(learning.skill_drafts(min_repeats=3), [])
        self.assertIn("editado", first[0].read_text(encoding="utf-8"))

    def test_pending_notes_and_other_stacks_do_not_count(self) -> None:
        from hefesto import learning, memory

        self.approved_note("a", "p1", "angular", "Usa resilientCollection con signals")
        self.approved_note("b", "p2", "flutter", "Usa resilientCollection con signals")
        pending = util.hefesto_home() / "memory" / "_pendientes"
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "c.md").write_text(memory.format_note(
            {"proyecto": "p3", "stack": "angular", "tipo": "errores-resueltos", "confianza": "0.9",
             "alcance": "proyecto", "clave": "c"}, "Usa resilientCollection con signals"), encoding="utf-8")
        self.assertEqual(learning.skill_drafts(min_repeats=2), [])

    def test_drafts_are_never_promoted(self) -> None:
        from hefesto import learning

        for i in range(3):
            self.approved_note(f"n{i}", f"p{i}", "angular", "Usa resilientCollection con signals")
        learning.skill_drafts(min_repeats=3)
        skills_root = util.hefesto_home() / "skills"
        promoted = [p for p in skills_root.rglob("SKILL.md") if "_proposals" not in p.parts]
        self.assertEqual(promoted, [])

    def test_two_notes_of_the_same_project_are_not_global(self) -> None:
        from hefesto import learning

        self.approved_note("a", "sentinel", "node", "Falta el índice compuesto", clave="k1")
        self.approved_note("b", "sentinel", "node", "Falta el índice compuesto", clave="k1")
        self.assertEqual(learning.global_candidates(), [])

    def test_same_lesson_in_two_projects_is_proposed_as_global(self) -> None:
        from hefesto import learning

        self.approved_note("a", "sentinel", "node", "Falta el índice compuesto", clave="k1")
        self.approved_note("b", "otro", "node", "Falta el índice compuesto", clave="k1")
        created = learning.global_candidates()
        self.assertEqual(len(created), 1)
        self.assertEqual(_frontmatter(created[0])["alcance"], "global")
        self.assertTrue(str(created[0]).startswith(str(util.hefesto_home() / "memory" / "_pendientes")))
        self.assertEqual(learning.global_candidates(), [])

    def test_cli_learn_skills(self) -> None:
        for i in range(3):
            self.approved_note(f"n{i}", f"p{i}", "angular", "Usa resilientCollection con signals")
        code, out, err = self.cli("learn", "skills")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(list((util.hefesto_home() / "skills" / "_proposals").glob("*/SKILL.md")))


class PacketLessonTests(LearningCase):
    def task(self) -> roadmap.Task:
        return roadmap.load_valid(self.project).get("TASK-001")

    def test_lessons_are_scored_by_title_terms_and_tags(self) -> None:
        from hefesto import packet

        (self.project / ".hefesto" / "LEARNINGS.md").write_text(
            "- 2026-10-01: Nada que ver con esto, configuración de impresoras.\n"
            "- 2026-10-02: En un formulario con signals, valida al perder el foco. #formularios\n"
            "- 2026-10-03: Otra nota sin relación sobre despliegues.\n", encoding="utf-8")
        entries = packet._memory_entries(self.project, self.task())
        texts = [text for _src, text in entries]
        self.assertTrue(texts)
        self.assertIn("signals", texts[0])
        self.assertFalse(any("impresoras" in t for t in texts))

    def test_tag_alone_selects_a_lesson(self) -> None:
        from hefesto import packet

        (self.project / ".hefesto" / "LEARNINGS.md").write_text(
            "- 2026-10-01: Valida antes de guardar. #alta\n"
            "- 2026-10-02: Nota sin relación sobre impresoras.\n", encoding="utf-8")
        texts = [t for _s, t in packet._memory_entries(self.project, self.task())]
        self.assertEqual(len(texts), 1)
        self.assertIn("#alta", texts[0])

    def test_path_alone_selects_a_lesson(self) -> None:
        from hefesto import packet

        (self.project / ".hefesto" / "LEARNINGS.md").write_text(
            "- 2026-10-01: En src/app/alta/alta.component.ts el foco se pierde al guardar.\n"
            "- 2026-10-02: Nota sin relación sobre impresoras.\n", encoding="utf-8")
        texts = [t for _s, t in packet._memory_entries(self.project, self.task())]
        self.assertEqual(len(texts), 1)
        self.assertIn("alta.component.ts", texts[0])

    def test_more_matches_rank_first(self) -> None:
        from hefesto import packet

        (self.project / ".hefesto" / "LEARNINGS.md").write_text(
            "- 2026-10-01: Un formulario cualquiera.\n"
            "- 2026-10-02: Formulario de alta con signals: valida al perder el foco. #alta\n", encoding="utf-8")
        texts = [t for _s, t in packet._memory_entries(self.project, self.task())]
        self.assertGreaterEqual(len(texts), 2)
        self.assertIn("#alta", texts[0])

    def test_lessons_respect_max_chars(self) -> None:
        from hefesto import packet

        config.set_setting(config.project_settings_path(self.project), ("memory", "max_chars"), 300)
        big = "".join(f"- 2026-10-{i % 28 + 1:02d}: formulario signals alta caso {i} " + "x" * 80 + "\n"
                      for i in range(60))
        (self.project / ".hefesto" / "LEARNINGS.md").write_text(big, encoding="utf-8")
        entries = packet._memory_entries(self.project, self.task())
        self.assertTrue(entries)
        self.assertLessEqual(sum(len(text) for _src, text in entries), 300)

    def test_stack_rules_include_common_and_detected_stack(self) -> None:
        from hefesto import packet

        rules = self.tmp / "rules"
        for folder, text in (("common", "Regla común"), ("angular", "Regla de Angular"),
                             ("react", "Regla de React")):
            (rules / folder).mkdir(parents=True)
            (rules / folder / "coding-style.md").write_text(text, encoding="utf-8")
        (self.project / "package.json").write_text(json.dumps({"dependencies": {"@angular/core": "19.2.0"}}),
                                                   encoding="utf-8")
        config.set_setting(config.project_settings_path(self.project), ("packet", "rules_roots"), [str(rules)])
        cfg = config.load(self.project)
        joined = "\n".join(text for _src, text in packet.stack_rules(self.project, cfg))
        self.assertIn("Regla común", joined)
        self.assertIn("Regla de Angular", joined)
        self.assertNotIn("Regla de React", joined)

    def test_stack_rules_respect_budget_and_reach_the_packet(self) -> None:
        from hefesto import packet

        rules = self.tmp / "rules"
        (rules / "common").mkdir(parents=True)
        (rules / "common" / "a.md").write_text("Regla común breve", encoding="utf-8")
        (rules / "common" / "b.md").write_text("y" * 5000, encoding="utf-8")
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("packet", "rules_roots"), [str(rules)])
        config.set_setting(settings, ("packet", "rules_max_chars"), 1000)
        cfg = config.load(self.project)
        parts = packet.stack_rules(self.project, cfg)
        self.assertLessEqual(sum(len(t) for _s, t in parts), 1000)
        rm = roadmap.load_valid(self.project)
        built = packet.build(self.project, cfg, "runner", "haz algo", rm.get("TASK-001"), rm, None,
                             "gemini:default", self.project)
        self.assertIn("Regla común breve", built.text)

    def test_stack_rules_are_empty_without_roots(self) -> None:
        from hefesto import packet

        self.assertEqual(packet.stack_rules(self.project, config.load(self.project)), [])


class RoutingTests(LearningCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini")  # proveedores instalados: el enrutado puede elegir
        self.settings = config.project_settings_path(self.project)
        config.set_setting(self.settings, ("roles", "runner", "use"), ["gemini", "codex"])

    def run_record(self, task_id: str, model: str, success: bool, minute: int) -> None:
        util.append_jsonl(self.project / ".hefesto" / "logs" / "runs.jsonl", {
            "run_id": f"{task_id}-{model}-{minute}", "task_id": task_id, "role": "runner",
            "provider": model.split(":")[0], "model_id": model, "success": success,
            "at": f"2026-10-17T10:{minute:02d}:00+00:00"})

    def seed(self) -> None:
        # gemini: falla a la primera en 3 de 3 tareas y nunca acaba; codex: acierta siempre a la primera.
        minute = 0
        for i in range(3):
            self.run_record(f"TASK-1{i}", "gemini:default", False, minute)
            self.run_record(f"TASK-1{i}", "codex:default", True, minute + 1)
            minute += 2

    def test_success_stats(self) -> None:
        from hefesto import router

        self.seed()
        stats = router.success_stats(self.project, "runner")
        self.assertEqual(stats["codex:default"]["tasks"], 3)
        self.assertEqual(stats["codex:default"]["pass1"], 1.0)
        self.assertEqual(stats["gemini:default"]["passk"], 0.0)

    def test_pass1_and_passk_differ_when_a_model_retries(self) -> None:
        from hefesto import router

        self.run_record("TASK-20", "gemini:default", False, 1)
        self.run_record("TASK-20", "gemini:default", True, 2)
        self.run_record("TASK-21", "gemini:default", True, 3)
        stats = router.success_stats(self.project, "runner")["gemini:default"]
        self.assertEqual(stats["tasks"], 2)
        self.assertEqual(stats["pass1"], 0.5)
        self.assertEqual(stats["passk"], 1.0)

    def test_stats_are_per_role(self) -> None:
        from hefesto import router

        self.seed()
        util.append_jsonl(self.project / ".hefesto" / "logs" / "runs.jsonl", {
            "run_id": "rv", "task_id": "TASK-99", "role": "reviewer", "provider": "claude",
            "model_id": "claude:sonnet", "success": True, "at": "2026-10-17T11:00:00+00:00"})
        self.assertNotIn("claude:sonnet", router.success_stats(self.project, "runner"))
        self.assertNotIn("codex:default", router.success_stats(self.project, "reviewer"))

    def test_history_never_breaks_reviewer_independence(self) -> None:
        from hefesto import router

        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["codex", "gemini"])
        config.set_setting(self.settings, ("routing", "history"), True)
        config.set_setting(self.settings, ("routing", "min_samples"), 1)
        for i in range(3):  # gemini revisa mejor que codex según el historial…
            for model, ok in (("gemini:default", True), ("codex:default", False)):
                util.append_jsonl(self.project / ".hefesto" / "logs" / "runs.jsonl", {
                    "run_id": f"r{i}{model}", "task_id": f"TASK-3{i}", "role": "reviewer",
                    "provider": model.split(":")[0], "model_id": model, "success": ok,
                    "at": f"2026-10-17T12:0{i}:00+00:00"})
        # …pero gemini es el autor del cambio: no puede revisarlo.
        hops = router.plan_route(config.load(self.project), "reviewer", author_providers={"gemini"},
                                 project=self.project)
        chosen = [h for h in hops if h.outcome.startswith("elegido")]
        self.assertEqual(chosen[0].entry, "codex")

    def test_history_disabled_keeps_configured_order(self) -> None:
        from hefesto import router

        self.seed()
        hops = router.plan_route(config.load(self.project), "runner", project=self.project)
        self.assertEqual(hops[0].entry, "gemini")

    def test_history_reorders_by_pass_rates(self) -> None:
        from hefesto import router

        self.seed()
        config.set_setting(self.settings, ("routing", "history"), True)
        config.set_setting(self.settings, ("routing", "min_samples"), 3)
        hops = router.plan_route(config.load(self.project), "runner", project=self.project)
        self.assertEqual(hops[0].entry, "codex")
        self.assertIn("pass@1", hops[0].outcome)

    def test_too_few_samples_do_not_reorder(self) -> None:
        from hefesto import router

        self.seed()
        config.set_setting(self.settings, ("routing", "history"), True)
        config.set_setting(self.settings, ("routing", "min_samples"), 5)
        hops = router.plan_route(config.load(self.project), "runner", project=self.project)
        self.assertEqual(hops[0].entry, "gemini")

    def test_cooldown_still_wins_over_history(self) -> None:
        from hefesto import quota, router

        self.seed()
        config.set_setting(self.settings, ("routing", "history"), True)
        config.set_setting(self.settings, ("routing", "min_samples"), 3)
        quota.mark_cooldown("codex", dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1), "quota", "límite")
        hops = router.plan_route(config.load(self.project), "runner", project=self.project)
        chosen = [h for h in hops if h.outcome.startswith("elegido")]
        self.assertEqual(chosen[0].entry, "gemini")

    def test_route_command_prints_the_figures(self) -> None:
        self.seed()
        config.set_setting(self.settings, ("routing", "history"), True)
        config.set_setting(self.settings, ("routing", "min_samples"), 3)
        code, out, err = self.cli("route", "TASK-001", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pass@1", out)


class ChallengeTests(LearningCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini")
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({
            "codex@challenge": {"mode": "ok", "summary": "Riesgo: la instrucción no dice qué hacer con el DNI vacío."},
            "*": {"mode": "ok", "summary": "hecho", "write": {"src/app/alta/alta.component.ts": "x\n"}},
        }), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(scenario)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.tmp / "calls.jsonl")
        self.settings = config.project_settings_path(self.project)
        config.set_setting(self.settings, ("roles", "runner", "use"), ["gemini", "codex"])
        self.cli("start", "TASK-001")

    def test_challenge_uses_another_provider_and_saves_the_critique(self) -> None:
        code, out, err = self.cli("run", "runner", "Haz el formulario", "--challenge")
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(result["challenge"]["provider"], "codex")
        saved = self.project / result["challenge"]["path"]
        self.assertTrue(saved.exists())
        self.assertIn("challenges", saved.as_posix())
        self.assertIn("DNI", saved.read_text(encoding="utf-8"))
        self.assertIn("DNI", result["challenge"]["summary"])
        self.assertIn("DNI", err)  # se muestra al humano
        calls = read_jsonl(self.tmp / "calls.jsonl")
        self.assertEqual([c["name"] for c in calls], ["codex", "gemini"])  # crítica primero, delegación después
        # La crítica existe antes de delegar: viaja dentro del paquete del trabajador.
        self.assertIn("DNI", str(calls[1]["stdin"]) + " ".join(calls[1]["argv"]))

    def test_without_other_provider_delegates_anyway(self) -> None:
        config.set_setting(self.settings, ("roles", "runner", "use"), ["gemini"])
        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["gemini"])
        config.set_setting(self.settings, ("roles", "core", "use"), ["gemini"])
        config.set_setting(self.settings, ("roles", "forge", "use"), ["gemini"])
        config.set_setting(self.settings, ("roles", "visual", "use"), ["gemini"])
        code, out, err = self.cli("run", "runner", "Haz el formulario", "--challenge")
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertEqual(result["provider"], "gemini")
        self.assertIsNone(result["challenge"])

    def test_challenge_never_counts_as_an_attempt(self) -> None:
        code, out, err = self.cli("run", "runner", "Haz el formulario", "--challenge")
        self.assertEqual(code, 0, out + err)
        self.assertIsNotNone(json.loads(out)["challenge"])
        self.assertEqual(state.load(self.project)["attempts"], 0)

    def test_failed_challenge_does_not_block_the_delegation(self) -> None:
        scenario = Path(os.environ["HEFESTO_FAKE_SCENARIO"])
        data = json.loads(scenario.read_text(encoding="utf-8"))
        data["codex@challenge"] = {"mode": "fail"}
        scenario.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self.cli("run", "runner", "Haz el formulario", "--challenge")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["provider"], "gemini")
        self.assertEqual(state.load(self.project)["attempts"], 0)


class NotifyTests(LearningCase):
    def test_disabled_by_default_and_calls_nothing(self) -> None:
        from hefesto import notify

        with mock.patch("subprocess.run") as run:
            self.assertFalse(notify.send(config.load(self.project), "Hefesto", "hola"))
        run.assert_not_called()

    def test_enabled_sends_and_never_raises(self) -> None:
        from hefesto import notify

        config.set_setting(config.project_settings_path(self.project), ("notify", "enabled"), True)
        cfg = config.load(self.project)
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            self.assertTrue(notify.send(cfg, "Hefesto", "hola"))
        run.assert_called_once()
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotEqual(run.call_args.kwargs.get("shell"), True)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("sin notificador")):
            self.assertFalse(notify.send(cfg, "Hefesto", "hola"))

    def test_circuit_breaker_notifies(self) -> None:
        self.cli("start", "TASK-001")
        with mock.patch("hefesto.notify.event", return_value=True) as event:
            for _ in range(3):
                self.cli("fail", "TASK-001", "no compila")
        kinds = [c.args[1] for c in event.call_args_list]
        self.assertIn("blocked", kinds)

    def test_untrusted_verify_notifies(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("trust", "auto_safe"), False)
        self.cli("start", "TASK-001")
        with mock.patch("hefesto.notify.event", return_value=True) as event:
            code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        self.assertIn("trust", [c.args[1] for c in event.call_args_list])

    def test_phase_ready_for_finish_notifies(self) -> None:
        self.cli("start", "TASK-001")
        with mock.patch("hefesto.notify.event", return_value=True) as event:
            code, out, err = self.cli("done", "TASK-001")  # verify `true`, sin cambios de código
        self.assertEqual(code, 0, out + err)
        self.assertIn("finish", [c.args[1] for c in event.call_args_list])

    def test_quota_pause_notifies(self) -> None:
        self.use_fakes("gemini", "codex")
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({"*": {"mode": "quota"}}), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(scenario)
        config.set_setting(config.project_settings_path(self.project), ("roles", "runner", "use"), ["gemini", "codex"])
        self.cli("start", "TASK-001")
        with mock.patch("hefesto.notify.event", return_value=True) as event:
            code, out, err = self.cli("run", "runner", "haz algo")
        self.assertEqual(code, 3, out + err)
        self.assertIn("quota", [c.args[1] for c in event.call_args_list])

    def test_cli_notify_test(self) -> None:
        code, out, err = self.cli("notify", "test")
        self.assertEqual(code, 0, out + err)
        self.assertIn("desactivad", (out + err).lower())


class AutopilotTests(LearningCase):
    WHEN = dt.datetime(2026, 10, 18, 7, 30, tzinfo=dt.timezone.utc)

    def trust_next(self) -> None:
        task = roadmap.load_valid(self.project).get("TASK-001")
        data = trust.load(self.project)
        data["verify"][task.id] = {"sha256": trust.verify_hash(task.verify), "approved_at": util.now_iso()}
        util.atomic_write_json(trust.trust_path(self.project), data)

    def test_windows_command_is_an_argument_list(self) -> None:
        from hefesto import autopilot

        argv = autopilot.windows_command(self.project, self.WHEN)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0].lower(), "schtasks")
        self.assertIn("/Create", argv)
        self.assertIn("ONCE", argv)
        name = argv[argv.index("/TN") + 1]
        self.assertTrue(name.startswith("hefesto-autopilot-"))
        self.assertIn("loop", argv[argv.index("/TR") + 1])

    def test_windows_command_runs_once_at_local_time(self) -> None:
        from hefesto import autopilot

        argv = autopilot.windows_command(self.project, self.WHEN)
        self.assertEqual(argv[argv.index("/SC") + 1], "ONCE")
        self.assertEqual(argv[argv.index("/ST") + 1], self.WHEN.astimezone().strftime("%H:%M"))
        self.assertIn(str(self.project), argv[argv.index("/TR") + 1])

    def test_windows_command_rejects_quotes_in_the_path(self) -> None:
        from hefesto import autopilot

        with self.assertRaises(HefestoError):
            autopilot.windows_command(Path('C:/x" & del /q C:/'), self.WHEN)

    def test_crontab_line_is_marked_quoted_and_runs_once(self) -> None:
        import shlex

        from hefesto import autopilot

        evil = self.tmp / "mi proyecto'; rm -rf ~; echo '"
        line = autopilot.crontab_line(evil, self.WHEN)
        self.assertIn("# hefesto-autopilot", line)
        local = self.WHEN.astimezone()
        fields = line.split()
        self.assertEqual(fields[:4], [str(local.minute), str(local.hour), str(local.day), str(local.month)])
        command = line.split("#", 1)[0]
        tokens = shlex.split(" ".join(command.split()[5:]))
        self.assertIn(str(evil), tokens)          # la ruta llega como un único argumento
        self.assertNotIn("rm", tokens)
        self.assertIn("loop", tokens)
        # Una única vez: tras `loop`, la propia línea ejecuta `autopilot cancel` y se retira del crontab.
        loop_at = tokens.index("loop")
        auto_at = [i for i, t in enumerate(tokens) if t == "autopilot"]
        self.assertTrue(auto_at, tokens)
        self.assertGreater(auto_at[-1], loop_at)
        self.assertEqual(tokens[auto_at[-1] + 1], "cancel")

    def test_refuses_with_untrusted_verify(self) -> None:
        from hefesto import autopilot, router

        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("subprocess.run") as run, self.assertRaises(HefestoError) as ctx:
            autopilot.schedule(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        run.assert_not_called()

    def test_refuses_with_blocked_task(self) -> None:
        from hefesto import autopilot, router

        self.trust_next()
        self.cli("start", "TASK-001")
        for _ in range(3):  # circuit breaker por el camino real: un BLOCKED sin tarea en curso es un estado inválido
            self.cli("fail", "TASK-001", "no compila")
        self.assertEqual(state.load(self.project)["status"], "BLOCKED_NEED_ARCHITECT_REVIEW")
        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("subprocess.run") as run, self.assertRaises(HefestoError):
            autopilot.schedule(self.project)
        run.assert_not_called()

    def test_schedules_once_without_shell(self) -> None:
        from hefesto import autopilot, router

        self.trust_next()
        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            message = autopilot.schedule(self.project)
        self.assertTrue(run.called)
        for call in run.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertNotEqual(call.kwargs.get("shell"), True)
        self.assertIn("loop", message.lower() + " ".join(" ".join(c.args[0]) for c in run.call_args_list))

    def test_without_cooldown_nothing_is_scheduled(self) -> None:
        from hefesto import autopilot, router

        self.trust_next()
        with mock.patch.object(router, "earliest_cooldown", return_value=None), \
                mock.patch("subprocess.run") as run:
            autopilot.schedule(self.project)
        run.assert_not_called()

    def test_cli_status(self) -> None:
        code, out, err = self.cli("autopilot", "status")
        self.assertEqual(code, 0, out + err)

    def test_cli_schedule_status_and_cancel(self) -> None:
        from hefesto import router

        self.trust_next()
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("subprocess.run", return_value=ok) as run:
            code, out, err = self.cli("autopilot", "schedule")
            self.assertEqual(code, 0, out + err)
            scheduled = run.call_count
            code, out, err = self.cli("autopilot", "status")
            self.assertEqual(code, 0, out + err)
            self.assertIn(self.WHEN.astimezone().strftime("%H:%M"), out)
            code, out, err = self.cli("autopilot", "cancel")
            self.assertEqual(code, 0, out + err)
            self.assertGreater(run.call_count, scheduled)  # cancel también llama al planificador del sistema
            code, out, err = self.cli("autopilot", "status")
        self.assertNotIn(self.WHEN.astimezone().strftime("%H:%M"), out)

    def test_schedule_twice_keeps_a_single_entry(self) -> None:
        from hefesto import router

        self.trust_next()
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("platform.system", return_value="Windows"), \
                mock.patch("subprocess.run", return_value=ok) as run:
            self.cli("autopilot", "schedule")
            self.cli("autopilot", "schedule")
            code, out, err = self.cli("autopilot", "status")
            creates = [c.args[0] for c in run.call_args_list if "/Create" in c.args[0]]
            self.assertEqual(len(creates), 2)
            names = {argv[argv.index("/TN") + 1] for argv in creates}
            self.assertEqual(len(names), 1)          # mismo nombre de tarea por proyecto…
            self.assertTrue(all("/F" in argv for argv in creates))  # …y /F: la segunda sustituye a la primera
            self.assertEqual(out.count(self.WHEN.astimezone().strftime("%H:%M")), 1, out)
            self.cli("autopilot", "cancel")
            deletes = [c.args[0] for c in run.call_args_list if "/Delete" in c.args[0]]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][deletes[0].index("/TN") + 1], names.pop())

    def test_crontab_schedule_replaces_the_previous_line(self) -> None:
        from hefesto import router

        self.trust_next()
        crontab = {"text": "0 3 * * * backup\n"}

        def fake_run(argv, **kwargs):
            if argv[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout=crontab["text"], stderr="")
            if argv == ["crontab", "-"]:
                crontab["text"] = kwargs.get("input", "")
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(router, "earliest_cooldown", return_value=self.WHEN.isoformat()), \
                mock.patch("platform.system", return_value="Linux"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            self.cli("autopilot", "schedule")
            self.cli("autopilot", "schedule")
            self.assertEqual(crontab["text"].count("# hefesto-autopilot"), 1, crontab["text"])
            self.assertIn("backup", crontab["text"])   # nunca toca las líneas ajenas
            self.cli("autopilot", "cancel")
        self.assertNotIn("# hefesto-autopilot", crontab["text"])
        self.assertIn("backup", crontab["text"])
