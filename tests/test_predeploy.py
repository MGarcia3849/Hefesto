"""TASK-3301: contrato de la auditoría predespliegue A–E (`hefesto security predeploy`).

API que fija este contrato (`hefesto/predeploy.py`):

- `SCHEMA_VERSION = 1`.
- `CONTROLS = ("costs", "accessibility", "data-access", "communications", "abuse")`: A costes inesperados,
  B accesibilidad, C bases de datos y control de acceso, D comunicaciones y consentimiento, E abuso y gasto sin
  control.
- `STATUSES = ("confirmed", "suspected", "not_tested", "not_applicable", "passed")` y `STATUS_FROM_SECURITY`, que
  traduce los estados del módulo de seguridad (`confirmado` → `confirmed`, `candidato` → `suspected`) para no crear
  otra fuente de verdad: los hallazgos de secretos y de Firestore salen de `security.scan` y `security.rules_test`.
- `VERDICTS = ("PASS", "REVIEW_REQUIRED", "BLOCKED")`. `BLOCKED` si hay un hallazgo `critical` o `high`
  `confirmed` sin excepción vigente; `REVIEW_REQUIRED` si no lo hay pero algún control o hallazgo queda
  `suspected` o `not_tested`; `PASS` solo si todo es `passed` o `not_applicable`. Un control no ejecutado es
  `not_tested`, nunca `passed`.
- `PROFILES = ("web-general", "public-sector-sensitive", "auto")`; `auto` detecta el stack técnico y no presume el
  régimen jurídico.
- `FINDING_KEYS = ("id", "control", "severity", "status", "title", "evidence", "recommendation")` y
  `REPORT_KEYS = ("schema_version", "profile", "stack", "controls", "findings", "verdict", "notes")`; cada entrada de
  `controls` tiene `id`, `status` y `reason`.
- `run(project, profile="auto", emulator=None, a11y_runner=None) -> dict`: análisis local y pasivo. `emulator` es el
  mismo que usa `security.rules_test`; `a11y_runner(project) -> list[dict]` solo se usa si el proyecto ya tiene
  axe-core con Playwright: sin él, la accesibilidad automática queda `not_tested` y `notes` incluye la
  checklist manual (teclado, foco visible, nombres accesibles, etiquetas, contraste y zoom). Detecta estáticamente
  integraciones de pago, polling frecuente y reintentos sin límite (`suspected`, para revisión humana). Sin
  comunicaciones comerciales, el control D es `not_applicable` con su motivo. Nunca despliega, no cambia reglas,
  no abre conexiones de red y redacta la evidencia (sin secretos ni datos personales en claro).
- `write_report(report, path)` escribe JSON; `render_markdown(report) -> str` el resumen.
- `gate(project, cwd) -> dict | None`: lo llama `delivery.deploy` después del gate de seguridad. Según
  `security.predeploy` (`"warn"` por defecto, `"block"` u `"off"`): `off` no ejecuta nada y devuelve `None`; `warn`
  imprime el veredicto y nunca bloquea; `block` lanza `HefestoError` con `EXIT_BLOCKED` si el veredicto es `BLOCKED`.
- CLI: `hefesto security predeploy [--profile P] [--report RUTA]`; sale con `EXIT_BLOCKED` si el veredicto es
  `BLOCKED` y con 0 en otro caso.

Los controles aportan indicios: no certifican cumplimiento de RGPD, LOPDGDD, WCAG ni ENS, que exigen evaluación
competente. Los ejemplos vulnerables viven solo en fixtures de tests.
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path
from unittest import mock

from tests.helpers import HefestoTestCase, git
from tests.test_delivery_contract import DeliveryCase, _delivery

SECRET = "ghp_" + "B" * 36  # sintético y partido: el checkpoint no ve un secreto literal
OPEN_RULES = "rules_version = '2';\nservice cloud.firestore {\n  match /databases/{db}/documents {\n" \
             "    match /{document=**} {\n      allow read, write: if true;\n    }\n  }\n}\n"
SAFE_RULES = "rules_version = '2';\nservice cloud.firestore {\n  match /databases/{db}/documents {\n" \
             "    match /documentos/{id} {\n      allow read, write: if request.auth != null && " \
             "request.auth.uid == resource.data.owner;\n    }\n  }\n}\n"
SAFE_HTML = "<!doctype html><html lang='es'><head><title>Inicio</title></head><body><main><h1>Hola</h1>" \
            "<img src='logo.png' alt='Logo'><button>Entrar</button></main></body></html>\n"


def pd():
    from hefesto import predeploy
    return predeploy


def control(report: dict, cid: str) -> dict:
    return next(c for c in report["controls"] if c["id"] == cid)


def findings(report: dict, cid: str) -> list[dict]:
    return [f for f in report["findings"] if f["control"] == cid]


class PredeployCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def put(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def safe_project(self) -> None:
        self.put("web/index.html", SAFE_HTML)
        self.put("firestore.rules", SAFE_RULES)


def deny_all(_rules: str, _request: dict) -> bool:
    return False


def allow_all(_rules: str, _request: dict) -> bool:
    return True


def axe_clean(_project: Path) -> list[dict]:
    return []


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"ReportTests": 6, "DataAccessTests": 3, "CostAbuseTests": 3, "AccessibilityTests": 3,
                        "CommsTests": 2, "DeployGateTests": 3, "DocsTests": 2}
    API = ("SCHEMA_VERSION", "CONTROLS", "costs", "accessibility", "data-access", "communications", "abuse",
           "STATUSES", "confirmed", "suspected", "not_tested", "not_applicable", "passed", "STATUS_FROM_SECURITY",
           "security.scan", "security.rules_test", "VERDICTS", "PASS", "REVIEW_REQUIRED", "BLOCKED", "PROFILES",
           "web-general", "public-sector-sensitive", "auto", "FINDING_KEYS", "REPORT_KEYS",
           'run(project, profile="auto", emulator=None, a11y_runner=None)', "a11y_runner(project)", "axe-core",
           "checklist manual", "write_report(report, path)", "render_markdown(report)", "gate(project, cwd)",
           "security.predeploy", '"warn"', '"block"', '"off"', "EXIT_BLOCKED", "hefesto security predeploy",
           "no certifican cumplimiento", "Nunca despliega")

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

    def test_fixtures_are_synthetic(self) -> None:
        self.assertNotIn(SECRET, Path(__file__).read_text(encoding="utf-8"), "el secreto no aparece literal")


class ReportTests(PredeployCase):
    def test_constants(self) -> None:
        p = pd()
        self.assertEqual(p.SCHEMA_VERSION, 1)
        self.assertEqual(p.CONTROLS, ("costs", "accessibility", "data-access", "communications", "abuse"))
        self.assertEqual(p.STATUSES, ("confirmed", "suspected", "not_tested", "not_applicable", "passed"))
        self.assertEqual(p.VERDICTS, ("PASS", "REVIEW_REQUIRED", "BLOCKED"))
        self.assertEqual(p.PROFILES, ("web-general", "public-sector-sensitive", "auto"))
        self.assertEqual(p.STATUS_FROM_SECURITY["confirmado"], "confirmed")
        self.assertEqual(p.STATUS_FROM_SECURITY["candidato"], "suspected")

    def test_report_shape(self) -> None:
        p = pd()
        self.safe_project()
        report = p.run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(tuple(report), p.REPORT_KEYS)
        self.assertEqual([c["id"] for c in report["controls"]], list(p.CONTROLS))
        for entry in report["controls"]:
            self.assertIn(entry["status"], p.STATUSES)
            self.assertTrue(entry["reason"].strip())
        for finding in report["findings"]:
            self.assertEqual(tuple(finding), p.FINDING_KEYS)

    def test_safe_fixture_does_not_block(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertIn(report["verdict"], ("PASS", "REVIEW_REQUIRED"))
        self.assertEqual(control(report, "data-access")["status"], "passed")
        self.assertEqual(control(report, "communications")["status"], "not_applicable")

    def test_seeded_secret_is_reported_redacted(self) -> None:
        self.safe_project()
        self.put("src/config.js", f'export const token = "{SECRET}";\n')
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertTrue(any("secret" in f["id"] or "secreto" in f["title"].lower() for f in report["findings"]))
        text = json.dumps(report) + pd().render_markdown(report)
        self.assertNotIn(SECRET, text)

    def test_not_run_is_never_passed(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=None, a11y_runner=None)
        self.assertEqual(control(report, "accessibility")["status"], "not_tested")
        self.assertEqual(control(report, "data-access")["status"], "not_tested", "reglas sin emulador: no probadas")
        self.assertEqual(report["verdict"], "REVIEW_REQUIRED")

    def test_runs_offline_and_never_deploys(self) -> None:
        self.safe_project()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("sin red")), \
                mock.patch("hefesto.delivery.deploy", side_effect=AssertionError("sin despliegue")):
            report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertIn(report["verdict"], pd().VERDICTS)
        self.assertEqual((self.project / "firestore.rules").read_text(encoding="utf-8"), SAFE_RULES)

    def test_write_report_and_markdown(self) -> None:
        p = pd()
        self.safe_project()
        report = p.run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        out = self.tmp / "predeploy.json"
        p.write_report(report, out)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["verdict"], report["verdict"])
        md = p.render_markdown(report)
        self.assertIn(report["verdict"], md)
        self.assertIn("no certifica", md.lower())

    def test_cli(self) -> None:
        self.safe_project()
        out = self.tmp / "r.json"
        code, stdout, err = self.cli("security", "predeploy", "--report", str(out))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.exists())
        self.put("firestore.rules", OPEN_RULES)
        code, _, _ = self.cli("security", "predeploy")
        from hefesto.util import EXIT_BLOCKED
        self.assertEqual(code, EXIT_BLOCKED)


class DataAccessTests(PredeployCase):
    def test_open_rule_blocks(self) -> None:
        self.safe_project()
        self.put("firestore.rules", OPEN_RULES)
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(control(report, "data-access")["status"], "confirmed")

    def test_cross_user_access_in_emulator_blocks(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=allow_all, a11y_runner=axe_clean)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertTrue([f for f in findings(report, "data-access") if f["status"] == "confirmed"])

    def test_project_without_data_layer_is_not_applicable(self) -> None:
        self.put("web/index.html", SAFE_HTML)
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(control(report, "data-access")["status"], "not_applicable")
        self.assertTrue(control(report, "data-access")["reason"].strip())


class CostAbuseTests(PredeployCase):
    def test_paid_integration_is_flagged_for_review(self) -> None:
        self.safe_project()
        self.put("src/ai.js", "import OpenAI from 'openai';\nconst client = new OpenAI();\n")
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(control(report, "costs")["status"], "suspected")
        self.assertTrue(findings(report, "costs"))
        self.assertNotEqual(report["verdict"], "PASS")

    def test_unbounded_retry_loop_is_flagged(self) -> None:
        self.safe_project()
        self.put("src/sync.py", "import requests\n\ndef sync():\n    while True:\n"
                               "        try:\n            return requests.get('https://example.invalid')\n"
                               "        except Exception:\n            continue\n")
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(control(report, "abuse")["status"], "suspected")
        self.assertTrue(any(f["evidence"] and "src/sync.py" in json.dumps(f["evidence"]) for f in findings(report, "abuse")))

    def test_frequent_polling_is_flagged(self) -> None:
        self.safe_project()
        self.put("src/poll.js", "setInterval(() => fetch('/api/estado'), 500);\n")
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertTrue(findings(report, "costs") or findings(report, "abuse"))

    def test_project_without_risky_patterns_passes_both(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(control(report, "costs")["status"], "passed")
        self.assertEqual(control(report, "abuse")["status"], "passed")


class AccessibilityTests(PredeployCase):
    def test_accessibility_failure_produces_a_finding(self) -> None:
        self.safe_project()
        self.put("web/index.html", "<html><body><img src='a.png'><input type='text'></body></html>\n")
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertTrue(findings(report, "accessibility"))
        self.assertNotEqual(control(report, "accessibility")["status"], "passed")

    def test_runner_findings_are_included(self) -> None:
        self.safe_project()
        runner = mock.Mock(return_value=[{"rule": "color-contrast", "target": "button", "impact": "serious"}])
        report = pd().run(self.project, emulator=deny_all, a11y_runner=runner)
        runner.assert_called_once()
        self.assertTrue(any("color-contrast" in json.dumps(f) for f in findings(report, "accessibility")))

    def test_manual_checks_are_always_listed(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        notes = " ".join(report["notes"]).lower()
        for word in ("teclado", "foco", "contraste"):
            self.assertIn(word, notes)

    def test_without_runner_it_is_not_tested(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=deny_all, a11y_runner=None)
        self.assertEqual(control(report, "accessibility")["status"], "not_tested")


class CommsTests(PredeployCase):
    def test_without_marketing_it_is_not_applicable(self) -> None:
        self.safe_project()
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        entry = control(report, "communications")
        self.assertEqual(entry["status"], "not_applicable")
        self.assertTrue(entry["reason"].strip())

    def test_marketing_without_unsubscribe_needs_review(self) -> None:
        self.safe_project()
        self.put("src/newsletter.js", "import sgMail from '@sendgrid/mail';\n"
                                      "export function campaign(to) { return sgMail.send({to, subject: 'Oferta'}); }\n")
        report = pd().run(self.project, emulator=deny_all, a11y_runner=axe_clean)
        self.assertEqual(control(report, "communications")["status"], "suspected")
        self.assertTrue(findings(report, "communications"))


class DeployGateTests(DeliveryCase):
    def ready(self) -> None:
        self.close_task()
        self.write_target()
        _delivery().authorize(self.project, "deploy", yes=True)

    def blocked_report(self) -> dict:
        return {"verdict": "BLOCKED", "findings": [], "controls": [], "notes": []}

    def set_mode(self, mode: str) -> None:
        from hefesto import config
        config.set_setting(config.project_settings_path(self.project), ("security", "predeploy"), mode)

    def test_default_mode_warns_and_deploys(self) -> None:
        from hefesto import config
        self.ready()
        self.assertEqual(config.load(self.project).get("security", "predeploy"), "warn")
        with mock.patch("hefesto.predeploy.run", return_value=self.blocked_report()) as run:
            result = _delivery().deploy(self.project)
        run.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(self.deploys(), 1)

    def test_block_mode_stops_a_blocked_deploy(self) -> None:
        from hefesto.util import EXIT_BLOCKED, HefestoError
        self.ready()
        self.set_mode("block")
        with mock.patch("hefesto.predeploy.run", return_value=self.blocked_report()), \
                self.assertRaises(HefestoError) as ctx:
            _delivery().deploy(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(self.deploys(), 0)

    def test_block_mode_lets_review_required_through(self) -> None:
        self.ready()
        self.set_mode("block")
        report = dict(self.blocked_report(), verdict="REVIEW_REQUIRED")
        with mock.patch("hefesto.predeploy.run", return_value=report):
            self.assertTrue(_delivery().deploy(self.project).ok)

    def test_off_mode_does_not_run(self) -> None:
        self.ready()
        self.set_mode("off")
        with mock.patch("hefesto.predeploy.run") as run:
            self.assertTrue(_delivery().deploy(self.project).ok)
        run.assert_not_called()


class DocsTests(HefestoTestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_readme_documents_predeploy(self) -> None:
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("hefesto security predeploy", readme)
        self.assertIn("security.predeploy", readme)

    def test_addon_doc_is_honest_about_limits(self) -> None:
        doc = (self.ROOT / "docs" / "HEFESTO_ADDON_V2_6_V2_7.md").read_text(encoding="utf-8")
        self.assertIn("hefesto security predeploy", doc)
        self.assertIn("no certifica", doc.lower())
        self.assertIn("not_applicable", doc)
