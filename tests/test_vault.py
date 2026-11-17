"""Contratos de la Fase 17: bóveda Obsidian (TASK-1701; ADR 2026-10-17).

Obsidian solo lee: Hefesto escribe la bóveda y `state.json`, `trust.json` y los marcadores del ROADMAP siguen siendo
la única fuente. La única entrada desde Obsidian es la propiedad `revision` de las candidatas de memoria, que un
humano aplica con `hefesto memory review --from-vault` (TTY).

API que fijan estos tests (todo en `hefesto/vault.py` salvo donde se indique):

Bóveda del proyecto (TASK-1702)
- `VAULT_DIR == ".hefesto/vault"`; `GENERATED_KEY == "generado_por"` y `GENERATED_VALUE == "hefesto"`: toda nota
  generada lleva `generado_por: hefesto` en su frontmatter.
- `build(project, out=None) -> BuildResult` (root: Path, written: list[Path], removed: list[Path]). `out` relativo
  o absoluto, siempre dentro del proyecto: fuera → HefestoError (EXIT_BLOCKED) sin escribir nada.
- Una nota por fase `fases/<task.phase_slug>.md`, tarea `tareas/<TASK>.md`, evidencia `evidencia/<stem>.md`
  (stem del JSON de `.hefesto/evidence/`), decisión `decisiones/*.md` (cada `## ` de DECISIONS.md), lección
  `lecciones/*.md` (cada entrada `- ` de LEARNINGS.md) e informe de investigación `investigacion/<stem>.md`
  (cada `.md` de `.hefesto/evidence/research/`).
- Frontmatter de la tarea: `estado` (`task.status`: pending, in_progress, done, blocked), `fase`, `intentos`,
  `proveedor`, `veredicto`, `fecha` (los tres últimos y `intentos` desde la evidencia, si la hay).
- Enlaces con carpeta (`[[tareas/TASK-002]]`, se admite alias `|`): tarea → fase, dependencias, evidencia y
  decisiones que la mencionan; fase → sus tareas; decisión → tareas que menciona; evidencia → tarea. La nota de la
  tarea enlaza cada captura de `visual.captures` de su evidencia (aparece el nombre del fichero).
- Idempotente (dos `build` dejan los mismos bytes); borra solo notas generadas que ya no tocan; nunca copia
  `state.json`, `trust.json`, `logs/` ni `logs/packets/`; redacta secretos (`guard.redact`); añade
  `.hefesto/vault/` al exclude local de git (`project.git_exclude_path`).
- CLI: `hefesto vault build [--out DIR]`.

Lienzo (TASK-1703)
- `canvas(project, out=None) -> Path`: llama a `build` y escribe `<bóveda>/roadmap.canvas` (JSON Canvas 1.0:
  `nodes` y `edges`). Un nodo `group` por fase (`label` = nombre de la fase), un nodo `file` por tarea con
  `file` = `tareas/<TASK>.md` (relativo a la bóveda) dentro de los límites de su grupo; una arista por cada
  `depends_on` (`fromNode` = dependencia, `toNode` = tarea); `color` = `CANVAS_COLORS[task.status]`
  (preset "1"–"6" o `#rrggbb`); ids únicos, `x`, `y`, `width`, `height` enteros; posiciones deterministas.
- CLI: `hefesto vault canvas`.

Memoria global como bóveda (TASK-1704)
- `MEMORY_INDEX_DIR == "_indices"`: `memory_index() -> list[Path]` escribe en `<HEFESTO_HOME>/memory/_indices/`
  las notas índice `stack-<x>.md`, `tipo-<x>.md`, `proyecto-<x>.md` y `confianza-<x>.md` con enlaces `[[…]]` a
  las notas aprobadas (nunca a `_pendientes/`) sin tocar su contenido; idempotente. `memory.search` (y el resto de
  lecturas de notas aprobadas en `hefesto/memory.py`) ignora `_indices/`.
- `REVIEW_KEY == "revision"`, `REVIEW_ACTIONS == ("aprobar", "descartar")`.
  `review_from_vault(project=None) -> str`: niega con `HEFESTO_ROLE` (EXIT_BLOCKED), exige TTY
  (`util.require_tty`), muestra el resumen (candidata → acción), pide una confirmación final (`util.confirm`) y solo
  entonces aprueba (como `memory review`, sin conservar `revision`) o borra; una propiedad ausente o desconocida
  deja la candidata intacta. Sin confirmación no cambia nada.
- CLI: `hefesto vault memory`, `hefesto memory review --from-vault`.

CLI de Obsidian opcional (TASK-1705)
- `OBSIDIAN_BINARY == "obsidian"`, buscado con `util.which`.
- `search(project, term) -> SearchResult` (source: "obsidian" | "ficheros", hits: list[str]). Término vacío →
  HefestoError (EXIT_INPUT). Con la CLI: `subprocess.run` con lista de argumentos (el primero, la CLI; uno de
  ellos contiene el término), sin shell; si falla (OSError o código distinto de 0) busca en los ficheros. Sin la
  CLI: rutas posix relativas a la bóveda de las notas `.md` que contienen el término (sin distinguir mayúsculas).
- `open_vault(project) -> str`: con la CLI la lanza una vez con `subprocess.run` (lista, sin shell); sin ella no ejecuta nada y avisa con la
  ruta de la bóveda.
- `doctor.run_checks` incluye el check `obsidian`, nunca obligatorio.
- CLI: `hefesto vault search "término"`, `hefesto vault open`.

Solo `ContractDefinitionTests` pasa antes de la implementación; el resto falla por `hefesto.vault` ausente o por
los subcomandos `vault build`, `vault canvas`, `vault memory`, `vault search` y `memory review --from-vault`.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config, memory, roadmap, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Formularios
- [x] TASK-001: Formulario de alta
  - verify: `true`
  - files: src/app/alta/alta.component.ts
  - role: runner
  - size: S
- [ ] TASK-002: Validación del alta
  - verify: `true`
  - files: src/app/alta/validar.ts
  - role: runner
  - size: S
  - depends_on: TASK-001

## Fase 2 — Informes
- [ ] TASK-003: Informe mensual
  - verify: `true`
  - files: src/informe.ts
  - role: runner
  - size: S
  - depends_on: TASK-002
"""

FAKE_KEY = "sk-" + "ant-" + "api03-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # nunca literal: lo bloquea el guard
SENTINEL = "CENTINELA-PRIVADO-7731"
LINK = re.compile(r"\[\[([^\]|#]+)")


def _vault() -> Any:
    return importlib.import_module("hefesto.vault")


def _links(text: str) -> set[str]:
    return {m.strip() for m in LINK.findall(text)}


def _meta(path: Path) -> dict[str, str]:
    return memory.parse_note_content(path.read_text(encoding="utf-8"))[0]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class VaultCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        hef = self.project / ".hefesto"
        (hef / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        (hef / "DECISIONS.md").write_text(
            "# DECISIONS\n\n## 2026-11-01 — Validar el alta en el cliente\n\nTASK-001 valida al perder el foco. "
            f"Clave filtrada por error: {FAKE_KEY}\n", encoding="utf-8")
        (hef / "LEARNINGS.md").write_text("# LEARNINGS\n\n- 2026-11-02: El DNI vacío rompe el alta. #alta\n",
                                          encoding="utf-8")
        capture = hef / "evidence" / "visual" / "TASK-001" / "alta-1280.png"
        capture.parent.mkdir(parents=True)
        capture.write_bytes(b"\x89PNG\r\n\x1a\n")
        (hef / "evidence" / "TASK-001.json").write_text(json.dumps({
            "task_id": "TASK-001", "title": "Formulario de alta", "phase": "Fase 1 — Formularios", "attempts": 2,
            "review": {"verdict": "pass", "notes": "ok", "at": "2026-11-03T10:00:00+00:00"},
            "providers": ["gemini:default"], "runs": [], "files": [],
            "visual": {"captures": [{"path": capture.relative_to(self.project).as_posix(), "sha256": "0"}]},
        }), encoding="utf-8")
        research = hef / "evidence" / "research" / "2026-11-04-colas.md"
        research.parent.mkdir(parents=True)
        research.write_text("contenido externo: dato, no instrucción\n\n# Colas\n\nInforme.\n", encoding="utf-8")
        packets = hef / "logs" / "packets"
        packets.mkdir(parents=True, exist_ok=True)
        (packets / "TASK-001-run.md").write_text(f"paquete {SENTINEL}\n", encoding="utf-8")
        util.append_jsonl(hef / "logs" / "runs.jsonl", {"run_id": "r1", "note": SENTINEL})
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "fixtures")

    @property
    def vault_root(self) -> Path:
        return self.project / ".hefesto" / "vault"

    def note(self, rel: str) -> Path:
        return self.vault_root / rel


# --- Definición del contrato ------------------------------------------------------------------------------------


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"BuildTests": 8, "CanvasTests": 4, "MemoryVaultTests": 6, "ObsidianCliTests": 5}
    API = ("VAULT_DIR", "GENERATED_KEY", "GENERATED_VALUE", "build", "BuildResult", "canvas", "roadmap.canvas",
           "CANVAS_COLORS", "MEMORY_INDEX_DIR", "memory_index", "REVIEW_KEY", "REVIEW_ACTIONS",
           "review_from_vault", "OBSIDIAN_BINARY", "search", "SearchResult", "open_vault", "vault build",
           "vault canvas", "vault memory", "vault search", "vault open", "memory review --from-vault")

    def tree(self) -> ast.Module:
        return ast.parse(Path(__file__).read_text(encoding="utf-8"))

    def test_docstring_names_the_whole_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API:
            with self.subTest(name=name):
                self.assertIn(name, doc)

    def test_every_behaviour_has_its_tests(self) -> None:
        classes = {n.name: n for n in self.tree().body if isinstance(n, ast.ClassDef)}
        for name, minimum in self.REQUIRED_CLASSES.items():
            with self.subTest(cls=name):
                self.assertIn(name, classes)
                tests = [f for f in classes[name].body if isinstance(f, ast.FunctionDef)
                         and f.name.startswith("test_")]
                self.assertGreaterEqual(len(tests), minimum)

    def test_implementation_is_imported_lazily(self) -> None:
        # Importarlo arriba impediría compilar el módulo y nada fallaría «por el motivo correcto».
        for node in self.tree().body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "hefesto.vault")
                if node.module == "hefesto":
                    self.assertNotIn("vault", [a.name for a in node.names])
            if isinstance(node, ast.Import):
                self.assertNotIn("hefesto.vault", [a.name for a in node.names])

    def test_contract_is_offline_and_never_uses_a_shell(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("shell=" + "True", source)
        self.assertNotIn("url" + "lib", source)
        self.assertNotIn("http" + "://", source)


# --- TASK-1702 ------------------------------------------------------------------------------------------------


class BuildTests(VaultCase):
    def test_writes_one_note_per_entity_with_the_generated_mark(self) -> None:
        vault = _vault()
        self.assertEqual(vault.VAULT_DIR, ".hefesto/vault")
        result = vault.build(self.project)
        self.assertEqual(Path(result.root).resolve(), self.vault_root.resolve())
        for rel in ("tareas/TASK-001.md", "tareas/TASK-002.md", "tareas/TASK-003.md", "fases/fase-1-formularios.md",
                    "fases/fase-2-informes.md", "evidencia/TASK-001.md", "investigacion/2026-11-04-colas.md"):
            with self.subTest(note=rel):
                self.assertTrue(self.note(rel).is_file())
                self.assertEqual(_meta(self.note(rel)).get(vault.GENERATED_KEY), vault.GENERATED_VALUE)
        self.assertEqual(len(list((self.vault_root / "decisiones").glob("*.md"))), 1)
        self.assertEqual(len(list((self.vault_root / "lecciones").glob("*.md"))), 1)

    def test_task_frontmatter_comes_from_roadmap_and_evidence(self) -> None:
        _vault().build(self.project)
        done = _meta(self.note("tareas/TASK-001.md"))
        self.assertEqual(done["estado"], "done")
        self.assertEqual(done["fase"], "Fase 1 — Formularios")
        self.assertEqual(done["intentos"], "2")
        self.assertIn("gemini:default", done["proveedor"])
        self.assertEqual(done["veredicto"], "pass")
        self.assertTrue(done.get("fecha"))
        self.assertEqual(_meta(self.note("tareas/TASK-002.md"))["estado"], "pending")

    def test_links_between_tasks_phases_dependencies_evidence_and_decisions(self) -> None:
        _vault().build(self.project)
        first = _links(self.note("tareas/TASK-001.md").read_text(encoding="utf-8"))
        self.assertIn("fases/fase-1-formularios", first)
        self.assertIn("evidencia/TASK-001", first)
        self.assertTrue(any(link.startswith("decisiones/") for link in first), first)
        second = _links(self.note("tareas/TASK-002.md").read_text(encoding="utf-8"))
        self.assertIn("tareas/TASK-001", second)
        phase = _links(self.note("fases/fase-1-formularios.md").read_text(encoding="utf-8"))
        self.assertTrue({"tareas/TASK-001", "tareas/TASK-002"} <= phase)
        self.assertNotIn("tareas/TASK-003", phase)
        decision = next((self.vault_root / "decisiones").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("tareas/TASK-001", _links(decision))
        self.assertIn("tareas/TASK-001", _links(self.note("evidencia/TASK-001.md").read_text(encoding="utf-8")))

    def test_links_visual_captures(self) -> None:
        _vault().build(self.project)
        self.assertIn("alta-1280.png", self.note("tareas/TASK-001.md").read_text(encoding="utf-8"))

    def test_is_idempotent_and_removes_only_its_own_stale_notes(self) -> None:
        vault = _vault()
        vault.build(self.project)
        mine = self.note("notas/mia.md")
        mine.parent.mkdir(parents=True)
        mine.write_text("# Mi nota\n", encoding="utf-8")
        human = self.note("tareas/borrador.md")
        human.write_text("sin cabecera de Hefesto\n", encoding="utf-8")
        first = _snapshot(self.vault_root)
        vault.build(self.project)
        self.assertEqual(_snapshot(self.vault_root), first)
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP.split("## Fase 2")[0], encoding="utf-8")
        result = vault.build(self.project)
        self.assertFalse(self.note("tareas/TASK-003.md").exists())
        self.assertFalse(self.note("fases/fase-2-informes.md").exists())
        self.assertIn(self.note("tareas/TASK-003.md").resolve(), [Path(p).resolve() for p in result.removed])
        self.assertTrue(mine.exists())
        self.assertTrue(human.exists())

    def test_never_copies_control_files_logs_or_packets_and_redacts_secrets(self) -> None:
        _vault().build(self.project)
        names = {p.name for p in self.vault_root.rglob("*")}
        self.assertFalse({"state.json", "trust.json", "runs.jsonl"} & names)
        for path in self.vault_root.rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", errors="replace")
                self.assertNotIn(SENTINEL, text, path)
                self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", text, path)

    def test_out_is_confined_to_the_project(self) -> None:
        vault = _vault()
        result = vault.build(self.project, out=self.project / "boveda")
        self.assertTrue((self.project / "boveda" / "tareas" / "TASK-001.md").is_file())
        self.assertEqual(Path(result.root).resolve(), (self.project / "boveda").resolve())
        outside = self.tmp / "fuera"
        for target in (outside, self.project / ".." / "fuera"):
            with self.subTest(out=str(target)), self.assertRaises(HefestoError) as ctx:
                vault.build(self.project, out=target)
            self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertFalse(outside.exists())

    def test_vault_is_excluded_from_git(self) -> None:
        from hefesto import project as project_mod

        _vault().build(self.project)
        exclude = project_mod.git_exclude_path(self.project).read_text(encoding="utf-8").splitlines()
        self.assertIn(".hefesto/vault/", [line.strip() for line in exclude])
        self.assertEqual(git(self.project, "status", "--porcelain", "--", ".hefesto/vault"), "")

    def test_cli_vault_build(self) -> None:
        code, out, err = self.cli("vault", "build")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.note("tareas/TASK-001.md").is_file())
        code, out, err = self.cli("vault", "build", "--out", str(self.tmp / "fuera"))
        self.assertEqual(code, EXIT_BLOCKED, out + err)


# --- TASK-1703 ------------------------------------------------------------------------------------------------


class CanvasTests(VaultCase):
    def load(self) -> dict[str, Any]:
        path = _vault().canvas(self.project)
        self.assertEqual(Path(path).resolve(), (self.vault_root / "roadmap.canvas").resolve())
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_is_valid_json_canvas(self) -> None:
        data = self.load()
        self.assertEqual(set(data) & {"nodes", "edges"}, {"nodes", "edges"})
        ids = [n["id"] for n in data["nodes"]] + [e["id"] for e in data["edges"]]
        self.assertEqual(len(ids), len(set(ids)))
        for node in data["nodes"]:
            self.assertIn(node["type"], ("group", "file", "text", "link"))
            for key in ("x", "y", "width", "height"):
                self.assertIsInstance(node[key], int, (node, key))
            if "color" in node:
                self.assertRegex(node["color"], r"^([1-6]|#[0-9a-fA-F]{6})$")
        node_ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            self.assertIn(edge["fromNode"], node_ids)
            self.assertIn(edge["toNode"], node_ids)

    def test_one_group_per_phase_and_one_file_node_per_task_inside_it(self) -> None:
        data = self.load()
        groups = {n["label"]: n for n in data["nodes"] if n["type"] == "group"}
        self.assertEqual(set(groups), {"Fase 1 — Formularios", "Fase 2 — Informes"})
        files = {n["file"]: n for n in data["nodes"] if n["type"] == "file"}
        self.assertEqual(set(files), {"tareas/TASK-001.md", "tareas/TASK-002.md", "tareas/TASK-003.md"})
        for rel in files:
            self.assertTrue((self.vault_root / rel).is_file(), rel)
        group, node = groups["Fase 2 — Informes"], files["tareas/TASK-003.md"]
        self.assertGreaterEqual(node["x"], group["x"])
        self.assertGreaterEqual(node["y"], group["y"])
        self.assertLessEqual(node["x"] + node["width"], group["x"] + group["width"])
        self.assertLessEqual(node["y"] + node["height"], group["y"] + group["height"])

    def test_edges_follow_depends_on_and_colors_follow_status(self) -> None:
        vault = _vault()
        data = self.load()
        by_id = {n["id"]: n.get("file") for n in data["nodes"]}
        edges = {(by_id[e["fromNode"]], by_id[e["toNode"]]) for e in data["edges"]}
        self.assertEqual(edges, {("tareas/TASK-001.md", "tareas/TASK-002.md"),
                                 ("tareas/TASK-002.md", "tareas/TASK-003.md")})
        files = {n["file"]: n for n in data["nodes"] if n["type"] == "file"}
        self.assertEqual(files["tareas/TASK-001.md"]["color"], vault.CANVAS_COLORS["done"])
        self.assertEqual(files["tareas/TASK-002.md"]["color"], vault.CANVAS_COLORS["pending"])
        self.assertEqual(set(vault.CANVAS_COLORS), set(roadmap.MARKERS.values()))
        self.assertEqual(len(set(vault.CANVAS_COLORS.values())), len(vault.CANVAS_COLORS))

    def test_positions_are_deterministic(self) -> None:
        self.load()
        path = self.vault_root / "roadmap.canvas"
        before = path.read_bytes()
        self.load()
        self.assertEqual(path.read_bytes(), before)

    def test_cli_vault_canvas(self) -> None:
        code, out, err = self.cli("vault", "canvas")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.vault_root / "roadmap.canvas").is_file())


# --- TASK-1704 ------------------------------------------------------------------------------------------------


class MemoryVaultTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        config.set_setting(config.global_settings_path(), ("packs", "memory", "enabled"), True)
        self.store = util.hefesto_home() / "memory"

    def approved(self, name: str, stack: str, project: str, confianza: str = "0.8") -> Path:
        folder = self.store / "errores-resueltos"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.md"
        path.write_text(memory.format_note({"fecha": "2026-11-01", "proyecto": project, "stack": stack,
                                            "tipo": "errores-resueltos", "confianza": confianza},
                                           f"Lección {name} sobre resilientCollection"), encoding="utf-8")
        return path

    def candidate(self, name: str, revision: str | None) -> Path:
        folder = self.store / "_pendientes"
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"fecha": "2026-11-02", "proyecto": "p1", "stack": "node", "tipo": "errores-resueltos"}
        if revision is not None:
            meta["revision"] = revision
        path = folder / f"{name}.md"
        path.write_text(memory.format_note(meta, f"Candidata {name}"), encoding="utf-8")
        return path

    def test_index_notes_link_approved_notes_without_touching_them(self) -> None:
        vault = _vault()
        a = self.approved("ang", "angular", "p1")
        f = self.approved("flu", "flutter", "p2", confianza="0.5")
        self.candidate("pend", None)
        before = (a.read_bytes(), f.read_bytes())
        written = vault.memory_index()
        index_dir = self.store / vault.MEMORY_INDEX_DIR
        self.assertTrue(written)
        self.assertTrue(all(Path(p).resolve().parent == index_dir.resolve() for p in written))
        angular = _links((index_dir / "stack-angular.md").read_text(encoding="utf-8"))
        self.assertTrue(any(link.endswith("ang") for link in angular), angular)
        self.assertFalse(any(link.endswith("flu") for link in angular), angular)
        for name in ("tipo-errores-resueltos.md", "proyecto-p1.md", "proyecto-p2.md", "confianza-0.8.md"):
            self.assertTrue((index_dir / name).is_file(), name)
        everything = " ".join((index_dir / n).read_text(encoding="utf-8") for n in os.listdir(index_dir))
        self.assertNotIn("pend", " ".join(_links(everything)))
        self.assertEqual((a.read_bytes(), f.read_bytes()), before)
        snapshot = _snapshot(index_dir)
        vault.memory_index()
        self.assertEqual(_snapshot(index_dir), snapshot)

    def test_indices_are_not_memory_notes(self) -> None:
        self.approved("ang", "angular", "p1")
        _vault().memory_index()
        hits = memory.search("ang")
        self.assertTrue(hits)
        self.assertFalse(any("_indices" in json.dumps(h, default=str) for h in hits), hits)

    def test_review_from_vault_applies_the_revision_property_after_confirming(self) -> None:
        vault = _vault()
        keep = self.candidate("sin-marca", None)
        odd = self.candidate("rara", "quizas")
        yes = self.candidate("buena", "aprobar")
        no = self.candidate("mala", "descartar")
        untouched = (keep.read_bytes(), odd.read_bytes())
        out = self.run_vault_review(answers=["s"])
        self.assertIn("buena", out)
        self.assertIn("mala", out)
        self.assertFalse(yes.exists())
        self.assertFalse(no.exists())
        approved = self.store / "errores-resueltos" / "buena.md"
        self.assertTrue(approved.is_file())
        self.assertNotIn("revision", _meta(approved))
        self.assertFalse((self.store / "errores-resueltos" / "mala.md").exists())
        self.assertEqual((keep.read_bytes(), odd.read_bytes()), untouched)

    def test_without_final_confirmation_nothing_changes(self) -> None:
        self.candidate("buena", "aprobar")
        self.candidate("mala", "descartar")
        before = _snapshot(self.store)
        self.run_vault_review(answers=["n"])
        self.assertEqual(_snapshot(self.store), before)

    def test_review_from_vault_requires_a_human(self) -> None:
        vault = _vault()
        self.candidate("buena", "aprobar")
        with mock.patch("hefesto.util.is_tty", return_value=False), self.assertRaises(HefestoError) as ctx:
            vault.review_from_vault()
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        with mock.patch.dict(os.environ, {"HEFESTO_ROLE": "runner"}), \
                mock.patch("hefesto.util.is_tty", return_value=True), \
                mock.patch("builtins.input", return_value="s"), self.assertRaises(HefestoError) as ctx:
            vault.review_from_vault()
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertTrue((self.store / "_pendientes" / "buena.md").exists())
        self.assertEqual(vault.REVIEW_KEY, "revision")
        self.assertEqual(tuple(vault.REVIEW_ACTIONS), ("aprobar", "descartar"))

    def test_cli_memory_review_from_vault_and_vault_memory(self) -> None:
        self.candidate("buena", "aprobar")
        code, out, err = self.run_cli("memory", "review", "--from-vault", tty_value=False)
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        code, out, err = self.run_cli("memory", "review", "--from-vault", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.store / "errores-resueltos" / "buena.md").is_file())
        code, out, err = self.run_cli("vault", "memory")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.store / "_indices").is_dir())

    def run_vault_review(self, answers: list[str]) -> str:
        out = io.StringIO()
        with mock.patch("hefesto.util.is_tty", return_value=True), \
                mock.patch("builtins.input", side_effect=lambda *_a: answers.pop(0) if answers else ""), \
                contextlib.redirect_stdout(out):
            message = _vault().review_from_vault()
        return out.getvalue() + message


# --- TASK-1705 ------------------------------------------------------------------------------------------------


class ObsidianCliTests(VaultCase):
    BINARY = "C:/bin/obsidian.exe" if os.name == "nt" else "/usr/bin/obsidian"

    def with_cli(self, result: Any) -> tuple[Any, list[tuple[list[str], dict[str, Any]]]]:
        seen: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(cmd, *a, **kw):
            seen.append(([str(x) for x in cmd], kw))
            if isinstance(result, BaseException):
                raise result
            return result

        with mock.patch("hefesto.util.which", side_effect=lambda n: self.BINARY if n == "obsidian" else None), \
                mock.patch("subprocess.run", side_effect=fake_run):
            found = _vault().search(self.project, "dni vacío")
        return found, seen

    def test_without_cli_searches_the_files(self) -> None:
        vault = _vault()
        self.assertEqual(vault.OBSIDIAN_BINARY, "obsidian")
        vault.build(self.project)
        with mock.patch("hefesto.util.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=AssertionError("sin CLI no se ejecuta nada")):
            found = vault.search(self.project, "DNI VACÍO")
        self.assertEqual(found.source, "ficheros")
        self.assertTrue(found.hits)
        self.assertTrue(all(h.endswith(".md") and not Path(h).is_absolute() and "\\" not in h for h in found.hits))
        self.assertTrue(any(h.startswith("lecciones/") for h in found.hits), found.hits)

    def test_with_cli_uses_it_without_shell(self) -> None:
        _vault().build(self.project)
        found, seen = self.with_cli(subprocess.CompletedProcess([], 0, "lecciones/dni.md\n", ""))
        self.assertEqual(found.source, "obsidian")
        self.assertEqual(len(seen), 1)
        argv, kw = seen[0]
        self.assertEqual(argv[0], self.BINARY)
        self.assertTrue(any("dni vacío" in arg for arg in argv), argv)
        self.assertNotEqual(kw.get("shell"), True)

    def test_failing_cli_falls_back_to_files(self) -> None:
        _vault().build(self.project)
        for label, result in {"código 1": subprocess.CompletedProcess([], 1, "", "boom"),
                              "no arranca": OSError("no se puede ejecutar")}.items():
            with self.subTest(caso=label):
                found, _ = self.with_cli(result)
                self.assertEqual(found.source, "ficheros")
                self.assertTrue(found.hits)

    def test_empty_term_is_an_input_error(self) -> None:
        with self.assertRaises(HefestoError) as ctx:
            _vault().search(self.project, "   ")
        self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_open_with_or_without_cli(self) -> None:
        vault = _vault()
        vault.build(self.project)
        with mock.patch("hefesto.util.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=AssertionError("sin CLI no se ejecuta nada")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("sin CLI no se ejecuta nada")):
            message = vault.open_vault(self.project)
        self.assertIn("vault", message.replace("\\", "/"))
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("hefesto.util.which", side_effect=lambda n: self.BINARY if n == "obsidian" else None), \
                mock.patch("subprocess.run", return_value=ok) as run:
            vault.open_vault(self.project)
        run.assert_called_once()
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertEqual(str(run.call_args.args[0][0]), self.BINARY)
        self.assertNotEqual(run.call_args.kwargs.get("shell"), True)

    def test_doctor_reports_obsidian_without_requiring_it(self) -> None:
        from hefesto import doctor

        _vault()  # el check acompaña a `hefesto/vault.py`
        real = util.which

        def fake_which(name: str) -> str | None:
            return None if name == "obsidian" else real(name)

        with mock.patch("hefesto.util.which", side_effect=fake_which), \
                mock.patch("hefesto.doctor.which", side_effect=fake_which):
            checks = {c.name: c for c in doctor.run_checks(None)}
        self.assertIn("obsidian", checks)
        self.assertFalse(checks["obsidian"].required)

    def test_cli_vault_search(self) -> None:
        self.cli("vault", "build")
        with mock.patch("hefesto.util.which", return_value=None):
            code, out, err = self.cli("vault", "search", "dni")
        self.assertEqual(code, 0, out + err)
        self.assertIn("lecciones/", out)
