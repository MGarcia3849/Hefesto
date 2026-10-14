"""Contrato (TASK-815 → TASK-816/817): límites de uso reales y actividad de los agentes en directo.

Petición del usuario: «poder ver qué está haciendo cada agente» y «ver cómo van los límites de la ventana de
5 horas y semanal para controlar el gasto». Regla de la especificación: nunca se inventa cuota; sin dato,
`unknown`. Todo se lee en local, sin gastar llamadas.

`hefesto/limits.py`:
- `codex_limits() -> dict`: lee los logs de sesión de Codex en `$CODEX_HOME/sessions` (por defecto
  `~/.codex/sessions`), ficheros `**/rollout-*.jsonl`, del más reciente al más antiguo (por fecha de
  modificación), y toma el último evento `{"type": "event_msg", "payload": {"type": "token_count",
  "rate_limits": {...}}}` cuyo `rate_limits.primary` no sea null. Devuelve:
      {"provider": "codex",
       "five_hour": {"used_percent": float, "resets_at": ISO-8601 UTC} | "unknown",   # primary (300 min)
       "weekly":    {"used_percent": float, "resets_at": ISO-8601 UTC} | "unknown",   # secondary (10080 min)
       "observed_at": ISO del campo timestamp del evento | None, "plan": plan_type | None}
  Si `resets_at` ya pasó, esa ventana se devuelve con `used_percent` 0.0 (ya se reinició).
- `claude_limits() -> dict`: mismo formato, leído de la caché `util.state_home()/limits/claude.json` que escribe
  `hefesto statusline`. Sin caché: "unknown" en ambas ventanas.
- `gemini_limits() -> dict`: ninguna CLI de Google expone su cuota: "unknown" en ambas ventanas.
- `all_limits() -> list[dict]`: claude, codex y gemini, en ese orden.

CLI:
- `hefesto limits [--json]`: tabla con proveedor, uso de 5 h, reinicio, uso semanal, reinicio y antigüedad de la
  lectura (texto con «5 h» y «semanal»). Con `--json`, la lista de `all_limits()`.
- `hefesto statusline`: comando de línea de estado de Claude Code. Lee por stdin el JSON que pasa Claude Code
  (con `rate_limits.five_hour.used_percentage`/`resets_at` y `rate_limits.seven_day.*`, `resets_at` en segundos
  epoch, y `cwd`), guarda los límites en la caché (sin borrar la anterior si el JSON no trae `rate_limits`) e
  imprime una sola línea con «5h N%» y «7d N%» y, si `cwd` está en un proyecto Hefesto con tarea en curso, su ID.
  Nunca falla (sale con 0 aunque el JSON sea inválido).
- `sync` en modo local añade a `.claude/settings.local.json` `"statusLine": {"type": "command", "command":
  "hefesto statusline"}`.

Preflight (nivel 2 de la sección 2.2): un proveedor cuya ventana de 5 h o semanal (no reiniciada) tenga
`used_percent >= quota.near_limit_threshold * 100` no está disponible («cerca del límite»): lo saltan
`run --dry-run`, `route` y `run`, sin llamarlo ni gastar intento.

`hefesto/activity.py` (agentes activos):
- Cada delegación de `hefesto run` y cada iteración de `hefesto loop` se registra al empezar en
  `.hefesto/logs/active/<run_id>.json` con `{"run_id", "kind" ("worker"|"orchestrator"), "pid", "role",
  "provider", "model_id", "task_id", "started_at", "log_path"}` y se elimina al terminar (también si falla).
- La salida del proceso hijo (stdout y stderr) se escribe en `log_path` MIENTRAS el proceso corre, no al final.
- `list_active(project) -> list[dict]`: entradas activas; las de un `pid` que ya no existe se eliminan.
- Formatos de streaming: Claude con `--output-format stream-json --verbose` (el resultado es el último evento
  `{"type": "result", ...}`); agy con `--output-format stream-json` (el resultado es el evento
  `{"event": "result", "result": {...}}`); Codex sigue con `exec --json`.
- CLI `hefesto jobs [--json]`: agentes activos con tipo, rol, proveedor, tarea, segundos transcurridos y la última
  línea de su log. `hefesto watch [--once] [--interval S]`: panel con un bloque «Límites» (como `hefesto limits`)
  y, por cada agente activo, su cabecera y las últimas líneas de su log; con `--once` imprime una vez y sale; sin
  él refresca cada `S` segundos hasta Ctrl+C.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

from hefesto import activity, config, limits, util
from tests.helpers import HefestoTestCase, git


def codex_event(primary: float | None, secondary: float, resets_in: int = 3600, ts: str = "2026-10-14T16:34:27.209Z") -> str:
    now = int(time.time())
    rate = {"limit_id": "codex", "plan_type": "plus",
            "primary": None if primary is None else {"used_percent": primary, "window_minutes": 300,
                                                     "resets_at": now + resets_in},
            "secondary": {"used_percent": secondary, "window_minutes": 10080, "resets_at": now + 86400}}
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "token_count", "info": None, "rate_limits": rate}})


class LimitsCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.codex_home = self.tmp / "codex-home"
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)

    def write_session(self, name: str, *lines: str, mtime: float | None = None) -> Path:
        path = self.codex_home / "sessions" / "2026" / "10" / "14" / f"rollout-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def statusline(self, payload: object) -> tuple[int, str]:
        code, out, _ = self.run_cli("statusline", stdin=json.dumps(payload) if not isinstance(payload, str) else payload)
        return code, out


class LimitsTests(LimitsCase):
    def test_codex_reads_latest_non_null_rate_limits(self) -> None:
        old = time.time() - 600
        self.write_session("viejo", codex_event(10.0, 5.0), mtime=old)
        self.write_session("nuevo", codex_event(99.0, 47.0), codex_event(None, 48.0, ts="2026-10-14T17:00:00Z"))
        data = limits.codex_limits()
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(data["five_hour"]["used_percent"], 99.0)
        self.assertEqual(data["weekly"]["used_percent"], 47.0)
        self.assertEqual(data["plan"], "plus")
        self.assertEqual(data["observed_at"], "2026-10-14T16:34:27.209Z")
        dt.datetime.fromisoformat(data["five_hour"]["resets_at"])

    def test_expired_window_counts_as_reset(self) -> None:
        self.write_session("x", codex_event(99.0, 47.0, resets_in=-60))
        self.assertEqual(limits.codex_limits()["five_hour"]["used_percent"], 0.0)

    def test_unknown_without_data(self) -> None:
        data = limits.codex_limits()
        self.assertEqual((data["five_hour"], data["weekly"]), ("unknown", "unknown"))
        self.assertEqual(limits.claude_limits()["five_hour"], "unknown")
        gem = limits.gemini_limits()
        self.assertEqual((gem["five_hour"], gem["weekly"]), ("unknown", "unknown"))

    def test_cli_limits(self) -> None:
        self.write_session("x", codex_event(80.0, 30.0))
        code, out, _ = self.run_cli("limits")
        self.assertEqual(code, 0)
        self.assertIn("5 h", out)
        self.assertIn("semanal", out)
        self.assertIn("80", out)
        code, out, _ = self.run_cli("limits", "--json")
        data = json.loads(out)
        self.assertEqual([d["provider"] for d in data], ["claude", "codex", "gemini"])


class StatuslineTests(LimitsCase):
    def test_statusline_caches_claude_limits_and_prints_line(self) -> None:
        resets = int(time.time()) + 3600
        payload = {"cwd": str(self.tmp), "rate_limits": {"five_hour": {"used_percentage": 37, "resets_at": resets},
                                                         "seven_day": {"used_percentage": 12, "resets_at": resets + 86400}}}
        code, out = self.statusline(payload)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("5h 37%", out)
        self.assertIn("7d 12%", out)
        data = limits.claude_limits()
        self.assertEqual(data["five_hour"]["used_percent"], 37.0)
        self.assertEqual(data["weekly"]["used_percent"], 12.0)
        self.assertEqual(self.statusline({"cwd": str(self.tmp)})[0], 0)
        self.assertEqual(limits.claude_limits()["five_hour"]["used_percent"], 37.0)
        self.assertEqual(self.statusline("esto no es json")[0], 0)

    def test_statusline_shows_current_task(self) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(
            "# R\n\n## Fase 1 — A\n- [ ] TASK-001: Algo\n  - verify: `true`\n  - files: src/\n  - role: runner\n  - size: S\n",
            encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "r")
        self.cli("start", "TASK-001")
        code, out = self.statusline({"cwd": str(self.project)})
        self.assertEqual(code, 0)
        self.assertIn("TASK-001", out)

    def test_local_install_configures_statusline(self) -> None:
        local = self.tmp / "local"
        local.mkdir()
        git(local, "init", "-q")
        self.assertEqual(self.run_cli("init", str(local), "--providers", "gemini")[0], 0)
        settings = json.loads((local / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["statusLine"], {"type": "command", "command": "hefesto statusline"})


class PreflightLimitTests(LimitsCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls)
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({"*": {"mode": "ok", "summary": "hecho"}}), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(scenario)
        self.use_fakes("claude", "codex", "gemini")
        self.init_project()

    def test_provider_near_limit_is_skipped_without_calls(self) -> None:
        self.write_session("x", codex_event(99.0, 47.0))
        code, out, _ = self.cli("run", "forge", "x", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("cerca del límite", out)
        code, out, _ = self.cli("run", "forge", "implementa")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(json.loads(out)["provider"], "codex")
        self.assertEqual([c for c in util.read_jsonl(self.calls) if c["name"] == "codex"], [])

    def test_weekly_window_also_counts_and_below_threshold_is_used(self) -> None:
        self.write_session("x", codex_event(10.0, 95.0))
        self.assertIn("cerca del límite", self.cli("run", "forge", "x", "--dry-run")[1])
        self.write_session("y", codex_event(50.0, 40.0), mtime=time.time() + 5)
        self.assertIn("elegido", self.cli("run", "forge", "x", "--dry-run")[1].splitlines()[0])

    def test_claude_cache_counts_too(self) -> None:
        resets = int(time.time()) + 3600
        self.statusline({"rate_limits": {"five_hour": {"used_percentage": 96, "resets_at": resets},
                                         "seven_day": {"used_percentage": 10, "resets_at": resets}}})
        code, out, _ = self.cli("run", "reviewer", "x", "--dry-run")
        self.assertIn("claude:sonnet", out)
        self.assertIn("cerca del límite", out)


class ActivityTests(LimitsCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls)
        self.scenario = self.tmp / "scenario.json"
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario)
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.init_project()
        self.active_dir = self.project / ".hefesto" / "logs" / "active"

    def set_scenario(self, **entries: object) -> None:
        self.scenario.write_text(json.dumps(entries), encoding="utf-8")

    def wait_for(self, predicate, timeout: float = 20.0) -> object:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail("tiempo de espera agotado")

    def run_in_thread(self, *args: str) -> tuple[threading.Thread, dict[str, object]]:
        result: dict[str, object] = {}

        def target() -> None:
            result["value"] = self.cli(*args)

        thread = threading.Thread(target=target)
        thread.start()
        return thread, result

    def test_live_log_and_active_registry(self) -> None:
        self.set_scenario(gemini={"mode": "ok", "summary": "hecho", "steps": ["PASO 1", "PASO 2"], "step_delay": 2.0})
        thread, result = self.run_in_thread("run", "runner", "implementa")
        entry_file = self.wait_for(lambda: next(iter(self.active_dir.glob("*.json")), None))
        entry = json.loads(Path(entry_file).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        self.assertEqual((entry["kind"], entry["role"], entry["provider"]), ("worker", "runner", "gemini"))
        self.assertIsInstance(entry["pid"], int)
        log_path = self.project / entry["log_path"]
        self.wait_for(lambda: log_path.exists() and "PASO 1" in log_path.read_text(encoding="utf-8", errors="replace"))
        self.assertTrue(Path(entry_file).exists(), "la entrada activa desapareció antes de terminar")  # type: ignore[arg-type]
        code, out, _ = self.cli("jobs", "--json")
        jobs = json.loads(out)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["provider"], "gemini")
        thread.join(timeout=60)
        self.assertEqual(result["value"][0], 0)  # type: ignore[index]
        self.assertEqual(list(self.active_dir.glob("*.json")), [])
        self.assertIn("PASO 2", log_path.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(json.loads(self.cli("jobs", "--json")[1]), [])

    def test_orphan_entries_are_dropped(self) -> None:
        self.active_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        (self.active_dir / "huerfano.json").write_text(json.dumps({
            "run_id": "huerfano", "kind": "worker", "pid": proc.pid, "role": "runner", "provider": "gemini",
            "model_id": "gemini:default", "task_id": None, "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/huerfano.log"}), encoding="utf-8")
        self.assertEqual(activity.list_active(self.project), [])
        self.assertFalse((self.active_dir / "huerfano.json").exists())

    def test_watch_once_shows_limits_and_active_agents(self) -> None:
        self.write_session("x", codex_event(42.0, 7.0))
        self.active_dir.mkdir(parents=True, exist_ok=True)
        log = self.project / ".hefesto" / "logs" / "runs" / "vivo.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("linea uno\nleyendo src/suma.py\n", encoding="utf-8")
        (self.active_dir / "vivo.json").write_text(json.dumps({
            "run_id": "vivo", "kind": "worker", "pid": os.getpid(), "role": "runner", "provider": "gemini",
            "model_id": "gemini:default", "task_id": "TASK-001", "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/vivo.log"}), encoding="utf-8")
        code, out, _ = self.cli("watch", "--once")
        self.assertEqual(code, 0)
        self.assertIn("Límites", out)
        self.assertIn("42", out)
        self.assertIn("gemini", out)
        self.assertIn("TASK-001", out)
        self.assertIn("leyendo src/suma.py", out)
        code, out, _ = self.cli("jobs")
        self.assertIn("leyendo src/suma.py", out)

    def test_streaming_formats(self) -> None:
        self.set_scenario(**{"*": {"mode": "ok", "summary": "respuesta final"}})
        config.set_setting(config.project_settings_path(self.project), ("roles", "reviewer", "use"), ["claude:sonnet"])
        code, out, _ = self.cli("run", "reviewer", "revisa")
        self.assertEqual(json.loads(out)["summary"], "respuesta final")
        claude_argv = [c for c in util.read_jsonl(self.calls) if c["name"] == "claude"][-1]["argv"]
        self.assertIn("stream-json", claude_argv)
        config.set_setting(config.project_settings_path(self.project), ("adapters", "gemini", "binary"), "agy")
        code, out, _ = self.cli("run", "runner", "implementa")
        self.assertEqual(json.loads(out)["summary"], "respuesta final")
        agy_argv = [c for c in util.read_jsonl(self.calls) if c["name"] == "agy"][-1]["argv"]
        self.assertIn("stream-json", agy_argv)

    def test_loop_iteration_is_registered_as_orchestrator(self) -> None:
        (self.project / ".hefesto" / "ROADMAP.md").write_text(
            "# R\n\n## Fase 1 — A\n- [ ] TASK-001: Algo\n  - verify: `test -f src/a.py`\n  - files: src/\n"
            "  - role: runner\n  - size: S\n", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "r")
        self.set_scenario(**{"claude@orchestrator": {"mode": "orchestrate", "delay": 3},
                             "gemini": {"mode": "ok", "write": {"src/a.py": "a = 1\n"}},
                             "claude": {"mode": "ok", "summary": "VEREDICTO: pass"}})
        thread, _ = self.run_in_thread("loop", "--orchestrator", "claude", "--max-tasks", "1")
        entry_file = self.wait_for(lambda: next((p for p in self.active_dir.glob("*.json")
                                                 if json.loads(p.read_text(encoding="utf-8"))["kind"] == "orchestrator"), None))
        entry = json.loads(Path(entry_file).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        self.assertEqual(entry["provider"], "claude")
        thread.join(timeout=120)
        self.assertEqual(list(self.active_dir.glob("*.json")), [])
