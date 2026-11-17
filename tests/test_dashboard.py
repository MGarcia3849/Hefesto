"""Contratos de la Fase 22: tablero animado de la forja (TASK-2201).

`hefesto dashboard` es un tablero de consola de **solo lectura**: Hefesto en el centro como coordinador, sus
agentes por rol alrededor, el progreso del ROADMAP, la tarea en curso, la siguiente y las últimas delegaciones
reales. Nunca inventa actividad (un agente solo «trabaja» si hay una delegación real abierta), nunca cambia el
estado, nunca lanza procesos para pintarse, nunca lee secretos y no usa librerías externas (solo la biblioteca
estándar). Todo es offline: estado, ROADMAP y logs falsos en un temporal y CLIs falsas de `tests/fakes`.

API que fijan estos tests (todo en `hefesto/dashboard.py` salvo donde se indique):

Constantes
- `ROLES`: tupla con, al menos y en este orden, `"core"`, `"runner"`, `"forge"`, `"reviewer"`.
- `STATE_LABELS == {"idle": "en espera", "working": "trabajando", "done": "terminó", "failed": "falló"}`.
- `MAX_EVENTS == 8`.
- `events_path(project) -> Path`: `<project>/.hefesto/logs/delegations.jsonl`.

Instantánea: `snapshot(project) -> dict` (serializable con `json.dumps`; todo texto pasa por `guard.redact`)
- Lee solo `state.load`, el ROADMAP, la configuración y `events_path`; nunca escribe nada, nunca lanza procesos
  (ni git ni CLIs) y nunca abre `.env*`, `*.pem`, `*.key` ni `id_*`.
- Claves (`SNAPSHOT_KEYS`): `project` (nombre de la carpeta), `status` (de `state.json`), `task`, `progress`,
  `next`, `agents` y `events`.
- `task`: `None` o `{"id", "title", "role", "attempts", "phase"}` de `current_task_id` (rol: `active_role` o el de
  la tarea).
- `progress`: `{"done", "total", "in_progress", "blocked", "pending", "remaining", "percent"}` contando los
  marcadores `[x]`, `[~]`, `[!]` y `[ ]`; `remaining = total - done`; `percent = done * 100 // total` (0 sin
  tareas).
- `next`: `None` o `{"id", "title"}` de la primera tarea pendiente (`[ ]`) del ROADMAP en orden.
- `agents`: una entrada por rol de `ROLES`, en ese orden (`AGENT_KEYS`): `role`, `providers` (proveedores de
  `cfg.role_chain(role)`, sin modelo), `state` (clave de `STATE_LABELS`), `provider`, `model_id`, `cli`, `run_id`
  y `task_id` de su última delegación real (o `None`). Sin eventos el agente está `idle` con todo `None`.
- `events`: los últimos `MAX_EVENTS` eventos de `events_path`, el más reciente al final, redactados.

Eventos de delegación (TASK-2203, `hefesto/router.py`)
- Cada invocación de un proveedor en `hefesto run` añade a `events_path` un `start` antes de llamar a la CLI y un
  `end` al terminar (también si falla o sin cuota): `{"event": "start" | "end", "run_id", "task_id", "role",
  "provider", "model_id", "cli", "pid", "orchestrator", "at"}`; el `end` añade `success` (bool) y `error_type`.
  `run_id` es el de la salida de `hefesto run`; `model_id` el de `logs/runs.jsonl`; `cli` el nombre del
  ejecutable real sin extensión (`gemini`, `agy`, `codex`, `claude`) o `"http"` para `local`; `pid` el del
  proceso de Hefesto que delega (`os.getpid()`); `orchestrator` el de `progress.current_orchestrator()`. Se
  escriben con `log.record` (textos redactados).
- Atribución: el último evento de cada rol manda. `end` → `done` o `failed` según `success`; `start` sin `end`
  con `pid` vivo (`lock.pid_alive`) → `working`; `start` sin `end` con `pid` muerto no cuenta como actividad.

Dibujo: `render(snapshot, frame=0, width=80, color=False) -> str`
- Función pura y determinista: mismo `snapshot`, `frame` y `width` → mismo texto. Ninguna línea supera `width`
  (los textos largos se recortan). Con `color=False` no hay secuencias ANSI; con `color=True` sí, y al quitarlas
  queda exactamente el texto sin color.
- Muestra `HEFESTO` (en mayúsculas) centrado horizontalmente; los roles como `Core`, `Runner`, `Forge` y
  `Reviewer` con la etiqueta de su estado; de cada agente con delegación, su `provider`, `model_id` y `cli`; el
  progreso como `done/total` y `percent%`; `N restantes`; la tarea en curso (id y título) y la siguiente (id).
- Animación: con un agente `working`, `frame` distintos dan dibujos distintos, siempre con el mismo número de
  líneas. Sin actividad no aparece `trabajando`.
- Los eventos (`events`) se listan con rol, proveedor y `cli`; un `end` fallido muestra `falló`.

Bucle: `run(project, once=False, interval=1.0, stream=None, max_frames=None) -> int`
- `interval <= 0` → HefestoError `EXIT_INPUT`. `stream` por defecto `sys.stdout`.
- Con `once=True` o sin TTY (`util.is_tty()` falso): escribe una sola instantánea estática (sin ANSI) con el
  ancho de `shutil.get_terminal_size((80, 24))`, nunca llama a `time.sleep` y devuelve 0.
- En TTY: oculta el cursor (`\\x1b[?25l`), cada fotograma empieza con `\\x1b[H` y recalcula `snapshot(project)`,
  entre fotogramas `time.sleep(interval)` (atributo del módulo `time`); para tras `max_frames` fotogramas o con
  Ctrl+C (`KeyboardInterrupt`), restaura el cursor (`\\x1b[?25h`) y devuelve 0.

CLI (`hefesto/cli.py`)
- `hefesto dashboard [--once] [--interval S]`: llama a `dashboard.run`; sin TTY o con `--once` imprime una
  instantánea legible y sale con 0; `--interval 0` sale con 1.

Solo `ContractDefinitionTests` pasa antes de implementar; `RenderTests` y `LiveTests` fallan porque aún no existe
`hefesto.dashboard` (y, para `LiveTests`, los eventos de `hefesto run`).
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from hefesto import config, roadmap, state
from hefesto.util import EXIT_INPUT, FRAMEWORK_ROOT, HefestoError, read_jsonl
from tests.helpers import HefestoTestCase, git, tty

FOUR_ROLES = ("core", "runner", "forge", "reviewer")
ROLE_NAMES = ("Core", "Runner", "Forge", "Reviewer")
LABELS = {"idle": "en espera", "working": "trabajando", "done": "terminó", "failed": "falló"}
SNAPSHOT_KEYS = {"project", "status", "task", "progress", "next", "agents", "events"}
PROGRESS_KEYS = {"done", "total", "in_progress", "blocked", "pending", "remaining", "percent"}
AGENT_KEYS = {"role", "providers", "state", "provider", "model_id", "cli", "run_id", "task_id"}
EVENT_KEYS = {"event", "run_id", "task_id", "role", "provider", "model_id", "cli", "pid", "orchestrator", "at"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

ROADMAP = """# ROADMAP

## Fase 1 — Forja
- [x] TASK-001: Fuelle de la fragua
  - verify: `true`
  - files: src/fuelle.py
  - role: runner
  - size: S
- [ ] TASK-002: Yunque de la forja
  - verify: `true`
  - files: src/yunque.py
  - role: runner
  - size: S
- [ ] TASK-003: Martillo
  - verify: `true`
  - files: src/martillo.py
  - role: runner
  - depends_on: TASK-002
  - size: S

## Fase 2 — Temple
- [ ] TASK-101: Temple del acero
  - verify: `true`
  - files: src/temple.py
  - role: runner
  - size: S
"""


def _dashboard() -> Any:
    from hefesto import dashboard  # import diferido: el contrato compila sin la implementación

    return dashboard


def _secret() -> str:
    return "sk-" + "ant-" + "api03-" + "FORJAFORJAFORJAFORJAFORJA0123456789"  # nunca literal: lo bloquea el guard


SECRET_TAIL = "FORJAFORJAFORJAFORJAFORJA0123456789"


def _agent(role: str, providers: list[str], **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"role": role, "providers": providers, "state": "idle", "provider": None,
                            "model_id": None, "cli": None, "run_id": None, "task_id": None}
    base.update(fields)
    return base


def sample_snapshot(working: bool = True, title: str = "Yunque de la forja") -> dict[str, Any]:
    """Instantánea escrita a mano con el esquema del contrato (para probar `render` sin depender de `snapshot`)."""
    runner = (_agent("runner", ["gemini", "codex"], state="working", provider="gemini", model_id="gemini:default",
                     cli="agy", run_id="run-7", task_id="TASK-002")
              if working else _agent("runner", ["gemini", "codex"]))
    events = ([{"event": "start", "run_id": "run-7", "task_id": "TASK-002", "role": "runner", "provider": "gemini",
                "model_id": "gemini:default", "cli": "agy", "pid": 4242, "orchestrator": "claude",
                "at": "2026-11-17T10:00:00+00:00"}] if working else [])
    return {
        "project": "proyecto",
        "status": "IN_PROGRESS" if working else "IDLE",
        "task": ({"id": "TASK-002", "title": title, "role": "runner", "attempts": 0, "phase": "Fase 1 — Forja"}
                 if working else None),
        "progress": {"done": 1, "total": 4, "in_progress": 1 if working else 0, "blocked": 0,
                     "pending": 2 if working else 3, "remaining": 3, "percent": 25},
        "next": {"id": "TASK-003", "title": "Martillo"} if working else {"id": "TASK-002", "title": title},
        "agents": [_agent("core", ["claude"]), runner, _agent("forge", ["codex"]), _agent("reviewer", ["codex"])],
        "events": events,
    }


def _tree(*roots: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for root in roots:
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    st = path.stat()
                    out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


def _is_secret_name(name: str) -> bool:
    return name.startswith(".env") or name.startswith("id_") or name.endswith((".pem", ".key"))


@contextlib.contextmanager
def _spy_open() -> Iterator[list[str]]:
    """Registra cada fichero abierto con `open`/`io.open` (también `Path.read_text`)."""
    import builtins

    real = builtins.open
    seen: list[str] = []

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(file))
        return real(file, *args, **kwargs)

    with mock.patch("builtins.open", spy), mock.patch("io.open", spy):
        yield seen


# --- Definición del contrato (pasa antes de implementar) --------------------------------------------------------


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"RenderTests": 18, "LiveTests": 9}
    API = ("hefesto/dashboard.py", "ROLES", "STATE_LABELS", "en espera", "trabajando", "terminó", "falló",
           "MAX_EVENTS", "events_path", "delegations.jsonl", "snapshot(project)", "SNAPSHOT_KEYS", "AGENT_KEYS",
           "progress", "remaining", "percent", "next", "agents", "events", "guard.redact", "state.load",
           "role_chain", "start", "end", "run_id", "model_id", "cli", "pid", "orchestrator", "lock.pid_alive",
           "hefesto/router.py", "log.record", "render(snapshot, frame=0, width=80, color=False)", "HEFESTO",
           "Core", "Runner", "Forge", "Reviewer", "restantes", "run(project, once=False, interval=1.0",
           "max_frames", "util.is_tty()", "shutil.get_terminal_size", "time.sleep", "KeyboardInterrupt",
           "\\x1b[H", "\\x1b[?25l", "\\x1b[?25h", "EXIT_INPUT", "hefesto dashboard", "--once", "--interval",
           ".env*", "*.pem", "*.key", "id_*")

    def source(self) -> str:
        return Path(__file__).read_text(encoding="utf-8")

    def tree(self) -> ast.Module:
        return ast.parse(self.source())

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
        for node in self.tree().body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "hefesto.dashboard")
                if node.module == "hefesto":
                    self.assertNotIn("dashboard", [a.name for a in node.names])
            if isinstance(node, ast.Import):
                self.assertNotIn("hefesto.dashboard", [a.name for a in node.names])

    def test_contract_is_offline_and_never_uses_a_shell(self) -> None:
        source = self.source()
        self.assertNotIn("shell=" + "True", source)
        for module in ("requests", "urllib.request", "socket", "curses", "rich"):
            self.assertNotIn("import " + module, source)
        self.assertNotRegex(source, r"https?:" + r"//")

    def test_sample_snapshot_matches_the_documented_schema(self) -> None:
        for working in (True, False):
            with self.subTest(working=working):
                snap = sample_snapshot(working)
                json.dumps(snap)  # serializable
                self.assertEqual(set(snap), SNAPSHOT_KEYS)
                self.assertEqual(set(snap["progress"]), PROGRESS_KEYS)
                self.assertEqual([a["role"] for a in snap["agents"]], list(FOUR_ROLES))
                for agent in snap["agents"]:
                    self.assertEqual(set(agent), AGENT_KEYS)
                    self.assertIn(agent["state"], LABELS)
                for event in snap["events"]:
                    self.assertEqual(set(event), EVENT_KEYS)
                progress = snap["progress"]
                self.assertEqual(progress["remaining"], progress["total"] - progress["done"])
                self.assertEqual(progress["percent"], progress["done"] * 100 // progress["total"])

    def test_roadmap_fixture_is_valid_and_offline(self) -> None:
        path = self.tmp / "ROADMAP.md"
        path.write_text(ROADMAP, encoding="utf-8")
        rm = roadmap.parse(path)
        self.assertEqual([t.id for t in rm.tasks], ["TASK-001", "TASK-002", "TASK-003", "TASK-101"])
        self.assertEqual([t.marker for t in rm.tasks], ["x", " ", " ", " "])
        self.assertEqual(rm.parse_errors, [])

    def test_secret_helper_is_never_a_literal(self) -> None:
        self.assertNotIn(_secret(), self.source())
        self.assertTrue(_secret().endswith(SECRET_TAIL))

    def test_behaviour_tests_fail_for_the_right_reason(self) -> None:
        # Sin implementación, el fallo es un ImportError que nombra `dashboard` (y no un error del contrato).
        try:
            _dashboard()
        except ImportError as exc:
            self.assertIn("dashboard", str(exc))


# --- Base ------------------------------------------------------------------------------------------------------


class DashboardCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        self.write_roadmap(ROADMAP)
        self.settings = config.project_settings_path(self.project)
        config.set_setting(self.settings, ("roles", "runner", "use"), ["gemini", "codex"])
        config.set_setting(self.settings, ("roles", "reviewer", "use"), ["codex"])
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def write_roadmap(self, text: str) -> None:
        (self.project / ".hefesto" / "ROADMAP.md").write_text(text, encoding="utf-8")

    def events_file(self) -> Path:
        return self.project / ".hefesto" / "logs" / "delegations.jsonl"

    def write_event(self, **fields: Any) -> None:
        row: dict[str, Any] = {"event": "start", "run_id": "run-1", "task_id": "TASK-002", "role": "runner",
                               "provider": "gemini", "model_id": "gemini:default", "cli": "gemini",
                               "pid": os.getpid(), "orchestrator": "claude", "at": "2026-11-17T10:00:00+00:00"}
        row.update(fields)
        path = self.events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def agent(self, snap: dict[str, Any], role: str) -> dict[str, Any]:
        return next(a for a in snap["agents"] if a["role"] == role)

    def watched(self) -> dict[str, tuple[int, int]]:
        return _tree(self.project / ".hefesto", self.tmp / "state", self.tmp / "home")


# --- Instantánea, dibujo y bucle (TASK-2202) --------------------------------------------------------------------


class RenderTests(DashboardCase):
    def test_constants(self) -> None:
        dashboard = _dashboard()
        self.assertEqual(tuple(dashboard.ROLES[:4]), FOUR_ROLES)
        self.assertEqual(dashboard.STATE_LABELS, LABELS)
        self.assertEqual(dashboard.MAX_EVENTS, 8)
        self.assertEqual(Path(dashboard.events_path(self.project)), self.events_file())

    def test_snapshot_reads_roadmap_and_idle_state(self) -> None:
        snap = _dashboard().snapshot(self.project)
        self.assertTrue(SNAPSHOT_KEYS <= set(snap))
        self.assertEqual(snap["project"], self.project.name)
        self.assertEqual(snap["status"], "IDLE")
        self.assertIsNone(snap["task"])
        self.assertEqual(snap["progress"], {"done": 1, "total": 4, "in_progress": 0, "blocked": 0, "pending": 3,
                                            "remaining": 3, "percent": 25})
        self.assertEqual(snap["next"], {"id": "TASK-002", "title": "Yunque de la forja"})
        self.assertEqual(snap["events"], [])

    def test_snapshot_follows_the_task_in_progress(self) -> None:
        code, out, err = self.cli("start", "TASK-002")
        self.assertEqual(code, 0, out + err)
        snap = _dashboard().snapshot(self.project)
        self.assertEqual(snap["status"], "IN_PROGRESS")
        self.assertEqual(snap["task"], {"id": "TASK-002", "title": "Yunque de la forja", "role": "runner",
                                        "attempts": 0, "phase": "Fase 1 — Forja"})
        self.assertEqual(snap["progress"]["in_progress"], 1)
        self.assertEqual(snap["progress"]["pending"], 2)
        self.assertEqual(snap["progress"]["remaining"], 3)
        self.assertEqual(snap["next"], {"id": "TASK-003", "title": "Martillo"})

    def test_snapshot_lists_every_role_with_its_providers_and_no_invented_activity(self) -> None:
        snap = _dashboard().snapshot(self.project)
        roles = [a["role"] for a in snap["agents"]]
        self.assertEqual(tuple(roles[:4]), FOUR_ROLES)
        self.assertEqual(self.agent(snap, "runner")["providers"], ["gemini", "codex"])
        self.assertEqual(self.agent(snap, "reviewer")["providers"], ["codex"])
        for agent in snap["agents"]:
            with self.subTest(role=agent["role"]):
                self.assertTrue(AGENT_KEYS <= set(agent))
                self.assertEqual(agent["state"], "idle")
                for key in ("provider", "model_id", "cli", "run_id", "task_id"):
                    self.assertIsNone(agent[key])

    def test_snapshot_is_json_and_read_only(self) -> None:
        before = self.watched()
        state_before = state.load(self.project)
        with mock.patch("subprocess.run", side_effect=AssertionError("el tablero no lanza procesos")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("el tablero no lanza procesos")):
            snap = _dashboard().snapshot(self.project)
        json.dumps(snap)
        self.assertEqual(state.load(self.project), state_before)
        self.assertEqual(self.watched(), before)

    def test_empty_roadmap_has_zero_progress(self) -> None:
        self.write_roadmap("# ROADMAP\n")
        snap = _dashboard().snapshot(self.project)
        self.assertEqual(snap["progress"]["total"], 0)
        self.assertEqual(snap["progress"]["percent"], 0)
        self.assertIsNone(snap["next"])
        self.assertIn("0/0", _dashboard().render(snap))

    def test_never_reads_secrets_and_redacts_texts(self) -> None:
        (self.project / ".env").write_text(f"ANTHROPIC_API_KEY={_secret()}\n", encoding="utf-8")
        (self.project / "id_rsa").write_text(_secret(), encoding="utf-8")
        (self.project / "deploy.pem").write_text(_secret(), encoding="utf-8")
        self.write_roadmap(ROADMAP.replace("Yunque de la forja", f"Yunque con {_secret()}"))
        dashboard = _dashboard()
        with _spy_open() as seen:
            snap = dashboard.snapshot(self.project)
            text = dashboard.render(snap, width=200)
        opened = [Path(p).name for p in seen]
        self.assertFalse([n for n in opened if _is_secret_name(n)], opened)
        self.assertNotIn(SECRET_TAIL, json.dumps(snap, ensure_ascii=False))
        self.assertNotIn(SECRET_TAIL, text)
        self.assertIn("TASK-002", text)

    def test_render_shows_hefesto_at_the_centre(self) -> None:
        dashboard = _dashboard()
        for width in (60, 80, 100):
            for snap in (sample_snapshot(True), sample_snapshot(False)):
                with self.subTest(width=width, status=snap["status"]):
                    lines = dashboard.render(snap, width=width).splitlines()
                    line = next(l for l in lines if "HEFESTO" in l)
                    centre = line.index("HEFESTO") + len("HEFESTO") / 2
                    self.assertLessEqual(abs(centre - width / 2), 4, line)

    def test_render_shows_roles_progress_task_and_next(self) -> None:
        text = _dashboard().render(sample_snapshot(True))
        for name in ROLE_NAMES:
            self.assertIn(name, text)
        self.assertIn("1/4", text)
        self.assertIn("25%", text)
        self.assertRegex(text, r"\b3 restantes\b")
        self.assertIn("TASK-002", text)
        self.assertIn("Yunque de la forja", text)
        self.assertIn("TASK-003", text)

    def test_render_attributes_provider_model_and_cli(self) -> None:
        text = _dashboard().render(sample_snapshot(True))
        self.assertIn(LABELS["working"], text)
        self.assertIn("gemini:default", text)
        self.assertIn("agy", text)  # la herramienta real, aunque el proveedor sea gemini

    def test_idle_agents_wait_and_nothing_is_invented(self) -> None:
        text = _dashboard().render(sample_snapshot(False))
        self.assertGreaterEqual(text.count(LABELS["idle"]), len(FOUR_ROLES))
        self.assertNotIn(LABELS["working"], text)
        self.assertNotIn("agy", text)

    def test_render_fits_the_width_and_truncates_long_texts(self) -> None:
        dashboard = _dashboard()
        snap = sample_snapshot(True, title="Yunque " + "muy largo " * 40)
        for width in (60, 80, 120):
            for frame in range(4):
                with self.subTest(width=width, frame=frame):
                    for line in dashboard.render(snap, frame=frame, width=width).splitlines():
                        self.assertLessEqual(len(line), width, line)

    def test_render_is_deterministic(self) -> None:
        dashboard = _dashboard()
        snap = sample_snapshot(True)
        self.assertEqual(dashboard.render(snap, frame=3), dashboard.render(snap, frame=3))

    def test_color_only_adds_ansi(self) -> None:
        dashboard = _dashboard()
        snap = sample_snapshot(True)
        plain = dashboard.render(snap, frame=1, width=80, color=False)
        colored = dashboard.render(snap, frame=1, width=80, color=True)
        self.assertNotIn("\x1b", plain)
        self.assertIn("\x1b[", colored)
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_frames_animate_working_agents_with_a_stable_layout(self) -> None:
        dashboard = _dashboard()
        snap = sample_snapshot(True)
        frames = [dashboard.render(snap, frame=i, width=80) for i in range(8)]
        self.assertGreater(len(set(frames)), 1)
        self.assertEqual(len({len(f.splitlines()) for f in frames}), 1)

    def test_run_once_prints_one_static_snapshot(self) -> None:
        dashboard = _dashboard()
        before = self.watched()
        stream = io.StringIO()
        with tty(True), mock.patch("time.sleep", side_effect=AssertionError("--once no espera")), \
                mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((70, 24))):
            code = dashboard.run(self.project, once=True, stream=stream)
        self.assertEqual(code, 0)
        text = stream.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertEqual(text.count("HEFESTO"), 1)
        self.assertIn("TASK-002", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 70, line)
        self.assertEqual(self.watched(), before)

    def test_run_without_tty_is_static_even_without_once(self) -> None:
        stream = io.StringIO()
        with tty(False), mock.patch("time.sleep", side_effect=AssertionError("sin TTY no espera")):
            code = _dashboard().run(self.project, stream=stream)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", stream.getvalue())
        self.assertEqual(stream.getvalue().count("HEFESTO"), 1)

    def test_run_in_tty_animates_and_refreshes_each_frame(self) -> None:
        dashboard = _dashboard()
        stream = io.StringIO()
        calls: list[float] = []

        def sleep(seconds: float) -> None:
            calls.append(seconds)
            if len(calls) == 1:  # entre fotogramas cambia el estado real: el siguiente debe verlo
                code, out, err = self.cli("start", "TASK-002")
                self.assertEqual(code, 0, out + err)

        with tty(True), mock.patch("time.sleep", side_effect=sleep):
            code = dashboard.run(self.project, interval=0.5, stream=stream, max_frames=3)
        self.assertEqual(code, 0)
        text = stream.getvalue()
        self.assertTrue(text.startswith("\x1b[?25l"), text[:20])
        self.assertTrue(text.rstrip().endswith("\x1b[?25h"), text[-20:])
        frames = [ANSI.sub("", f) for f in text.split("\x1b[H")[1:]]
        self.assertEqual(len(frames), 3)
        self.assertTrue(calls and all(s == 0.5 for s in calls), calls)
        self.assertIn("IDLE", frames[0])
        self.assertIn("IN_PROGRESS", frames[-1])
        self.assertIn("TASK-003", frames[-1])  # la siguiente ya es la 003

    def test_ctrl_c_restores_the_cursor_without_traceback(self) -> None:
        stream = io.StringIO()
        with tty(True), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            code = _dashboard().run(self.project, interval=1.0, stream=stream)
        self.assertEqual(code, 0)
        self.assertTrue(stream.getvalue().rstrip().endswith("\x1b[?25h"))

    def test_invalid_interval(self) -> None:
        dashboard = _dashboard()
        for interval in (0, -1):
            with self.subTest(interval=interval), self.assertRaises(HefestoError) as ctx:
                dashboard.run(self.project, interval=interval, stream=io.StringIO())
            self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_cli_dashboard_once_and_without_tty(self) -> None:
        _dashboard()
        for args in (("dashboard", "--once"), ("dashboard",), ("dashboard", "--interval", "2")):
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertEqual(code, 0, out + err)
                self.assertIn("HEFESTO", out)
                for name in ROLE_NAMES:
                    self.assertIn(name, out)
                self.assertIn("1/4", out)
                self.assertNotIn("\x1b", out)
                self.assertNotIn("Traceback", out + err)
        code, out, err = self.cli("dashboard", "--interval", "0")
        self.assertEqual(code, EXIT_INPUT, out + err)
        self.assertNotIn("Traceback", out + err)

    def test_module_uses_only_the_standard_library(self) -> None:
        _dashboard()
        source = (FRAMEWORK_ROOT / "hefesto" / "dashboard.py").read_text(encoding="utf-8")
        allowed = set(sys.stdlib_module_names) | {"hefesto", "__future__"}
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                with self.subTest(module=name):
                    self.assertIn(name.split(".")[0], allowed)
        self.assertNotIn("shell=" + "True", source)


# --- Actividad real de delegaciones (TASK-2203) -----------------------------------------------------------------


class LiveTests(DashboardCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini")
        self.scenario = self.tmp / "scenario.json"
        self.set_scenario({"*": {"mode": "ok", "summary": "hecho", "write": {"src/yunque.py": "x = 1\n"}}})
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.tmp / "calls.jsonl")
        os.environ["HEFESTO_ORCHESTRATOR"] = "codex"
        code, out, err = self.cli("start", "TASK-002")
        self.assertEqual(code, 0, out + err)

    def set_scenario(self, data: dict[str, Any]) -> None:
        self.scenario.write_text(json.dumps(data), encoding="utf-8")

    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.events_file())

    def delegate(self, expect: int = 0) -> dict[str, Any]:
        code, out, err = self.cli("run", "runner", "Forja el yunque")
        self.assertEqual(code, expect, out + err)
        return json.loads(out) if out.strip().startswith("{") else {}

    def assert_paired(self, rows: list[dict[str, Any]]) -> None:
        starts = [(r["run_id"], r["provider"]) for r in rows if r["event"] == "start"]
        ends = [(r["run_id"], r["provider"]) for r in rows if r["event"] == "end"]
        self.assertEqual(sorted(starts), sorted(ends), rows)
        for key in starts:  # cada `start` va antes que su `end`
            order = [r["event"] for r in rows if (r["run_id"], r["provider"]) == key]
            self.assertEqual(order[0], "start")

    def test_run_emits_start_and_end_with_attribution(self) -> None:
        result = self.delegate()
        rows = self.events()
        self.assertEqual([r["event"] for r in rows], ["start", "end"])
        runs = read_jsonl(self.project / ".hefesto" / "logs" / "runs.jsonl")
        for row in rows:
            with self.subTest(event=row["event"]):
                self.assertTrue(EVENT_KEYS <= set(row), row)
                self.assertEqual(row["run_id"], result["run_id"])
                self.assertEqual(row["task_id"], "TASK-002")
                self.assertEqual(row["role"], "runner")
                self.assertEqual(row["provider"], "gemini")
                self.assertEqual(row["model_id"], runs[-1]["model_id"])
                self.assertEqual(row["cli"], "gemini")
                self.assertEqual(row["pid"], os.getpid())
                self.assertEqual(row["orchestrator"], "codex")
        self.assertIs(rows[-1]["success"], True)
        self.assertIsNone(rows[-1]["error_type"])

    def test_the_real_tool_is_attributed(self) -> None:
        self.use_fakes("claude", "codex", "agy")  # sin `gemini` en el PATH: el proveedor gemini usa `agy`
        result = self.delegate()
        rows = self.events()
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual({r["cli"] for r in rows}, {"agy"})
        snap = _dashboard().snapshot(self.project)
        runner = self.agent(snap, "runner")
        self.assertEqual((runner["provider"], runner["cli"]), ("gemini", "agy"))

    def test_failed_delegation_closes_with_failure(self) -> None:
        self.set_scenario({"*": {"mode": "fail", "summary": "no compila"}})
        self.delegate(expect=EXIT_INPUT)
        rows = self.events()
        self.assert_paired(rows)
        self.assertIs(rows[-1]["success"], False)
        self.assertTrue(rows[-1]["error_type"])
        snap = _dashboard().snapshot(self.project)
        self.assertEqual(self.agent(snap, "runner")["state"], "failed")
        self.assertIn(LABELS["failed"], _dashboard().render(snap, width=100))

    def test_without_quota_every_start_is_closed(self) -> None:
        self.set_scenario({"*": {"mode": "quota"}})
        self.delegate(expect=3)
        rows = self.events()
        self.assert_paired(rows)
        self.assertTrue(all(r["success"] is False for r in rows if r["event"] == "end"), rows)
        snap = _dashboard().snapshot(self.project)
        self.assertNotIn(LABELS["working"], _dashboard().render(snap, width=100))

    def test_events_and_dashboard_are_redacted(self) -> None:
        self.set_scenario({"*": {"mode": "fail", "summary": f"falló con {_secret()}"}})
        self.delegate(expect=EXIT_INPUT)
        self.write_event(event="end", run_id="run-x", success=False, error_type=f"error {_secret()}")
        self.assertNotIn(SECRET_TAIL, self.events_file().read_text(encoding="utf-8").split("run-x")[0])
        snap = _dashboard().snapshot(self.project)
        self.assertNotIn(SECRET_TAIL, json.dumps(snap, ensure_ascii=False))
        self.assertNotIn(SECRET_TAIL, _dashboard().render(snap, width=200))

    def test_snapshot_attributes_the_last_delegation_per_role(self) -> None:
        result = self.delegate()
        snap = _dashboard().snapshot(self.project)
        runner = self.agent(snap, "runner")
        self.assertEqual(runner["state"], "done")
        self.assertEqual(runner["provider"], "gemini")
        self.assertEqual(runner["cli"], "gemini")
        self.assertEqual(runner["run_id"], result["run_id"])
        self.assertEqual(runner["task_id"], "TASK-002")
        for role in ("core", "forge", "reviewer"):
            with self.subTest(role=role):
                self.assertEqual(self.agent(snap, role)["state"], "idle")
                self.assertIsNone(self.agent(snap, role)["provider"])
        self.assertEqual(snap["events"][-1]["event"], "end")
        self.assertIn(LABELS["done"], _dashboard().render(snap, width=100))

    def test_open_delegation_with_a_live_process_is_working(self) -> None:
        self.write_event(event="start", run_id="run-live", role="reviewer", provider="codex",
                         model_id="codex:default", cli="codex", pid=os.getpid())
        dashboard = _dashboard()
        snap = dashboard.snapshot(self.project)
        reviewer = self.agent(snap, "reviewer")
        self.assertEqual(reviewer["state"], "working")
        self.assertEqual((reviewer["provider"], reviewer["model_id"], reviewer["cli"], reviewer["run_id"]),
                         ("codex", "codex:default", "codex", "run-live"))
        text = dashboard.render(snap, width=100)
        self.assertIn(LABELS["working"], text)
        self.assertIn("codex:default", text)
        frames = {dashboard.render(snap, frame=i, width=100) for i in range(8)}
        self.assertGreater(len(frames), 1)

    def test_open_delegation_with_a_dead_process_is_not_activity(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.write_event(event="start", run_id="run-dead", role="forge", provider="codex",
                         model_id="codex:default", cli="codex", pid=proc.pid)
        snap = _dashboard().snapshot(self.project)
        forge = self.agent(snap, "forge")
        self.assertNotEqual(forge["state"], "working")
        self.assertNotIn(LABELS["working"], _dashboard().render(snap, width=100))

    def test_only_the_last_events_are_kept_in_order(self) -> None:
        for i in range(12):
            self.write_event(event="end", run_id=f"run-{i:02d}", success=True, error_type=None,
                             at=f"2026-11-17T10:{i:02d}:00+00:00")
        snap = _dashboard().snapshot(self.project)
        self.assertEqual([e["run_id"] for e in snap["events"]], [f"run-{i:02d}" for i in range(4, 12)])
        self.assertEqual(self.agent(snap, "runner")["run_id"], "run-11")

    def test_render_lists_events_with_role_provider_and_cli(self) -> None:
        self.write_event(event="start", run_id="run-a", role="core", provider="claude", model_id="claude:opus",
                         cli="claude", pid=os.getpid())
        self.write_event(event="end", run_id="run-a", role="core", provider="claude", model_id="claude:opus",
                         cli="claude", success=False, error_type="quota")
        text = _dashboard().render(_dashboard().snapshot(self.project), width=120)
        self.assertIn("claude:opus", text)
        self.assertIn("claude", text)
        self.assertIn(LABELS["failed"], text)
        self.assertNotIn(LABELS["working"], text)
