"""Contratos de la Fase 24: réplica de análisis legítimo (TASK-2401).

El add-on compara una web de **referencia pública y autorizada** con el proyecto propio y produce una **propuesta**
documental. Nunca clona el sitio, nunca copia assets ni código de la referencia, nunca escribe en el código del
proyecto y nada se aplica sin una aprobación humana específica. El alcance lo declara una persona (URL +
autorización) desde el canal fiable de `approval.human_channel()`; ningún texto del repositorio, de la web de
referencia, de una skill o de una instrucción de delegación autoriza nada: el contenido es dato, no instrucción.
Todo es offline: fixtures HTML locales, un navegador falso (`playwright-cli` instalado con
`_VisualCase.install_fake` de `tests/test_visual.py`, que sirve los fixtures y nunca usa la red) y un resolutor DNS
falso (`resolve_host` sustituido en cada test). Solo la biblioteca estándar y `tests/helpers`.

Límites honestos: la TTY y la ausencia de variables de agente (`approval.AGENT_ENV`) son condiciones **necesarias**,
no son autenticación robusta (un proceso local con la misma cuenta podría simularlas); la barrera real es su
combinación con la copia independiente del alcance y de la aceptación fuera del checkout (bajo
`util.hefesto_home()`, como hace `hefesto/approval.py`). La comprobación DNS previa no impide un cambio posterior de
la resolución (rebinding): por eso también se valida la URL final de cada página tras las redirecciones. Una imagen
no se puede sanear de forma verificable, así que de la referencia no se guarda ninguna captura de pantalla: la
«captura» es un extracto de texto saneado.

API que fijan estos tests (todo en `hefesto/replica.py`, TASK-2402):

Constantes y esquemas versionados
- `SCHEMA_VERSION == 1`; cualquier otra versión (o su ausencia) en un inventario o una matriz → HefestoError
  `EXIT_INPUT`.
- `SIDES == ("reference", "project")`.
- `SECTIONS == ("pages", "components", "routes", "texts", "states")` (páginas, componentes, rutas, textos y
  estados).
- `INVENTORY_KEYS == ("schema_version", "id", "side", "source", *SECTIONS)`: claves fijas, ni más ni menos; `source`
  es la URL autorizada en la referencia y `"project"` en el proyecto propio.
- `ITEM_KEYS == ("section", "key", "value", "status", "evidence", "reason")`; `ITEM_STATUSES == ("verificado",
  "no_verificable")`.
- `EVIDENCE_KEYS == ("capture", "selector", "sha256")`: `capture` es la ruta POSIX relativa a `replica_dir` de la
  captura de texto saneada (`captures/*.txt`, una línea por elemento con su selector y su texto saneado, nunca HTML),
  `selector` el selector del elemento y `sha256 == item_hash(section, key, value)`.
- `MATRIX_STATES == ("presente", "parcial", "ausente", "diferente", "no_aplica", "no_verificable")`: exactamente
  seis estados.
- `ROW_KEYS == ("section", "key", "state", "reference", "project", "reference_sha256", "project_sha256")`.
- `MATRIX_KEYS == ("schema_version", "id", "reference", "project", "rows", "counts")`; `counts` tiene los seis
  estados (también con 0).
- `AUTH_KEYS == ("id", "project", "url", "host", "channel", "authorized_at", "sha256")`; `channel == "tty"`.
- `ACCEPT_KEYS == ("id", "project", "proposal_sha256", "matrix_sha256", "channel", "accepted_at")`.
- `ALLOWED_SCHEMES == ("http", "https")`.

Rutas
- `replica_dir(project, replica_id) -> Path`: `<project>/.hefesto/replica/<id>/`; el id cumple
  `[a-z0-9][a-z0-9-]*` (si no, `EXIT_INPUT`). Ahí y solo ahí se escribe: `scope.json`, `reference.json`,
  `project.json`, `matrix.json`, `proposal.md`, `acceptance.json` y `captures/*.txt`.

Alcance: solo análisis público autorizado
- `resolve_host(host) -> list[str]`: direcciones IP del host (los tests lo sustituyen; sin resolución → `[]`).
- `check_url(url) -> str`: URL normalizada (host en minúsculas). `EXIT_INPUT` si el esquema no está en
  `ALLOWED_SCHEMES` (`file:`, `ftp:`, `javascript:`, `data:`, relativa o vacía), si lleva credenciales
  (`usuario@` o `usuario:clave@`; el mensaje no repite la clave), si el host es `localhost`, `*.localhost`,
  `*.local` o `*.internal`, si es una IP (también decimal o hexadecimal) privada, loopback, link-local, reservada o
  sin especificar, o si `resolve_host` no devuelve nada o devuelve alguna de esas IP (fail closed).
- `in_scope(authorized, url) -> bool`: mismo esquema, mismo host exacto, mismo puerto y ruta bajo la ruta autorizada
  (por segmentos: `/docs/` no cubre `/docsx`). Un subdominio, otro host o el paso de https a http quedan fuera.
- `authorize(project, replica_id, url) -> dict | None`: valida la URL antes de preguntar; sin
  `approval.human_channel()` → HefestoError `EXIT_BLOCKED` sin guardar nada; muestra el alcance y pregunta con
  `util.confirm` (por defecto «no»); si la persona dice que no, `None`. Guarda `scope.json` con `AUTH_KEYS` y una
  copia independiente fuera del checkout. No hay `yes`, `force` ni `--yes`.
- `load_scope(project, replica_id) -> dict | None`: el alcance solo vale si su huella, su `project`
  (`approval.project_id`) y la copia independiente coinciden; un `scope.json` escrito a mano o copiado de otro
  proyecto → `None`.

Inventarios: evidencia o «no verificable», nunca inventado
- `item_hash(section, key, value) -> str`: sha256 de `json.dumps([section, key, value], ensure_ascii=False,
  separators=(",", ":"))` en UTF-8.
- `sanitize(text) -> str`: pasa por `guard.redact` y además elimina emails, cookies (`Cookie:`, `Set-Cookie:`,
  `document.cookie`), `Bearer` y parámetros de consulta con tokens o claves; colapsa los espacios.
- `capture_reference(project, replica_id, routes=()) -> dict`: exige `load_scope` (si no, `EXIT_BLOCKED` sin lanzar
  ningún proceso); `routes` son rutas relativas a la URL autorizada y cada una pasa `check_url` e `in_scope` antes de
  lanzar nada (si no, `EXIT_INPUT`). Sin `playwright-cli` en el PATH → `EXIT_INPUT` que lo nombra. Usa
  `playwright-cli` sin shell, cada llamada con `-s=hefesto-replica-<id>`: `open`, y por página `goto <url>` y
  `--raw eval <sonda>` (JSON `{"url": URL final tras redirecciones, "html": DOM}`); la última llamada es siempre
  `close`, también tras un error. Nunca `screenshot`. Nunca sigue enlaces por su cuenta: solo visita la URL
  autorizada y `routes`. Una URL final fuera del alcance (redirección que sale) → `EXIT_BLOCKED` («redirección») sin
  escribir el inventario. Un `goto` fallido deja esa página como `no_verificable` (`value` y `evidence` `None`,
  `reason` no vacío) y sigue. Escribe `reference.json` y las capturas de texto; el HTML, los scripts y los estilos
  se analizan en memoria y nunca se guardan.
- `inventory_project(project, replica_id, pages) -> dict`: `pages` es `{ruta: fichero}` con ficheros HTML del
  proyecto (rutas POSIX relativas; absolutas, con `..` o fuera del proyecto → `EXIT_INPUT`); solo los lee. Un fichero
  que no existe deja la página `no_verificable`. Escribe `project.json`.
- Reglas de extracción (iguales en los dos lados): `pages` → clave la ruta, valor el `<title>`; `routes` → clave la
  ruta (sin consulta ni fragmento) de cada `<a href>` dentro del alcance, valor su texto (los enlaces fuera del
  alcance se ignoran); `components` → clave `<ruta>#<data-component>`, valor su texto visible; `texts` → clave
  `<ruta>#<etiqueta>[n]` para `h1`, `h2`, `h3` y `button` (n desde 1 en orden del documento), valor su texto;
  `states` → clave `<ruta>#<data-state>`, valor su texto. Todo valor pasa por `sanitize`; nunca el contenido de
  `<script>` ni `<style>`.
- `validate_inventory(data) -> dict`: `EXIT_INPUT` si la versión es desconocida, faltan o sobran claves, el lado no
  está en `SIDES`, un elemento `verificado` no tiene evidencia completa o tiene una `capture` absoluta o con `..`, o
  uno `no_verificable` tiene `value` o `evidence`.

Matriz de comparación reproducible
- `compare(reference, project) -> dict`: valida ambos lados (lados correctos y mismo `id`, si no `EXIT_INPUT`); una
  fila por `(section, key)` de la unión: `no_verificable` si algún lado lo es; `ausente` si solo está en la
  referencia; `no_aplica` si solo está en el proyecto; `presente` si los valores normalizados (minúsculas y espacios
  colapsados) coinciden; `parcial` si uno contiene al otro; `diferente` en otro caso. Filas ordenadas por el orden de
  `SECTIONS` y por clave; sin marcas de tiempo.
- `matrix_json(matrix) -> str`: JSON con `sort_keys`, `indent=2`, `ensure_ascii=False` y salto final; mismo input
  (en cualquier orden) → mismos bytes.

Propuesta: solo un documento en `.hefesto/`
- `propose(project, replica_id) -> dict` (`{"proposal": Path, "matrix": Path}`): lee y valida `reference.json` y
  `project.json` (si falta alguno o es inválido, `EXIT_INPUT` sin escribir nada), escribe `matrix.json`
  (`matrix_json`) y `proposal.md`, que enumera las filas `ausente`, `parcial` y `diferente`, dice «sin aplicar» y
  nombra `hefesto replica accept`. No contiene código de la referencia (ni `<script`, ni `<style`) y es
  reproducible. No toca ningún fichero fuera de `replica_dir`.
- `accept(project, replica_id) -> dict | None`: sin `approval.human_channel()` → `EXIT_BLOCKED`; muestra las huellas
  y pregunta con `util.confirm` (por defecto «no»); guarda `acceptance.json` con `ACCEPT_KEYS` y una copia
  independiente. Aceptar **no escribe código**: habilita que el Core planifique tareas con `/hefesto-plan`.
- `accepted(project, replica_id) -> bool`: vigente solo si las huellas de `proposal.md` y `matrix.json` siguen
  coincidiendo, el proyecto es este y la copia independiente coincide.
- El módulo nunca descarga ni clona: sin `urlopen`, `urllib.request`, `http.client` ni `requests`, nunca con shell
  y sin procesos `git clone`, `wget` o `curl`.

CLI (`hefesto/cli.py`, TASK-2402)
- `hefesto replica authorize ID URL`: solo humanos con TTY; URL inválida → `EXIT_INPUT`; agente o sin TTY →
  `EXIT_BLOCKED`.
- `hefesto replica capture ID [--route RUTA ...]`.
- `hefesto replica inventory ID --page RUTA=FICHERO ...`.
- `hefesto replica propose ID`: imprime la ruta de `proposal.md` y los recuentos.
- `hefesto replica accept ID`: solo humanos con TTY.
- No existen `hefesto replica apply` ni `hefesto replica clone`, ni la opción `--yes`.

Solo `ContractDefinitionTests` pasa antes de implementar; las demás clases fallan porque aún no existe
`hefesto.replica`.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import random
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import guard
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError
from tests.helpers import HefestoTestCase, git, tty
from tests.test_visual import _VisualCase

SCHEMA_VERSION = 1
SIDES = ("reference", "project")
SECTIONS = ("pages", "components", "routes", "texts", "states")
INVENTORY_KEYS = ("schema_version", "id", "side", "source", *SECTIONS)
ITEM_KEYS = ("section", "key", "value", "status", "evidence", "reason")
ITEM_STATUSES = ("verificado", "no_verificable")
EVIDENCE_KEYS = ("capture", "selector", "sha256")
MATRIX_STATES = ("presente", "parcial", "ausente", "diferente", "no_aplica", "no_verificable")
ROW_KEYS = ("section", "key", "state", "reference", "project", "reference_sha256", "project_sha256")
MATRIX_KEYS = ("schema_version", "id", "reference", "project", "rows", "counts")
AUTH_KEYS = ("id", "project", "url", "host", "channel", "authorized_at", "sha256")
ACCEPT_KEYS = ("id", "project", "proposal_sha256", "matrix_sha256", "channel", "accepted_at")
ALLOWED_FILES = {"scope.json", "reference.json", "project.json", "matrix.json", "proposal.md", "acceptance.json"}

RID = "portal-avisos"
# Nunca hay una URL literal en este fichero: se construyen con el esquema separado.
HTTPS = "https:" + "//"
HTTP = "http:" + "//"
REF_HOST = "referencia.example"
REF_URL = HTTPS + REF_HOST + "/"
REF_AVISOS = REF_URL + "avisos"
OTHER_URL = HTTPS + "otra.example/"
FAKE_DNS = {REF_HOST: ["93.184.216.34"], "otra.example": ["93.184.216.35"],
            "interno.example": ["10.0.0.5"], "mixto.example": ["93.184.216.36", "127.0.0.1"]}

GH_TOKEN = "ghp_" + "A1b2" * 9  # se construye para no dejar un token literal en el repositorio
AWS_KEY = "AKIA" + "Q" * 16
EMAIL = "soporte@" + REF_HOST
SECRET_MARKERS = (GH_TOKEN, EMAIL, "SECRETO-COOKIE", "SECRETO-QUERY")
CODE_MARKERS = ("CODIGO-REFERENCIA-UNICO", "CSS-REFERENCIA-UNICO", "<script", "<style")

REF_HTML = (
    "<!doctype html><html><head><title>Avisos vecinales</title>"
    "<style>.hero { color: #123 } /* CSS-REFERENCIA-UNICO */</style>"
    '<script>const clave = "CODIGO-REFERENCIA-UNICO"; document.cookie = "sesion=SECRETO-COOKIE";</script>'
    "</head><body>"
    '<header data-component="cabecera"><strong>Barrio</strong></header>'
    '<nav data-component="menu"><a href="/avisos?token=SECRETO-QUERY">Avisos</a> '
    f'<a href="/contacto">Contacto</a> <a href="{OTHER_URL}fuera">Fuera</a></nav>'
    "<main><h1>Buscar avisos por fecha</h1>"
    '<form data-component="buscador"><button>Buscar</button></form>'
    '<section data-component="mapa"><h2>Mapa de incidencias</h2></section>'
    f'<p role="alert" data-state="error">Error: escribe a {EMAIL}; token {GH_TOKEN}; '
    "Set-Cookie: sesion=SECRETO-COOKIE</p>"
    '<p data-state="vacio">No hay avisos</p>'
    "</main></body></html>\n"
)

PROJECT_PAGE = "web/index.html"
PROJECT_HTML = (
    "<!doctype html><html><head><title>Avisos vecinales</title></head><body>"
    '<header data-component="cabecera"><strong>Barrio</strong></header>'
    '<nav data-component="menu"><a href="/avisos">Avisos</a></nav>'
    "<main><h1>Buscar avisos</h1>"
    '<form data-component="buscador"><button>Enviar</button></form>'
    '<p data-state="vacio">No hay avisos</p>'
    "</main>"
    '<footer data-component="pie">Ayuntamiento</footer>'
    "</body></html>\n"
)

# Lo que deben extraer las reglas de los fixtures (None = no verificable). El estado de error se comprueba aparte
# porque su texto saneado depende de los marcadores de `sanitize`.
REF_VALUES: dict[tuple[str, str], str | None] = {
    ("pages", "/"): "Avisos vecinales",
    ("pages", "/avisos"): None,  # el navegador falso no puede cargarla
    ("components", "/#cabecera"): "Barrio",
    ("components", "/#menu"): "Avisos Contacto Fuera",
    ("components", "/#buscador"): "Buscar",
    ("components", "/#mapa"): "Mapa de incidencias",
    ("routes", "/avisos"): "Avisos",
    ("routes", "/contacto"): "Contacto",
    ("texts", "/#h1[1]"): "Buscar avisos por fecha",
    ("texts", "/#h2[1]"): "Mapa de incidencias",
    ("texts", "/#button[1]"): "Buscar",
    ("states", "/#vacio"): "No hay avisos",
    ("states", "/#error"): "Error: escribe a [REDACTED]",
}
PROJECT_VALUES: dict[tuple[str, str], str | None] = {
    ("pages", "/"): "Avisos vecinales",
    ("components", "/#cabecera"): "Barrio",
    ("components", "/#menu"): "Avisos",
    ("components", "/#buscador"): "Enviar",
    ("components", "/#pie"): "Ayuntamiento",
    ("routes", "/avisos"): "Avisos",
    ("texts", "/#h1[1]"): "Buscar avisos",
    ("texts", "/#button[1]"): "Enviar",
    ("states", "/#vacio"): "No hay avisos",
}
EXPECTED_ROWS: dict[tuple[str, str], str] = {
    ("pages", "/"): "presente",
    ("pages", "/avisos"): "no_verificable",
    ("components", "/#buscador"): "diferente",
    ("components", "/#cabecera"): "presente",
    ("components", "/#mapa"): "ausente",
    ("components", "/#menu"): "parcial",
    ("components", "/#pie"): "no_aplica",
    ("routes", "/avisos"): "presente",
    ("routes", "/contacto"): "ausente",
    ("texts", "/#button[1]"): "diferente",
    ("texts", "/#h1[1]"): "parcial",
    ("texts", "/#h2[1]"): "ausente",
    ("states", "/#error"): "ausente",
    ("states", "/#vacio"): "presente",
}

BAD_SCHEMES = ("file:" + "///etc/passwd", "ftp:" + "//" + REF_HOST + "/", "javascript:alert(1)",
               "data:text/html,hola", "/avisos", "", REF_HOST + "/avisos")
PRIVATE_HOSTS = ("127.0.0.1", "localhost", "[::1]", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                 "[fe80::1]", "0.0.0.0", "2130706433", "0x7f000001", "intranet.local", "panel.localhost",
                 "api.internal", "interno.example", "mixto.example", "desconocido.example")
CREDENTIAL_URLS = (HTTPS + "ana:clave-secreta@" + REF_HOST + "/", HTTPS + "ana@" + REF_HOST + "/")

# Navegador falso: sirve fixtures locales por URL y nunca toca la red (misma forma que FAKE_PLAYWRIGHT).
FAKE_BROWSER = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_RB_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
current = log + ".url"
cmds = [a for a in args if not a.startswith("-")]
cmd = cmds[0] if cmds else ""
pages = json.loads(os.environ.get("FAKE_RB_PAGES", "{}"))
if cmd == "goto":
    url = cmds[1] if len(cmds) > 1 else ""
    hops = 0
    while url in pages and pages[url].get("redirect") and hops < 5:
        url = pages[url]["redirect"]
        hops += 1
    page = pages.get(url)
    if page is None or page.get("fail"):
        sys.stderr.write("sin red: página desconocida o fallida\n")
        sys.exit(1)
    with open(current, "w", encoding="utf-8") as fh:
        fh.write(url)
elif cmd == "eval":
    url = open(current, encoding="utf-8").read() if os.path.exists(current) else ""
    page = pages.get(url) or {}
    html = open(page["file"], encoding="utf-8").read() if page.get("file") else ""
    print(json.dumps({"url": url, "html": html}))
'''


def _replica() -> Any:
    from hefesto import replica  # import diferido: el contrato compila sin la implementación

    return replica


def _fake_resolve(host: str) -> list[str]:
    return list(FAKE_DNS.get(host, []))


class _Attrs(HTMLParser):
    """Recoge los `data-component` y `data-state` de un fixture para comprobar que está bien formado."""

    def __init__(self) -> None:
        super().__init__()
        self.components: list[str] = []
        self.states: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("data-component"):
            self.components.append(str(values["data-component"]))
        if values.get("data-state"):
            self.states.append(str(values["data-state"]))
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))


def _attrs(html: str) -> _Attrs:
    parser = _Attrs()
    parser.feed(html)
    return parser


def _hash(section: str, key: str, value: str) -> str:
    raw = json.dumps([section, key, value], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_inventory(side: str, values: dict[tuple[str, str], str | None], rid: str = RID,
                   version: Any = SCHEMA_VERSION) -> dict[str, Any]:
    """Inventario sintético con la forma del contrato (evidencia completa o `no_verificable`)."""
    data: dict[str, Any] = {"schema_version": version, "id": rid, "side": side,
                            "source": REF_URL if side == "reference" else "project"}
    for section in SECTIONS:
        data[section] = []
    for (section, key), value in values.items():
        if value is None:
            item = {"section": section, "key": key, "value": None, "status": "no_verificable", "evidence": None,
                    "reason": "no se pudo cargar"}
        else:
            item = {"section": section, "key": key, "value": value, "status": "verificado",
                    "evidence": {"capture": f"captures/{side}-root.txt", "selector": f"sel {key}",
                                 "sha256": _hash(section, key, value)},
                    "reason": ""}
        data[section].append(item)
    return data


def rows_by_key(matrix: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["section"], row["key"]): row for row in matrix["rows"]}


# --- Definición del contrato (pasa antes de implementar) --------------------------------------------------------


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {"ScopeTests": 9, "InventoryTests": 8, "ComparisonTests": 7, "ProposalTests": 7,
                        "CliTests": 4}
    API = ("hefesto/replica.py", "SCHEMA_VERSION", "SIDES", "reference", "project", "SECTIONS", "pages",
           "components", "routes", "texts", "states", "INVENTORY_KEYS", "ITEM_KEYS", "ITEM_STATUSES",
           "verificado", "no_verificable", "EVIDENCE_KEYS", "capture", "selector", "sha256", "MATRIX_STATES",
           "presente", "parcial", "ausente", "diferente", "no_aplica", "ROW_KEYS", "MATRIX_KEYS", "counts",
           "AUTH_KEYS", "ACCEPT_KEYS", "ALLOWED_SCHEMES", "replica_dir(project, replica_id)",
           ".hefesto/replica/<id>/", "resolve_host(host)", "check_url(url)", "in_scope(authorized, url)",
           "authorize(project, replica_id, url)", "approval.human_channel()", "approval.AGENT_ENV",
           "util.confirm", "util.hefesto_home()", "load_scope(project, replica_id)", "approval.project_id",
           "item_hash(section, key, value)", "sanitize(text)", "guard.redact",
           "capture_reference(project, replica_id, routes=())", "playwright-cli", "-s=hefesto-replica-<id>",
           "goto", "--raw eval", "close", "screenshot", "inventory_project(project, replica_id, pages)",
           "validate_inventory(data)", "compare(reference, project)", "matrix_json(matrix)",
           "propose(project, replica_id)", "proposal.md", "matrix.json", "scope.json", "reference.json",
           "project.json", "acceptance.json", "captures/*.txt", "accept(project, replica_id)",
           "accepted(project, replica_id)", "/hefesto-plan", "EXIT_INPUT", "EXIT_BLOCKED",
           "hefesto replica authorize", "hefesto replica capture", "hefesto replica inventory",
           "hefesto replica propose", "hefesto replica accept", "hefesto replica apply", "hefesto replica clone",
           "--yes", "_VisualCase.install_fake", "sin aplicar")
    API_NAMES = {"SCHEMA_VERSION", "SIDES", "SECTIONS", "INVENTORY_KEYS", "ITEM_KEYS", "ITEM_STATUSES",
                 "EVIDENCE_KEYS", "MATRIX_STATES", "ROW_KEYS", "MATRIX_KEYS", "AUTH_KEYS", "ACCEPT_KEYS",
                 "ALLOWED_SCHEMES", "replica_dir", "resolve_host", "check_url", "in_scope", "authorize",
                 "load_scope", "item_hash", "sanitize", "capture_reference", "inventory_project",
                 "validate_inventory", "compare", "matrix_json", "propose", "accept", "accepted"}

    def source(self) -> str:
        return Path(__file__).read_text(encoding="utf-8")

    def tree(self) -> ast.Module:
        return ast.parse(self.source())

    def test_docstring_names_the_whole_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API:
            with self.subTest(name=name):
                self.assertIn(name, doc)

    def test_docstring_is_honest_about_its_limits(self) -> None:
        doc = " ".join((ast.get_docstring(self.tree()) or "").split())
        self.assertIn("no son autenticación robusta", doc)
        self.assertIn("condiciones **necesarias**", doc)
        self.assertIn("rebinding", doc)
        self.assertIn("Nunca clona", doc)
        self.assertNotRegex(doc.lower(), r"(tty|hefesto_role|dns)[^.]*\b(garantiza|demuestra|prueba)\b")

    def test_every_behaviour_has_its_tests(self) -> None:
        classes = {n.name: n for n in self.tree().body if isinstance(n, ast.ClassDef)}
        for name, minimum in self.REQUIRED_CLASSES.items():
            with self.subTest(cls=name):
                self.assertIn(name, classes)
                tests = [f for f in classes[name].body if isinstance(f, ast.FunctionDef)
                         and f.name.startswith("test_")]
                self.assertGreaterEqual(len(tests), minimum)

    def test_tests_use_nothing_outside_the_documented_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API_NAMES:
            with self.subTest(name=name):
                self.assertIn("`" + name, doc)
        used: set[str] = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "replica":
                if not node.attr.startswith("__"):
                    used.add(node.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "object"
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "replica"
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                used.add(str(node.args[1].value))
        used |= set(re.findall(r"hefesto\.replica\.([A-Za-z_]+)", self.source()))
        self.assertTrue(used, "los tests de comportamiento deben usar la API")
        self.assertEqual(used - self.API_NAMES, set())

    def test_implementation_is_imported_lazily(self) -> None:
        for node in self.tree().body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "hefesto.replica")
                if node.module == "hefesto":
                    self.assertNotIn("replica", [a.name for a in node.names])
            if isinstance(node, ast.Import):
                self.assertNotIn("hefesto.replica", [a.name for a in node.names])

    def test_contract_is_offline_standard_library_and_never_uses_a_shell(self) -> None:
        source = self.source()
        self.assertNotIn("shell=" + "True", source)
        for module in ("requests", "urllib.request", "socket", "http.client"):
            self.assertNotIn("import " + module, source)
        self.assertNotRegex(source, r"https?:" + r"//")
        allowed = set(sys.stdlib_module_names) | {"hefesto", "tests", "__future__"}
        for node in ast.walk(self.tree()):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                with self.subTest(module=name):
                    self.assertIn(name.split(".")[0], allowed)
        browser = ast.parse(FAKE_BROWSER)
        imported = {a.name for n in ast.walk(browser) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(imported, {"json", "os", "sys"})  # el navegador falso nunca abre la red

    def test_html_fixtures_are_well_formed_and_carry_the_traps(self) -> None:
        ref, own = _attrs(REF_HTML), _attrs(PROJECT_HTML)
        self.assertEqual(ref.components, ["cabecera", "menu", "buscador", "mapa"])
        self.assertEqual(own.components, ["cabecera", "menu", "buscador", "pie"])
        self.assertEqual(ref.states, ["error", "vacio"])
        self.assertEqual(own.states, ["vacio"])
        self.assertTrue(any(link.startswith(OTHER_URL) for link in ref.links))  # enlace fuera del alcance
        for marker in SECRET_MARKERS + CODE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, REF_HTML)  # sin trampa, el test de saneado no probaría nada
        self.assertTrue(guard.find_secrets(GH_TOKEN))
        self.assertTrue(guard.find_secrets(AWS_KEY))

    def test_expected_values_and_rows_are_coherent(self) -> None:
        self.assertEqual(len(MATRIX_STATES), 6)
        self.assertEqual(len(set(MATRIX_STATES)), 6)
        self.assertEqual(set(EXPECTED_ROWS.values()), set(MATRIX_STATES))  # el fixture ejercita los seis
        self.assertEqual(set(EXPECTED_ROWS), set(REF_VALUES) | set(PROJECT_VALUES))
        self.assertEqual({s for s, _k in EXPECTED_ROWS}, set(SECTIONS))
        self.assertEqual(INVENTORY_KEYS[:4], ("schema_version", "id", "side", "source"))
        for (section, key), state in EXPECTED_ROWS.items():
            with self.subTest(section=section, key=key):
                ref, own = REF_VALUES.get((section, key), "-"), PROJECT_VALUES.get((section, key), "-")
                if state == "no_verificable":
                    self.assertTrue(ref is None or own is None)
                elif state == "ausente":
                    self.assertEqual(own, "-")
                elif state == "no_aplica":
                    self.assertEqual(ref, "-")
        for side, values in (("reference", REF_VALUES), ("project", PROJECT_VALUES)):
            inventory = make_inventory(side, values)
            self.assertEqual(tuple(inventory), INVENTORY_KEYS)
            for section in SECTIONS:
                for item in inventory[section]:
                    self.assertEqual(tuple(item), ITEM_KEYS)
                    self.assertIn(item["status"], ITEM_STATUSES)
                    if item["evidence"]:
                        self.assertEqual(tuple(item["evidence"]), EVIDENCE_KEYS)

    def test_url_fixtures_are_built_without_literals(self) -> None:
        self.assertTrue(REF_URL.startswith(HTTPS) and REF_URL.endswith("/"))
        self.assertTrue(HTTP.startswith("http:"))
        for host in PRIVATE_HOSTS:
            with self.subTest(host=host):  # o no resuelve, o resuelve (al menos) a una IP privada
                ips = FAKE_DNS.get(host)
                self.assertTrue(ips is None or any(ip.startswith(("10.", "127.")) for ip in ips))
        self.assertNotIn(REF_HOST, PRIVATE_HOSTS)
        self.assertTrue(all("@" in url for url in CREDENTIAL_URLS))

    def test_behaviour_tests_fail_for_the_right_reason(self) -> None:
        # Sin implementación, el fallo es un ImportError que nombra `replica` (y no un error del contrato).
        try:
            _replica()
        except ImportError as exc:
            self.assertIn("replica", str(exc))


# --- Base común de los tests de comportamiento (TASK-2402) ------------------------------------------------------


class _ReplicaCase(_VisualCase):
    def setUp(self) -> None:
        super().setUp()
        replica = _replica()
        patcher = mock.patch.object(replica, "resolve_host", side_effect=_fake_resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.init_project()
        page = self.project / PROJECT_PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PROJECT_HTML, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "portada propia")
        self.ref_file = self.tmp / "fixtures-referencia" / "index.html"
        self.ref_file.parent.mkdir(parents=True, exist_ok=True)
        self.ref_file.write_text(REF_HTML, encoding="utf-8")

    def browser(self, pages: dict[str, dict[str, Any]] | None = None) -> Path:
        self.install_fake("playwright-cli", FAKE_BROWSER)
        log = self.tmp / "browser.jsonl"
        if pages is None:
            pages = {REF_URL: {"file": str(self.ref_file)}, REF_AVISOS: {"fail": True}}
        os.environ["FAKE_RB_LOG"] = str(log)
        os.environ["FAKE_RB_PAGES"] = json.dumps(pages)
        return log

    def authorize(self, url: str = REF_URL, rid: str = RID, answer: str = "s",
                  project: Path | None = None) -> Any:
        replica = _replica()
        with tty(True, [answer]):
            return replica.authorize(project or self.project, rid, url)

    def captured(self) -> dict[str, Any]:
        replica = _replica()
        self.browser()
        self.assertIsNotNone(self.authorize())
        return replica.capture_reference(self.project, RID, routes=("/avisos",))

    def project_inventory(self) -> dict[str, Any]:
        replica = _replica()
        return replica.inventory_project(self.project, RID, {"/": PROJECT_PAGE})

    def folder(self) -> Path:
        replica = _replica()
        return Path(replica.replica_dir(self.project, RID))

    def stored_files(self) -> list[Path]:
        return sorted(p for p in self.folder().rglob("*") if p.is_file()) if self.folder().exists() else []

    def snapshot(self) -> dict[str, str]:
        """Huella de cada fichero del proyecto salvo `.git/` y `.hefesto/replica/`."""
        result: dict[str, str] = {}
        for path in self.project.rglob("*"):
            rel = path.relative_to(self.project).as_posix()
            if path.is_file() and not rel.startswith((".git/", ".hefesto/replica/")):
                result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def values(self, inventory: dict[str, Any]) -> dict[tuple[str, str], Any]:
        return {(item["section"], item["key"]): item["value"] for s in SECTIONS for item in inventory[s]}

    def cli_code(self, *args: str, **kw: Any) -> tuple[int, str, str]:
        try:
            return self.cli(*args, **kw)
        except SystemExit as exc:  # argparse rechaza subcomandos u opciones desconocidas
            return int(exc.code or 0) or 2, "", ""


def _agent_env(var: str) -> Any:
    return mock.patch.dict("os.environ", {var: "1"})


# --- Alcance: análisis público autorizado -----------------------------------------------------------------------


class ScopeTests(_ReplicaCase):
    def test_constants_and_paths(self) -> None:
        replica = _replica()
        self.assertEqual(replica.SCHEMA_VERSION, SCHEMA_VERSION)
        self.assertEqual(tuple(replica.SIDES), SIDES)
        self.assertEqual(tuple(replica.SECTIONS), SECTIONS)
        self.assertEqual(tuple(replica.INVENTORY_KEYS), INVENTORY_KEYS)
        self.assertEqual(tuple(replica.ITEM_KEYS), ITEM_KEYS)
        self.assertEqual(tuple(replica.ITEM_STATUSES), ITEM_STATUSES)
        self.assertEqual(tuple(replica.EVIDENCE_KEYS), EVIDENCE_KEYS)
        self.assertEqual(tuple(replica.MATRIX_STATES), MATRIX_STATES)
        self.assertEqual(tuple(replica.ROW_KEYS), ROW_KEYS)
        self.assertEqual(tuple(replica.MATRIX_KEYS), MATRIX_KEYS)
        self.assertEqual(tuple(replica.AUTH_KEYS), AUTH_KEYS)
        self.assertEqual(tuple(replica.ACCEPT_KEYS), ACCEPT_KEYS)
        self.assertEqual(tuple(replica.ALLOWED_SCHEMES), ("http", "https"))
        self.assertEqual(Path(replica.replica_dir(self.project, RID)),
                         self.project / ".hefesto" / "replica" / RID)
        for bad in ("../fuera", "Con Espacios", "", "-guion", "a/b"):
            with self.subTest(rid=bad), self.assertRaises(HefestoError) as ctx:
                replica.replica_dir(self.project, bad)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_public_http_and_https_urls_are_accepted_and_normalized(self) -> None:
        replica = _replica()
        self.assertEqual(replica.check_url(REF_URL), REF_URL)
        self.assertEqual(replica.check_url(HTTPS + "REFERENCIA.example/"), REF_URL)
        self.assertEqual(replica.check_url(HTTP + REF_HOST + "/avisos"), HTTP + REF_HOST + "/avisos")

    def test_non_http_schemes_are_rejected(self) -> None:
        replica = _replica()
        for url in BAD_SCHEMES:
            with self.subTest(url=url), self.assertRaises(HefestoError) as ctx:
                replica.check_url(url)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_private_loopback_link_local_and_unresolved_hosts_are_rejected(self) -> None:
        replica = _replica()
        for host in PRIVATE_HOSTS:
            with self.subTest(host=host), self.assertRaises(HefestoError) as ctx:
                replica.check_url(HTTPS + host + "/")
            self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_credentials_in_the_url_are_rejected_without_echoing_them(self) -> None:
        replica = _replica()
        for url in CREDENTIAL_URLS:
            with self.subTest(url=url), self.assertRaises(HefestoError) as ctx:
                replica.check_url(url)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
            self.assertNotIn("clave-secreta", str(ctx.exception))

    def test_scope_is_same_scheme_host_port_and_path_prefix(self) -> None:
        replica = _replica()
        self.assertTrue(replica.in_scope(REF_URL, REF_AVISOS))
        self.assertTrue(replica.in_scope(REF_URL, REF_URL))
        docs = REF_URL + "docs/"
        self.assertTrue(replica.in_scope(docs, docs + "guia"))
        for url in (REF_URL + "docsx", OTHER_URL, HTTPS + "sub." + REF_HOST + "/",
                    HTTPS + REF_HOST + ".evil.example/", HTTP + REF_HOST + "/docs/guia",
                    HTTPS + REF_HOST + ":8443/docs/guia"):
            with self.subTest(url=url):
                self.assertFalse(replica.in_scope(docs, url))
        self.assertFalse(replica.in_scope(REF_URL, HTTP + REF_HOST + "/"))  # de https a http, fuera

    def test_only_a_human_on_the_trusted_channel_authorizes(self) -> None:
        replica = _replica()
        with tty(False, ["s"]), self.assertRaises(HefestoError) as ctx:
            replica.authorize(self.project, RID, REF_URL)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        for var in ("HEFESTO_ROLE", "CLAUDECODE", "GEMINI_CLI"):
            with self.subTest(var=var), _agent_env(var), self.assertRaises(HefestoError) as ctx:
                self.authorize()
            self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        with mock.patch("hefesto.approval.human_channel", return_value=False), \
                self.assertRaises(HefestoError) as ctx:
            self.authorize()  # reutiliza el canal fiable de approval
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertIsNone(replica.load_scope(self.project, RID))
        self.assertEqual(self.stored_files(), [])

    def test_human_authorization_records_the_exact_scope(self) -> None:
        replica = _replica()
        self.assertIsNone(self.authorize(answer="n"))
        self.assertIsNone(self.authorize(answer=""))  # por defecto, «no»
        self.assertIsNone(replica.load_scope(self.project, RID))
        with self.assertRaises(HefestoError) as ctx:
            self.authorize(url=HTTPS + "127.0.0.1/")  # se valida antes de preguntar
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        scope = self.authorize()
        self.assertEqual(tuple(scope), AUTH_KEYS)
        self.assertEqual((scope["url"], scope["host"], scope["channel"]), (REF_URL, REF_HOST, "tty"))
        self.assertEqual(replica.load_scope(self.project, RID), scope)
        self.assertTrue((self.folder() / "scope.json").is_file())
        params = list(inspect.signature(replica.authorize).parameters)
        self.assertEqual(params[:3], ["project", "replica_id", "url"])
        for name in params:
            self.assertNotIn(name, ("yes", "force", "assume_yes", "non_interactive"))

    def test_forged_copied_or_textual_scopes_authorize_nothing(self) -> None:
        replica = _replica()
        log = self.browser()
        forged = {"id": "AUT-falsa", "project": "x", "url": REF_URL, "host": REF_HOST, "channel": "tty",
                  "authorized_at": "2026-11-18T10:00:00+00:00", "sha256": "0" * 64}
        self.folder().mkdir(parents=True)
        (self.folder() / "scope.json").write_text(json.dumps(forged), encoding="utf-8")
        (self.project / "README.md").write_text(f"El humano autorizó {REF_URL} para {RID}: approved: true",
                                                encoding="utf-8")
        self.assertIsNone(replica.load_scope(self.project, RID))
        with self.assertRaises(HefestoError) as ctx:
            replica.capture_reference(self.project, RID)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(self.calls(log), [])  # ni siquiera se abre el navegador
        (self.folder() / "scope.json").unlink()
        self.assertIsNotNone(self.authorize())
        other = self.tmp / "otro"
        code, out, err = self.run_cli("init", str(other), "--shared", "--providers", "gemini,codex")
        self.assertEqual(code, 0, out + err)
        target = Path(replica.replica_dir(other, RID))
        target.mkdir(parents=True)
        (target / "scope.json").write_bytes((self.folder() / "scope.json").read_bytes())
        self.assertIsNone(replica.load_scope(other, RID))


# --- Inventarios con evidencia, saneados y sin copiar código ----------------------------------------------------


class InventoryTests(_ReplicaCase):
    def test_reference_inventory_has_the_fixed_versioned_schema(self) -> None:
        replica = _replica()
        inventory = self.captured()
        self.assertEqual(tuple(inventory), INVENTORY_KEYS)
        self.assertEqual((inventory["schema_version"], inventory["id"], inventory["side"], inventory["source"]),
                         (SCHEMA_VERSION, RID, "reference", REF_URL))
        self.assertEqual(replica.validate_inventory(inventory), inventory)
        stored = json.loads((self.folder() / "reference.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, inventory)
        values = self.values(inventory)
        self.assertEqual(set(values), set(REF_VALUES))
        for key, expected in REF_VALUES.items():
            if key != ("states", "/#error"):
                with self.subTest(key=key):
                    self.assertEqual(values[key], expected)

    def test_browser_session_visits_only_the_authorized_scope(self) -> None:
        log = self.browser()
        self.assertIsNotNone(self.authorize())
        _replica().capture_reference(self.project, RID, routes=("/avisos",))
        calls = self.calls(log)
        self.assertTrue(calls)
        self.assertTrue(all(f"-s=hefesto-replica-{RID}" in call for call in calls))
        commands = [[a for a in call if not a.startswith("-")] for call in calls]
        self.assertEqual(commands[0][0], "open")
        self.assertEqual(commands[-1], ["close"])
        self.assertEqual([c[1] for c in commands if c[0] == "goto"], [REF_URL, REF_AVISOS])
        self.assertNotIn("screenshot", [c[0] for c in commands])
        self.assertFalse(any(OTHER_URL in " ".join(call) for call in calls))  # el enlace externo no se sigue

    def test_redirects_leaving_the_scope_are_blocked(self) -> None:
        replica = _replica()
        for target in (OTHER_URL, HTTPS + "127.0.0.1/", HTTP + REF_HOST + "/"):
            with self.subTest(target=target):
                log = self.browser({REF_URL: {"redirect": target}, target: {"file": str(self.ref_file)}})
                log.unlink(missing_ok=True)
                self.assertIsNotNone(self.authorize())
                with self.assertRaises(HefestoError) as ctx:
                    replica.capture_reference(self.project, RID)
                self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
                self.assertIn("redirecci", str(ctx.exception).lower())
                self.assertEqual([a for a in self.calls(log)[-1] if not a.startswith("-")], ["close"])
                self.assertFalse((self.folder() / "reference.json").exists())

    def test_capture_needs_scope_valid_routes_and_a_browser(self) -> None:
        replica = _replica()
        log = self.browser()
        with self.assertRaises(HefestoError) as ctx:
            replica.capture_reference(self.project, RID)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertIsNotNone(self.authorize(url=REF_URL + "docs/"))
        for route in (OTHER_URL, "/otra-seccion", "../../", HTTPS + "127.0.0.1/docs/"):
            with self.subTest(route=route), self.assertRaises(HefestoError) as ctx:
                replica.capture_reference(self.project, RID, routes=(route,))
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(self.calls(log), [])  # todo se rechaza antes de lanzar ningún proceso
        self.use_fakes()  # PATH sin playwright-cli
        with self.assertRaises(HefestoError) as ctx:
            replica.capture_reference(self.project, RID)
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertIn("playwright-cli", str(ctx.exception))
        self.assertFalse((self.folder() / "reference.json").exists())

    def test_every_item_has_evidence_or_is_marked_unverifiable(self) -> None:
        replica = _replica()
        inventory = self.captured()
        for section in SECTIONS:
            for item in inventory[section]:
                with self.subTest(section=section, key=item["key"]):
                    self.assertEqual(tuple(item), ITEM_KEYS)
                    self.assertEqual(item["section"], section)
                    if item["status"] == "no_verificable":
                        self.assertIsNone(item["value"])
                        self.assertIsNone(item["evidence"])
                        self.assertTrue(item["reason"])
                        continue
                    self.assertEqual(item["status"], "verificado")
                    evidence = item["evidence"]
                    self.assertEqual(tuple(evidence), EVIDENCE_KEYS)
                    self.assertEqual(evidence["sha256"], replica.item_hash(section, item["key"], item["value"]))
                    self.assertEqual(evidence["sha256"], _hash(section, item["key"], item["value"]))
                    self.assertFalse(Path(evidence["capture"]).is_absolute())
                    self.assertNotIn("..", Path(evidence["capture"]).parts)
                    capture = self.folder() / evidence["capture"]
                    self.assertTrue(capture.is_file())
                    self.assertTrue(evidence["selector"])
                    self.assertIn(evidence["selector"], capture.read_text(encoding="utf-8"))
        page = next(i for i in inventory["pages"] if i["key"] == "/avisos")
        self.assertEqual(page["status"], "no_verificable")  # el goto falló: no se inventa nada

    def test_stored_artifacts_are_sanitized_and_never_copy_reference_code(self) -> None:
        self.captured()
        files = self.stored_files()
        self.assertTrue(files)
        for path in files:
            rel = path.relative_to(self.folder()).as_posix()
            with self.subTest(file=rel):
                self.assertTrue(rel in ALLOWED_FILES or re.fullmatch(r"captures/[^/]+\.txt", rel), rel)
                text = path.read_text(encoding="utf-8")
                for marker in SECRET_MARKERS + CODE_MARKERS:
                    self.assertNotIn(marker, text)
                self.assertEqual(guard.find_secrets(text), [])
                if rel.startswith("captures/"):
                    self.assertNotIn("<", text)  # extracto de texto, nunca HTML

    def test_sanitize_uses_guard_redact_and_removes_tokens_cookies_and_emails(self) -> None:
        replica = _replica()
        dirty = (f"Escribe a {EMAIL}. Authorization: Bearer abcdefghijklmnop1234 "
                 f"Cookie: sesion=SECRETO-COOKIE; Set-Cookie: id=SECRETO-COOKIE "
                 f"document.cookie = 'sid=SECRETO-COOKIE' {REF_URL}?token=SECRETO-QUERY&pagina=2 "
                 f"{GH_TOKEN} {AWS_KEY}")
        with mock.patch("hefesto.guard.redact", wraps=guard.redact) as spy:
            clean = replica.sanitize(dirty)
        self.assertTrue(spy.called)
        for marker in (EMAIL, "abcdefghijklmnop1234", "SECRETO-COOKIE", "SECRETO-QUERY", GH_TOKEN, AWS_KEY):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, clean)
        self.assertIn("Escribe a", clean)
        self.assertEqual(replica.sanitize("  Hola \n  mundo "), "Hola mundo")

    def test_project_inventory_reads_local_pages_only(self) -> None:
        replica = _replica()
        before = self.snapshot()
        inventory = replica.inventory_project(self.project, RID, {"/": PROJECT_PAGE, "/falta": "web/falta.html"})
        self.assertEqual(tuple(inventory), INVENTORY_KEYS)
        self.assertEqual((inventory["side"], inventory["source"]), ("project", "project"))
        values = self.values(inventory)
        for key, expected in PROJECT_VALUES.items():
            with self.subTest(key=key):
                self.assertEqual(values[key], expected)
        missing = next(i for i in inventory["pages"] if i["key"] == "/falta")
        self.assertEqual((missing["status"], missing["value"], missing["evidence"]), ("no_verificable", None, None))
        self.assertTrue((self.folder() / "project.json").is_file())
        self.assertEqual(self.snapshot(), before)
        for bad in ("../fuera.html", str(self.ref_file), "web/../../fuera.html"):
            with self.subTest(page=bad), self.assertRaises(HefestoError) as ctx:
                replica.inventory_project(self.project, RID, {"/": bad})
            self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_validation_rejects_unknown_versions_and_invented_items(self) -> None:
        replica = _replica()
        good = make_inventory("reference", REF_VALUES)
        self.assertEqual(replica.validate_inventory(good), good)
        broken: list[dict[str, Any]] = []
        for version in (2, "1", None, 0):
            broken.append(make_inventory("reference", REF_VALUES, version=version))
        missing = make_inventory("reference", REF_VALUES)
        del missing["schema_version"]
        broken.append(missing)
        extra = make_inventory("reference", REF_VALUES)
        extra["assets"] = []
        broken.append(extra)
        side = make_inventory("referencia", REF_VALUES)
        broken.append(side)
        invented = make_inventory("reference", REF_VALUES)
        invented["components"][0]["evidence"] = None  # verificado sin evidencia
        broken.append(invented)
        guessed = make_inventory("reference", REF_VALUES)
        nv = next(i for i in guessed["pages"] if i["status"] == "no_verificable")
        nv["value"] = "Avisos (supuesto)"  # no verificable con valor inventado
        broken.append(guessed)
        escape = make_inventory("reference", REF_VALUES)
        escape["texts"][0]["evidence"]["capture"] = "../../fuera.txt"
        broken.append(escape)
        for number, data in enumerate(broken):
            with self.subTest(case=number), self.assertRaises(HefestoError) as ctx:
                replica.validate_inventory(data)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)


# --- Matriz de seis estados, reproducible -----------------------------------------------------------------------


class ComparisonTests(_ReplicaCase):
    def matrix(self) -> dict[str, Any]:
        replica = _replica()
        return replica.compare(make_inventory("reference", REF_VALUES), make_inventory("project", PROJECT_VALUES))

    def test_matrix_has_the_fixed_schema_and_exactly_six_states(self) -> None:
        matrix = self.matrix()
        self.assertEqual(tuple(matrix), MATRIX_KEYS)
        self.assertEqual((matrix["schema_version"], matrix["id"], matrix["reference"], matrix["project"]),
                         (SCHEMA_VERSION, RID, REF_URL, "project"))
        self.assertEqual(set(matrix["counts"]), set(MATRIX_STATES))
        self.assertEqual(sum(matrix["counts"].values()), len(matrix["rows"]))
        for row in matrix["rows"]:
            self.assertEqual(tuple(row), ROW_KEYS)
            self.assertIn(row["state"], MATRIX_STATES)

    def test_each_row_gets_the_expected_state(self) -> None:
        rows = rows_by_key(self.matrix())
        self.assertEqual({key: row["state"] for key, row in rows.items()}, EXPECTED_ROWS)

    def test_rows_are_traceable_to_the_evidence(self) -> None:
        reference, own = make_inventory("reference", REF_VALUES), make_inventory("project", PROJECT_VALUES)
        rows = rows_by_key(_replica().compare(reference, own))
        for side, inventory in (("reference", reference), ("project", own)):
            items = {(i["section"], i["key"]): i for s in SECTIONS for i in inventory[s]}
            for key, row in rows.items():
                with self.subTest(side=side, key=key):
                    item = items.get(key)
                    sha = item["evidence"]["sha256"] if item and item["evidence"] else None
                    self.assertEqual(row[f"{side}_sha256"], sha)
                    self.assertEqual(row[side], item["value"] if item else None)

    def test_output_is_reproducible_and_sorted(self) -> None:
        replica = _replica()
        reference, own = make_inventory("reference", REF_VALUES), make_inventory("project", PROJECT_VALUES)
        first = replica.matrix_json(replica.compare(reference, own))
        shuffled_ref = json.loads(json.dumps(reference))
        shuffled_own = {k: own[k] for k in reversed(list(own))}
        for section in SECTIONS:
            random.Random(7).shuffle(shuffled_ref[section])
            shuffled_own[section] = list(reversed(shuffled_own[section]))
        second = replica.matrix_json(replica.compare(shuffled_ref, shuffled_own))
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        data = json.loads(first)
        self.assertEqual(first, json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        order = [(SECTIONS.index(r["section"]), r["key"]) for r in data["rows"]]
        self.assertEqual(order, sorted(order))

    def test_unverifiable_never_becomes_present_or_absent(self) -> None:
        replica = _replica()
        reference = make_inventory("reference", {("pages", "/"): None, ("texts", "/#h1[1]"): "Hola"})
        own = make_inventory("project", {("pages", "/"): "Portada", ("texts", "/#h1[1]"): None})
        states = {k: r["state"] for k, r in rows_by_key(replica.compare(reference, own)).items()}
        self.assertEqual(states, {("pages", "/"): "no_verificable", ("texts", "/#h1[1]"): "no_verificable"})

    def test_partial_and_different_follow_the_normalized_rule(self) -> None:
        replica = _replica()
        cases = (("Buscar avisos", "  buscar   AVISOS ", "presente"), ("Buscar avisos por fecha", "Buscar avisos",
                 "parcial"), ("Avisos", "Avisos vecinales", "parcial"), ("Buscar", "Enviar", "diferente"))
        for ref_value, own_value, expected in cases:
            with self.subTest(ref=ref_value, own=own_value):
                matrix = replica.compare(make_inventory("reference", {("texts", "/#h1[1]"): ref_value}),
                                         make_inventory("project", {("texts", "/#h1[1]"): own_value}))
                self.assertEqual(matrix["rows"][0]["state"], expected)

    def test_unknown_versions_wrong_sides_or_ids_are_rejected(self) -> None:
        replica = _replica()
        reference, own = make_inventory("reference", REF_VALUES), make_inventory("project", PROJECT_VALUES)
        for ref_case, own_case in ((make_inventory("reference", REF_VALUES, version=2), own),
                                   (reference, make_inventory("project", PROJECT_VALUES, version="1")),
                                   (own, reference),
                                   (reference, make_inventory("project", PROJECT_VALUES, rid="otro-id"))):
            with self.subTest(ref=ref_case["side"], own=own_case["side"]), self.assertRaises(HefestoError) as ctx:
                replica.compare(ref_case, own_case)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)


# --- Propuesta documental y aceptación humana -------------------------------------------------------------------


class ProposalTests(_ReplicaCase):
    def prepared(self) -> dict[str, Any]:
        replica = _replica()
        self.captured()
        self.project_inventory()
        return replica.propose(self.project, RID)

    def accept(self, answer: str = "s") -> Any:
        replica = _replica()
        with tty(True, [answer]):
            return replica.accept(self.project, RID)

    def test_full_flow_produces_the_expected_matrix(self) -> None:
        replica = _replica()
        result = self.prepared()
        self.assertEqual(Path(result["proposal"]), self.folder() / "proposal.md")
        self.assertEqual(Path(result["matrix"]), self.folder() / "matrix.json")
        text = Path(result["matrix"]).read_text(encoding="utf-8")
        matrix = json.loads(text)
        self.assertEqual({k: r["state"] for k, r in rows_by_key(matrix).items()}, EXPECTED_ROWS)
        reference = json.loads((self.folder() / "reference.json").read_text(encoding="utf-8"))
        own = json.loads((self.folder() / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(text, replica.matrix_json(replica.compare(reference, own)))

    def test_proposal_only_writes_inside_its_hefesto_folder(self) -> None:
        self.captured()
        self.project_inventory()
        before = self.snapshot()
        _replica().propose(self.project, RID)
        self.assertEqual(self.snapshot(), before)
        status = git(self.project, "status", "--porcelain", "--untracked-files=all")
        for line in status.splitlines():
            with self.subTest(line=line):
                self.assertTrue(line[3:].startswith(f".hefesto/replica/{RID}/"), line)

    def test_proposal_lists_gaps_and_never_carries_reference_code(self) -> None:
        result = self.prepared()
        text = Path(result["proposal"]).read_text(encoding="utf-8")
        for (section, key), state in EXPECTED_ROWS.items():
            if state in ("ausente", "parcial", "diferente"):
                with self.subTest(key=key):
                    self.assertIn(key, text)
        self.assertIn("sin aplicar", text.lower())
        self.assertIn("hefesto replica accept", text)
        for marker in SECRET_MARKERS + CODE_MARKERS + ("<form", "<nav"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_proposal_is_reproducible(self) -> None:
        replica = _replica()
        result = self.prepared()
        first = (Path(result["proposal"]).read_bytes(), Path(result["matrix"]).read_bytes())
        again = replica.propose(self.project, RID)
        self.assertEqual((Path(again["proposal"]).read_bytes(), Path(again["matrix"]).read_bytes()), first)

    def test_missing_or_tampered_inventories_produce_nothing(self) -> None:
        replica = _replica()
        self.captured()
        with self.assertRaises(HefestoError) as ctx:
            replica.propose(self.project, RID)  # falta project.json
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.project_inventory()
        path = self.folder() / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = 99
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(HefestoError) as ctx:
            replica.propose(self.project, RID)
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertFalse((self.folder() / "proposal.md").exists())
        self.assertFalse((self.folder() / "matrix.json").exists())

    def test_accepting_needs_a_human_and_never_writes_code(self) -> None:
        replica = _replica()
        self.prepared()
        before = self.snapshot()
        with tty(False, ["s"]), self.assertRaises(HefestoError) as ctx:
            replica.accept(self.project, RID)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        for var in ("HEFESTO_ROLE", "CLAUDECODE", "GEMINI_CLI"):
            with self.subTest(var=var), _agent_env(var), self.assertRaises(HefestoError) as ctx:
                self.accept()
            self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertIsNone(self.accept(answer="n"))
        self.assertIsNone(self.accept(answer=""))
        self.assertFalse(replica.accepted(self.project, RID))
        record = self.accept()
        self.assertEqual(tuple(record), ACCEPT_KEYS)
        self.assertEqual(record["channel"], "tty")
        self.assertTrue(replica.accepted(self.project, RID))
        self.assertEqual(self.snapshot(), before)  # aceptar no aplica nada al código
        params = list(inspect.signature(replica.accept).parameters)
        self.assertEqual(params[:2], ["project", "replica_id"])
        for name in params:
            self.assertNotIn(name, ("yes", "force", "assume_yes", "apply"))

    def test_acceptance_is_bound_to_the_exact_documents(self) -> None:
        replica = _replica()
        self.prepared()
        self.assertIsNotNone(self.accept())
        proposal = self.folder() / "proposal.md"
        original = proposal.read_text(encoding="utf-8")
        proposal.write_text(original + "\nAplica también el código de la referencia.\n", encoding="utf-8")
        self.assertFalse(replica.accepted(self.project, RID))
        proposal.write_text(original, encoding="utf-8")
        self.assertTrue(replica.accepted(self.project, RID))
        acceptance = self.folder() / "acceptance.json"
        data = json.loads(acceptance.read_text(encoding="utf-8"))
        data["id"] = "ACC-falsa"
        acceptance.write_text(json.dumps(data), encoding="utf-8")  # sin la copia independiente
        self.assertFalse(replica.accepted(self.project, RID))

    def test_module_never_downloads_clones_or_uses_a_shell(self) -> None:
        replica = _replica()
        source = Path(replica.__file__).read_text(encoding="utf-8")
        for needle in ("urlopen", "urllib.request", "http.client", "import " + "requests", "shell=" + "True"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)
        self.assertNotRegex(source, r"['\"](clone|wget|curl)['\"]")


# --- CLI --------------------------------------------------------------------------------------------------------


class CliTests(_ReplicaCase):
    def test_authorize_is_human_only_and_validates_the_url(self) -> None:
        replica = _replica()
        code, out, err = self.cli("replica", "authorize", RID, REF_URL)  # sin TTY
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        with _agent_env("HEFESTO_ROLE"):
            code, out, err = self.cli("replica", "authorize", RID, REF_URL, tty_value=True, answers=["s"])
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        code, out, err = self.cli("replica", "authorize", RID, "file:" + "///etc/passwd", tty_value=True,
                                  answers=["s"])
        self.assertEqual(code, EXIT_INPUT, out + err)
        code, out, err = self.cli_code("replica", "authorize", RID, REF_URL, "--yes", tty_value=True,
                                       answers=["s"])
        self.assertNotEqual(code, 0, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIsNone(replica.load_scope(self.project, RID))
        code, out, err = self.cli("replica", "authorize", RID, REF_URL, tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertIsNotNone(replica.load_scope(self.project, RID))

    def test_full_flow_through_the_cli(self) -> None:
        self.browser()
        code, out, err = self.cli("replica", "authorize", RID, REF_URL, tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("replica", "capture", RID, "--route", "/avisos")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("replica", "inventory", RID, "--page", f"/={PROJECT_PAGE}")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("replica", "propose", RID)
        self.assertEqual(code, 0, out + err)
        self.assertIn("proposal.md", out)
        self.assertNotIn("Traceback", out + err)
        matrix = json.loads((self.folder() / "matrix.json").read_text(encoding="utf-8"))
        self.assertEqual({k: r["state"] for k, r in rows_by_key(matrix).items()}, EXPECTED_ROWS)

    def test_capture_without_scope_or_with_a_redirect_out_of_scope_is_blocked(self) -> None:
        log = self.browser({REF_URL: {"redirect": OTHER_URL}, OTHER_URL: {"file": str(self.ref_file)}})
        code, out, err = self.cli("replica", "capture", RID)
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        self.assertEqual(self.calls(log), [])
        code, out, err = self.cli("replica", "authorize", RID, REF_URL, tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("replica", "capture", RID)
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertFalse((self.folder() / "reference.json").exists())

    def test_accept_is_human_only_and_there_is_no_apply_or_clone(self) -> None:
        replica = _replica()
        self.browser()
        self.assertIsNotNone(self.authorize())
        replica.capture_reference(self.project, RID, routes=("/avisos",))
        self.project_inventory()
        replica.propose(self.project, RID)
        with _agent_env("CLAUDECODE"):
            code, out, err = self.cli("replica", "accept", RID, tty_value=True, answers=["s"])
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        self.assertFalse(replica.accepted(self.project, RID))
        for args in (("replica", "apply", RID), ("replica", "clone", RID), ("replica", "accept", RID, "--yes")):
            with self.subTest(args=args):
                code, out, err = self.cli_code(*args, tty_value=True, answers=["s"])
                self.assertNotEqual(code, 0, out + err)
                self.assertNotIn("Traceback", out + err)
        self.assertFalse(replica.accepted(self.project, RID))
        code, out, err = self.cli("replica", "accept", RID, tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(replica.accepted(self.project, RID))
