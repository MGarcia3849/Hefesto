"""Contratos de la Fase 15: catálogo de skills y herramientas por rol, modo y tipo de tarea (ADR 2026-10-17).

Regla del usuario: las skills no pertenecen a ningún proveedor. El orquestador decide qué agente o subagente usa
cada skill o herramienta, en qué modo y para qué. Dónde esté instalada una skill en disco no decide quién la usa.

API que fijan estos tests:

hefesto/roadmap.py (TASK-1502)
- campo opcional `kind` por tarea: `roadmap.TASK_KINDS == ("app", "web", "backend", "docs", "infra")`
- `Task.kind -> str`: el valor explícito (sin distinguir mayúsculas) o, sin él, `roadmap.infer_kind(task.files)`
- `infer_kind(files: list[str]) -> str`: determinista; clasifica cada fichero y gana el tipo con más ficheros
  (empate: app > web > backend > infra > docs; sin ficheros: backend). Componentes (`.component.*`, `.tsx`,
  `.jsx`, `.vue`, `.svelte`) → app; `.html`/`.css`/`.scss` sueltos → web; `.md` o `docs/` → docs;
  `Dockerfile`, `.github/`, `*.yml`/`*.yaml` → infra; el resto → backend.
- un `kind` desconocido es error de `roadmap.validate` (el mensaje nombra `kind`)

hefesto/skill_catalog.py + catalog/skills.toml (TASK-1503)
- `CATALOG_PATH`: `<FRAMEWORK_ROOT>/catalog/skills.toml`
- `ALLOWED_LICENSES`: licencias permitidas (permisivas; nunca AGPL ni propietarias)
- `ROLES == ("core", "runner", "forge", "visual", "reviewer", "verify")`
- `load(path: Path | None = None) -> dict[str, CatalogEntry]` — HefestoError ante cualquier entrada inválida
- `CatalogEntry`: name, kind ("skill" | "tool"), source ("owner/repo"), commit (40 hex, obligatorio en skills),
  path (opcional), license, roles (tuple), modes (dict rol → tuple de modos; un rol sin modos usa la skill
  entera), kinds (tuple; vacía = todos), interactive (tuple de modos que exigen un humano), providers (tuple:
  preferencia, nunca exclusiva), binary (obligatorio en tools)
- formato TOML: `[skills.<nombre>]` con esos campos y `[skills.<nombre>.modes]`; claves desconocidas son error

hefesto/skill_select.py + hefesto/packet.py + `hefesto route` (TASK-1504)
- `from_catalog(catalog, role, task, provider=None) -> list[CatalogChoice]` (CatalogChoice: name, modes, reasons)
  filtra por rol y `kind`; quita los modos `interactive` a cualquier rol que no sea `core` y descarta la entrada
  si no le queda ninguno; el rol `verify` nunca devuelve nada (las herramientas de verify no van en paquetes);
  con `provider`, las entradas que lo prefieren van primero, pero las demás siguen entrando
- el paquete nombra cada entrada del catálogo con «modos: a, b»; `hefesto route TASK --dry-run` lo explica
  para cada rol de la ruta (implementación, reviewer…), no solo para el de la tarea

hefesto/skill_catalog.py + `hefesto skill sync` + catalog/packs.toml (TASK-1505)
- `skills.fetch(spec, commit=None)`: con `commit`, trae exactamente ese commit
- `sync(catalog=None) -> SyncResult` (promoted: list[str], rejected: list[tuple[str, str]],
  skipped: list[tuple[str, str]]): por cada skill, `skills.fetch(f"{source}@{name}", commit=commit)`; promueve
  con `skills.promote(nombre_propuesta, yes=True)` solo si no hay riesgos, el commit descargado es el fijado y
  AgentShield (si está) no ve nada alto; las tools se saltan; nunca `npx` ni instaladores
- el pack `design` de packs.toml usa exactamente los nombres de taste-skill del catálogo

hefesto/skills.py + skill_auto.py + doctor.py (TASK-1506)
- `skills.agentshield_scan(path: Path) -> list[dict[str, str]] | None`: None si `agentshield` no está en el
  PATH (nunca vía npx); si está, ejecuta exactamente `[binario, "scan", "--path", str(path), "--format",
  "json"]` sin shell y devuelve `findings[]` del informe JSON (cada uno con `severity`), también si el binario
  sale con código distinto de 0; falla cerrado: salida no JSON, sin `findings`, error sin salida, OSError o
  timeout devuelven un hallazgo `high` que explica el fallo
- un hallazgo `high` o `critical` (sin distinguir mayúsculas) descarta la skill en `skill_auto.ensure` y en
  `skill_catalog.sync` con un motivo que nombra «agentshield»
- `doctor.run_checks` incluye el check `agentshield`, nunca obligatorio; con el binario, propone `agentshield scan`
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import config, roadmap, util
from hefesto.util import FRAMEWORK_ROOT, HefestoError
from tests.helpers import HefestoTestCase, git

SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def task_from(files: str, kind: str | None = None, role: str = "runner") -> roadmap.Task:
    extra = f"\n  - kind: {kind}" if kind is not None else ""
    text = f"""# ROADMAP

## Fase 1 — Prueba
- [ ] TASK-001: Tarea de prueba
  - verify: `true`
  - files: {files}
  - role: {role}
  - size: M{extra}
"""
    return roadmap.parse_text(text).get("TASK-001")


SYNTHETIC = f"""
[skills.pulidor]
kind = "skill"
source = "acme/pulidor"
commit = "{SHA_A}"
license = "MIT"
roles = ["core", "reviewer", "visual", "verify"]
kinds = ["app", "web"]
interactive = ["iniciar-humano"]

[skills.pulidor.modes]
core = ["iniciar-humano", "pase-final"]
reviewer = ["diagnostico"]
visual = ["pase-final"]
verify = ["detector"]

[skills.salida-completa]
kind = "skill"
source = "acme/salida"
commit = "{SHA_B}"
license = "MIT"
roles = ["runner", "visual", "forge"]
providers = ["gemini"]

[skills.entrevista]
kind = "skill"
source = "acme/entrevista"
commit = "{SHA_A}"
license = "Apache-2.0"
roles = ["core", "runner"]
interactive = ["preguntar"]

[skills.entrevista.modes]
core = ["preguntar"]
runner = ["preguntar"]

[skills.solo-verify]
kind = "tool"
source = "acme/detector"
license = "MIT"
roles = ["verify"]
binary = "detector"

[skills.backend-reglas]
kind = "skill"
source = "acme/backend"
commit = "{SHA_B}"
license = "MIT"
roles = ["runner", "forge"]
kinds = ["backend"]
"""


class _CatalogFile(HefestoTestCase):
    def write_catalog(self, text: str) -> Path:
        path = self.tmp / "skills.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def synthetic(self) -> dict[str, Any]:
        from hefesto import skill_catalog

        return skill_catalog.load(self.write_catalog(SYNTHETIC))


# --- TASK-1502 ------------------------------------------------------------------------------------------------


class TaskKindTests(HefestoTestCase):
    def test_kinds_constant(self) -> None:
        self.assertEqual(roadmap.TASK_KINDS, ("app", "web", "backend", "docs", "infra"))

    def test_explicit_kind_wins_and_is_case_insensitive(self) -> None:
        self.assertEqual(task_from("hefesto/cli.py", kind="Docs").kind, "docs")

    def test_inferred_from_files(self) -> None:
        cases = {
            "src/app/avisos/aviso.component.ts, src/app/avisos/aviso.component.css": "app",
            "src/App.tsx": "app",
            "web/index.html, web/estilos.css": "web",
            "hefesto/cli.py, hefesto/router.py": "backend",
            "README.md, docs/guia.md": "docs",
            "Dockerfile, .github/workflows/ci.yml": "infra",
            "hefesto/cli.py, README.md": "backend",
        }
        for files, expected in cases.items():
            with self.subTest(files=files):
                self.assertEqual(task_from(files).kind, expected)
                self.assertEqual(roadmap.infer_kind([f.strip() for f in files.split(",")]), expected)

    def test_infer_kind_without_files_is_backend(self) -> None:
        self.assertEqual(roadmap.infer_kind([]), "backend")

    def test_unknown_kind_is_a_validation_error(self) -> None:
        self.init_project()
        text = """# ROADMAP

## Fase 1 — Prueba
- [ ] TASK-001: Tarea de prueba
  - verify: `true`
  - files: hefesto/cli.py
  - role: runner
  - kind: movil
"""
        errors = roadmap.validate(roadmap.parse_text(text), self.project)
        # El error es el del valor, no el de «campo desconocido» (que ya existía antes de la Fase 15).
        self.assertTrue(any("kind" in e and "movil" in e for e in errors), errors)
        self.assertFalse(any("campo desconocido" in e for e in errors), errors)

    def test_known_kind_validates(self) -> None:
        self.init_project()
        text = """# ROADMAP

## Fase 1 — Prueba
- [ ] TASK-001: Tarea de prueba
  - verify: `true`
  - files: web/index.html
  - role: visual
  - kind: web
"""
        self.assertEqual(roadmap.validate(roadmap.parse_text(text), self.project), [])


# --- TASK-1503 ------------------------------------------------------------------------------------------------


class CatalogTests(_CatalogFile):
    def real(self) -> dict[str, Any]:
        from hefesto import skill_catalog

        return skill_catalog.load()

    def test_catalog_path_and_constants(self) -> None:
        from hefesto import skill_catalog

        self.assertEqual(Path(skill_catalog.CATALOG_PATH), FRAMEWORK_ROOT / "catalog" / "skills.toml")
        self.assertEqual(skill_catalog.ROLES, ("core", "runner", "forge", "visual", "reviewer", "verify"))
        for lic in ("MIT", "Apache-2.0"):
            self.assertIn(lic, skill_catalog.ALLOWED_LICENSES)
        for lic in ("AGPL-3.0", "Proprietary", "GPL-3.0"):
            self.assertNotIn(lic, skill_catalog.ALLOWED_LICENSES)

    def test_synthetic_catalog_loads_fields(self) -> None:
        cat = self.synthetic()
        pul = cat["pulidor"]
        self.assertEqual(pul.name, "pulidor")
        self.assertEqual(pul.kind, "skill")
        self.assertEqual(pul.source, "acme/pulidor")
        self.assertEqual(pul.commit, SHA_A)
        self.assertEqual(pul.license, "MIT")
        self.assertEqual(tuple(pul.roles), ("core", "reviewer", "visual", "verify"))
        self.assertEqual(tuple(pul.modes["reviewer"]), ("diagnostico",))
        self.assertEqual(tuple(pul.kinds), ("app", "web"))
        self.assertEqual(tuple(pul.interactive), ("iniciar-humano",))
        self.assertEqual(tuple(cat["salida-completa"].providers), ("gemini",))
        self.assertEqual(tuple(cat["salida-completa"].kinds), ())
        self.assertEqual(cat["solo-verify"].binary, "detector")

    def test_invalid_entries_are_rejected(self) -> None:
        from hefesto import skill_catalog

        base = 'kind = "skill"\nsource = "acme/x"\ncommit = "' + SHA_A + '"\nlicense = "MIT"\nroles = ["runner"]\n'
        bad = {
            "licencia AGPL": base.replace('"MIT"', '"AGPL-3.0"'),
            "licencia propietaria": base.replace('"MIT"', '"Proprietary"'),
            "rol desconocido": base.replace('["runner"]', '["director"]'),
            "kind de tarea desconocido": base + 'kinds = ["movil"]\n',
            "skill sin commit": base.replace(f'commit = "{SHA_A}"\n', ""),
            "commit no fijado": base.replace(SHA_A, "main"),
            "tool sin binary": base.replace('kind = "skill"', 'kind = "tool"'),
            "atada a un proveedor": base + 'provider_only = "codex"\n',
            "exclusiva": base + "exclusive = true\n",
            "modos de un rol que no tiene": base + '[skills.x.modes]\nreviewer = ["critica"]\n',
            "interactivo que no es modo": base + 'interactive = ["fantasma"]\n',
            "proveedor desconocido": base + 'providers = ["openai-api"]\n',
        }
        for label, body in bad.items():
            with self.subTest(label=label):
                path = self.write_catalog("[skills.x]\n" + body)
                with self.assertRaises(HefestoError):
                    skill_catalog.load(path)

    def test_real_catalog_has_the_agreed_entries(self) -> None:
        cat = self.real()
        for name in ("impeccable", "web-design-guidelines", "playwright-cli", "agentshield"):
            self.assertIn(name, cat)
        sources = {e.source for e in cat.values()}
        self.assertIn("Leonxlnx/taste-skill", sources)
        self.assertIn("affaan-m/ECC", sources)
        self.assertEqual(cat["web-design-guidelines"].source, "vercel-labs/agent-skills")
        self.assertEqual(cat["impeccable"].source, "pbakaus/impeccable")
        self.assertEqual(cat["impeccable"].license, "Apache-2.0")
        self.assertEqual(cat["playwright-cli"].license, "Apache-2.0")
        self.assertIn("verify", cat["playwright-cli"].roles)

    def test_real_catalog_pins_every_skill_and_names_every_tool_binary(self) -> None:
        for entry in self.real().values():
            with self.subTest(entry=entry.name):
                if entry.kind == "skill":
                    self.assertRegex(entry.commit, HEX40)
                else:
                    self.assertTrue(entry.binary)
                self.assertNotIn("npx", (entry.binary or ""))

    def test_impeccable_follows_its_v4_flow(self) -> None:
        imp = self.real()["impeccable"]
        modes = {role: tuple(m) for role, m in imp.modes.items()}
        every_mode = {m for ms in modes.values() for m in ms}
        self.assertTrue({"init", "shape"} <= set(imp.interactive))
        self.assertTrue({"init", "shape"} <= set(modes.get("core", ())))
        for delegated in ("runner", "forge", "visual", "reviewer"):
            self.assertFalse({"init", "shape"} & set(modes.get(delegated, ())), delegated)
        self.assertNotIn("document", imp.interactive)
        self.assertTrue(any("document" in ms for role, ms in modes.items() if role != "core"))
        self.assertTrue({"critique", "audit"} <= set(modes.get("reviewer", ())))
        self.assertTrue({"polish", "harden"} <= set(modes.get("visual", ())))
        self.assertEqual(modes.get("verify"), ("detect",))
        for role, ms in modes.items():
            if role != "verify":
                self.assertNotIn("detect", ms, role)
        self.assertNotIn("craft", every_mode)
        self.assertNotIn("craft", imp.interactive)

    def test_real_catalog_has_no_single_provider_entry(self) -> None:
        from hefesto import skill_catalog

        raw = tomllib.loads(Path(skill_catalog.CATALOG_PATH).read_text(encoding="utf-8"))
        for name, body in raw["skills"].items():
            with self.subTest(entry=name):
                self.assertFalse({"provider_only", "exclusive", "only_provider"} & set(body))


# --- TASK-1504 ------------------------------------------------------------------------------------------------


ROADMAP_VISUAL = """# ROADMAP

## Fase 1 — Interfaz
- [ ] TASK-001: Panel de avisos
  - verify: `true`
  - files: src/app/avisos/aviso-panel.component.ts
  - role: visual
  - size: M
"""


class SelectionTests(_CatalogFile):
    def choose(self, role: str, task: roadmap.Task, provider: str | None = None) -> dict[str, Any]:
        from hefesto import skill_select

        return {c.name: c for c in skill_select.from_catalog(self.synthetic(), role, task, provider=provider)}

    def test_role_and_kind_filter(self) -> None:
        app_task = task_from("src/app/x.component.ts", role="visual")
        visual = self.choose("visual", app_task)
        self.assertIn("pulidor", visual)
        self.assertIn("salida-completa", visual)
        self.assertNotIn("backend-reglas", visual)
        backend = self.choose("runner", task_from("hefesto/cli.py"))
        self.assertIn("backend-reglas", backend)
        self.assertNotIn("pulidor", backend)
        # Mismo rol, distinto tipo de tarea: decide el `kind`, no el rol.
        self.assertNotIn("backend-reglas", self.choose("runner", task_from("src/app/x.component.ts")))
        self.assertNotIn("pulidor", self.choose("visual", task_from("hefesto/cli.py", role="visual")))
        self.assertIn("pulidor", self.choose("visual", task_from("web/index.html", role="visual")))
        self.assertIn("salida-completa", self.choose("runner", task_from("README.md")))

    def test_modes_per_role(self) -> None:
        app_task = task_from("src/app/x.component.ts", role="visual")
        self.assertEqual(tuple(self.choose("reviewer", app_task)["pulidor"].modes), ("diagnostico",))
        self.assertEqual(tuple(self.choose("visual", app_task)["pulidor"].modes), ("pase-final",))
        self.assertEqual(tuple(self.choose("visual", app_task)["salida-completa"].modes), ())

    def test_interactive_modes_only_for_core(self) -> None:
        app_task = task_from("src/app/x.component.ts", role="visual")
        core = self.choose("core", app_task)
        self.assertIn("iniciar-humano", core["pulidor"].modes)
        self.assertIn("entrevista", core)
        runner = self.choose("runner", app_task)
        self.assertNotIn("entrevista", runner)
        for role in ("runner", "forge", "visual", "reviewer"):
            for choice in self.choose(role, app_task).values():
                self.assertNotIn("iniciar-humano", choice.modes)
                self.assertNotIn("preguntar", choice.modes)

    def test_verify_never_enters_a_packet(self) -> None:
        app_task = task_from("src/app/x.component.ts", role="visual")
        self.assertEqual(self.choose("verify", app_task), {})
        for role in ("core", "runner", "forge", "visual", "reviewer"):
            self.assertNotIn("solo-verify", self.choose(role, app_task))
            pul = self.choose(role, app_task).get("pulidor")
            if pul is not None:
                self.assertNotIn("detector", pul.modes)

    def test_provider_is_a_preference_not_an_owner(self) -> None:
        from hefesto import skill_select

        app_task = task_from("src/app/x.component.ts", role="visual")
        cat = self.synthetic()
        gem = [c.name for c in skill_select.from_catalog(cat, "visual", app_task, provider="gemini")]
        cod = [c.name for c in skill_select.from_catalog(cat, "visual", app_task, provider="codex")]
        self.assertEqual(gem[0], "salida-completa")
        self.assertIn("salida-completa", cod)
        self.assertEqual(set(gem), set(cod))
        choice = next(c for c in skill_select.from_catalog(cat, "visual", app_task, provider="gemini")
                      if c.name == "salida-completa")
        self.assertTrue(choice.reasons)

    def prepare(self) -> None:
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_VISUAL, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def test_packet_names_modes_and_hides_interactive_and_verify(self) -> None:
        from hefesto import packet

        self.prepare()
        cfg = config.load(self.project)
        rm = roadmap.load_valid(self.project)
        task = rm.get("TASK-001")
        with mock.patch("hefesto.skill_catalog.load", return_value=self.synthetic()):
            rev = packet.build(self.project, cfg, "reviewer", "Revisa", task, rm, None, "codex:default", self.project)
            vis = packet.build(self.project, cfg, "visual", "Haz", task, rm, None, "codex:default", self.project)
        self.assertIn("pulidor", rev.text)
        self.assertIn("modos: diagnostico", rev.text)
        self.assertIn("modos: pase-final", vis.text)
        for text in (rev.text, vis.text):
            self.assertNotIn("iniciar-humano", text)
            self.assertNotIn("solo-verify", text)
            self.assertNotIn("detector", text)

    def test_route_dry_run_explains_catalog_choice(self) -> None:
        self.prepare()
        with mock.patch("hefesto.skill_catalog.load", return_value=self.synthetic()):
            code, out, err = self.cli("route", "TASK-001", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pulidor", out)
        self.assertIn("diagnostico", out)
        self.assertIn("pase-final", out)
        self.assertNotIn("iniciar-humano", out)


# --- TASK-1505 ------------------------------------------------------------------------------------------------


SAFE_SKILL = """---
name: pulidor
description: Pule interfaces.
---

Usa una escala fija.
"""


def fake_fetch_factory(commit_by_spec: dict[str, str] | None = None,
                       risks_by_spec: dict[str, list[dict[str, str]]] | None = None):
    commit_by_spec = commit_by_spec or {}
    risks_by_spec = risks_by_spec or {}

    def fake_fetch(spec: str, commit: str | None = None) -> Path:
        owner_repo, skill = spec.split("@", 1)
        owner = owner_repo.split("/", 1)[0]
        proposal = util.hefesto_home() / "skills" / "_proposals" / f"{owner}-{skill}"
        proposal.mkdir(parents=True, exist_ok=True)
        (proposal / "SKILL.md").write_text(SAFE_SKILL.replace("pulidor", skill), encoding="utf-8")
        (proposal / "SOURCE.json").write_text(json.dumps({
            "spec": spec, "commit": commit_by_spec.get(spec, commit), "files": {}, "trust": {},
            "risks": risks_by_spec.get(spec, []),
        }), encoding="utf-8")
        return proposal

    return fake_fetch


SYNC_CATALOG = f"""
[skills.pulidor]
kind = "skill"
source = "acme/pulidor"
commit = "{SHA_A}"
license = "MIT"
roles = ["visual"]

[skills.riesgosa]
kind = "skill"
source = "acme/riesgosa"
commit = "{SHA_A}"
license = "MIT"
roles = ["runner"]

[skills.movida]
kind = "skill"
source = "acme/movida"
commit = "{SHA_A}"
license = "MIT"
roles = ["runner"]

[skills.herramienta]
kind = "tool"
source = "acme/herramienta"
license = "MIT"
roles = ["verify"]
binary = "herramienta"
"""


class SyncTests(_CatalogFile):
    def sync(self, **fetch_kw: Any):
        from hefesto import skill_catalog

        cat = skill_catalog.load(self.write_catalog(SYNC_CATALOG))
        fetch = fake_fetch_factory(**fetch_kw)
        with mock.patch("hefesto.skills.fetch", side_effect=fetch) as fetched, \
                mock.patch("hefesto.skills.promote") as promote, \
                mock.patch("hefesto.skills.agentshield_scan", return_value=None, create=True), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no se lanzan procesos")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = skill_catalog.sync(cat)
        return result, fetched, promote

    def test_fetches_pinned_commit_and_promotes_clean_skills(self) -> None:
        result, fetched, promote = self.sync()
        specs = {c.args[0]: c.kwargs.get("commit") for c in fetched.call_args_list}
        self.assertEqual(specs["acme/pulidor@pulidor"], SHA_A)
        self.assertNotIn("acme/herramienta@herramienta", specs)
        self.assertIn("pulidor", result.promoted)
        promoted_names = [c.args[0] for c in promote.call_args_list]
        self.assertIn("acme-pulidor", promoted_names)
        for c in promote.call_args_list:
            self.assertTrue(c.kwargs.get("yes"))
        self.assertIn("herramienta", [name for name, _ in result.skipped])

    def test_risks_and_moved_commit_are_rejected(self) -> None:
        result, _, promote = self.sync(
            commit_by_spec={"acme/movida@movida": SHA_B},
            risks_by_spec={"acme/riesgosa@riesgosa": [{"kind": "script", "detail": "run.sh"}]},
        )
        rejected = dict(result.rejected)
        self.assertIn("riesgosa", rejected)
        self.assertIn("movida", rejected)
        self.assertIn("commit", rejected["movida"])
        promoted_names = [c.args[0] for c in promote.call_args_list]
        self.assertNotIn("acme-riesgosa", promoted_names)
        self.assertNotIn("acme-movida", promoted_names)
        self.assertFalse((util.hefesto_home() / "skills" / "_proposals" / "acme-riesgosa").exists())

    def test_never_runs_npx_or_installers(self) -> None:
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            argv = [str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd])]
            calls.append(argv)
            return real_run(cmd, *a, **kw)

        with mock.patch("subprocess.run", side_effect=spy):
            self.sync()
        for argv in calls:
            joined = " ".join(argv)
            self.assertNotIn("npx", joined)
            self.assertNotIn("skills add", joined)

    def test_cli_skill_sync(self) -> None:
        from hefesto import skill_catalog

        self.init_project()
        cat = skill_catalog.load(self.write_catalog(SYNC_CATALOG))
        with mock.patch("hefesto.skill_catalog.load", return_value=cat), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()), \
                mock.patch("hefesto.skills.promote"), \
                mock.patch("hefesto.skills.agentshield_scan", return_value=None, create=True):
            code, out, err = self.cli("skill", "sync")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pulidor", out)

    def test_fetch_with_commit_pins_that_commit(self) -> None:
        from hefesto import skills

        repo = self.tmp / "gh" / "acme" / "ui-kit.git"
        target = repo / "skills" / "pretty-ui" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(SAFE_SKILL.replace("pulidor", "pretty-ui"), encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "v1")
        first = git(repo, "rev-parse", "HEAD")
        target.write_text(SAFE_SKILL.replace("pulidor", "pretty-ui").replace("escala fija", "versión dos"),
                          encoding="utf-8")
        git(repo, "commit", "-q", "-am", "v2")
        config.set_setting(config.global_settings_path(), ("skills_sources", "git_base"), (self.tmp / "gh").as_posix())
        path = skills.fetch("acme/ui-kit@pretty-ui", commit=first)
        self.assertEqual(json.loads((path / "SOURCE.json").read_text(encoding="utf-8"))["commit"], first)
        self.assertIn("escala fija", (path / "SKILL.md").read_text(encoding="utf-8"))

    def test_design_pack_uses_real_taste_skill_names(self) -> None:
        from hefesto import skill_catalog

        packs = tomllib.loads((FRAMEWORK_ROOT / "catalog" / "packs.toml").read_text(encoding="utf-8"))
        design = set(packs["packs"]["design"]["skills"])
        taste = {e.name for e in skill_catalog.load().values() if e.source == "Leonxlnx/taste-skill"}
        self.assertTrue(taste)
        self.assertEqual(design, taste)


# --- TASK-1506 ------------------------------------------------------------------------------------------------


class AgentShieldTests(_CatalogFile):
    def test_scan_without_binary_returns_none_and_runs_nothing(self) -> None:
        from hefesto import skills

        with mock.patch("hefesto.util.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=AssertionError("sin binario no se ejecuta nada")):
            self.assertIsNone(skills.agentshield_scan(self.tmp))

    def _scan(self, result: Any) -> tuple[Any, list[tuple[list[str], dict[str, Any]]]]:
        """Ejecuta `agentshield_scan` con un binario simulado; `result` es un CompletedProcess o una excepción."""
        from hefesto import skills

        seen: list[tuple[list[str], dict[str, Any]]] = []
        binary = str(self.tmp / "bin" / "agentshield")

        def fake_run(cmd, *a, **kw):
            seen.append(([str(x) for x in cmd], kw))
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)

        with mock.patch("hefesto.util.which", side_effect=lambda n: binary if n == "agentshield" else None), \
                mock.patch("subprocess.run", side_effect=fake_run):
            findings = skills.agentshield_scan(self.tmp / "skill")
        return findings, seen

    @staticmethod
    def _report(*severities: str, code: int = 0) -> subprocess.CompletedProcess:
        findings = [{"severity": s, "title": f"hallazgo {s}", "file": "SKILL.md"} for s in severities]
        return subprocess.CompletedProcess([], code, json.dumps({"findings": findings}), "")

    @staticmethod
    def _blocking(findings: list[dict[str, str]]) -> bool:
        return any(str(f.get("severity", "")).lower() in ("high", "critical") for f in findings)

    def test_scan_runs_the_installed_binary_with_fixed_safe_arguments(self) -> None:
        findings, seen = self._scan(self._report())
        binary = str(self.tmp / "bin" / "agentshield")
        self.assertEqual(len(seen), 1)
        argv, kw = seen[0]
        self.assertEqual(argv, [binary, "scan", "--path", str(self.tmp / "skill"), "--format", "json"])
        self.assertFalse(kw.get("shell", False))
        self.assertNotIn("npx", " ".join(argv))
        self.assertEqual(findings, [])

    def test_scan_parses_the_json_report(self) -> None:
        findings, _ = self._scan(self._report("low", "HIGH", "medium"))
        self.assertEqual(sorted(str(f["severity"]).lower() for f in findings), ["high", "low", "medium"])
        self.assertTrue(self._blocking(findings))
        findings, _ = self._scan(self._report("low", "info"))
        self.assertFalse(self._blocking(findings))

    def test_scan_with_findings_and_nonzero_exit_still_reports_them(self) -> None:
        findings, _ = self._scan(self._report("critical", "low", code=1))
        self.assertTrue(self._blocking(findings))
        # Conserva el informe real, no lo sustituye por un hallazgo genérico.
        self.assertEqual(sorted(f.get("title") for f in findings), ["hallazgo critical", "hallazgo low"])

    def test_scan_fails_closed(self) -> None:
        broken = {
            "salida no JSON": subprocess.CompletedProcess([], 0, "esto no es json", ""),
            "JSON sin findings": subprocess.CompletedProcess([], 0, json.dumps({"score": 100}), ""),
            "error sin salida": subprocess.CompletedProcess([], 2, "", "boom"),
            "no arranca": OSError("no se puede ejecutar"),
            "se cuelga": subprocess.TimeoutExpired(["agentshield"], 60),
        }
        for label, result in broken.items():
            with self.subTest(caso=label):
                findings, _ = self._scan(result)
                self.assertIsInstance(findings, list)
                self.assertTrue(self._blocking(findings), findings)

    def test_sync_discards_high_findings(self) -> None:
        from hefesto import skill_catalog

        cat = skill_catalog.load(self.write_catalog(SYNC_CATALOG))
        with mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()), \
                mock.patch("hefesto.skills.promote") as promote, \
                mock.patch("hefesto.skills.agentshield_scan", return_value=[{"severity": "HIGH", "detail": "x"}]), \
                contextlib.redirect_stdout(io.StringIO()):
            result = skill_catalog.sync(cat)
        promote.assert_not_called()
        rejected = dict(result.rejected)
        self.assertIn("pulidor", rejected)
        self.assertIn("agentshield", rejected["pulidor"].lower())

    def test_sync_keeps_low_findings(self) -> None:
        from hefesto import skill_catalog

        cat = skill_catalog.load(self.write_catalog(SYNC_CATALOG))
        with mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()), \
                mock.patch("hefesto.skills.promote"), \
                mock.patch("hefesto.skills.agentshield_scan", return_value=[{"severity": "low", "detail": "x"}]), \
                contextlib.redirect_stdout(io.StringIO()):
            result = skill_catalog.sync(cat)
        self.assertIn("pulidor", result.promoted)

    def _auto(self, findings: list[dict[str, str]] | None):
        from hefesto import skill_auto

        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text("""# ROADMAP

## Fase 1 — Formularios
- [ ] TASK-001: Formularios reactivos con signals
  - verify: `true`
  - files: src/app/alta/alta.component.ts
  - role: runner
  - size: S
""", encoding="utf-8")
        config.set_setting(config.project_settings_path(self.project), ("skills_selection", "user_roots"),
                           [str(self.tmp / "nada")])
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        good = {"name": "acme/forms-kit@reactive-forms", "owner": "acme", "repo": "forms-kit",
                "skill": "reactive-forms", "installs": 25000, "stars": 1200, "trust": "media", "url": "unknown"}
        task = roadmap.load_valid(self.project).get("TASK-001")
        with mock.patch("hefesto.skills.find", return_value=[good]), \
                mock.patch("hefesto.skills.fetch", side_effect=fake_fetch_factory()), \
                mock.patch("hefesto.skills.promote") as promote, \
                mock.patch("hefesto.skills.agentshield_scan", return_value=findings):
            result = skill_auto.ensure(self.project, config.load(self.project), "runner", task)
        return result, promote

    def test_auto_install_discards_critical_findings(self) -> None:
        result, promote = self._auto([{"severity": "Critical", "detail": "exfiltra"}])
        promote.assert_not_called()
        self.assertEqual(result.installed, [])
        self.assertTrue(any("agentshield" in reason.lower() for _, reason in result.rejected))

    def test_auto_install_unchanged_without_binary(self) -> None:
        result, promote = self._auto(None)
        promote.assert_called_once()
        self.assertEqual(result.installed, ["reactive-forms"])

    def _doctor(self, agentshield: str | None) -> dict[str, Any]:
        from hefesto import doctor

        real = util.which

        def fake_which(name: str) -> str | None:
            return agentshield if name == "agentshield" else real(name)

        with mock.patch("hefesto.util.which", side_effect=fake_which),                 mock.patch("hefesto.doctor.which", side_effect=fake_which),                 mock.patch("hefesto.doctor._version", return_value="1.0.0"):
            return {c.name: c for c in doctor.run_checks(None)}

    def test_doctor_reports_agentshield_without_requiring_it(self) -> None:
        checks = self._doctor(None)
        self.assertIn("agentshield", checks)
        self.assertFalse(checks["agentshield"].required)

    def test_doctor_offers_scan_when_installed(self) -> None:
        checks = self._doctor(str(self.tmp / "bin" / "agentshield"))
        self.assertIn("agentshield scan", checks["agentshield"].detail)
