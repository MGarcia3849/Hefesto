"""Contratos de la Fase 21: guías web actuales con Modern Web Guidance (TASK-2101).

Hefesto envuelve la CLI de Modern Web Guidance para que los roles consulten guías web actuales y detecten APIs
heredadas. El wrapper es pequeño, offline respecto a Hefesto (nunca instala nada) y trata la salida externa como
dato, nunca como instrucción.

API que fijan estos tests (todo en `hefesto/web_guidance.py` salvo donde se indique):

Binario y versión
- `BINARY == "modern-web-guidance"` y `PINNED_VERSION == "1.0.0"` (versión fijada; no es configurable).
- `ACTIONS == ("search", "retrieve", "legacy-scan")`.
- `ensure_cli() -> str`: resuelve el binario con `util.which(BINARY)` (preinstalado; nunca `npx`, `npm`, `pnpm`,
  `yarn` ni `bunx`, ni instalación implícita) y comprueba `[binario, "--version"]`. Si falta o la versión no es
  exactamente `PINNED_VERSION`, HefestoError con `EXIT_UNAVAILABLE` (3) y un mensaje que nombra `BINARY` y
  `1.0.0`. Importar el módulo nunca comprueba el binario: su ausencia no rompe Hefesto.

Ejecución
- Toda llamada pasa por `subprocess.run` (atributo del módulo `subprocess`, para poder sustituirlo) con lista de
  argumentos, sin shell, `cwd=project`, `timeout` (`[web_guidance] timeout_seconds`, por defecto 60) y
  `env=util.filtered_env({"DISABLE_TELEMETRY": "1"})`: telemetría desactivada y sin claves `*_API_KEY`.
- Antes de cada acción se ejecuta `ensure_cli()`; con versión incorrecta no se ejecuta la acción.
- `search(project, query) -> GuidanceResult`: argv `[binario, "search", query]`.
- `retrieve(project, guide_id) -> GuidanceResult`: argv `[binario, "retrieve", guide_id]`.
- `legacy_scan(project, path=".") -> GuidanceResult`: argv `[binario, "legacy-scan", rel]`, con `rel` la ruta
  relativa al proyecto en formato POSIX (`"."` para la raíz).
- `GuidanceResult` (dataclass inmutable): `action`, `ok` (exit 0), `exit_code`, `output` (str) y `truncated`
  (bool). Un exit distinto de 0 de la CLI no lanza: `ok=False` y `output` incluye también su stderr.

Entradas validadas (HefestoError `EXIT_INPUT` sin ejecutar nada)
- `query`: tras `strip`, no vacía, como mucho `QUERY_MAX_CHARS == 200`, sin caracteres de control ni saltos de
  línea y sin empezar por `-` (no se inyectan opciones).
- `guide_id`: casa con `GUIDE_ID_RE` (letras, dígitos, `.`, `_`, `-` y `/`; empieza por letra o dígito; máx. 128),
  sin segmentos `..`, sin `\\`, sin ruta absoluta.
- `path` de legacy-scan: confinada al proyecto tras resolver (también symlinks); relativa o absoluta dentro del
  proyecto; debe existir.

Salidas acotadas y redactadas
- `output` pasa por `guard.redact` y después se recorta a `[web_guidance] max_output_chars` (por defecto
  `MAX_OUTPUT_CHARS == 20000`); `len(output) <= max_output_chars` incluida cualquier marca de recorte, y
  `truncated` lo indica.

CLI (`hefesto/cli.py`)
- `hefesto web-guidance search QUERY`, `hefesto web-guidance retrieve ID`, `hefesto web-guidance legacy-scan
  [RUTA]`: imprimen `output` en stdout; salen con 0 si `ok`, 1 si la CLI falló o la entrada es inválida, y 3
  (con el mensaje en stderr) si falta el binario o su versión no es la fijada.

Solo `ContractDefinitionTests` pasa antes de la implementación; el resto falla por `hefesto.web_guidance` ausente.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config
from hefesto.util import EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError, read_jsonl
from tests.helpers import HefestoTestCase

BINARY_NAME = "modern-web-guidance"

# CLI falsa de Modern Web Guidance: registra cada llamada junto a sí misma y responde según `scenario.json`.
FAKE_CLI = """import json, os, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
scenario_path = here / "scenario.json"
scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}
tool, args = sys.argv[1], sys.argv[2:]  # el lanzador pasa su propio nombre como primer argumento
with (here / "calls.jsonl").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": tool, "argv": args, "cwd": os.getcwd(),
                         "telemetry": os.environ.get("DISABLE_TELEMETRY"),
                         "leaked": sorted(k for k in os.environ if k.upper().endswith("_API_KEY"))}) + "\\n")
if args[:1] == ["--version"]:
    print(scenario.get("version", "1.0.0"))
    sys.exit(0)
action = args[0] if args else ""
text = scenario.get(action, action + ": " + " ".join(args[1:]))
print(text * int(scenario.get("repeat", 1)))
code = int(scenario.get("exit", 0))
if code:
    print("error de la guia: " + action, file=sys.stderr)
sys.exit(code)
"""


def _wg() -> Any:
    from hefesto import web_guidance  # import diferido: el contrato compila sin la implementación

    return web_guidance


def _secret() -> str:
    return "sk-" + "ant-" + "api03-" + "ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210"  # nunca literal: lo bloquea el guard


class GuidanceCase(HefestoTestCase):
    install_cli = True

    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / "src").mkdir()
        (self.project / "src" / "app.js").write_text("document.write('hola');\n", encoding="utf-8")
        self.fakedir = self.tmp / "mwg"
        self.fakedir.mkdir()
        self.script = self.fakedir / "fake_cli.py"
        self.script.write_text(FAKE_CLI, encoding="utf-8")
        bindir = self.tmp / "mwg-bin"
        bindir.mkdir()
        names = ["npx", "npm"] + ([BINARY_NAME] if self.install_cli else [])
        for name in names:  # npx/npm falsos: si el wrapper intentara instalar, quedaría registrado
            self.launcher(bindir, name)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]

    def launcher(self, bindir: Path, name: str) -> None:
        if os.name == "nt":
            (bindir / f"{name}.cmd").write_text(
                f'@echo off\r\n"{sys.executable}" "{self.script}" {name} %*\r\n', encoding="utf-8")
        else:
            dest = bindir / name
            dest.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.script}" {name} "$@"\n', encoding="utf-8")
            dest.chmod(0o755)

    def scenario(self, **data: Any) -> None:
        (self.fakedir / "scenario.json").write_text(json.dumps(data), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        return read_jsonl(self.fakedir / "calls.jsonl")

    def action_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["argv"][:1] != ["--version"]]

    def installers(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["tool"].lower() in ("npx", "npm")]


# --- Definición del contrato (pasa antes de implementar) --------------------------------------------------------


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {
        "CliDiscoveryTests": 4, "MissingCliTests": 3, "SearchTests": 3, "RetrieveTests": 3, "LegacyScanTests": 4,
        "OutputTests": 3, "CommandLineTests": 4,
    }
    API = ("BINARY", "PINNED_VERSION", "1.0.0", "ACTIONS", "ensure_cli", "EXIT_UNAVAILABLE", "DISABLE_TELEMETRY",
           "filtered_env", "search", "retrieve", "legacy_scan", "GuidanceResult", "QUERY_MAX_CHARS",
           "GUIDE_ID_RE", "MAX_OUTPUT_CHARS", "max_output_chars", "timeout_seconds", "guard.redact",
           "web-guidance search", "web-guidance retrieve", "web-guidance legacy-scan")

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
        # Si se importara arriba, el módulo no compilaría sin la implementación y nada fallaría «por el motivo
        # correcto»: cada test de comportamiento debe fallar con ModuleNotFoundError de `hefesto.web_guidance`.
        for node in self.tree().body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "hefesto.web_guidance")
                if node.module == "hefesto":
                    self.assertNotIn("web_guidance", [a.name for a in node.names])
            if isinstance(node, ast.Import):
                self.assertNotIn("hefesto.web_guidance", [a.name for a in node.names])

    def test_contract_is_offline_and_never_uses_a_shell(self) -> None:
        source = self.source()
        self.assertNotIn("shell=" + "True", source)
        self.assertNotIn("url" + "lib", source)
        self.assertNotIn("import " + "requests", source)
        self.assertNotIn("http" + "://", source)
        self.assertNotIn("https" + "://", source)

    def test_fake_cli_is_valid_python_and_the_name_matches(self) -> None:
        compile(FAKE_CLI, "fake_cli.py", "exec")
        self.assertIn(f'`BINARY == "{BINARY_NAME}"`', ast.get_docstring(self.tree()) or "")

    def test_behaviour_tests_fail_for_the_right_reason(self) -> None:
        # Ausencia de implementación demostrada sin depender de ella: si el módulo aún no existe, el fallo es
        # un ImportError que nombra `web_guidance` (y no un error del propio contrato).
        try:
            _wg()
        except ImportError as exc:
            self.assertIn("web_guidance", str(exc))


# --- Binario preinstalado y versión fijada ----------------------------------------------------------------------


class CliDiscoveryTests(GuidanceCase):
    def test_constants(self) -> None:
        wg = _wg()
        self.assertEqual(wg.BINARY, BINARY_NAME)
        self.assertEqual(wg.PINNED_VERSION, "1.0.0")
        self.assertEqual(wg.ACTIONS, ("search", "retrieve", "legacy-scan"))

    def test_preinstalled_cli_with_pinned_version_is_found(self) -> None:
        path = _wg().ensure_cli()
        self.assertEqual(Path(path).stem, BINARY_NAME)
        self.assertEqual([c["argv"] for c in self.calls()], [["--version"]])
        self.assertEqual(self.installers(), [])

    def test_wrong_version_is_unavailable_and_runs_no_action(self) -> None:
        self.scenario(version="1.1.0")
        with self.assertRaises(HefestoError) as ctx:
            _wg().ensure_cli()
        self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE)
        self.assertIn("1.0.0", str(ctx.exception))
        with self.assertRaises(HefestoError) as ctx:
            _wg().search(self.project, "container queries")
        self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE)
        self.assertEqual(self.action_calls(), [])

    def test_calls_disable_telemetry_and_strip_api_keys(self) -> None:
        with mock.patch.dict(os.environ, {"WEBFAKE_API_KEY": "no-debe-salir"}):
            _wg().search(self.project, "container queries")
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["telemetry"], "1", call)
            self.assertEqual(call["leaked"], [], call)

    def test_every_call_is_an_argument_list_without_shell_and_with_timeout(self) -> None:
        real = subprocess.run
        seen: list[tuple[Any, dict[str, Any]]] = []

        def spy(cmd: Any, *a: Any, **kw: Any) -> Any:
            seen.append((cmd, kw))
            return real(cmd, *a, **kw)

        with mock.patch("subprocess.run", side_effect=spy):
            _wg().retrieve(self.project, "css/container-queries")
        self.assertTrue(seen)
        for cmd, kw in seen:
            self.assertIsInstance(cmd, (list, tuple))
            self.assertNotEqual(kw.get("shell"), True, cmd)
            self.assertTrue(kw.get("timeout"), cmd)
            self.assertEqual(kw.get("env", {}).get("DISABLE_TELEMETRY"), "1", cmd)
            self.assertNotIn(Path(str(cmd[0])).stem.lower(), {"npx", "npm", "pnpm", "yarn", "bunx"})


class MissingCliTests(GuidanceCase):
    install_cli = False

    def test_missing_cli_is_unavailable_and_never_installed(self) -> None:
        wg = _wg()  # importar el módulo sin binario no falla
        with self.assertRaises(HefestoError) as ctx:
            wg.ensure_cli()
        self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE)
        self.assertIn(BINARY_NAME, str(ctx.exception))
        self.assertIn("1.0.0", str(ctx.exception))
        for action in (lambda: wg.search(self.project, "fetch priority"),
                       lambda: wg.retrieve(self.project, "html/popover"),
                       lambda: wg.legacy_scan(self.project, "src")):
            with self.assertRaises(HefestoError) as ctx:
                action()
            self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE)
        self.assertEqual(self.calls(), [])  # ni npx ni npm: nada se instala ni se descarga

    def test_cli_exits_3_with_a_clear_message(self) -> None:
        _wg()
        code, out, err = self.cli("web-guidance", "search", "container queries")
        self.assertEqual(code, EXIT_UNAVAILABLE, out + err)
        self.assertIn(BINARY_NAME, err)
        self.assertEqual(self.calls(), [])

    def test_missing_cli_does_not_break_hefesto(self) -> None:
        _wg()
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, out + err)


# --- Búsqueda ---------------------------------------------------------------------------------------------------


class SearchTests(GuidanceCase):
    def test_search_runs_the_pinned_cli(self) -> None:
        self.scenario(search="Usa @container en lugar de media queries por componente")
        result = _wg().search(self.project, "  container queries  ")
        self.assertTrue(result.ok)
        self.assertEqual((result.action, result.exit_code, result.truncated), ("search", 0, False))
        self.assertIn("@container", result.output)
        actions = self.action_calls()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["argv"], ["search", "container queries"])
        self.assertEqual(Path(actions[0]["cwd"]).resolve(), self.project.resolve())

    def test_invalid_queries_are_rejected_without_running(self) -> None:
        wg = _wg()
        cases = {"vacía": "", "solo espacios": "   ", "opción inyectada": "--help", "salto de línea": "a\nb",
                 "control": "a\x00b", "demasiado larga": "x" * (wg.QUERY_MAX_CHARS + 1)}
        self.assertEqual(wg.QUERY_MAX_CHARS, 200)
        for label, query in cases.items():
            with self.subTest(caso=label):
                with self.assertRaises(HefestoError) as ctx:
                    wg.search(self.project, query)
                self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(self.calls(), [])

    def test_failed_cli_is_reported_not_raised(self) -> None:
        self.scenario(exit=2)
        result = _wg().search(self.project, "view transitions")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("error de la guia", result.output)


# --- Recuperación -----------------------------------------------------------------------------------------------


class RetrieveTests(GuidanceCase):
    def test_retrieve_a_guide(self) -> None:
        self.scenario(retrieve="# Container queries\nGuía completa")
        result = _wg().retrieve(self.project, "css/container-queries")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "retrieve")
        self.assertIn("Guía completa", result.output)
        self.assertEqual(self.action_calls()[0]["argv"], ["retrieve", "css/container-queries"])

    def test_invalid_ids_are_rejected_without_running(self) -> None:
        wg = _wg()
        for guide_id in ("", "-v", "../secreto", "css/../../x", "a b", "C:\\x", "/abs", "x" * 129, "a\nb"):
            with self.subTest(guide_id=guide_id):
                with self.assertRaises(HefestoError) as ctx:
                    wg.retrieve(self.project, guide_id)
                self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(self.calls(), [])

    def test_valid_ids_match_the_pattern(self) -> None:
        import re

        pattern = re.compile(_wg().GUIDE_ID_RE)
        for guide_id in ("css/container-queries", "html.popover", "js_temporal-2", "a"):
            with self.subTest(guide_id=guide_id):
                self.assertTrue(pattern.fullmatch(guide_id))


# --- Legacy-scan con rutas confinadas ---------------------------------------------------------------------------


class LegacyScanTests(GuidanceCase):
    def test_scans_a_relative_path_inside_the_project(self) -> None:
        self.scenario(**{"legacy-scan": "src/app.js:1 document.write es heredado"})
        result = _wg().legacy_scan(self.project, "src")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "legacy-scan")
        self.assertIn("document.write", result.output)
        call = self.action_calls()[0]
        self.assertEqual(call["argv"], ["legacy-scan", "src"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.project.resolve())

    def test_default_and_absolute_paths_are_made_relative(self) -> None:
        wg = _wg()
        wg.legacy_scan(self.project)
        wg.legacy_scan(self.project, str(self.project / "src" / "app.js"))
        self.assertEqual([c["argv"] for c in self.action_calls()],
                         [["legacy-scan", "."], ["legacy-scan", "src/app.js"]])

    def test_paths_outside_the_project_are_rejected(self) -> None:
        wg = _wg()
        outside = self.tmp / "fuera"
        outside.mkdir()
        for path in ("..", "../fuera", str(outside), "src/../../fuera"):
            with self.subTest(path=path):
                with self.assertRaises(HefestoError) as ctx:
                    wg.legacy_scan(self.project, path)
                self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(self.calls(), [])

    def test_symlink_escape_and_missing_paths_are_rejected(self) -> None:
        wg = _wg()
        with self.assertRaises(HefestoError) as ctx:
            wg.legacy_scan(self.project, "no-existe")
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        outside = self.tmp / "fuera"
        outside.mkdir()
        link = self.project / "enlace"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("el sistema no permite crear enlaces simbólicos")
        with self.assertRaises(HefestoError) as ctx:
            wg.legacy_scan(self.project, "enlace")
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(self.action_calls(), [])


# --- Salidas acotadas y redactadas ------------------------------------------------------------------------------


class OutputTests(GuidanceCase):
    def test_output_is_bounded_by_default(self) -> None:
        wg = _wg()
        self.assertEqual(wg.MAX_OUTPUT_CHARS, 20000)
        self.scenario(search="x" * 1000, repeat=30)
        result = wg.search(self.project, "grid")
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output), wg.MAX_OUTPUT_CHARS)

    def test_limit_comes_from_project_config(self) -> None:
        _wg()  # primero el módulo: sin implementación, el fallo es ModuleNotFoundError y no de configuración
        config.set_setting(config.project_settings_path(self.project), ("web_guidance", "max_output_chars"), 300)
        self.scenario(retrieve="y" * 2000)
        result = _wg().retrieve(self.project, "css/grid")
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output), 300)
        self.assertGreater(len(result.output), 0)

    def test_output_is_redacted(self) -> None:
        self.scenario(search="clave filtrada: " + _secret())
        result = _wg().search(self.project, "fetch")
        self.assertNotIn("ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210", result.output)
        self.assertIn("clave filtrada", result.output)
        self.assertFalse(result.truncated)


# --- CLI de Hefesto ---------------------------------------------------------------------------------------------


class CommandLineTests(GuidanceCase):
    def test_search_prints_the_output(self) -> None:
        self.scenario(search="Usa structuredClone")
        code, out, err = self.cli("web-guidance", "search", "deep copy")
        self.assertEqual(code, 0, out + err)
        self.assertIn("structuredClone", out)

    def test_retrieve_and_legacy_scan(self) -> None:
        code, out, err = self.cli("web-guidance", "retrieve", "html/popover")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("web-guidance", "legacy-scan", "src")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([c["argv"] for c in self.action_calls()],
                         [["retrieve", "html/popover"], ["legacy-scan", "src"]])

    def test_invalid_input_exits_1_without_running(self) -> None:
        code, out, err = self.cli("web-guidance", "legacy-scan", "../fuera")
        self.assertEqual(code, EXIT_INPUT, out + err)
        code, out, err = self.cli("web-guidance", "retrieve", "../secreto")
        self.assertEqual(code, EXIT_INPUT, out + err)
        self.assertEqual(self.calls(), [])

    def test_failed_guidance_cli_exits_1_and_output_is_redacted(self) -> None:
        self.scenario(exit=1, search="token " + _secret())
        code, out, err = self.cli("web-guidance", "search", "fetch")
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210", out + err)

    def test_wrong_version_exits_3(self) -> None:
        self.scenario(version="0.9.0")
        code, out, err = self.cli("web-guidance", "search", "fetch")
        self.assertEqual(code, EXIT_UNAVAILABLE, out + err)
        self.assertIn("1.0.0", err)
        self.assertEqual(self.action_calls(), [])
        self.assertEqual(self.installers(), [])
