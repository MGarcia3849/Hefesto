"""TASK-3001: contrato de los campos de especificación opcionales y de la coherencia de instrucciones.

API que fija este contrato:

- `hefesto/roadmap.py`: `SPEC_FIELDS = ("scope", "out_of_scope", "risks", "requires_approval")`, todos opcionales
  y añadidos a `KNOWN_FIELDS`; `APPROVAL_KINDS = ("install", "deploy", "animation", "external_data", "architecture")`.
  `Task.scope`, `Task.out_of_scope` y `Task.risks` devuelven el texto (vacío si falta) y `Task.requires_approval`
  la lista normalizada en minúsculas (`none` o `-` equivalen a vacía). `validate(rm, project)` rechaza un valor de
  `requires_approval` que no esté en `APPROVAL_KINDS`. Un ROADMAP sin estos campos sigue siendo válido.
- `hefesto/packet.py`: `build(...)` añade a la sección «Tarea» las líneas `- alcance: …`, `- fuera de alcance: …`,
  `- riesgos: …` y `- aprobaciones pendientes: …` solo cuando el campo tiene valor.
- `prompts/protocol/plan.md`: el paso `plan` documenta los cuatro campos (y, por `hefesto sync`, sus copias
  generadas).
- `hefesto/sync.py`: `CRITICAL_RULES`, pares (nombre, texto) que el protocolo debe contener: `state.json`,
  `git push` y `hefesto finish`; `coherence_problems(project) -> list[str]` devuelve los problemas de coherencia:
  protocolo ausente (`AGENTS.md` o `.hefesto/AGENTS.md`), regla crítica ausente, `CLAUDE.md` o `GEMINI.md` en la
  raíz que no importan `AGENTS.md`, y ficheros generados que se desvían de `sync` (`run(project, check=True)`).
  No modifica nada.
- `hefesto/doctor.py`: `run_checks(project)` incluye un check cuyo nombre es `instrucciones`: `OK` sin problemas
  y `WARN` con el primer problema en el detalle.

Los campos describen la tarea para el trabajador y el revisor; no conceden permisos ni sustituyen las aprobaciones
humanas existentes (`trust`, `approval`, `finish`).
"""

from __future__ import annotations

import ast
from pathlib import Path

from hefesto import config, doctor, packet, providers, roadmap, state, sync
from tests.helpers import HefestoTestCase

BASE_TASK = """## Fase 1 — Prueba
- [ ] TASK-001: Tarea de prueba
  - verify: `true`
  - files: src/a.py
  - role: runner
"""


def roadmap_with(*extra: str) -> str:
    return BASE_TASK + "".join(f"  - {line}\n" for line in extra)


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"SpecFieldTests": 6, "PacketTests": 2, "CoherenceTests": 5}
    API = ("SPEC_FIELDS", "APPROVAL_KINDS", "KNOWN_FIELDS", "requires_approval", "out_of_scope", "validate(rm, project)",
           "alcance", "fuera de alcance", "riesgos", "aprobaciones pendientes", "prompts/protocol/plan.md",
           "CRITICAL_RULES", "coherence_problems(project)", "run(project, check=True)", "instrucciones",
           "run_checks(project)", "no conceden permisos")

    def tree(self) -> ast.Module:
        return ast.parse(Path(__file__).read_text(encoding="utf-8"))

    def test_docstring_names_the_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API:
            with self.subTest(name=name):
                self.assertIn(name, doc)

    def test_every_behaviour_has_its_tests(self) -> None:
        classes = {n.name: n for n in self.tree().body if isinstance(n, ast.ClassDef)}
        for name, minimum in self.REQUIRED_CLASSES.items():
            with self.subTest(cls=name):
                self.assertIn(name, classes)
                tests = [n for n in classes[name].body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
                self.assertGreaterEqual(len(tests), minimum)

    def test_base_fixture_is_valid_today(self) -> None:
        rm = roadmap.parse_text(BASE_TASK)
        self.assertEqual(roadmap.validate(rm, self.tmp), [])


class SpecFieldTests(HefestoTestCase):
    def parse(self, *extra: str) -> roadmap.Roadmap:
        return roadmap.parse_text(roadmap_with(*extra))

    def test_constants(self) -> None:
        self.assertEqual(roadmap.SPEC_FIELDS, ("scope", "out_of_scope", "risks", "requires_approval"))
        self.assertEqual(roadmap.APPROVAL_KINDS, ("install", "deploy", "animation", "external_data", "architecture"))
        for name in roadmap.SPEC_FIELDS:
            self.assertIn(name, roadmap.KNOWN_FIELDS)
        for name in roadmap.REQUIRED:
            self.assertNotIn(name, roadmap.SPEC_FIELDS, "los campos nuevos son opcionales")

    def test_roadmap_without_spec_fields_stays_valid(self) -> None:
        rm = self.parse()
        self.assertEqual(roadmap.validate(rm, self.tmp), [])
        task = rm.get("TASK-001")
        self.assertEqual((task.scope, task.out_of_scope, task.risks, task.requires_approval), ("", "", "", []))

    def test_fields_are_parsed(self) -> None:
        rm = self.parse("scope: solo el parser", "out_of_scope: la CLI", "risks: rompe ROADMAP antiguos",
                        "requires_approval: Install, deploy")
        self.assertEqual(roadmap.validate(rm, self.tmp), [])
        task = rm.get("TASK-001")
        self.assertEqual(task.scope, "solo el parser")
        self.assertEqual(task.out_of_scope, "la CLI")
        self.assertEqual(task.risks, "rompe ROADMAP antiguos")
        self.assertEqual(task.requires_approval, ["install", "deploy"])

    def test_none_means_no_approval(self) -> None:
        for value in ("none", "-"):
            with self.subTest(value=value):
                rm = self.parse(f"requires_approval: {value}")
                self.assertEqual(roadmap.validate(rm, self.tmp), [])
                self.assertEqual(rm.get("TASK-001").requires_approval, [])

    def test_unknown_approval_kind_is_rejected(self) -> None:
        errors = roadmap.validate(self.parse("requires_approval: install, magia"), self.tmp)
        self.assertTrue(any("requires_approval" in e and "magia" in e for e in errors), errors)

    def test_project_roadmap_stays_valid(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        rm = roadmap.parse(repo / ".hefesto" / "ROADMAP.md")
        self.assertEqual(roadmap.validate(rm, repo), [])

    def test_plan_step_documents_the_fields(self) -> None:
        body = sync.render_prompt("plan")
        for name in roadmap.SPEC_FIELDS:
            with self.subTest(field=name):
                self.assertIn(f"{name}:", body)


class PacketTests(HefestoTestCase):
    def build(self, text: str) -> str:
        self.init_project()
        path = self.project / ".hefesto" / "ROADMAP.md"
        path.write_text(text, encoding="utf-8")
        rm = roadmap.load_valid(self.project)
        task = rm.get("TASK-001")
        cfg = config.load(self.project)
        entry = (cfg.role_chain("runner") or ["claude"])[0]
        pkt = packet.build(self.project, cfg, "runner", "instrucción", task, rm, state.load(self.project),
                           providers.model_id(cfg, entry), self.project)
        return pkt.text

    def test_spec_fields_reach_the_packet(self) -> None:
        text = self.build(roadmap_with("scope: solo el parser", "out_of_scope: la CLI", "risks: datos antiguos",
                                       "requires_approval: install"))
        self.assertIn("- alcance: solo el parser", text)
        self.assertIn("- fuera de alcance: la CLI", text)
        self.assertIn("- riesgos: datos antiguos", text)
        self.assertIn("- aprobaciones pendientes: install", text)

    def test_absent_fields_add_nothing(self) -> None:
        text = self.build(roadmap_with())
        for label in ("- alcance:", "- fuera de alcance:", "- riesgos:", "- aprobaciones pendientes:"):
            self.assertNotIn(label, text)


class CoherenceTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def protocol(self) -> Path:
        for rel in ("AGENTS.md", ".hefesto/AGENTS.md"):
            if (self.project / rel).is_file():
                return self.project / rel
        self.fail("el proyecto de prueba no tiene protocolo")

    def test_critical_rules(self) -> None:
        names = dict(sync.CRITICAL_RULES)
        self.assertEqual(set(names.values()), {"state.json", "git push", "hefesto finish"})

    def test_fresh_project_is_coherent(self) -> None:
        self.assertEqual(sync.coherence_problems(self.project), [])

    def test_missing_critical_rule_is_reported(self) -> None:
        path = self.protocol()
        path.write_text(path.read_text(encoding="utf-8").replace("git push", "git pull"), encoding="utf-8")
        problems = sync.coherence_problems(self.project)
        self.assertTrue(any("git push" in p for p in problems), problems)

    def test_root_instructions_must_import_the_protocol(self) -> None:
        (self.project / "GEMINI.md").write_text("# Gemini\n\nPuedes hacer lo que quieras.\n", encoding="utf-8")
        problems = sync.coherence_problems(self.project)
        self.assertTrue(any("GEMINI.md" in p for p in problems), problems)

    def test_generated_drift_is_reported_without_fixing_it(self) -> None:
        rel = ".claude/skills/hefesto-next/SKILL.md"
        target = self.project / rel
        target.write_text(target.read_text(encoding="utf-8") + "\nHaz git push al terminar.\n", encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        problems = sync.coherence_problems(self.project)
        self.assertTrue(any(rel in p for p in problems), problems)
        self.assertEqual(target.read_text(encoding="utf-8"), before, "coherence_problems no modifica nada")

    def test_doctor_reports_instructions(self) -> None:
        checks = {c.name: c for c in doctor.run_checks(self.project)}
        self.assertIn("instrucciones", checks)
        self.assertEqual(checks["instrucciones"].level, doctor.OK)
        (self.project / "GEMINI.md").write_text("# Gemini\n", encoding="utf-8")
        check = {c.name: c for c in doctor.run_checks(self.project)}["instrucciones"]
        self.assertEqual(check.level, doctor.WARN)
        self.assertIn("GEMINI.md", check.detail)
