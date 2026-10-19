"""Contratos de la Fase 16: diseño y verificación visual (ADR 2026-10-17).

DESIGN.md y PRODUCT.md son contexto fijo; `playwright-cli` (capturas) e `impeccable detect` (determinista) son
verify, porque revisar no es verificar. impeccable se usa según su flujo v4 (webreactiva.com/blog/impeccable):
`init` una vez con el humano (PRODUCT.md real), `document` delegable, y por tarea visual diagnóstico
(`critique`/`audit`) → comandos sugeridos, uno por delegación con checkpoint → `polish` al final. Telemetría de
impeccable siempre desactivada. Todo es opcional: sin binarios se avisa y no se bloquea.

API que fijan estos tests:

Configuración `[visual]` (hefesto/config.py + hefesto.toml, TASK-1604): enabled=false, base_url, routes=["/"],
viewports=["390x844", "1280x800"], start_command=[] (argv, sin shell), start_timeout=30, detect=false, detect_fail_on="warning", max_images=4,
impeccable_version="4.6.0", design_refs_commit=visual.DESIGN_REFS_COMMIT.

hefesto/project.py (+ packet.py, distribution.py, TASK-1602)
- `design_context(project) -> DesignContext` (design: str | None, product: str | None, product_ready: bool,
  suggestions: list[str]). PRODUCT.md está listo si existe, tiene más de 200 caracteres sin espacios de los
  extremos y no contiene `[TODO]`. Sin él, una sugerencia nombra «impeccable init» (solo el Core, con el humano);
  con él y sin DESIGN.md, una sugerencia nombra «impeccable document» (delegable). Nunca escribe nada.
- en el paquete de reviewer y visual de tareas `kind` app o web: sección «Contexto de diseño» con DESIGN.md y
  PRODUCT.md; nunca en otros roles ni tipos. Sin PRODUCT.md listo, impeccable no entra en ningún paquete
  (ninguna cabecera «### impeccable»); el paquete del Core lleva las sugerencias.
- `update_impeccable_gitignore(project) -> bool`: bloque entre «# >>> impeccable-ignore» y «# <<< impeccable-ignore»
  que ignora `.impeccable/config.local.json` y deja versionados `.impeccable/config.json`, `.impeccable/design.json`
  y `.impeccable/critique/`; idempotente (True si cambió). `distribution.adopt` lo aplica solo si existe
  `.impeccable/`.

hefesto/visual.py (TASK-1603..1607)
- `DESIGN_REFS_SOURCE == "VoltAgent/awesome-design-md"`, `DESIGN_REFS_COMMIT` (40 hex, fijado);
  `design_ref(project, site) -> Path` (`.hefesto/design/refs/<site>.md` desde `design-md/<site>/DESIGN.md` del
  commit `[visual] design_refs_commit`, con el `git_base` de `[skills_sources]` y git endurecido); `list_design_refs(project) -> list[str]`
  ordenada con los sitios de `design-md/` en ese commit fijado (descargados o no); un sitio que no está en el commit
  → HefestoError; nombre de sitio `[a-z0-9][a-z0-9._-]*` sin `..`, validado antes de lanzar ningún proceso.
  CLI: `hefesto design ref SITIO` y `hefesto design ref --list`.
- `artifacts_dir(project, task_id) -> Path` = `.hefesto/logs/visual/<task_id>`.
- `capture(project, cfg, task, out_dir=None) -> CaptureReport` (ok, problems: list[str], images: list[Path],
  skipped: str | None). Solo con `[visual] enabled` y `kind` app o web. Sin `playwright-cli` en el PATH: ok y
  skipped que lo nombra. Con `start_command`, lo lanza sin shell antes de la primera llamada a `playwright-cli`,
  espera a que `base_url` responda como mucho `start_timeout` segundos (si no responde: ok False, un problema que
  lo dice y ninguna llamada a `playwright-cli`) y lo termina siempre al acabar, también tras el timeout (el proceso
  y el puerto quedan libres). Cada llamada a `playwright-cli` lleva `-s=hefesto-<id en minúsculas>`; por cada ruta y
  tamaño: `resize W H`, `goto <base_url><ruta>`, `--raw eval <sonda>` (JSON con scrollWidth, clientWidth,
  clipped: list, overlaps: list) y `screenshot --filename=<out_dir>/<ruta>-<WxH>.png` (ruta «/» → «root»,
  «/a/b» → «a-b»); la última llamada es siempre `close`, también tras un error (sonda ilegible o `goto`/`screenshot`
  que sale con código distinto de 0, que es un problema). Problemas: scroll horizontal (scrollWidth > clientWidth),
  contenido recortado, elementos solapados, sonda ilegible o comando fallido, cada uno con la ruta y el tamaño.
- `detect(project, cfg, task) -> DetectReport` (ok, findings: list[dict], skipped: str | None, note: str).
  Solo con `[visual] detect`. Binario `impeccable` del PATH (nunca npx); si `--version` no es
  `impeccable_version`, skipped que nombra la versión y no bloquea. Ejecuta `[bin, "detect", "--json", *ficheros
  de la tarea]` sin shell, con DO_NOT_TRACK=1 e IMPECCABLE_NO_TELEMETRY=1; sale 0 o 2 con un array JSON de
  hallazgos; otro código o JSON ilegible → ok False. Solo pasa los ficheros de interfaz existentes de la tarea
  (`.html`, `.css`, `.scss`, `.tsx`, `.jsx`, `.vue`, `.svelte`, `.component.*`); sin ninguno, skipped. Nunca usa
  `shell=True` (en Windows los binarios son `.cmd`): un fichero cuyo nombre lleve metacaracteres de cmd (`&|<>^%!"`)
  no se pasa y `note` lo nombra. Las
  excepciones (`detector.ignoreRules`, `ignoreFiles` o `ignoreValues` no vacíos) de `.impeccable/config.local.json`
  (ignorado por git, no auditable) hacen fallar la detección con un hallazgo `{"antipattern": "local-ignores",
  "severity": "error", "file": ".impeccable/config.local.json", …}`; ese fichero sin excepciones no cuenta. `SEVERITY_ORDER == ("advisory", "warning", "error")`;
  falla con hallazgos de severidad >= `detect_fail_on` (los `advisory` nunca). `note` dice que el detector no
  prueba que la página funcione. Escribe `artifacts_dir/detect.json` con `findings` y `note`.
- `ignores_changed(before: str | None, after: str | None) -> bool`: cambia `detector.ignoreRules`,
  `ignoreFiles` o `ignoreValues` del JSON de `.impeccable/config.json` (un JSON ilegible cuenta como cambio). `inline_ignores_added(diff_text) -> bool`:
  una línea añadida con `impeccable-disable`.
- `done` de una tarea `kind` app o web: tras el verify, captura y detecta según `[visual]`; un problema o hallazgo
  hace fallar la tarea como un verify en rojo (cuenta intento y queda como error previo del siguiente); un
  trabajador delegado (rol distinto de core) que cambie las excepciones del detector o añada `impeccable-disable`
  también falla; el Core puede cambiarlas solo con motivo: el diff de la tarea añade a `.hefesto/DECISIONS.md` una
  línea que nombra cada regla, fichero o valor nuevo de las excepciones (si no, falla y lo dice con «motivo»). La evidencia
  guarda `visual` con `captures` (lista de `{"path": relativa al proyecto en posix, "sha256"}`) y `detect` (el
  informe del detector).
- `SHORTCUTS == {"impacto": ("critique", "bolder", "polish"), "robustez": ("audit", "harden", "optimize"),
  "tipografia": ("typeset", "layout", "polish")}`; `parse_suggestions(text) -> list[str]` (comandos de impeccable
  citados como `impeccable X`, `/impeccable X` o `$impeccable X`, en orden y sin repetir, solo los de los modos del
  rol visual del catálogo); `plan_pass(diagnosis, suggestions) -> list[str]` (quita desconocidos, interactivos,
  `craft`, `overdrive`, `detect` y los de diagnóstico; `bolder` solo con diagnóstico; `animate` tras los demás;
  `polish` una vez y al final).
- `design_pass(project, cfg, task, delegate, checkpoint, diagnosis="critique", shortcut=None) -> PassReport`
  (ok, steps: list[str], failed: str | None, report_path: Path | None). `delegate(role, instruction) -> (ok, texto)`;
  con `shortcut`, si el primer elemento del atajo es `critique` o `audit` es el diagnóstico (si no, no hay paso de
  diagnóstico) y los comandos son el resto, pasados por `plan_pass`; las sugerencias del diagnóstico se ignoran;
  si la delegación del diagnóstico falla, se para con `failed` igual al diagnóstico y sin informe;
  el diagnóstico va al rol reviewer y se guarda en `.hefesto/design/reports/<TASK>-<diagnóstico>.md`; cada comando va
  al rol visual con una instrucción «impeccable <comando> …» y un `checkpoint(etiqueta)` justo después de cada una
  (nunca agrupados al final; el diagnóstico no lleva checkpoint); si una delegación
  falla, se para (sin `polish`). Tareas que no son app o web, o sin PRODUCT.md listo: HefestoError (que en el
  segundo caso nombra «impeccable init», paso del Core con el humano). CLI: `hefesto design pass TASK
  [--dry-run] [--shortcut S] [--diagnosis critique|audit]`: con `--dry-run` imprime el plan «a → b → polish» sin
  delegar; sin él llama a `visual.design_pass` con esos argumentos, un `delegate(role, instruction)` que llama a
  `router.run(project, role, instruction, task_id=TASK)` y devuelve `(code == 0 and result.success, result.summary)`,
  y un `checkpoint(label)` que llama a `gitops.checkpoint` con un mensaje que contiene la etiqueta; imprime los pasos
  y sale con 0 solo si el informe es ok.
- `reviewer_images(project, cfg, task) -> list[Path]`: las capturas de `artifacts_dir`, como mucho `max_images`.
  El paquete del reviewer (tareas app o web) lleva la sección «Evidencia visual» con los hallazgos del detector, la
  nota y las capturas; `router.run` rellena `Call.extra["images"]` con `reviewer_images` solo para el rol reviewer
  de tareas app o web (vacío o ausente en cualquier otro caso); el adaptador de codex adjunta `call.extra["images"]` con `--image`; claude y gemini no
  reciben imágenes (solo el texto del paquete).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config, roadmap, state, util
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git

PRODUCT_READY = ("# Producto\n\nSuite policial para la Policía Local: avisos, cuadrante y partes. Marca sobria, "
                 "institucional y legible a pleno sol. Anti-referencias: nada de gradientes morados, nada de tarjetas "
                 "anidadas, nada de tipografía genérica de plantilla SaaS ni ilustraciones decorativas.\n")
DESIGN_TEXT = "---\nname: Prueba\ncolors:\n  primary: '#003087'\n---\n\n# Diseño\n\nMARCA-DISENO-UNICA\n"

WEB_ROADMAP = """# ROADMAP

## Fase 1 — Web
- [ ] TASK-001: Portada
  - verify: `true`
  - files: web/index.html, .impeccable/config.json
  - role: {role}
  - size: S
  - kind: web
"""

UI_FILES_ROADMAP = """# ROADMAP

## Fase 1 — Web
- [ ] TASK-001: Portada
  - verify: `true`
  - files: web/index.html, web/app.component.ts, web/estilo.scss, web/App.tsx, web/falta.vue, src/calculo.py, .impeccable/config.json
  - role: visual
  - size: S
  - kind: web
"""

CORE_ROADMAP = """# ROADMAP

## Fase 1 — Web
- [ ] TASK-001: Portada
  - verify: `true`
  - files: web/index.html, .impeccable/config.json, .hefesto/DECISIONS.md
  - role: core
  - size: S
  - kind: web
"""

BACKEND_ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Cálculo
  - verify: `true`
  - files: src/calculo.py
  - role: runner
  - size: S
"""

FAKE_PLAYWRIGHT = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_PW_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
size_file = log + ".size"
cmds = [a for a in args if not a.startswith("-")]
cmd = cmds[0] if cmds else ""
if cmd and cmd == os.environ.get("FAKE_PW_FAIL"):
    sys.stderr.write("fallo simulado\n")
    sys.exit(1)
if cmd == "goto" and os.environ.get("FAKE_PW_REACH"):
    import urllib.request
    url = [a for a in cmds[1:] if a.startswith("http")][0]
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            reached = str(resp.status)
    except Exception as exc:
        reached = "error " + type(exc).__name__
    with open(os.environ["FAKE_PW_REACH"], "a", encoding="utf-8") as fh:
        fh.write(reached + "\n")
if cmd == "resize":
    with open(size_file, "w", encoding="utf-8") as fh:
        fh.write(f"{cmds[1]}x{cmds[2]}")
elif cmd == "screenshot":
    for a in args:
        if a.startswith("--filename="):
            path = a.split("=", 1)[1]
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG fake")
elif cmd == "eval":
    size = open(size_file, encoding="utf-8").read() if os.path.exists(size_file) else ""
    table = json.loads(os.environ.get("FAKE_PW_EVAL", "{}"))
    w = int(size.split("x")[0]) if size else 1000
    print(table.get(size, json.dumps({"scrollWidth": w, "clientWidth": w, "clipped": [], "overlaps": []})))
'''

FAKE_IMPECCABLE = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_IMP_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "DO_NOT_TRACK": os.environ.get("DO_NOT_TRACK"),
                         "IMPECCABLE_NO_TELEMETRY": os.environ.get("IMPECCABLE_NO_TELEMETRY")}) + "\n")
if "--version" in args:
    print(os.environ.get("FAKE_IMP_VERSION", "4.6.0"))
    sys.exit(0)
print(os.environ.get("FAKE_IMP_JSON", "[]"))
sys.exit(int(os.environ.get("FAKE_IMP_CODE", "0")))
'''


def finding(severity: str, rule: str = "nested-cards") -> dict[str, Any]:
    item: dict[str, Any] = {"antipattern": rule, "name": rule, "description": "d", "severity": severity,
                            "category": "quality", "file": "web/index.html", "line": 3, "snippet": "<div>"}
    if severity == "advisory":
        item["advisory"] = True
    return item


class _VisualCase(HefestoTestCase):
    def install_fake(self, name: str, source: str) -> None:
        bindir = self.tmp / "bin"
        script = self.tmp / "fakes-src" / f"{name}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(source, encoding="utf-8")
        if os.name == "nt":
            (bindir / f"{name}.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            dest = bindir / name
            dest.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            dest.chmod(0o755)

    def fake_playwright(self, eval_table: dict[str, dict[str, Any] | str] | None = None, fail: str = "") -> Path:
        self.install_fake("playwright-cli", FAKE_PLAYWRIGHT)
        log = self.tmp / "pw.jsonl"
        os.environ["FAKE_PW_LOG"] = str(log)
        os.environ["FAKE_PW_FAIL"] = fail
        os.environ["FAKE_PW_REACH"] = ""
        table = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in (eval_table or {}).items()}
        os.environ["FAKE_PW_EVAL"] = json.dumps(table)
        return log

    def fake_impeccable(self, findings: list[dict[str, Any]] | str = "[]", code: int | None = None,
                        version: str = "4.6.0") -> Path:
        self.install_fake("impeccable", FAKE_IMPECCABLE)
        log = self.tmp / "imp.jsonl"
        os.environ["FAKE_IMP_LOG"] = str(log)
        os.environ["FAKE_IMP_JSON"] = findings if isinstance(findings, str) else json.dumps(findings)
        os.environ["FAKE_IMP_CODE"] = str(code if code is not None else (2 if findings not in ("[]", []) else 0))
        os.environ["FAKE_IMP_VERSION"] = version
        return log

    @staticmethod
    def calls(log: Path) -> list[Any]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def prepare(self, roadmap_text: str = WEB_ROADMAP.format(role="visual"), **visual: Any) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
        settings = config.project_settings_path(self.project)
        for key, value in visual.items():
            config.set_setting(settings, ("visual", key), value)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)

    def cfg(self) -> config.Config:
        return config.load(self.project)

    def task(self) -> roadmap.Task:
        return roadmap.load_valid(self.project).get("TASK-001")

    def workdir(self) -> Path:
        return Path(state.load(self.project).get("workdir") or self.project)

    def write(self, rel: str, content: str) -> None:
        path = self.workdir() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def run_task(self, files: dict[str, str] | None = None) -> tuple[int, str, str]:
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        for rel, content in (files or {"web/index.html": "<main>hola</main>\n"}).items():
            self.write(rel, content)
        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, out + err)
        return self.cli("done", "TASK-001")


# --- TASK-1602 ------------------------------------------------------------------------------------------------


class DesignContextTests(_VisualCase):
    def build(self, role: str) -> str:
        from hefesto import packet

        rm = roadmap.load_valid(self.project)
        return packet.build(self.project, self.cfg(), role, "Haz", rm.get("TASK-001"), rm, None,
                            "codex:default", self.project).text

    def test_product_ready_rules(self) -> None:
        from hefesto import project as project_mod

        self.prepare()
        ctx = project_mod.design_context(self.project)
        self.assertFalse(ctx.product_ready)
        self.assertIsNone(ctx.product)
        self.assertTrue(any("impeccable init" in s for s in ctx.suggestions))
        for text in ("corto", PRODUCT_READY + "\n[TODO] completar\n"):
            (self.project / "PRODUCT.md").write_text(text, encoding="utf-8")
            self.assertFalse(project_mod.design_context(self.project).product_ready)
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")
        ctx = project_mod.design_context(self.project)
        self.assertTrue(ctx.product_ready)
        self.assertFalse(any("impeccable init" in s for s in ctx.suggestions))
        self.assertTrue(any("impeccable document" in s for s in ctx.suggestions))
        (self.project / "DESIGN.md").write_text(DESIGN_TEXT, encoding="utf-8")
        ctx = project_mod.design_context(self.project)
        self.assertEqual(ctx.design, DESIGN_TEXT)
        self.assertFalse(any("impeccable document" in s for s in ctx.suggestions))
        self.assertEqual((self.project / "DESIGN.md").read_text(encoding="utf-8"), DESIGN_TEXT)

    def test_design_context_in_visual_and_reviewer_packets_only(self) -> None:
        self.prepare()
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")
        (self.project / "DESIGN.md").write_text(DESIGN_TEXT, encoding="utf-8")
        for role in ("visual", "reviewer"):
            text = self.build(role)
            self.assertIn("Contexto de diseño", text)
            self.assertIn("MARCA-DISENO-UNICA", text)
            self.assertIn("Anti-referencias", text)
        for role in ("runner", "forge", "core"):
            self.assertNotIn("MARCA-DISENO-UNICA", self.build(role), role)
            self.assertNotIn("Contexto de diseño", self.build(role), role)

    def test_no_design_context_for_backend_tasks(self) -> None:
        self.prepare(BACKEND_ROADMAP)
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")
        (self.project / "DESIGN.md").write_text(DESIGN_TEXT, encoding="utf-8")
        self.assertNotIn("MARCA-DISENO-UNICA", self.build("reviewer"))

    def test_impeccable_needs_a_real_product_md(self) -> None:
        self.prepare()
        for role in ("visual", "reviewer"):
            self.assertNotIn("### impeccable", self.build(role))
        self.assertIn("impeccable init", self.build("core"))
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")
        self.assertIn("### impeccable", self.build("visual"))

    def test_gitignore_block_keeps_shared_impeccable_files_versioned(self) -> None:
        from hefesto import project as project_mod

        self.init_project()
        self.assertTrue(project_mod.update_impeccable_gitignore(self.project))
        self.assertFalse(project_mod.update_impeccable_gitignore(self.project))
        text = (self.project / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(text.count("# >>> impeccable-ignore"), 1)
        self.assertEqual(text.count("# <<< impeccable-ignore"), 1)
        for rel in (".impeccable/config.json", ".impeccable/design.json", ".impeccable/critique/a.md"):
            self.assertFalse(self._ignored(rel), rel)
        self.assertTrue(self._ignored(".impeccable/config.local.json"))

    def _ignored(self, rel: str) -> bool:
        import subprocess

        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=self.project).returncode == 0

    def test_adopt_adds_block_only_when_impeccable_is_used(self) -> None:
        from hefesto import distribution

        self.init_project()
        distribution.adopt(self.project, providers="gemini")
        self.assertNotIn("impeccable-ignore", (self.project / ".gitignore").read_text(encoding="utf-8"))
        (self.project / ".impeccable").mkdir()
        (self.project / ".impeccable" / "config.json").write_text("{}", encoding="utf-8")
        distribution.adopt(self.project, providers="gemini")
        self.assertIn("# >>> impeccable-ignore", (self.project / ".gitignore").read_text(encoding="utf-8"))


# --- TASK-1603 ------------------------------------------------------------------------------------------------


class DesignRefTests(_VisualCase):
    def make_refs_repo(self) -> str:
        """Devuelve el primer commit; HEAD (segundo) cambia apple y añade nike, para probar que se usa el fijado."""
        repo = self.tmp / "gh" / "VoltAgent" / "awesome-design-md.git"
        for site in ("apple", "stripe"):
            target = repo / "design-md" / site / "DESIGN.md"
            target.parent.mkdir(parents=True)
            target.write_text(f"# {site}\n\nREF-{site.upper()}\n", encoding="utf-8")
        (repo / "README.md").write_text("# refs\n", encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "refs")
        first = git(repo, "rev-parse", "HEAD")
        (repo / "design-md" / "apple" / "DESIGN.md").write_text("# apple\n\nREF-APPLE-NUEVO\n", encoding="utf-8")
        (repo / "design-md" / "nike").mkdir()
        (repo / "design-md" / "nike" / "DESIGN.md").write_text("# nike\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "refs 2")
        config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"), (self.tmp / "gh").as_posix())
        return first

    def test_constants(self) -> None:
        from hefesto import visual

        self.assertEqual(visual.DESIGN_REFS_SOURCE, "VoltAgent/awesome-design-md")
        self.assertRegex(visual.DESIGN_REFS_COMMIT, r"^[0-9a-f]{40}$")

    def test_fetches_reference_without_touching_project_design(self) -> None:
        from hefesto import visual

        commit = self.make_refs_repo()
        self.prepare(design_refs_commit=commit)
        (self.project / "DESIGN.md").write_text(DESIGN_TEXT, encoding="utf-8")
        path = visual.design_ref(self.project, "apple")
        self.assertEqual(path, self.project / ".hefesto" / "design" / "refs" / "apple.md")
        self.assertIn("REF-APPLE", path.read_text(encoding="utf-8"))
        self.assertNotIn("NUEVO", path.read_text(encoding="utf-8"))
        self.assertEqual((self.project / "DESIGN.md").read_text(encoding="utf-8"), DESIGN_TEXT)
        self.assertEqual(visual.list_design_refs(self.project), ["apple", "stripe"])
        with self.assertRaises(HefestoError):
            visual.design_ref(self.project, "nike")

    def test_invalid_site_names_rejected_before_any_process(self) -> None:
        from hefesto import visual

        self.make_refs_repo()
        self.prepare()
        for bad in ("../x", "a/b", "a\\b", "", ".", "..", "Apple Store", "-rf", "x:y"):
            with self.subTest(site=bad), mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
                with self.assertRaises(HefestoError):
                    visual.design_ref(self.project, bad)
                run.assert_not_called()
                popen.assert_not_called()

    def test_cli(self) -> None:
        commit = self.make_refs_repo()
        self.prepare(design_refs_commit=commit)
        code, out, err = self.cli("design", "ref", "--list")
        self.assertEqual(code, 0, out + err)
        self.assertIn("stripe", out)
        code, out, err = self.cli("design", "ref", "apple")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.project / ".hefesto" / "design" / "refs" / "apple.md").is_file())


# --- TASK-1604 ------------------------------------------------------------------------------------------------


class CaptureTests(_VisualCase):
    def test_visual_config_defaults(self) -> None:
        self.init_project()
        cfg = self.cfg()
        self.assertIs(cfg.get("visual", "enabled"), False)
        self.assertIs(cfg.get("visual", "detect"), False)
        self.assertEqual(cfg.get("visual", "routes"), ["/"])
        self.assertEqual(cfg.get("visual", "viewports"), ["390x844", "1280x800"])
        self.assertEqual(cfg.get("visual", "detect_fail_on"), "warning")
        self.assertEqual(cfg.get("visual", "max_images"), 4)
        self.assertEqual(cfg.get("visual", "start_command"), [])
        self.assertEqual(cfg.get("visual", "start_timeout"), 30)
        self.assertEqual(cfg.get("visual", "impeccable_version"), "4.6.0")

    def test_disabled_is_skipped(self) -> None:
        from hefesto import visual

        log = self.fake_playwright()
        self.prepare()
        self.assertTrue(visual.capture(self.project, self.cfg(), self.task()).skipped)
        self.assertEqual(self.calls(log), [])

    def test_backend_is_skipped_even_when_enabled(self) -> None:
        from hefesto import visual

        log = self.fake_playwright()
        self.prepare(BACKEND_ROADMAP, enabled=True, base_url="http://localhost:9999")
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertTrue(report.skipped)
        self.assertEqual(self.calls(log), [])

    def test_without_binary_warns_and_does_not_block(self) -> None:
        from hefesto import visual

        self.prepare(enabled=True, base_url="http://localhost:9999")
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertIn("playwright-cli", report.skipped or "")

    def test_captures_every_route_and_viewport_in_a_named_session(self) -> None:
        from hefesto import visual

        log = self.fake_playwright()
        self.prepare(enabled=True, base_url="http://localhost:9999", routes=["/", "/operativa/avisos"],
                     viewports=["390x844", "1280x800"])
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok, report.problems)
        names = sorted(p.name for p in report.images)
        self.assertEqual(names, ["operativa-avisos-1280x800.png", "operativa-avisos-390x844.png",
                                 "root-1280x800.png", "root-390x844.png"])
        for p in report.images:
            self.assertTrue(p.is_file())
            self.assertEqual(p.parent, visual.artifacts_dir(self.project, "TASK-001"))
        calls = self.calls(log)
        for argv in calls:
            self.assertIn("-s=hefesto-task-001", argv)
        self.assertIn("close", calls[-1])
        gotos = [a for argv in calls if "goto" in argv for a in argv if a.startswith("http")]
        self.assertIn("http://localhost:9999/operativa/avisos", gotos)
        self.assertTrue(any("eval" in argv and "--raw" in argv for argv in calls))

    def test_detects_overflow_clipping_and_overlap(self) -> None:
        from hefesto import visual

        self.fake_playwright({
            "390x844": {"scrollWidth": 520, "clientWidth": 390, "clipped": [".tabla"], "overlaps": [[".a", ".b"]]},
        })
        self.prepare(enabled=True, base_url="http://localhost:9999", routes=["/operativa/avisos"])
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertFalse(report.ok)
        text = " ".join(report.problems).lower()
        for problem in report.problems:
            self.assertIn("/operativa/avisos", problem)
        self.assertIn("390x844", text)
        self.assertIn("scroll horizontal", text)
        self.assertIn("recortado", text)
        self.assertIn("solapad", text)
        self.assertNotIn("1280x800", text)

    def test_unreadable_probe_fails_and_still_closes_the_session(self) -> None:
        from hefesto import visual

        log = self.fake_playwright({"390x844": "esto no es json"})
        self.prepare(enabled=True, base_url="http://localhost:9999")
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertFalse(report.ok)
        self.assertIn("close", self.calls(log)[-1])

    def _failing_command(self, failing: str) -> None:
        from hefesto import visual

        log = self.fake_playwright(fail=failing)
        self.prepare(enabled=True, base_url="http://localhost:9999", routes=["/caida"])
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertFalse(report.ok)
        self.assertTrue(any("/caida" in p for p in report.problems), report.problems)
        self.assertIn("close", self.calls(log)[-1])

    def test_failed_goto_is_a_problem_and_still_closes_the_session(self) -> None:
        self._failing_command("goto")

    def test_failed_screenshot_is_a_problem_and_still_closes_the_session(self) -> None:
        self._failing_command("screenshot")

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _listening(port: int) -> bool:
        import socket
        import time

        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            except OSError:
                return False
            time.sleep(0.1)
        return True

    def test_start_command_serves_during_capture_and_is_stopped(self) -> None:
        from hefesto import visual

        log = self.fake_playwright()
        reach = self.tmp / "reach.txt"
        os.environ["FAKE_PW_REACH"] = str(reach)
        port = self._free_port()
        self.prepare(enabled=True, base_url=f"http://127.0.0.1:{port}",
                     start_command=[sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(len(report.images), 2)
        self.assertTrue(self.calls(log))
        self.assertEqual(reach.read_text(encoding="utf-8").split(), ["200", "200"])
        self.assertFalse(self._listening(port))

    def test_start_command_that_never_serves_fails_without_capturing(self) -> None:
        from hefesto import visual

        import time

        log = self.fake_playwright()
        port = self._free_port()
        beat = self.tmp / "latido.txt"
        script = self.tmp / "fakes-src" / "latido.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("import sys, time\nfor i in range(600):\n"
                          "    open(sys.argv[1], 'w').write(str(i))\n    time.sleep(0.05)\n", encoding="utf-8")
        self.prepare(enabled=True, base_url=f"http://127.0.0.1:{port}", start_timeout=1,
                     start_command=[sys.executable, str(script), str(beat)])
        began = time.monotonic()
        report = visual.capture(self.project, self.cfg(), self.task())
        self.assertLess(time.monotonic() - began, 10, "no respeta start_timeout")
        self.assertFalse(report.ok)
        self.assertTrue(report.problems)
        self.assertEqual(self.calls(log), [])
        self.assertTrue(beat.exists())
        first = beat.read_text(encoding="utf-8")
        time.sleep(0.6)
        self.assertEqual(beat.read_text(encoding="utf-8"), first, "start_command sigue vivo tras el timeout")

    def test_done_records_captures(self) -> None:
        import hashlib

        from hefesto import evidence

        self.fake_playwright()
        self.prepare(enabled=True, base_url="http://localhost:9999")
        code, out, err = self.run_task()
        self.assertEqual(code, 0, out + err)
        record = evidence.show(self.project, "TASK-001")
        shots = record["visual"]["captures"]
        self.assertEqual(len(shots), 2)
        for shot in shots:
            self.assertRegex(shot["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("\\", shot["path"])
            image = self.project / shot["path"]
            self.assertTrue(image.is_file(), shot["path"])
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), shot["sha256"])

    def test_done_fails_like_a_red_verify(self) -> None:
        self.fake_playwright({"390x844": {"scrollWidth": 600, "clientWidth": 390, "clipped": [], "overlaps": []}})
        self.prepare(enabled=True, base_url="http://localhost:9999")
        code, out, err = self.run_task()
        self.assertNotEqual(code, 0)
        self.assertIn("390x844", out + err)
        data = state.load(self.project)
        self.assertEqual(data["attempts"], 1)
        self.assertIn("390x844", json.dumps(data.get("error_history"), ensure_ascii=False))
        self.assertIn("- [~] TASK-001", (self.project / ".hefesto" / "ROADMAP.md").read_text(encoding="utf-8"))


# --- TASK-1605 ------------------------------------------------------------------------------------------------


class DetectTests(_VisualCase):
    def test_only_existing_interface_files_are_passed(self) -> None:
        from hefesto import visual

        log = self.fake_impeccable()
        self.prepare(UI_FILES_ROADMAP, detect=True)
        present = ("web/index.html", "web/app.component.ts", "web/estilo.scss", "web/App.tsx", "src/calculo.py",
                   ".impeccable/config.json")
        for rel in present:
            target = self.project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}" if rel.endswith(".json") else "x", encoding="utf-8")
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        call = [c for c in self.calls(log) if "detect" in c["args"]][0]
        passed = sorted(a.replace("\\", "/") for a in call["args"][2:])
        self.assertEqual(passed, ["web/App.tsx", "web/app.component.ts", "web/estilo.scss", "web/index.html"])

    def test_never_uses_a_shell_and_skips_cmd_metacharacters(self) -> None:
        import subprocess

        from hefesto import visual

        log = self.fake_impeccable()
        self.prepare(WEB_ROADMAP.format(role="visual").replace("web/index.html,", "web/index.html, web/a&b.html,"),
                     detect=True)
        for rel in ("web/index.html", "web/a&b.html"):
            target = self.project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("<main></main>", encoding="utf-8")
        real_run, real_popen = subprocess.run, subprocess.Popen
        shells: list[Any] = []

        def spy_run(*a: Any, **kw: Any) -> Any:
            shells.append(kw.get("shell"))
            return real_run(*a, **kw)

        def spy_popen(*a: Any, **kw: Any) -> Any:
            shells.append(kw.get("shell"))
            return real_popen(*a, **kw)

        with mock.patch("subprocess.run", side_effect=spy_run), mock.patch("subprocess.Popen", side_effect=spy_popen):
            report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(shells)
        self.assertNotIn(True, shells)
        self.assertTrue(report.ok)
        call = [c for c in self.calls(log) if "detect" in c["args"]][0]
        passed = [a.replace("\\", "/") for a in call["args"][2:]]
        self.assertEqual(passed, ["web/index.html"])
        self.assertIn("web/a&b.html", report.note)

    def test_runs_pinned_binary_with_telemetry_off(self) -> None:
        from hefesto import visual

        log = self.fake_impeccable()
        self.prepare(detect=True)
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("<main></main>", encoding="utf-8")
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertIn("no prueba que la página funcione", report.note)
        detect_calls = [c for c in self.calls(log) if "detect" in c["args"]]
        self.assertEqual(len(detect_calls), 1)
        call = detect_calls[0]
        self.assertEqual(call["args"][:2], ["detect", "--json"])
        passed = [a.replace("\\", "/") for a in call["args"]]
        self.assertIn("web/index.html", passed)
        self.assertFalse(any(a.endswith(".impeccable/config.json") for a in passed))
        self.assertEqual(call["DO_NOT_TRACK"], "1")
        self.assertEqual(call["IMPECCABLE_NO_TELEMETRY"], "1")
        saved = json.loads((visual.artifacts_dir(self.project, "TASK-001") / "detect.json").read_text(encoding="utf-8"))
        self.assertIn("findings", saved)
        self.assertIn("note", saved)

    def test_severity_threshold_and_advisories(self) -> None:
        from hefesto import visual

        self.assertEqual(visual.SEVERITY_ORDER, ("advisory", "warning", "error"))
        self.prepare(detect=True)
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("<main></main>", encoding="utf-8")
        cases = [
            ([finding("advisory")], "warning", True),
            ([finding("warning")], "warning", False),
            ([finding("warning")], "error", True),
            ([finding("error")], "error", False),
        ]
        for findings, fail_on, ok in cases:
            with self.subTest(findings=findings[0]["severity"], fail_on=fail_on):
                self.fake_impeccable(findings)
                config.set_setting(config.project_settings_path(self.project), ("visual", "detect_fail_on"), fail_on)
                report = visual.detect(self.project, self.cfg(), self.task())
                self.assertEqual(report.ok, ok)

    def test_operational_failure_is_not_ok(self) -> None:
        from hefesto import visual

        self.prepare(detect=True)
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("<main></main>", encoding="utf-8")
        for findings, code in (("no es json", 0), ("[]", 1)):
            with self.subTest(code=code):
                self.fake_impeccable(findings, code=code)
                self.assertFalse(visual.detect(self.project, self.cfg(), self.task()).ok)

    def test_other_version_or_missing_binary_does_not_block(self) -> None:
        from hefesto import visual

        self.prepare(detect=True)
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("<main></main>", encoding="utf-8")
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertIn("impeccable", report.skipped or "")
        log = self.fake_impeccable([finding("error")], version="5.0.0")
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertIn("5.0.0", report.skipped or "")
        self.assertFalse([c for c in self.calls(log) if "detect" in c["args"]])

    def test_ignore_changes_are_detected(self) -> None:
        from hefesto import visual

        base = json.dumps({"detector": {"ignoreRules": ["side-tab"]}, "other": 1})
        self.assertFalse(visual.ignores_changed(base, json.dumps({"detector": {"ignoreRules": ["side-tab"]}, "other": 2})))
        self.assertTrue(visual.ignores_changed(base, json.dumps({"detector": {"ignoreRules": ["side-tab", "nested-cards"]}})))
        self.assertTrue(visual.ignores_changed(None, json.dumps({"detector": {"ignoreFiles": ["web/**"]}})))
        self.assertTrue(visual.ignores_changed(base, json.dumps({"detector": {"ignoreRules": ["side-tab"],
                                                                              "ignoreValues": ["x"]}})))
        self.assertTrue(visual.ignores_changed(base, "{ esto no es json"))
        self.assertTrue(visual.inline_ignores_added("+<!-- impeccable-disable nested-cards -->\n"))
        self.assertFalse(visual.inline_ignores_added("-<!-- impeccable-disable nested-cards -->\n"))

    def test_done_fails_with_findings_and_passes_them_on(self) -> None:
        self.fake_impeccable([finding("warning", rule="nested-cards")])
        self.prepare(detect=True)
        code, out, err = self.run_task()
        self.assertNotEqual(code, 0)
        self.assertIn("nested-cards", out + err)
        self.assertIn("nested-cards", json.dumps(state.load(self.project).get("error_history"), ensure_ascii=False))

    def test_done_records_report_in_evidence(self) -> None:
        from hefesto import evidence

        self.fake_impeccable([finding("advisory")])
        self.prepare(detect=True)
        code, out, err = self.run_task()
        self.assertEqual(code, 0, out + err)
        detect = evidence.show(self.project, "TASK-001")["visual"]["detect"]
        self.assertIn("no prueba que la página funcione", detect["note"])

    def test_delegated_worker_cannot_change_detector_ignores(self) -> None:
        self.fake_impeccable()
        self.prepare(detect=True)
        code, out, err = self.run_task({
            "web/index.html": "<main>hola</main>\n",
            ".impeccable/config.json": json.dumps({"detector": {"ignoreRules": ["nested-cards"]}}),
        })
        self.assertNotEqual(code, 0)
        self.assertIn("excepciones", (out + err).lower())

    def test_delegated_worker_cannot_add_inline_ignores(self) -> None:
        self.fake_impeccable()
        self.prepare(detect=True)
        code, out, err = self.run_task({"web/index.html": "<!-- impeccable-disable nested-cards -->\n<main></main>\n"})
        self.assertNotEqual(code, 0)
        self.assertIn("impeccable-disable", out + err)

    def _core_ignores(self, decisions: str | None) -> tuple[int, str, str]:
        self.fake_impeccable()
        self.prepare(CORE_ROADMAP, detect=True)
        files = {
            "web/index.html": "<main>hola</main>\n",
            ".impeccable/config.json": json.dumps({"detector": {"ignoreRules": ["nested-cards"]}}),
        }
        if decisions is not None:
            current = (self.workdir() / ".hefesto" / "DECISIONS.md")
            before = current.read_text(encoding="utf-8") if current.exists() else ""
            files[".hefesto/DECISIONS.md"] = before + decisions
        return self.run_task(files)

    def test_core_may_change_detector_ignores_with_a_reason(self) -> None:
        code, out, err = self._core_ignores("\n- impeccable: se ignora nested-cards en la portada porque las tarjetas "
                                            "de aviso anidan a propósito.\n")
        self.assertEqual(code, 0, out + err)

    def test_core_needs_a_reason_to_change_detector_ignores(self) -> None:
        code, out, err = self._core_ignores(None)
        self.assertNotEqual(code, 0)
        self.assertIn("motivo", (out + err).lower())

    def test_local_ignores_fail_detection(self) -> None:
        from hefesto import visual

        self.fake_impeccable()
        self.prepare(detect=True)
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("<main></main>", encoding="utf-8")
        local = self.project / ".impeccable" / "config.local.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        for harmless in ("{}", json.dumps({"other": 1}), json.dumps({"detector": {"ignoreRules": []}})):
            with self.subTest(local=harmless):
                local.write_text(harmless, encoding="utf-8")
                self.assertTrue(visual.detect(self.project, self.cfg(), self.task()).ok)
        local.write_text(json.dumps({"detector": {"ignoreRules": ["nested-cards"]}}), encoding="utf-8")
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertFalse(report.ok)
        local_findings = [f for f in report.findings if f.get("file") == ".impeccable/config.local.json"]
        self.assertEqual(len(local_findings), 1)
        self.assertEqual(local_findings[0]["antipattern"], "local-ignores")
        self.assertEqual(local_findings[0]["severity"], "error")

    def test_no_interface_files_is_skipped(self) -> None:
        from hefesto import visual

        log = self.fake_impeccable()
        self.prepare(detect=True)
        report = visual.detect(self.project, self.cfg(), self.task())
        self.assertTrue(report.ok)
        self.assertTrue(report.skipped)
        self.assertFalse([c for c in self.calls(log) if "detect" in c["args"]])


# --- TASK-1606 ------------------------------------------------------------------------------------------------


class DesignPassTests(_VisualCase):
    def test_shortcuts(self) -> None:
        from hefesto import visual

        self.assertEqual(visual.SHORTCUTS, {
            "impacto": ("critique", "bolder", "polish"),
            "robustez": ("audit", "harden", "optimize"),
            "tipografia": ("typeset", "layout", "polish"),
        })

    def test_parse_suggestions(self) -> None:
        from hefesto import visual

        text = ("Prioridad: ejecuta /impeccable layout y después `impeccable typeset`. Luego $impeccable layout otra "
                "vez, impeccable craft, impeccable init, impeccable inventado e impeccable polish.")
        self.assertEqual(visual.parse_suggestions(text), ["layout", "typeset", "polish"])

    def test_plan_rules(self) -> None:
        from hefesto import visual

        plan = visual.plan_pass("critique", ["animate", "craft", "init", "overdrive", "detect", "critique",
                                             "layout", "polish", "bolder", "typeset"])
        self.assertEqual(plan, ["layout", "bolder", "typeset", "animate", "polish"])
        self.assertEqual(visual.plan_pass(None, ["bolder", "layout"]), ["layout", "polish"])
        self.assertEqual(visual.plan_pass("audit", []), ["polish"])
        self.assertEqual(visual.plan_pass("audit", ["polish", "polish"]).count("polish"), 1)

    def _pass(self, diagnosis_text: str, fail_on: str | None = None, **kw: Any):
        from hefesto import visual

        calls: list[tuple[str, str]] = []
        checkpoints: list[str] = []
        self.events: list[str] = []

        def checkpoint(label: str) -> None:
            checkpoints.append(label)
            self.events.append("checkpoint")

        def delegate(role: str, instruction: str) -> tuple[bool, str]:
            calls.append((role, instruction))
            self.events.append(role)
            if fail_on and f"impeccable {fail_on}" in instruction:
                return False, "falló"
            return True, diagnosis_text if role == "reviewer" else "hecho"

        report = visual.design_pass(self.project, self.cfg(), self.task(), delegate, checkpoint, **kw)
        return report, calls, checkpoints

    def prepare_product(self) -> None:
        self.prepare()
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")

    def test_design_pass_runs_diagnosis_commands_and_polish(self) -> None:
        self.prepare_product()
        report, calls, checkpoints = self._pass("Sugerencias: impeccable typeset e impeccable layout.")
        self.assertTrue(report.ok)
        self.assertEqual(report.steps, ["critique", "typeset", "layout", "polish"])
        self.assertEqual(calls[0][0], "reviewer")
        self.assertIn("impeccable critique", calls[0][1])
        self.assertEqual([r for r, _ in calls[1:]], ["visual", "visual", "visual"])
        self.assertIn("impeccable polish", calls[-1][1])
        self.assertEqual(len(checkpoints), 3)
        self.assertIn("polish", checkpoints[-1])
        self.assertEqual(self.events, ["reviewer", "visual", "checkpoint", "visual", "checkpoint", "visual",
                                       "checkpoint"])
        self.assertEqual(report.report_path,
                         self.project / ".hefesto" / "design" / "reports" / "TASK-001-critique.md")
        self.assertIn("impeccable typeset", report.report_path.read_text(encoding="utf-8"))
        for _, instruction in calls:
            self.assertNotIn("impeccable craft", instruction)
            self.assertIn("web/index.html", instruction)

    def test_failed_step_stops_without_polish(self) -> None:
        self.prepare_product()
        report, calls, checkpoints = self._pass("impeccable typeset impeccable layout", fail_on="typeset")
        self.assertFalse(report.ok)
        self.assertEqual(report.failed, "typeset")
        self.assertFalse(any("impeccable polish" in i for _, i in calls))
        self.assertEqual(checkpoints, [])

    def test_shortcut_and_audit(self) -> None:
        self.prepare_product()
        report, calls, _ = self._pass("sin sugerencias", shortcut="robustez")
        self.assertEqual(report.steps, ["audit", "harden", "optimize", "polish"])
        self.assertIn("impeccable audit", calls[0][1])

    def test_shortcut_ignores_diagnosis_suggestions(self) -> None:
        self.prepare_product()
        report, calls, _ = self._pass("Sugerencias: impeccable layout e impeccable typeset.", shortcut="impacto")
        self.assertEqual(report.steps, ["critique", "bolder", "polish"])
        self.assertEqual(calls[0][0], "reviewer")
        self.assertFalse(any("impeccable layout" in i or "impeccable typeset" in i for _, i in calls[1:]))

    def test_shortcut_without_diagnosis(self) -> None:
        self.prepare_product()
        report, calls, _ = self._pass("impeccable audit", shortcut="tipografia")
        self.assertTrue(report.ok)
        self.assertEqual(report.steps, ["typeset", "layout", "polish"])
        self.assertEqual([r for r, _ in calls], ["visual", "visual", "visual"])
        self.assertIsNone(report.report_path)

    def test_failed_diagnosis_stops_everything(self) -> None:
        self.prepare_product()
        report, calls, checkpoints = self._pass("impeccable layout", fail_on="critique")
        self.assertFalse(report.ok)
        self.assertEqual(report.failed, "critique")
        self.assertEqual(len(calls), 1)
        self.assertEqual(checkpoints, [])
        self.assertIsNone(report.report_path)

    def test_only_for_app_or_web_tasks(self) -> None:
        from hefesto import visual

        self.prepare(BACKEND_ROADMAP)
        (self.project / "PRODUCT.md").write_text(PRODUCT_READY, encoding="utf-8")
        with self.assertRaises(HefestoError):
            visual.design_pass(self.project, self.cfg(), self.task(), lambda r, i: (True, ""), lambda label: None)

    def test_requires_a_real_product_md(self) -> None:
        from hefesto import visual

        self.prepare()
        calls: list[str] = []

        def delegate(role: str, instruction: str) -> tuple[bool, str]:
            calls.append(instruction)
            return True, ""

        with self.assertRaises(HefestoError) as ctx:
            visual.design_pass(self.project, self.cfg(), self.task(), delegate, lambda label: None)
        self.assertIn("impeccable init", str(ctx.exception))
        self.assertEqual(calls, [])
        code, out, err = self.cli("design", "pass", "TASK-001", "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("impeccable init", out + err)

    def test_cli_dry_run_prints_the_plan(self) -> None:
        self.prepare_product()
        code, out, err = self.cli("design", "pass", "TASK-001", "--dry-run", "--shortcut", "robustez")
        self.assertEqual(code, 0, out + err)
        self.assertIn("audit → harden → optimize → polish", out)
        code, out, err = self.cli("design", "pass", "TASK-001", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("critique", out)
        self.assertIn("polish", out)

    def test_cli_without_dry_run_runs_the_pass(self) -> None:
        from types import SimpleNamespace

        from hefesto.adapters.base import AdapterResult
        from hefesto.router import RunOutcome

        self.prepare_product()
        for ok in (True, False):
            with self.subTest(ok=ok):
                seen: dict[str, Any] = {}

                def fake_pass(project: Any, cfg: Any, task: Any, delegate: Any, checkpoint: Any,
                              diagnosis: str = "critique", shortcut: str | None = None) -> Any:
                    seen["shortcut"] = shortcut
                    seen["good"] = delegate("visual", "impeccable harden en web/index.html")
                    seen["bad"] = delegate("visual", "impeccable optimize en web/index.html")
                    checkpoint("harden")
                    return SimpleNamespace(ok=ok, steps=["audit", "harden", "optimize", "polish"],
                                           failed=None if ok else "optimize", report_path=None)

                good = RunOutcome(0, "r1", AdapterResult(True, "codex", "default", "hecho", None, "unknown", "x.log"))
                bad = RunOutcome(1, "r2", AdapterResult(False, "codex", "default", "roto", "crash", "unknown", "x.log"))
                with mock.patch("hefesto.visual.design_pass", side_effect=fake_pass), \
                        mock.patch("hefesto.router.run", side_effect=[good, bad]) as route, \
                        mock.patch("hefesto.gitops.checkpoint", return_value="abc123") as commit:
                    code, out, err = self.cli("design", "pass", "TASK-001", "--shortcut", "robustez")
                self.assertEqual(seen["shortcut"], "robustez")
                self.assertEqual(seen["good"], (True, "hecho"))
                self.assertFalse(seen["bad"][0])
                self.assertEqual(route.call_count, 2)
                args, kwargs = route.call_args_list[0]
                self.assertEqual(args[1] if len(args) > 1 else kwargs.get("role"), "visual")
                self.assertIn("impeccable harden", args[2] if len(args) > 2 else kwargs.get("instruction"))
                self.assertEqual(kwargs.get("task_id"), "TASK-001")
                commit.assert_called_once()
                self.assertIn("harden", json.dumps([str(a) for a in commit.call_args.args]
                                                   + [str(v) for v in commit.call_args.kwargs.values()]))
                if ok:
                    self.assertEqual(code, 0, out + err)
                    self.assertIn("harden", out)
                else:
                    self.assertNotEqual(code, 0)

    def test_next_protocol_mentions_design_pass(self) -> None:
        from hefesto.util import FRAMEWORK_ROOT

        text = (FRAMEWORK_ROOT / "prompts" / "protocol" / "next.md").read_text(encoding="utf-8")
        self.assertIn("hefesto design pass", text)


# --- TASK-1607 ------------------------------------------------------------------------------------------------


class ReviewerVisualTests(_VisualCase):
    def seed_artifacts(self, count: int = 6) -> Path:
        from hefesto import visual

        folder = visual.artifacts_dir(self.project, "TASK-001")
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (folder / f"root-{i}.png").write_bytes(b"\x89PNG fake")
        (folder / "detect.json").write_text(json.dumps({
            "findings": [finding("warning", rule="flat-type-hierarchy")],
            "note": "El detector no prueba que la página funcione.",
        }), encoding="utf-8")
        return folder

    def test_reviewer_images_respect_max(self) -> None:
        from hefesto import visual

        self.prepare(max_images=3)
        self.seed_artifacts()
        images = visual.reviewer_images(self.project, self.cfg(), self.task())
        self.assertEqual(len(images), 3)
        self.assertTrue(all(p.suffix == ".png" for p in images))

    def test_reviewer_packet_includes_visual_evidence(self) -> None:
        from hefesto import packet

        self.prepare()
        self.seed_artifacts(2)
        rm = roadmap.load_valid(self.project)
        task = rm.get("TASK-001")
        rev = packet.build(self.project, self.cfg(), "reviewer", "Revisa", task, rm, None, "codex:default",
                           self.project).text
        self.assertIn("Evidencia visual", rev)
        self.assertIn("flat-type-hierarchy", rev)
        self.assertIn("root-0.png", rev)
        self.assertIn("no prueba que la página funcione", rev)
        run = packet.build(self.project, self.cfg(), "visual", "Haz", task, rm, None, "codex:default",
                           self.project).text
        self.assertNotIn("Evidencia visual", run)

    def _routed_call(self, role: str) -> Any:
        from hefesto import router
        from hefesto.adapters.base import AdapterResult

        captured: list[Any] = []

        class _Fake:
            def run(self, call: Any) -> AdapterResult:
                captured.append(call)
                return AdapterResult(True, "codex", "default", "VEREDICTO: pass", None, "unknown", "x.log")

        with mock.patch("hefesto.preflight.ensure", return_value=(True, "")), \
                mock.patch("hefesto.adapters.get", return_value=_Fake()):
            router.run(self.project, role, "Revisa", task_id="TASK-001", entries=["codex"])
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_router_sends_images_only_to_reviewer_of_ui_tasks(self) -> None:
        self.prepare(max_images=3)
        self.seed_artifacts(5)
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        images = self._routed_call("reviewer").extra["images"]
        self.assertEqual(len(images), 3)
        self.assertTrue(all(Path(p).suffix == ".png" for p in images))
        self.assertFalse(self._routed_call("visual").extra.get("images"))

    def test_router_sends_no_images_for_backend_tasks(self) -> None:
        self.prepare(BACKEND_ROADMAP)
        self.seed_artifacts(2)
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self._routed_call("reviewer").extra.get("images"))

    def _call(self, images: list[Path]):
        from hefesto.adapters.base import Call

        return Call(role="reviewer", prompt="p", task_id="TASK-001", run_id="r", model="default",
                    workdir=self.tmp, project=self.tmp, extra={"images": images})

    def test_codex_attaches_images(self) -> None:
        from hefesto.adapters.codex import CodexAdapter

        self.init_project()
        images = [self.tmp / "a.png", self.tmp / "b.png"]
        argv = CodexAdapter(self.cfg()).command("codex", self._call(images))
        for img in images:
            i = argv.index(str(img))
            self.assertEqual(argv[i - 1], "--image")
        self.assertEqual(argv[-1], "-")

    def test_claude_and_gemini_get_text_only(self) -> None:
        from hefesto.adapters.claude import ClaudeAdapter
        from hefesto.adapters.gemini import GeminiAdapter

        self.init_project()
        images = [self.tmp / "a.png"]
        for adapter, binary in ((ClaudeAdapter(self.cfg()), "claude"), (GeminiAdapter(self.cfg()), "gemini")):
            argv = adapter.command(binary, self._call(images))
            self.assertNotIn(str(images[0]), argv)
            self.assertNotIn("--image", argv)

    def test_no_images_keeps_codex_command_unchanged(self) -> None:
        from hefesto.adapters.codex import CodexAdapter

        self.init_project()
        argv = CodexAdapter(self.cfg()).command("codex", self._call([]))
        self.assertNotIn("--image", argv)
