"""TASK-3101: contrato del benchmark interno de Hefesto (`hefesto bench`).

API que fija este contrato (`hefesto/bench.py`):

- `SCHEMA_VERSION = 1`.
- `SCENARIOS`: los 12 identificadores, en este orden: `easy-bug`, `medium-change`, `multi-file`,
  `failure-escalation`, `crash-resume`, `concurrency`, `secrets-and-forbidden`, `prompt-injection`,
  `review-rejects`, `quota-fallback`, `approval-gates`, `non-regression`.
  `FLOW_SCENARIOS` son los seis primeros y `GUARD_SCENARIOS` los seis últimos.
- `CRITICAL_SCENARIOS`: los que protegen la seguridad o la integridad del estado: `crash-resume`, `concurrency`,
  `secrets-and-forbidden`, `prompt-injection`, `review-rejects`, `approval-gates`.
- `RESULT_KEYS = ("id", "status", "verified", "false_completions", "retries", "blocked", "duration_s", "notes")`
  y `RESULT_STATUSES = ("pass", "fail", "error")`. `pass` significa que Hefesto se comportó como debe en el
  escenario (por ejemplo, que el revisor rechazó el cambio defectuoso), no que la tarea se completara.
- `REPORT_KEYS = ("schema_version", "hefesto_version", "started_at", "providers", "runs", "scenarios", "summary")`.
  `providers` vale siempre `"fake"`; `summary` cuenta `pass`, `fail` y `error` y suma las métricas.
- `fake_env(root) -> dict`: entorno para los escenarios con los proveedores falsos primero en el PATH, sin ninguna
  variable `*_API_KEY` y con `HEFESTO_HOME` y `HEFESTO_STATE_DIR` dentro de `root`.
- `run(scenarios=None, workdir_parent=None) -> dict`: ejecuta cada escenario en un directorio temporal propio con un
  estado inicial conocido, con proveedores falsos (coste comercial cero) y lo borra al terminar; un identificador
  desconocido lanza `HefestoError` con `EXIT_INPUT`.
- `write_report(report, path)` y `load_report(path) -> dict`; `load_report` rechaza un `schema_version` distinto con
  `HefestoError`.
- `compare(old, new) -> dict` con `regressions` (escenarios que pasaban y ya no), `critical` (las regresiones que
  están en `CRITICAL_SCENARIOS`), `metrics` (diferencias de las métricas del resumen) y `warnings`; si alguno de
  los dos informes tiene `runs` menor que `MIN_RUNS = 5`, `warnings` incluye «muestra pequeña». No inventa
  consumos: solo compara lo que está en los informes.
- CLI: `hefesto bench [--scenario ID ...] [--report RUTA] [--compare ANTERIOR.json]`; sale con 0 si todos los
  escenarios pasan y no hay regresiones críticas, y con 1 en otro caso.

El benchmark mide el harness con proveedores simulados; no mide la calidad de los modelos reales ni sustituye la
suite de tests. Nunca invoca un proveedor comercial ni usa datos reales.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from tests.helpers import HefestoTestCase

ALL = ("easy-bug", "medium-change", "multi-file", "failure-escalation", "crash-resume", "concurrency",
       "secrets-and-forbidden", "prompt-injection", "review-rejects", "quota-fallback", "approval-gates",
       "non-regression")


def bench():
    from hefesto import bench as module
    return module


def fake_report(statuses: dict[str, str], runs: int = 1) -> dict:
    scenarios = [{"id": sid, "status": status, "verified": status == "pass", "false_completions": 0, "retries": 0,
                  "blocked": False, "duration_s": 1.0, "notes": "x"} for sid, status in statuses.items()]
    summary = {key: sum(1 for s in scenarios if s["status"] == key) for key in ("pass", "fail", "error")}
    summary.update({"false_completions": 0, "retries": 0, "duration_s": float(len(scenarios))})
    return {"schema_version": 1, "hefesto_version": "x", "started_at": "2026-11-19T00:00:00+00:00",
            "providers": "fake", "runs": runs, "scenarios": scenarios, "summary": summary}


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"EngineTests": 6, "FlowScenarioTests": 4, "GuardScenarioTests": 3, "ReportTests": 6,
                        "DocsTests": 2}
    API = ("SCHEMA_VERSION", "SCENARIOS", "FLOW_SCENARIOS", "GUARD_SCENARIOS", "CRITICAL_SCENARIOS", "RESULT_KEYS",
           "RESULT_STATUSES", "REPORT_KEYS", "fake_env(root)", "run(scenarios=None, workdir_parent=None)",
           "write_report(report, path)", "load_report(path)", "compare(old, new)", "MIN_RUNS", "muestra pequeña",
           "hefesto bench", "--compare", "coste comercial cero", "Nunca invoca un proveedor comercial", *ALL)

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

    def test_fake_report_helper_is_consistent(self) -> None:
        report = fake_report({"easy-bug": "pass", "concurrency": "fail"})
        self.assertEqual(report["summary"]["pass"], 1)
        self.assertEqual(report["summary"]["fail"], 1)


class EngineTests(HefestoTestCase):
    def test_constants(self) -> None:
        b = bench()
        self.assertEqual(b.SCHEMA_VERSION, 1)
        self.assertEqual(tuple(b.SCENARIOS), ALL)
        self.assertEqual(tuple(b.FLOW_SCENARIOS), ALL[:6])
        self.assertEqual(tuple(b.GUARD_SCENARIOS), ALL[6:])
        self.assertEqual(set(b.CRITICAL_SCENARIOS), {"crash-resume", "concurrency", "secrets-and-forbidden",
                                                     "prompt-injection", "review-rejects", "approval-gates"})
        self.assertEqual(b.RESULT_STATUSES, ("pass", "fail", "error"))
        self.assertEqual(b.MIN_RUNS, 5)

    def test_fake_env_has_no_keys_and_fakes_first(self) -> None:
        os.environ["SOMETHING_API_KEY"] = "no-debe-pasar"
        try:
            env = bench().fake_env(self.tmp / "bench")
        finally:
            os.environ.pop("SOMETHING_API_KEY", None)
        self.assertFalse([k for k in env if k.upper().endswith("_API_KEY")])
        first = env["PATH"].split(os.pathsep)[0]
        self.assertTrue(Path(first).resolve().is_relative_to((self.tmp / "bench").resolve()), first)
        for name in ("HEFESTO_HOME", "HEFESTO_STATE_DIR"):
            self.assertTrue(Path(env[name]).resolve().is_relative_to((self.tmp / "bench").resolve()), name)

    def test_report_shape(self) -> None:
        b = bench()
        report = b.run(["easy-bug"], workdir_parent=self.tmp / "work")
        self.assertEqual(tuple(report), b.REPORT_KEYS)
        self.assertEqual(report["providers"], "fake")
        self.assertEqual(report["runs"], 1)
        self.assertEqual([s["id"] for s in report["scenarios"]], ["easy-bug"])
        self.assertEqual(tuple(report["scenarios"][0]), b.RESULT_KEYS)
        self.assertIn(report["scenarios"][0]["status"], b.RESULT_STATUSES)

    def test_easy_bug_passes(self) -> None:
        result = bench().run(["easy-bug"], workdir_parent=self.tmp / "work")["scenarios"][0]
        self.assertEqual(result["status"], "pass", result["notes"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["false_completions"], 0)

    def test_scenarios_clean_up(self) -> None:
        parent = self.tmp / "work"
        bench().run(["easy-bug"], workdir_parent=parent)
        self.assertEqual(list(parent.iterdir()) if parent.exists() else [], [])

    def test_unknown_scenario_is_an_input_error(self) -> None:
        from hefesto.util import EXIT_INPUT, HefestoError
        with self.assertRaises(HefestoError) as ctx:
            bench().run(["no-existe"], workdir_parent=self.tmp / "work")
        self.assertEqual(ctx.exception.code, EXIT_INPUT)


class FlowScenarioTests(HefestoTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.results: dict[str, dict] = {}

    def result(self, sid: str) -> dict:
        if sid not in self.results:
            self.results[sid] = bench().run([sid], workdir_parent=self.tmp / "work")["scenarios"][0]
        return self.results[sid]

    def test_routine_scenarios_pass(self) -> None:
        for sid in ("easy-bug", "medium-change", "multi-file"):
            with self.subTest(scenario=sid):
                result = self.result(sid)
                self.assertEqual(result["status"], "pass", result["notes"])
                self.assertTrue(result["verified"])

    def test_failure_escalates_and_breaks_the_circuit(self) -> None:
        result = self.result("failure-escalation")
        self.assertEqual(result["status"], "pass", result["notes"])
        self.assertGreaterEqual(result["retries"], 2)
        self.assertTrue(result["blocked"])
        self.assertFalse(result["verified"])

    def test_crash_resume_never_completes_twice(self) -> None:
        result = self.result("crash-resume")
        self.assertEqual(result["status"], "pass", result["notes"])
        self.assertEqual(result["false_completions"], 0)
        self.assertTrue(result["verified"])

    def test_concurrency_has_a_single_closer(self) -> None:
        result = self.result("concurrency")
        self.assertEqual(result["status"], "pass", result["notes"])
        self.assertEqual(result["false_completions"], 0)


class GuardScenarioTests(HefestoTestCase):
    def test_guard_scenarios_pass(self) -> None:
        report = bench().run(list(ALL[6:]), workdir_parent=self.tmp / "work")
        for result in report["scenarios"]:
            with self.subTest(scenario=result["id"]):
                self.assertEqual(result["status"], "pass", result["notes"])
                self.assertTrue(result["notes"].strip())
                self.assertEqual(result["false_completions"], 0)

    def test_rejections_are_not_completions(self) -> None:
        report = bench().run(["review-rejects", "approval-gates", "secrets-and-forbidden"],
                             workdir_parent=self.tmp / "work")
        for result in report["scenarios"]:
            with self.subTest(scenario=result["id"]):
                self.assertTrue(result["blocked"])
                self.assertFalse(result["verified"])

    def test_quota_fallback_reaches_another_provider(self) -> None:
        result = bench().run(["quota-fallback"], workdir_parent=self.tmp / "work")["scenarios"][0]
        self.assertEqual(result["status"], "pass", result["notes"])
        self.assertTrue(result["verified"])


class ReportTests(HefestoTestCase):
    def test_write_and_load_roundtrip(self) -> None:
        b = bench()
        path = self.tmp / "r.json"
        report = fake_report({"easy-bug": "pass"})
        b.write_report(report, path)
        self.assertEqual(b.load_report(path), report)

    def test_load_rejects_other_schema(self) -> None:
        from hefesto.util import HefestoError
        path = self.tmp / "r.json"
        path.write_text(json.dumps({**fake_report({"easy-bug": "pass"}), "schema_version": 99}), encoding="utf-8")
        with self.assertRaises(HefestoError):
            bench().load_report(path)

    def test_compare_detects_regressions(self) -> None:
        diff = bench().compare(fake_report({"easy-bug": "pass", "multi-file": "pass"}),
                               fake_report({"easy-bug": "pass", "multi-file": "fail"}))
        self.assertEqual(diff["regressions"], ["multi-file"])
        self.assertEqual(diff["critical"], [])

    def test_compare_flags_critical_regressions(self) -> None:
        diff = bench().compare(fake_report({"prompt-injection": "pass"}), fake_report({"prompt-injection": "error"}))
        self.assertEqual(diff["critical"], ["prompt-injection"])

    def test_small_samples_are_warned(self) -> None:
        b = bench()
        self.assertTrue(any("muestra pequeña" in w for w in b.compare(fake_report({"easy-bug": "pass"}),
                                                                      fake_report({"easy-bug": "pass"}))["warnings"]))
        big = b.compare(fake_report({"easy-bug": "pass"}, runs=5), fake_report({"easy-bug": "pass"}, runs=5))
        self.assertFalse(any("muestra pequeña" in w for w in big["warnings"]))

    def test_cli_writes_report_and_compares(self) -> None:
        self.init_project()
        out = self.tmp / "nuevo.json"
        code, _, err = self.cli("bench", "--scenario", "easy-bug", "--report", str(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["providers"], "fake")
        old = self.tmp / "anterior.json"
        old.write_text(json.dumps(fake_report({"easy-bug": "pass"})), encoding="utf-8")
        code, stdout, err = self.cli("bench", "--scenario", "easy-bug", "--compare", str(old))
        self.assertEqual(code, 0, err)
        self.assertIn("muestra pequeña", stdout + err)

    def test_compare_reports_metric_differences(self) -> None:
        old = fake_report({"easy-bug": "pass", "concurrency": "pass"})
        new = fake_report({"easy-bug": "pass", "concurrency": "fail"})
        new["summary"]["retries"] = 3
        diff = bench().compare(old, new)
        self.assertEqual(diff["critical"], ["concurrency"])
        self.assertEqual(diff["metrics"]["retries"], 3)
        self.assertEqual(diff["metrics"]["fail"], 1)


class DocsTests(HefestoTestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_readme_documents_bench(self) -> None:
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("hefesto bench", readme)
        self.assertIn("--compare", readme)

    def test_addon_doc_explains_limits(self) -> None:
        doc = (self.ROOT / "docs" / "HEFESTO_ADDON_V2_6_V2_7.md").read_text(encoding="utf-8")
        self.assertIn("hefesto bench", doc)
        self.assertIn("coste comercial cero", doc)
        self.assertIn("proveedores simulados", doc)
