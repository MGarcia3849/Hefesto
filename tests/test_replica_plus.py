"""TASK-3401: contrato de Replica funcional (`hefesto/replica_features.py`).

Qué tiene una app web autorizada, cómo lo hace (solo con señales observables) y cómo mejorarlo, comparado con lo
que ya tiene el proyecto propio. Reutiliza `hefesto/replica.py` para la autorización (`replica.load_scope`), el
alcance (`replica.check_url`, `replica.in_scope`), el saneado (`replica.sanitize`) y la carpeta
(`replica.replica_dir`); `replica.py` no cambia.

API que fija este contrato:

- `SCHEMA_VERSION = 1`, `FEATURE_KINDS = ("section", "form", "action", "search", "filter", "flow", "account",
  "state")` y `FEATURE_KEYS = ("kind", "key", "label", "details", "route", "evidence")`.
- `extract_features(html, route) -> list[dict]`: funcionalidades del DOM ya renderizado, sin red. Un formulario
  guarda sus campos (`name`, `type`, `required` y las validaciones visibles `pattern`, `min`, `max`, `minlength`,
  `maxlength`), nunca valores. Un campo de contraseña o un enlace de entrar/registrarse es `account`; un campo de
  búsqueda es `search`; `select`, casillas y radios agrupados son `filter`; pasos (`data-step`, `aria-current="step"`)
  son `flow`; `data-state`, `aria-busy` o textos de vacío y error son `state`; botones son `action`; los encabezados
  principales son `section`. `key` es estable: tipo y etiqueta normalizada (minúsculas, sin tildes ni signos).
- `how_it_works(html, headers=None, requests=None) -> dict` con `SIGNAL_KEYS = ("technologies", "requests",
  "patterns")`: tecnologías por marcadores públicos (`meta generator`, `ng-version`, `__NEXT_DATA__`, `data-reactroot`,
  nombres de scripts, cabeceras `server` y `x-powered-by` saneadas); peticiones observadas como `method`, `path` y
  `type`, sin cuerpos, sin cadenas de consulta ni valores; patrones de interacción. Nunca guarda HTML ni scripts.
- `study_reference(project, replica_id, browser, max_pages=None) -> dict`: exige un alcance autorizado; recorre como
  máximo `max_pages` (por defecto `[replica] max_pages`, 10) páginas dentro del alcance siguiendo enlaces; bloquea
  redirecciones fuera del alcance; nunca envía formularios, nunca inicia sesión y no guarda HTML. `browser` tiene
  `open()`, `goto(url) -> (url_final, html)`, `requests() -> list[dict]`, `headers() -> dict` y `close()`.
  Guarda `functional-reference.json` en `replica.replica_dir`.
- `study_project(project, replica_id) -> dict`: el mismo inventario del proyecto propio a partir de su código (HTML,
  JS/TS/JSX/TSX, plantillas Angular, Vue, Svelte y Dart), sin ejecutarlo; ignora `.git`, `node_modules`, `dist` y
  `build`. Guarda `functional-project.json`.
- `FEATURE_STATES = ("tenemos", "falta", "parcial", "mejor_nuestra", "no_aplica", "no_verificable")` y
  `feature_matrix(reference, project) -> dict` con `ROW_KEYS = ("kind", "key", "state", "reference", "project",
  "note")` y `counts`. Solo en la referencia es `falta`; con menos campos o validaciones es `parcial`; con todo lo de
  la referencia y más validaciones es `mejor_nuestra`; igual es `tenemos`; solo en el proyecto es `no_aplica`.
- `improvements(reference) -> list[dict]` con `IMPROVEMENT_KEYS = ("key", "title", "why", "effort", "risk")`, `effort`
  en `S`, `M`, `L` y `risk` en `bajo`, `medio`, `alto`: lo que la referencia hace mal y podemos hacer mejor (campos
  obligatorios sin validación visible, botones sin nombre, sin estados vacío o error, formularios muy largos, flujos
  sin indicador de progreso).
- `propose(project, replica_id) -> dict[str, Path]`: escribe `functional-matrix.json` y `functional-proposal.md`, una
  propuesta documental con lo que falta, lo parcial, lo que ya tenemos mejor y las mejoras sobre la referencia, cada
  una con esfuerzo y riesgo. No escribe código.
- `accept(project, replica_id)`: solo un humano por el canal de confianza (`approval.human_channel`), ligada al hash
  de los documentos; `accepted(project, replica_id) -> bool`.
- `plan(project, replica_id, gaps=None) -> list[str]`: exige una propuesta aceptada; añade al ROADMAP una fase con una
  tarea pendiente por hueco (`falta` o `parcial`, o solo los `gaps` pedidos) con verify
  `hefesto replica check ID --gap CLAVE`; no marca nada ni aplica código; el ROADMAP resultante es válido.
- `check(project, replica_id, gaps) -> tuple[int, str]`: vuelve a estudiar el proyecto y sale con 0 solo si todos los
  huecos pedidos están en `tenemos` o `mejor_nuestra`; un hueco desconocido es `EXIT_INPUT`.
- CLI: `hefesto replica study ID`, `hefesto replica features ID`, `hefesto replica approve ID`,
  `hefesto replica plan ID [--gap CLAVE]` y `hefesto replica check ID --gap CLAVE`.

Límites honestos: solo analiza lo observable usando la app autorizada; no lee ni descompila su código, no se salta
logins, DRM ni límites, y no reconstruye la aplicación. Craft Anything no se instala.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from hefesto import roadmap
from tests.helpers import HefestoTestCase, git, tty
from tests.test_addon_replica import REF_URL, RID, _ReplicaCase

HTTPS = REF_URL.split("://", 1)[0] + "://"
HOST = REF_URL.split("://", 1)[1].rstrip("/")
PRIVATE_MARK = "dato-privado-del-formulario"

REF_HOME = """<!doctype html><html lang="es"><head><title>Portal</title>
<meta name="generator" content="Next.js"><script id="__NEXT_DATA__" type="application/json">{}</script>
<script src="/_next/static/chunks/main.js"></script></head><body>
<h1>Avisos ciudadanos</h1>
<form action="/buscar" method="get"><input type="search" name="q" placeholder="Buscar avisos"></form>
<select name="categoria"><option>Tráfico</option><option>Ruido</option></select>
<a href="/entrar">Entrar</a> <a href="/alta">Nuevo aviso</a> <a href="https://otro.example/fuera">Fuera</a>
<button></button>
<div data-state="vacio">No hay avisos todavía</div>
</body></html>"""

REF_ALTA = """<!doctype html><html lang="es"><head><title>Nuevo aviso</title></head><body>
<h1>Nuevo aviso</h1><ol><li aria-current="step">Datos</li><li>Ubicación</li><li>Revisión</li></ol>
<form action="/api/avisos" method="post">
<input name="titulo" required minlength="5" maxlength="80">
<input name="email" type="email" required>
<input name="telefono" type="text">
<textarea name="descripcion"></textarea>
<input type="hidden" name="csrf" value="SECRETO-NO-GUARDAR">
<button>Enviar aviso</button></form>
<script>window.__STATE__ = {"token": "NO-COPIAR"}</script>
</body></html>"""

REF_ENTRAR = """<!doctype html><html lang="es"><head><title>Entrar</title></head><body>
<form action="/sesion" method="post"><input name="usuario" value="%s"><input name="clave" type="password">
<button>Entrar</button></form></body></html>""" % PRIVATE_MARK

PROJECT_FILES = {
    "src/app/avisos/avisos.component.html": (
        '<h1>Avisos ciudadanos</h1>\n<input type="search" name="q">\n'
        '<form [formGroup]="form"><input formControlName="titulo" name="titulo" required minlength="5" '
        'maxlength="80" pattern="[A-Za-z ]+"><input formControlName="email" name="email" type="email" required>'
        '<input name="telefono" type="tel" required pattern="[0-9]{9}"><textarea name="descripcion" required>'
        '</textarea><button>Enviar aviso</button></form>\n'
    ),
    "src/app/app.routes.ts": "export const routes = [{ path: 'avisos' }, { path: 'mapa' }];\n",
    "src/app/avisos/avisos.service.ts": "this.http.post('/api/avisos', aviso);\n",
    "node_modules/lib/index.html": "<form><input type='password' name='nada'></form>",
}


def rf() -> Any:
    from hefesto import replica_features
    return replica_features


class FakeBrowser:
    """Navegador falso en proceso: páginas por URL, peticiones observadas y registro de visitas."""

    def __init__(self, pages: dict[str, str], redirects: dict[str, str] | None = None) -> None:
        self.pages = pages
        self.redirects = redirects or {}
        self.visited: list[str] = []
        self.opened = self.closed = False

    def open(self) -> None:
        self.opened = True

    def goto(self, url: str) -> tuple[str, str]:
        self.visited.append(url)
        final = self.redirects.get(url, url)
        return final, self.pages.get(final, "<html><body><h1>404</h1></body></html>")

    def requests(self) -> list[dict]:
        return [{"method": "GET", "url": HTTPS + HOST + "/api/avisos?page=2&email=ana@example.com", "type": "xhr"},
                {"method": "POST", "url": HTTPS + HOST + "/api/avisos", "type": "fetch", "body": "{\"titulo\": 1}"}]

    def headers(self) -> dict:
        return {"server": "nginx/1.25", "x-powered-by": "Next.js", "set-cookie": "sesion=SECRETO"}

    def close(self) -> None:
        self.closed = True


def site() -> dict[str, str]:
    return {REF_URL: REF_HOME, REF_URL + "alta": REF_ALTA, REF_URL + "entrar": REF_ENTRAR}


class FeatureCase(_ReplicaCase):
    def setUp(self) -> None:
        super().setUp()
        for rel, text in PROJECT_FILES.items():
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "app propia")

    def studied(self, max_pages: int | None = None) -> dict:
        self.assertIsNotNone(self.authorize())
        self.fake = FakeBrowser(site())
        return rf().study_reference(self.project, RID, self.fake, max_pages=max_pages)

    def kinds(self, inventory: dict) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for feature in inventory["features"]:
            out.setdefault(feature["kind"], []).append(feature)
        return out


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"ReferenceTests": 8, "ProjectTests": 4, "MatrixTests": 5, "ProposalTests": 4,
                        "PlanCheckTests": 5, "DocsTests": 2}
    API = ("SCHEMA_VERSION", "FEATURE_KINDS", "FEATURE_KEYS", "extract_features(html, route)", "how_it_works(html",
           "SIGNAL_KEYS", "study_reference(project, replica_id, browser", "max_pages", "goto(url)", "requests()",
           "headers()", "functional-reference.json", "study_project(project, replica_id)", "functional-project.json",
           "FEATURE_STATES", "tenemos", "falta", "parcial", "mejor_nuestra", "no_aplica", "no_verificable",
           "feature_matrix(reference, project)", "ROW_KEYS", "improvements(reference)", "IMPROVEMENT_KEYS",
           "propose(project, replica_id)", "functional-proposal.md", "functional-matrix.json",
           "accept(project, replica_id)", "approval.human_channel", "accepted(project, replica_id)",
           "plan(project, replica_id, gaps=None)", "hefesto replica check ID --gap CLAVE",
           "check(project, replica_id, gaps)", "EXIT_INPUT", "hefesto replica study ID", "hefesto replica features ID",
           "hefesto replica approve ID", "no lee ni descompila", "Craft Anything no se instala", "replica.py` no cambia")

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

    def test_fixtures_carry_the_traps(self) -> None:
        self.assertIn("SECRETO-NO-GUARDAR", REF_ALTA)
        self.assertIn("NO-COPIAR", REF_ALTA)
        self.assertIn(PRIVATE_MARK, REF_ENTRAR)
        self.assertIn("otro.example", REF_HOME)


class ReferenceTests(FeatureCase):
    def test_constants(self) -> None:
        r = rf()
        self.assertEqual(r.SCHEMA_VERSION, 1)
        self.assertEqual(r.FEATURE_KINDS, ("section", "form", "action", "search", "filter", "flow", "account", "state"))
        self.assertEqual(r.FEATURE_KEYS, ("kind", "key", "label", "details", "route", "evidence"))
        self.assertEqual(r.SIGNAL_KEYS, ("technologies", "requests", "patterns"))

    def test_extract_features_finds_what_the_app_has(self) -> None:
        kinds = {f["kind"] for f in rf().extract_features(REF_HOME, "/")}
        self.assertTrue({"section", "search", "filter", "action", "state"} <= kinds, kinds)
        alta = rf().extract_features(REF_ALTA, "/alta")
        self.assertIn("flow", {f["kind"] for f in alta})
        form = next(f for f in alta if f["kind"] == "form")
        fields = {field["name"]: field for field in form["details"]["fields"]}
        self.assertTrue(fields["titulo"]["required"])
        self.assertEqual(fields["titulo"]["minlength"], "5")
        self.assertEqual(fields["email"]["type"], "email")
        self.assertNotIn("csrf", fields, "los campos ocultos no son funcionalidad visible")

    def test_feature_keys_are_stable(self) -> None:
        once = [f["key"] for f in rf().extract_features(REF_HOME, "/")]
        again = [f["key"] for f in rf().extract_features(REF_HOME, "/")]
        self.assertEqual(once, again)
        for feature in rf().extract_features(REF_HOME, "/"):
            self.assertEqual(tuple(feature), rf().FEATURE_KEYS)

    def test_how_it_works_uses_only_public_signals(self) -> None:
        fake = FakeBrowser(site())
        signals = rf().how_it_works(REF_HOME, fake.headers(), fake.requests())
        self.assertEqual(tuple(signals), rf().SIGNAL_KEYS)
        self.assertTrue(any("next" in t.lower() for t in signals["technologies"]), signals)
        paths = {(r["method"], r["path"]) for r in signals["requests"]}
        self.assertIn(("POST", "/api/avisos"), paths)
        text = json.dumps(signals)
        for secret in ("page=2", "ana@example.com", "titulo\\\": 1", "sesion=SECRETO"):
            self.assertNotIn(secret, text)

    def test_study_stays_in_scope_and_respects_the_page_limit(self) -> None:
        inventory = self.studied()
        self.assertTrue(all(url.startswith(REF_URL) for url in self.fake.visited), self.fake.visited)
        self.assertIn(REF_URL + "alta", self.fake.visited)
        self.assertTrue(self.fake.opened and self.fake.closed)
        self.assertTrue(self.kinds(inventory)["account"])
        self.authorize()
        limited = FakeBrowser(site())
        rf().study_reference(self.project, RID, limited, max_pages=1)
        self.assertEqual(len(limited.visited), 1)

    def test_study_needs_an_authorized_scope(self) -> None:
        from hefesto.util import HefestoError
        with self.assertRaises(HefestoError):
            rf().study_reference(self.project, RID, FakeBrowser(site()))

    def test_redirects_out_of_scope_are_blocked(self) -> None:
        from hefesto.util import HefestoError
        self.assertIsNotNone(self.authorize())
        fake = FakeBrowser(site(), redirects={REF_URL: "https://otro.example/"})
        with self.assertRaises(HefestoError):
            rf().study_reference(self.project, RID, fake)

    def test_nothing_private_or_copied_is_stored(self) -> None:
        self.studied()
        stored = "".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in Path(self.folder()).rglob("*") if p.is_file())
        self.assertIn("functional-reference.json", [p.name for p in Path(self.folder()).rglob("*")])
        for trap in ("SECRETO-NO-GUARDAR", "NO-COPIAR", PRIVATE_MARK, "<script", "sesion=SECRETO"):
            self.assertNotIn(trap, stored)

    def test_forms_are_never_submitted(self) -> None:
        self.studied()
        self.assertFalse([url for url in self.fake.visited if "/api/" in url or "/sesion" in url or "/buscar" in url])


class ProjectTests(FeatureCase):
    def test_project_features_come_from_the_code(self) -> None:
        inventory = rf().study_project(self.project, RID)
        kinds = self.kinds(inventory)
        self.assertTrue(kinds.get("form"))
        self.assertTrue(kinds.get("search"))
        self.assertTrue((Path(self.folder()) / "functional-project.json").exists())

    def test_project_form_fields_and_validations(self) -> None:
        inventory = rf().study_project(self.project, RID)
        form = next(f for f in inventory["features"] if f["kind"] == "form")
        fields = {field["name"]: field for field in form["details"]["fields"]}
        self.assertTrue({"titulo", "email", "telefono", "descripcion"} <= set(fields))
        self.assertEqual(fields["telefono"]["pattern"], "[0-9]{9}")

    def test_vendored_and_build_folders_are_ignored(self) -> None:
        inventory = rf().study_project(self.project, RID)
        self.assertFalse([f for f in inventory["features"] if "node_modules" in json.dumps(f["evidence"])])

    def test_study_project_never_changes_the_project(self) -> None:
        before = self.snapshot()
        rf().study_project(self.project, RID)
        self.assertEqual(self.snapshot(), before)


class MatrixTests(FeatureCase):
    def matrix(self) -> dict:
        return rf().feature_matrix(self.studied(), rf().study_project(self.project, RID))

    def test_schema_and_states(self) -> None:
        m = self.matrix()
        self.assertEqual(rf().FEATURE_STATES, ("tenemos", "falta", "parcial", "mejor_nuestra", "no_aplica",
                                               "no_verificable"))
        for row in m["rows"]:
            self.assertEqual(tuple(row), rf().ROW_KEYS)
            self.assertIn(row["state"], rf().FEATURE_STATES)
        self.assertEqual(sum(m["counts"].values()), len(m["rows"]))

    def test_missing_features_are_gaps(self) -> None:
        states = {(r["kind"], r["state"]) for r in self.matrix()["rows"]}
        self.assertIn(("filter", "falta"), states)
        self.assertIn(("account", "falta"), states)

    def test_shared_features_are_present(self) -> None:
        rows = {(r["kind"], r["key"]): r["state"] for r in self.matrix()["rows"]}
        self.assertIn("tenemos", [s for (k, _), s in rows.items() if k == "search"])

    def test_better_validation_is_ours(self) -> None:
        forms = [r for r in self.matrix()["rows"] if r["kind"] == "form" and r["reference"] and r["project"]]
        self.assertTrue(forms)
        self.assertEqual(forms[0]["state"], "mejor_nuestra")

    def test_matrix_is_reproducible(self) -> None:
        reference, project = self.studied(), rf().study_project(self.project, RID)
        self.assertEqual(rf().feature_matrix(reference, project), rf().feature_matrix(reference, project))


class ProposalTests(FeatureCase):
    def proposal(self) -> dict[str, Path]:
        self.studied()
        rf().study_project(self.project, RID)
        return rf().propose(self.project, RID)

    def test_improvements_of_the_reference(self) -> None:
        found = rf().improvements(self.studied())
        self.assertTrue(found)
        for item in found:
            self.assertEqual(tuple(item), rf().IMPROVEMENT_KEYS)
            self.assertIn(item["effort"], ("S", "M", "L"))
            self.assertIn(item["risk"], ("bajo", "medio", "alto"))
        titles = " ".join(i["title"].lower() for i in found)
        self.assertIn("valid", titles)

    def test_proposal_documents_gaps_and_improvements(self) -> None:
        paths = self.proposal()
        text = paths["proposal"].read_text(encoding="utf-8")
        for part in ("falta", "mejor", "esfuerzo", "riesgo"):
            self.assertIn(part, text.lower())
        self.assertTrue(json.loads(paths["matrix"].read_text(encoding="utf-8"))["rows"])

    def test_proposal_writes_no_code(self) -> None:
        before = self.snapshot()
        self.proposal()
        self.assertEqual(self.snapshot(), before)

    def test_accept_needs_a_human(self) -> None:
        self.proposal()
        with tty(False, []):
            self.assertFalse(rf().accept(self.project, RID))
        self.assertFalse(rf().accepted(self.project, RID))
        with tty(True, ["s"]):
            self.assertTrue(rf().accept(self.project, RID))
        self.assertTrue(rf().accepted(self.project, RID))


class PlanCheckTests(FeatureCase):
    def accepted_proposal(self) -> None:
        self.studied()
        rf().study_project(self.project, RID)
        rf().propose(self.project, RID)
        with tty(True, ["s"]):
            self.assertTrue(rf().accept(self.project, RID))

    def test_plan_needs_an_accepted_proposal(self) -> None:
        from hefesto.util import HefestoError
        self.studied()
        rf().study_project(self.project, RID)
        rf().propose(self.project, RID)
        with self.assertRaises(HefestoError):
            rf().plan(self.project, RID)

    def test_plan_adds_pending_tasks_with_check_verify(self) -> None:
        self.accepted_proposal()
        ids = rf().plan(self.project, RID)
        self.assertTrue(ids)
        rm = roadmap.load_valid(self.project)
        for task_id in ids:
            task = rm.get(task_id)
            self.assertEqual(task.marker, " ")
            self.assertIn(f"hefesto replica check {RID} --gap", task.verify)

    def test_plan_can_be_limited_to_some_gaps(self) -> None:
        self.accepted_proposal()
        gaps = [r["key"] for r in json.loads((Path(self.folder()) / "functional-matrix.json").read_text(
            encoding="utf-8"))["rows"] if r["state"] == "falta"]
        ids = rf().plan(self.project, RID, gaps=gaps[:1])
        self.assertEqual(len(ids), 1)

    def test_check_reports_open_and_closed_gaps(self) -> None:
        self.accepted_proposal()
        rows = json.loads((Path(self.folder()) / "functional-matrix.json").read_text(encoding="utf-8"))["rows"]
        gap = next(r["key"] for r in rows if r["kind"] == "filter" and r["state"] == "falta")
        code, _ = rf().check(self.project, RID, [gap])
        self.assertNotEqual(code, 0)
        page = self.project / "src/app/avisos/avisos.component.html"
        page.write_text(page.read_text(encoding="utf-8") + '<select name="categoria"><option>Tráfico</option></select>\n',
                        encoding="utf-8")
        code, text = rf().check(self.project, RID, [gap])
        self.assertEqual(code, 0, text)

    def test_unknown_gap_is_an_input_error(self) -> None:
        from hefesto.util import EXIT_INPUT
        self.accepted_proposal()
        code, _ = rf().check(self.project, RID, ["no-existe"])
        self.assertEqual(code, EXIT_INPUT)

    def test_cli(self) -> None:
        self.assertIsNotNone(self.authorize())
        code, out, err = self.cli("replica", "features", RID)
        self.assertNotEqual(code, 0, "sin estudio de la referencia no hay comparación")
        code, _, _ = self.cli("replica", "check", RID, "--gap", "x")
        self.assertNotEqual(code, 0)


class DocsTests(HefestoTestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_docs_explain_the_functional_flow(self) -> None:
        text = (self.ROOT / "docs" / "HEFESTO_ADDON_V2_5.md").read_text(encoding="utf-8")
        for part in ("hefesto replica study", "hefesto replica features", "hefesto replica plan",
                     "hefesto replica check"):
            self.assertIn(part, text)

    def test_craft_anything_decision_is_recorded(self) -> None:
        decisions = (self.ROOT / ".hefesto" / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn("Craft Anything", decisions)
