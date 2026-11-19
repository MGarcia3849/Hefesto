"""TASK-3201: contrato de clasificación de errores, presupuesto de reintentos y cierre idempotente.

API que fija este contrato:

- `hefesto/adapters/base.py`: `ERROR_CLASSES = ("transient", "deterministic", "auth", "quota", "security",
  "approval")` y `classify_error(error_type) -> str`: `timeout` es `transient`; `quota` y `rate_limit` son `quota`;
  `auth` es `auth`; `denied_by_policy` es `approval`; `missing_binary`, `invalid_output`, `None` (fallo sin tipo) y
  cualquier tipo desconocido son `deterministic`. `security` queda reservada para bloqueos del guard.
- `hefesto/router.py`: el registro `end` de `logs/delegations.jsonl` incluye `error_class` (o `null` si la
  delegación tuvo éxito). Solo los errores `transient` se reintentan **sobre la misma entrada** de la cadena, hasta
  `limits.transient_retries` veces (por defecto `0`: el comportamiento actual no cambia), esperando con
  `router._sleep(segundos)` un backoff exponencial con jitter: el reintento `n` (desde 0) espera entre
  `base * 2**n` y `base * 2**n + base`, con `base = limits.retry_backoff_seconds` (por defecto `5`). Agotado el
  presupuesto, se sigue con la cadena como hoy. Los errores deterministas, de cuota y de autenticación nunca se
  reintentan sobre la misma entrada. Los reintentos transitorios no cuentan como intentos de la tarea: el
  escalado y el circuit breaker no cambian.
- `hefesto/acceptance.py`: `done(project, task_id)` es idempotente frente a un corte: si el proceso muere después del
  checkpoint del código o de escribir la evidencia y antes de guardar el estado, volver a ejecutar `done` termina la
  tarea sin un segundo commit `[verified]`, con la cadena de evidencias íntegra (`evidence.verify_chain`) y el estado
  en `IDLE`. Una tarea ya cerrada sigue sin poder cerrarse dos veces.
- `hefesto/progress.py`: `resume_data(project)` incluye `interrupted`, la lista de `run_id` con evento `start` y sin
  `end` en `logs/delegations.jsonl` cuyo proceso ya no vive; `hefesto resume` los muestra para no darlos por hechos.

Reintentar no garantiza éxito ni ahorra cuota: cada reintento es otra llamada al proveedor.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import mock

from hefesto import config, evidence, roadmap, state
from tests.helpers import HefestoTestCase, git
from tests.test_git import GitFlowCase

MAPPING = {"timeout": "transient", "quota": "quota", "rate_limit": "quota", "auth": "auth",
           "denied_by_policy": "approval", "missing_binary": "deterministic", "invalid_output": "deterministic",
           None: "deterministic", "algo-nuevo": "deterministic"}


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"ErrorClassTests": 3, "RetryBudgetTests": 5, "ResumeTests": 4}
    API = ("ERROR_CLASSES", "classify_error(error_type)", "transient", "deterministic", "error_class",
           "limits.transient_retries", "limits.retry_backoff_seconds", "router._sleep(segundos)", "base * 2**n",
           "circuit breaker", "done(project, task_id)", "[verified]", "evidence.verify_chain", "resume_data(project)",
           "interrupted", "no garantiza éxito")

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

    def test_mapping_covers_every_known_error_type(self) -> None:
        from hefesto.adapters import base
        self.assertTrue(set(base.ERROR_TYPES) <= set(MAPPING), "cada error_type conocido tiene clase en el contrato")


class DelegationCase(GitFlowCase):
    """Proyecto con TASK-001 en curso y una cadena de runner gemini → codex con proveedores falsos."""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("gemini", "codex", "claude")
        self.scenario = self.tmp / "scenario.json"
        self.calls = self.tmp / "calls.jsonl"
        import os
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls)
        self.settings = config.project_settings_path(self.project)
        self.configure({("roles", "runner", "use"): ["gemini", "codex"], ("limits", "timeout_seconds"): 1})
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)

    def configure(self, values: dict[tuple[str, ...], object]) -> None:
        for keys, value in values.items():
            config.set_setting(self.settings, keys, value)

    def respond(self, **entries: object) -> None:
        self.scenario.write_text(json.dumps(entries), encoding="utf-8")

    def calls_to(self, name: str) -> int:
        if not self.calls.exists():
            return 0
        return sum(1 for line in self.calls.read_text(encoding="utf-8").splitlines()
                   if line.strip() and json.loads(line).get("name") == name)

    def delegation_ends(self) -> list[dict]:
        path = self.project / ".hefesto" / "logs" / "delegations.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r.get("event") == "end"]


class ErrorClassTests(HefestoTestCase):
    def test_classes(self) -> None:
        from hefesto.adapters import base
        self.assertEqual(base.ERROR_CLASSES, ("transient", "deterministic", "auth", "quota", "security", "approval"))

    def test_mapping(self) -> None:
        from hefesto.adapters import base
        for error_type, expected in MAPPING.items():
            with self.subTest(error_type=error_type):
                self.assertEqual(base.classify_error(error_type), expected)
                self.assertIn(base.classify_error(error_type), base.ERROR_CLASSES)

    def test_every_known_type_has_a_valid_class(self) -> None:
        from hefesto.adapters import base
        for error_type in base.ERROR_TYPES:
            with self.subTest(error_type=error_type):
                self.assertIn(base.classify_error(error_type), base.ERROR_CLASSES)


class RetryBudgetTests(DelegationCase):
    def test_delegation_log_records_the_class(self) -> None:
        self.respond(gemini={"mode": "fail"}, codex={"mode": "fail"})
        self.cli("run", "runner", "implementa")
        ends = self.delegation_ends()
        self.assertTrue(ends)
        self.assertEqual(ends[-1]["error_class"], "deterministic")
        self.respond(gemini={"mode": "ok", "write": {"src/suma.py": "x = 1\n"}})
        self.cli("run", "runner", "implementa")
        self.assertIsNone(self.delegation_ends()[-1]["error_class"])

    def test_defaults_keep_current_behaviour(self) -> None:
        cfg = config.load(self.project)
        self.assertEqual(int(cfg.get("limits", "transient_retries")), 0)
        self.assertEqual(float(cfg.get("limits", "retry_backoff_seconds")), 5)
        self.respond(gemini={"mode": "timeout", "sleep": 3}, codex={"mode": "ok", "write": {"src/suma.py": "x = 1\n"}})
        with mock.patch("hefesto.router._sleep") as sleep:
            self.cli("run", "runner", "implementa")
        self.assertEqual(self.calls_to("gemini"), 1)
        sleep.assert_not_called()

    def test_transient_errors_are_retried_with_backoff(self) -> None:
        self.configure({("limits", "transient_retries"): 2, ("limits", "retry_backoff_seconds"): 1})
        self.respond(gemini={"mode": "timeout", "sleep": 3}, codex={"mode": "ok", "write": {"src/suma.py": "x = 1\n"}})
        with mock.patch("hefesto.router._sleep") as sleep:
            self.cli("run", "runner", "implementa")
        self.assertEqual(self.calls_to("gemini"), 3, "un intento más dos reintentos sobre la misma entrada")
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        for n, delay in enumerate(delays):
            self.assertGreaterEqual(delay, 2 ** n)
            self.assertLessEqual(delay, 2 ** n + 1)
        self.assertEqual(self.calls_to("codex"), 1, "agotado el presupuesto, sigue la cadena")

    def test_a_transient_error_that_recovers_stops_retrying(self) -> None:
        self.configure({("limits", "transient_retries"): 3, ("limits", "retry_backoff_seconds"): 1})
        self.respond(gemini=[{"mode": "timeout", "sleep": 3}, {"mode": "ok", "write": {"src/suma.py": "x = 1\n"}}])
        with mock.patch("hefesto.router._sleep"):
            code, _, err = self.cli("run", "runner", "implementa")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls_to("gemini"), 2)
        self.assertEqual(self.calls_to("codex"), 0)

    def test_deterministic_errors_are_not_retried(self) -> None:
        self.configure({("limits", "transient_retries"): 2})
        self.respond(gemini={"mode": "fail"}, codex={"mode": "fail"})
        with mock.patch("hefesto.router._sleep") as sleep:
            self.cli("run", "runner", "implementa")
        self.assertEqual(self.calls_to("gemini"), 1)
        sleep.assert_not_called()

    def test_quota_is_not_retried_on_the_same_entry(self) -> None:
        self.configure({("limits", "transient_retries"): 2})
        self.respond(gemini={"mode": "quota"}, codex={"mode": "ok", "write": {"src/suma.py": "x = 1\n"}})
        with mock.patch("hefesto.router._sleep") as sleep:
            self.cli("run", "runner", "implementa")
        self.assertEqual(self.calls_to("gemini"), 1)
        self.assertEqual(self.calls_to("codex"), 1)
        sleep.assert_not_called()

    def test_retries_do_not_count_as_task_attempts(self) -> None:
        self.configure({("limits", "transient_retries"): 2, ("limits", "retry_backoff_seconds"): 1})
        self.respond(gemini={"mode": "timeout", "sleep": 3}, codex={"mode": "timeout", "sleep": 3})
        with mock.patch("hefesto.router._sleep"):
            self.cli("run", "runner", "implementa")
        data = state.load(self.project)
        self.assertEqual(data["attempts"], 0)
        self.assertEqual(data["active_role"], "runner")


class ResumeTests(GitFlowCase):
    def prepare(self) -> None:
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        self.write("src/suma.py")
        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, out + err)

    def verified_commits(self) -> int:
        log = git(self.project, "log", "--all", "--format=%s")
        return sum(1 for line in log.splitlines() if line.startswith("hefesto(TASK-001)") and "[verified]" in line)

    def assert_recovered(self) -> None:
        code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 0, out + err)
        data = state.load(self.project)
        self.assertEqual(data["status"], "IDLE")
        self.assertIsNone(data["current_task_id"])
        self.assertEqual(self.verified_commits(), 1, "sin segundo commit [verified]")
        self.assertEqual(evidence.verify_chain(self.project, data.get("evidence_head")), [])
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, "x")

    def test_crash_before_saving_state_is_recoverable(self) -> None:
        from hefesto import acceptance
        self.prepare()
        real_save = state.save

        def crash_on_final_save(project, data):
            if data.get("current_task_id") is None:
                raise RuntimeError("corte simulado antes de guardar el estado")
            return real_save(project, data)

        with mock.patch.object(acceptance.state, "save", side_effect=crash_on_final_save):
            with self.assertRaises(RuntimeError):
                acceptance.done(self.project, "TASK-001")
        self.assert_recovered()

    def test_crash_after_code_checkpoint_is_recoverable(self) -> None:
        from hefesto import acceptance
        self.prepare()
        with mock.patch.object(acceptance.evidence, "write", side_effect=RuntimeError("corte simulado")):
            with self.assertRaises(RuntimeError):
                acceptance.done(self.project, "TASK-001")
        self.assert_recovered()

    def test_completed_task_cannot_be_closed_twice(self) -> None:
        self.complete("TASK-001", "src/suma.py")
        code, _, _ = self.cli("done", "TASK-001")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.verified_commits(), 1)

    def test_resume_lists_interrupted_delegations(self) -> None:
        from hefesto import log, progress
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        log.record(self.project, "delegations", {"event": "start", "run_id": "deadbeef0001", "task_id": "TASK-001",
                                                 "role": "runner", "provider": "gemini", "pid": 999999})
        log.record(self.project, "delegations", {"event": "start", "run_id": "deadbeef0002", "task_id": "TASK-001",
                                                 "role": "runner", "provider": "gemini", "pid": 999999})
        log.record(self.project, "delegations", {"event": "end", "run_id": "deadbeef0002", "success": True})
        info = progress.resume_data(self.project)
        self.assertEqual(info["interrupted"], ["deadbeef0001"])
        self.assertIn("deadbeef0001", progress.render_resume(info))
