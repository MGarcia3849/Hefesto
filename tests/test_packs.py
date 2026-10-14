"""Contrato (TASK-804 → TASK-805/806): packs externos curados (sección 11.2 de HEFESTO_V2_1.md).

Catálogo `catalog/packs.toml` (ruta en `hefesto.packs.CATALOG_PATH`), una tabla `[packs.<nombre>]` por pack:
    kind = "tool" | "skills"
    source = "owner/repo"            # repositorio oficial en GitHub
    license = "..."
    default = "recommended" | "proposed" | "off"
    extra_cost = false               # true: genera coste fuera de las tres suscripciones
    requires = ["..."]               # texto informativo que doctor y pack list muestran
    # kind = "tool":
    binary = "graphify"              # ejecutable que debe estar en el PATH
    version = "0.9.13"               # versión fijada, exactamente X.Y.Z
    install = "uv tool install graphifyy==0.9.13"   # se MUESTRA al usuario; Hefesto nunca lo ejecuta
    # kind = "skills":
    skills = ["..."]                 # skills concretas que se toman del repositorio
    overlay = "texto"                # capa de adaptación de Hefesto; se escribe como OVERLAY.md
    # opcionales:
    confirm_changes = ["..."]        # cambios que el pack haría fuera de Hefesto; se muestran antes de confirmar
    policy_block = ["regex", ...]    # comandos que el hook bloquea mientras el pack está activo

El catálogo real contiene los 11 packs de la especificación: context, memory, usage, sandbox, spec, tokens,
design, anatomy, video, reverse y media-gen. Ninguna entrada menciona Docker.

`hefesto/packs.py`:
- `load_catalog() -> dict[str, dict]` (valida claves y tipos; `HefestoError` 1 si el catálogo es inválido).
- `list_packs(project) -> list[dict]`: `{"name", "kind", "default", "enabled", "extra_cost", "missing"}`, donde
  `missing` lista lo que falta (por ejemplo el binario de un pack tool).
- `enable(project, name, yes=False, accept_extra_cost=False) -> str`:
  - pack desconocido: código 1;
  - `extra_cost` sin `accept_extra_cost`: código 2 (mensaje con `--accept-extra-cost`);
  - muestra licencia, coste y `confirm_changes`; sin `yes` exige TTY y `util.confirm` (si no se confirma, nada
    cambia); con `yes`, las barreras de siempre (`HEFESTO_ROLE` → 2; `-C` → 1 en la CLI);
  - kind tool: si el binario no está, código 3 con la orden de `install` en el mensaje; nunca la ejecuta;
  - kind skills: para cada skill, `skills.fetch(f"{source}@{skill}")` y la promueve (sin volver a preguntar),
    marcando `"pack": nombre` en su `SOURCE.json` y escribiendo `OVERLAY.md` con el `overlay` del catálogo;
  - registra `settings["packs"][nombre] = {"enabled": true, ...}` y ejecuta `sync`.
- `disable(project, name) -> str`: marca `enabled: false` y ejecuta `sync`.
- `update(project, name) -> list[Path]`: para packs de skills, `skills.update` de cada skill (a propuestas, sin
  promover); para packs tool, informa de la versión fijada.
- `sync` materializa las skills promovidas a mano siempre, y las de un pack solo en proyectos con ese pack
  activo; además elimina los ficheros generados que ya no corresponden (si no se editaron a mano).
- El hook `pre-tool-use` bloquea (código 2) los comandos Bash que encajen en `policy_block` de un pack activo.

CLI: `hefesto pack list`, `hefesto pack enable NOMBRE [--yes] [--accept-extra-cost]`, `hefesto pack disable
NOMBRE`, `hefesto pack update NOMBRE`.

Pack `context` (Graphify), con el pack activo en el proyecto:
- `pack enable context` construye el grafo inicial con `graphify extract . --code-only` en el directorio del
  proyecto.
- `done` ejecuta `graphify update .` en el directorio de trabajo DESPUÉS de todos sus commits.
- El paquete de tarea incluye una sección `## Grafo de código` con la salida de
  `graphify query "<título de la tarea y sus files>" --budget N`; `hefesto resume --json` incluye `graph`.
- Toda llamada a graphify: entorno sin `*_API_KEY` y con `GRAPHIFY_QUERY_LOG_DISABLE=1`; nunca `install` ni
  `hook`. Si graphify no está instalado, el ciclo sigue (aviso, sin error).
- En modo local, `graphify-out/` está en `.git/info/exclude`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tomllib
from pathlib import Path
from unittest import mock

from hefesto import config, packs, util
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git, tty

SPEC_PACKS = {"context", "memory", "usage", "sandbox", "spec", "tokens", "design", "anatomy", "video", "reverse",
              "media-gen"}

TEST_CATALOG = """
[packs.toolpack]
kind = "tool"
source = "acme/tool"
license = "MIT"
default = "off"
extra_cost = false
requires = ["graphify"]
binary = "graphify"
version = "0.9.13"
install = "uv tool install graphifyy==0.9.13"

[packs.skillpack]
kind = "skills"
source = "acme/ui-kit"
license = "MIT"
default = "off"
extra_cost = false
requires = []
skills = ["pretty-ui"]
overlay = "Reglas de Hefesto para pretty-ui."

[packs.paid]
kind = "skills"
source = "acme/ui-kit"
license = "MIT"
default = "off"
extra_cost = true
requires = []
skills = ["pretty-ui"]
overlay = "x"

[packs.risky]
kind = "skills"
source = "acme/ui-kit"
license = "MIT"
default = "off"
extra_cost = false
requires = []
skills = ["pretty-ui"]
overlay = "x"
confirm_changes = ["Registra un servidor MCP en Claude Code, Codex y Gemini"]

[packs.blocked]
kind = "tool"
source = "acme/video"
license = "Apache-2.0"
default = "off"
extra_cost = false
requires = []
binary = "graphify"
version = "0.9.13"
install = "npm install -g acme-video@1.0.0"
policy_block = ["\\\\bcloud\\\\s+render\\\\b", "--lambda"]
"""

SKILL = "---\nname: pretty-ui\ndescription: Interfaces bonitas.\n---\n\n# Pretty UI\n"

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear suma
  - verify: `test -f src/suma.py`
  - files: src/
  - role: runner
  - size: S
"""


class PacksCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls)
        os.environ.pop("HEFESTO_ROLE", None)
        self.use_fakes()

    def graphify_calls(self) -> list[dict[str, object]]:
        return [c for c in util.read_jsonl(self.calls) if c["name"] == "graphify"]

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]

    def settings(self) -> dict[str, object]:
        return config.read_settings(config.project_settings_path(self.project))


class CatalogTests(PacksCase):
    def test_real_catalog_has_spec_packs_without_docker(self) -> None:
        catalog = packs.load_catalog()
        self.assertEqual(set(catalog), SPEC_PACKS)
        raw = packs.CATALOG_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("docker", raw)
        self.assertTrue(catalog["media-gen"]["extra_cost"])
        for name in ("context", "memory", "usage"):
            self.assertEqual(catalog[name]["default"], "recommended", name)
        self.assertIn("graphifyy", catalog["context"]["install"])
        for name, entry in catalog.items():
            self.assertIn(entry["kind"], ("tool", "skills"), name)
            if entry["kind"] == "tool":
                self.assertRegex(entry["version"], r"^\d+\.\d+\.\d+$", name)
                self.assertTrue(entry["binary"] and entry["install"], name)
            else:
                self.assertTrue(entry["skills"], name)
        self.assertTrue(catalog["video"]["policy_block"])
        self.assertTrue(any(re.search(p, "npx hyperframes cloud render intro") for p in catalog["video"]["policy_block"]))
        self.assertTrue(catalog["reverse"]["confirm_changes"])

    def test_invalid_catalog_is_rejected(self) -> None:
        bad = self.tmp / "bad.toml"
        bad.write_text('[packs.x]\nkind = "magia"\n', encoding="utf-8")
        with mock.patch("hefesto.packs.CATALOG_PATH", bad):
            with self.assertRaises(HefestoError) as ctx:
                packs.load_catalog()
        self.assertEqual(ctx.exception.code, 1)


class PackCommandTests(PacksCase):
    def setUp(self) -> None:
        super().setUp()
        catalog = self.tmp / "catalog.toml"
        catalog.write_text(TEST_CATALOG, encoding="utf-8")
        self.assertIn("toolpack", tomllib.loads(TEST_CATALOG)["packs"])
        patcher = mock.patch("hefesto.packs.CATALOG_PATH", catalog)
        patcher.start()
        self.addCleanup(patcher.stop)
        repo = self.tmp / "gh" / "acme" / "ui-kit.git"
        (repo / "skills" / "pretty-ui").mkdir(parents=True)
        (repo / "skills" / "pretty-ui" / "SKILL.md").write_text(SKILL, encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "skill")
        config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"), (self.tmp / "gh").as_posix())
        self.init_project()

    def test_list_shows_state_missing_and_cost(self) -> None:
        code, out, _ = self.cli("pack", "list")
        self.assertEqual(code, 0)
        for name in ("toolpack", "skillpack", "paid"):
            self.assertIn(name, out)
        listed = {p["name"]: p for p in packs.list_packs(self.project)}
        self.assertIn("graphify", " ".join(listed["toolpack"]["missing"]))
        self.assertTrue(listed["paid"]["extra_cost"])
        self.assertFalse(listed["toolpack"]["enabled"])

    def test_tool_pack_without_binary_shows_install_and_never_runs_it(self) -> None:
        code, out, err = self.here("pack", "enable", "toolpack", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("uv tool install graphifyy==0.9.13", out + err)
        self.assertFalse(self.settings().get("packs", {}).get("toolpack", {}).get("enabled", False))
        self.assertEqual(self.graphify_calls(), [])

    def test_tool_pack_enable_and_disable(self) -> None:
        self.use_fakes("graphify")
        code, out, err = self.here("pack", "enable", "toolpack", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.settings()["packs"]["toolpack"]["enabled"])
        self.assertTrue(all(c["argv"][0] not in ("install", "hook") for c in self.graphify_calls()))
        self.assertEqual(self.cli("pack", "disable", "toolpack")[0], 0)
        self.assertFalse(self.settings()["packs"]["toolpack"]["enabled"])

    def test_enable_needs_tty_or_yes_and_respects_barriers(self) -> None:
        self.use_fakes("graphify")
        self.assertEqual(self.here("pack", "enable", "toolpack")[0], 2)
        code, _, err = self.cli("pack", "enable", "toolpack", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("-C", err)
        os.environ["HEFESTO_ROLE"] = "orchestrator"
        try:
            code, _, err = self.here("pack", "enable", "toolpack", "--yes")
        finally:
            os.environ.pop("HEFESTO_ROLE")
        self.assertEqual(code, 2)
        self.assertIn("HEFESTO_ROLE", err)
        self.assertFalse(self.settings().get("packs", {}).get("toolpack", {}).get("enabled", False))
        code, out, err = self.here("pack", "enable", "toolpack", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.settings()["packs"]["toolpack"]["enabled"])

    def test_skills_pack_is_promoted_with_overlay_and_only_in_enabled_projects(self) -> None:
        code, out, err = self.here("pack", "enable", "skillpack", "--yes")
        self.assertEqual(code, 0, out + err)
        promoted = util.hefesto_home() / "skills" / "promoted" / "pretty-ui"
        self.assertEqual(json.loads((promoted / "SOURCE.json").read_text(encoding="utf-8"))["pack"], "skillpack")
        self.assertEqual((promoted / "OVERLAY.md").read_text(encoding="utf-8").strip(), "Reglas de Hefesto para pretty-ui.")
        self.assertTrue((self.project / ".claude" / "skills" / "pretty-ui" / "SKILL.md").exists())
        other = self.tmp / "otro"
        code, out, err = self.run_cli("init", str(other), "--shared", "--providers", "gemini")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((other / ".claude" / "skills" / "pretty-ui").exists())
        self.assertEqual(self.cli("pack", "disable", "skillpack")[0], 0)
        self.assertFalse((self.project / ".claude" / "skills" / "pretty-ui").exists())
        self.assertFalse((self.project / ".agents" / "skills" / "pretty-ui").exists())

    def test_extra_cost_requires_flag(self) -> None:
        code, out, err = self.here("pack", "enable", "paid", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("--accept-extra-cost", out + err)
        code, out, err = self.here("pack", "enable", "paid", "--yes", "--accept-extra-cost")
        self.assertEqual(code, 0, out + err)

    def test_changes_shown_before_confirmation(self) -> None:
        code, out, _ = self.here("pack", "enable", "risky", tty_value=True, answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("Registra un servidor MCP", out)
        self.assertFalse(self.settings().get("packs", {}).get("risky", {}).get("enabled", False))
        self.assertFalse((util.hefesto_home() / "skills" / "promoted" / "pretty-ui").exists())

    def test_unknown_pack(self) -> None:
        self.assertEqual(self.here("pack", "enable", "nopack", "--yes")[0], 1)

    def test_update_fetches_without_promoting(self) -> None:
        self.here("pack", "enable", "skillpack", "--yes")
        repo = self.tmp / "gh" / "acme" / "ui-kit.git"
        (repo / "skills" / "pretty-ui" / "SKILL.md").write_text(SKILL + "\nv2\n", encoding="utf-8")
        git(repo, "commit", "-q", "-am", "v2")
        code, out, err = self.cli("pack", "update", "skillpack")
        self.assertEqual(code, 0, out + err)
        promoted = util.hefesto_home() / "skills" / "promoted" / "pretty-ui" / "SKILL.md"
        self.assertNotIn("v2", promoted.read_text(encoding="utf-8"))
        proposal = util.hefesto_home() / "skills" / "_proposals" / "acme-pretty-ui" / "SKILL.md"
        self.assertIn("v2", proposal.read_text(encoding="utf-8"))


class PolicyTests(PacksCase):
    def test_policy_block_only_while_pack_enabled(self) -> None:
        catalog = self.tmp / "catalog.toml"
        catalog.write_text(TEST_CATALOG, encoding="utf-8")
        payload = json.dumps({"tool_name": "Bash", "cwd": str(self.tmp / "proyecto"),
                              "tool_input": {"command": "npx hyperframes cloud render intro"}})
        with mock.patch("hefesto.packs.CATALOG_PATH", catalog):
            self.init_project()
            self.assertEqual(self.run_cli("hook", "pre-tool-use", stdin=payload)[0], 0)
            self.use_fakes("graphify")
            self.assertEqual(self.here("pack", "enable", "blocked", "--yes")[0], 0)
            code, _, err = self.run_cli("hook", "pre-tool-use", stdin=payload)
        self.assertEqual(code, 2)
        self.assertIn("blocked", err)


class ContextPackTests(PacksCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["OPENAI_API_KEY"] = "sk-no-debe-verse"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        self.use_fakes("graphify")
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def enable(self) -> None:
        code, out, err = self.here("pack", "enable", "context", "--yes")
        self.assertEqual(code, 0, out + err)

    def complete_task(self) -> tuple[int, str]:
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "suma.py").write_text("def suma(a, b):\n    return a + b\n", encoding="utf-8")
        self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        code, out, err = self.cli("done", "TASK-001")
        return code, out + err

    def test_enable_builds_initial_graph_code_only(self) -> None:
        self.enable()
        calls = self.graphify_calls()
        self.assertEqual(calls[0]["argv"], ["extract", ".", "--code-only"])
        self.assertEqual(Path(str(calls[0]["cwd"])).resolve(), self.project.resolve())
        self.assertTrue((self.project / "graphify-out" / "graph.json").exists())

    def test_done_updates_graph_after_commits(self) -> None:
        self.enable()
        code, out = self.complete_task()
        self.assertEqual(code, 0, out)
        updates = [c for c in self.graphify_calls() if c["argv"] == ["update", "."]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["head"], git(self.project, "rev-parse", "HEAD"))

    def test_safe_environment_and_never_install(self) -> None:
        self.enable()
        self.complete_task()
        calls = self.graphify_calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["query_log_disabled"], "1")
            self.assertEqual(call["api_keys"], [])
            self.assertNotIn(call["argv"][0], ("install", "hook"))

    def test_packet_and_resume_include_graph(self) -> None:
        self.enable()
        self.cli("start", "TASK-001")
        code, out, _ = self.cli("packet", "TASK-001")
        self.assertEqual(code, 0)
        self.assertIn("## Grafo de código", out)
        self.assertIn("NODE suma()", out)
        queries = [c for c in self.graphify_calls() if c["argv"][0] == "query"]
        self.assertTrue(queries)
        self.assertIn("--budget", queries[0]["argv"])
        info = json.loads(self.cli("resume", "--json")[1])
        self.assertIn("NODE suma()", info["graph"])

    def test_cycle_continues_without_graphify(self) -> None:
        self.enable()
        self.use_fakes()
        code, out = self.complete_task()
        self.assertEqual(code, 0, out)

    def test_local_mode_excludes_graphify_out(self) -> None:
        local = self.tmp / "local"
        local.mkdir()
        git(local, "init", "-q")
        code, out, err = self.run_cli("init", str(local), "--providers", "gemini")
        self.assertEqual(code, 0, out + err)
        exclude = (local / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertIn("graphify-out/", exclude)
