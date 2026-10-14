"""Tests para el pack context de Graphify (TASK-806).

Cubre los tres puntos de robustez del pack context:
1. Normalización de consulta a graphify query (sin caracteres de control, recortado a 300 caracteres,
   y anteponiendo 'tarea ' si empieza por guion para evitar inyección de opciones).
2. Sanitización y redacción de secretos en el extracto del grafo antes de entrar en paquete y resume.
3. Construcción del grafo inicial con `extract --code-only` en `project.init` si el pack context
   está activo en el proyecto y no existe `graphify-out/graph.json`.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest import mock

from hefesto import config, packet, packs, progress, project, roadmap, state, util
from tests.helpers import HefestoTestCase, git

ROADMAP_DASH = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: -opcion-con-guion
  - verify: `test -f src/suma.py`
  - files: src/
  - role: runner
  - size: S
"""


class ContextPackRobustnessTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls)
        os.environ.pop("HEFESTO_ROLE", None)
        self.use_fakes("graphify")

    def graphify_calls(self) -> list[dict[str, object]]:
        return [c for c in util.read_jsonl(self.calls) if c.get("name") == "graphify"]

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]

    # -------------------------------------------------------------------------
    # Punto 1: Normalización de consulta
    # -------------------------------------------------------------------------

    def test_normalize_query_prepends_tarea_when_starts_with_dash(self) -> None:
        # Guion simple o doble opción
        self.assertEqual(packs.normalize_query("-v"), "tarea -v")
        self.assertEqual(packs.normalize_query("--help"), "tarea --help")
        self.assertEqual(packs.normalize_query("   -opcion"), "tarea -opcion")
        # Si no empieza por guion, no cambia
        self.assertEqual(packs.normalize_query("crear suma"), "crear suma")

    def test_normalize_query_strips_control_characters(self) -> None:
        raw = "tarea\x00con\x1b[31mcolor\x07y\rcontrol\x08"
        normalized = packs.normalize_query(raw)
        self.assertNotIn("\x00", normalized)
        self.assertNotIn("\x1b", normalized)
        self.assertNotIn("\x07", normalized)
        self.assertNotIn("\r", normalized)
        self.assertNotIn("\x08", normalized)
        self.assertIn("color", normalized)
        self.assertIn("control", normalized)

    def test_normalize_query_truncates_to_300_chars(self) -> None:
        long_text = "a" * 500
        normalized = packs.normalize_query(long_text)
        self.assertEqual(len(normalized), 300)
        self.assertEqual(normalized, "a" * 300)

        # Si además empezaba por guion, no debe superar 300 caracteres y no debe empezar por guion
        dash_long = "-" + "b" * 500
        dash_norm = packs.normalize_query(dash_long)
        self.assertLessEqual(len(dash_norm), 300)
        self.assertTrue(dash_norm.startswith("tarea -"))
        self.assertFalse(dash_norm.startswith("-"))

    def test_query_graph_passes_normalized_argument(self) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_DASH, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

        code, out, err = self.here("pack", "enable", "context", "--yes")
        self.assertEqual(code, 0, out + err)

        rm = roadmap.load_valid(self.project)
        task = rm.get("TASK-001")
        cfg = config.load(self.project)

        # query_graph con tarea cuyo título empieza por guion
        res = packs.query_graph(self.project, self.project, task, cfg)
        self.assertTrue(res)

        queries = [c for c in self.graphify_calls() if c["argv"][0] == "query"]
        self.assertTrue(queries)
        query_arg = queries[-1]["argv"][1]
        self.assertTrue(isinstance(query_arg, str))
        self.assertFalse(query_arg.startswith("-"), f"Argumento empieza por guion: {query_arg}")
        self.assertTrue(query_arg.startswith("tarea -opcion-con-guion"))

    # -------------------------------------------------------------------------
    # Punto 2: Sanitización y redacción de secretos en extracto
    # -------------------------------------------------------------------------

    def test_sanitize_graph_extract_redacts_and_sanitizes(self) -> None:
        prefix = "sk-" + "proj-"
        fake_secret = prefix + ("a" * 40)
        raw_output = f"NODE key={fake_secret} \x1b[31mcolor\x00"
        sanitized = packs.sanitize_graph_extract(raw_output)
        self.assertNotIn(prefix, sanitized)
        self.assertIn("[REDACTED]", sanitized)
        self.assertNotIn("\x00", sanitized)
        # Caracteres de control escapados visiblemente
        self.assertIn(r"\x1b", sanitized)

    def test_packet_and_resume_contain_sanitized_and_redacted_graph(self) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_DASH, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

        code, out, err = self.here("pack", "enable", "context", "--yes")
        self.assertEqual(code, 0, out + err)
        self.cli("start", "TASK-001")

        prefix = "sk-" + "proj-"
        fake_secret = prefix + ("b" * 40)
        raw_graph = f"NODE suma() [secret={fake_secret}] \x1b[32mOK\x00"
        with mock.patch("hefesto.packs.run_graphify", return_value=(0, raw_graph)):
            code, packet_out, _ = self.cli("packet", "TASK-001")
            self.assertEqual(code, 0)
            self.assertIn("## Grafo de código", packet_out)
            self.assertNotIn(prefix, packet_out)
            self.assertIn("[REDACTED]", packet_out)
            self.assertNotIn("\x00", packet_out)

            info = progress.resume_data(self.project)
            self.assertIn("[REDACTED]", info["graph"])
            self.assertNotIn(prefix, info["graph"])
            self.assertNotIn("\x00", info["graph"])

            cfg = config.load(self.project)
            rm = roadmap.load_valid(self.project)
            task = rm.get("TASK-001")
            data = state.load(self.project)
            pkt = packet.build(self.project, cfg, "runner", "instruccion", task, rm, data, "gemini:default", self.project)
            self.assertIn("[REDACTED]", pkt.text)
            self.assertNotIn(prefix, pkt.text)
            self.assertNotIn("\x00", pkt.text)

    # -------------------------------------------------------------------------
    # Punto 3: project.init construye grafo si context está activo y no existe
    # -------------------------------------------------------------------------

    def test_init_builds_graph_if_context_active_and_no_graph(self) -> None:
        self.init_project()
        # Activamos el pack context en settings.json
        settings_path = config.project_settings_path(self.project)
        settings = config.read_settings(settings_path)
        settings.setdefault("packs", {})["context"] = {"enabled": True}
        config.write_settings(settings_path, settings)

        graph_file = self.project / "graphify-out" / "graph.json"
        if graph_file.exists():
            graph_file.unlink()

        # Limpiamos llamadas registradas
        if self.calls.exists():
            self.calls.unlink()

        # Ejecutamos project.init sobre el proyecto existente
        report = project.init(self.project, shared=True)
        self.assertTrue(graph_file.is_file(), "graph.json debió ser creado por project.init")

        extracts = [c for c in self.graphify_calls() if c["argv"] == ["extract", ".", "--code-only"]]
        self.assertEqual(len(extracts), 1)

    def test_init_does_not_rebuild_graph_if_already_exists(self) -> None:
        self.init_project()
        settings_path = config.project_settings_path(self.project)
        settings = config.read_settings(settings_path)
        settings.setdefault("packs", {})["context"] = {"enabled": True}
        config.write_settings(settings_path, settings)

        graph_dir = self.project / "graphify-out"
        graph_dir.mkdir(parents=True, exist_ok=True)
        graph_file = graph_dir / "graph.json"
        graph_file.write_text(json.dumps({"existing": True}), encoding="utf-8")

        if self.calls.exists():
            self.calls.unlink()

        project.init(self.project, shared=True)
        extracts = [c for c in self.graphify_calls() if c["argv"] == ["extract", ".", "--code-only"]]
        self.assertEqual(len(extracts), 0, "No debió ejecutar extract si graph.json ya existía")

    def test_init_does_not_build_graph_if_context_disabled(self) -> None:
        self.init_project()
        graph_file = self.project / "graphify-out" / "graph.json"
        if graph_file.exists():
            graph_file.unlink()

        if self.calls.exists():
            self.calls.unlink()

        project.init(self.project, shared=True)
        self.assertFalse(graph_file.exists())
        extracts = [c for c in self.graphify_calls() if c["argv"] == ["extract", ".", "--code-only"]]
        self.assertEqual(len(extracts), 0)
