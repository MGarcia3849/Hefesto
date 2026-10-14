"""Contrato (TASK-701 → TASK-706/707): modo local, el predeterminado al instalar Hefesto en un proyecto.

Petición del usuario: «que se quede todo dentro de una carpeta que no se suba a GitHub, sin ensuciar el
proyecto, pero que se pueda usar».

Interfaz esperada:
- `hefesto init` (sin `--shared`) instala en **modo local**:
  - Todo lo de Hefesto vive en `.hefesto/` (estado, ROADMAP, DECISIONS, LEARNINGS, evidencias, logs,
    settings y el protocolo en `.hefesto/AGENTS.md`).
  - No crea `AGENTS.md`, `CLAUDE.md` ni `GEMINI.md` en la raíz, ni toca un `.gitignore` existente (ni lo crea).
  - `sync` genera solo los puntos de entrada que cada CLI necesita: `.claude/settings.local.json` (hooks y
    permisos), `.claude/skills/hefesto-*/`, `.claude/agents/hefesto-*.md`, `.agents/skills/hefesto-*/`,
    `.codex/hooks.json`, `.codex/config.toml`, `.gemini/settings.json`, `.gemini/commands/hefesto/`.
  - Todas esas rutas y `.hefesto/` se añaden (sin duplicar) a `.git/info/exclude`, que git no versiona.
  - Resultado: tras `init`, `git status --porcelain` está vacío.
  - El modo queda en `.hefesto/settings.json` como `"install_mode": "local"`.
  - Los hooks de inicio de sesión ejecutan `hefesto status --protocol`, que imprime el estado y el protocolo
    (contenido de `.hefesto/AGENTS.md`), para que la CLI lo conozca sin ficheros en la raíz.
- En modo local, `done` hace commits que contienen **solo** ficheros de la tarea (ningún `.hefesto/…`), las
  evidencias quedan solo en local y `evidence verify` sigue funcionando; `finish` integra solo código.
- `tests/helpers.py::HefestoTestCase.init_project` pasa a usar `--shared`: los tests existentes prueban el
  modo compartido (hacen commits de ficheros de `.hefesto/`). Lo cambia TASK-706 junto con el modo local.
- `hefesto init --shared` conserva el comportamiento anterior (ficheros en la raíz, `.gitignore`, ROADMAP y
  evidencias versionados) y guarda `"install_mode": "shared"`.
"""

from __future__ import annotations

import json
import os

from hefesto import config
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear a
  - verify: `test -f src/a.py`
  - files: src/
  - role: runner
  - size: S
"""

GENERATED = (
    ".claude/settings.local.json",
    ".claude/skills/hefesto-next/SKILL.md",
    ".claude/agents/hefesto-runner.md",
    ".agents/skills/hefesto-next/SKILL.md",
    ".codex/hooks.json",
    ".gemini/settings.json",
    ".gemini/commands/hefesto/next.toml",
)


class LocalInstallTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project.mkdir()
        git(self.project, "init", "-q")
        (self.project / "CLAUDE.md").write_text("# Instrucciones propias del proyecto\n", encoding="utf-8")
        (self.project / "app.py").write_text("print('hola')\n", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "proyecto del usuario")

    def init_local(self) -> None:
        code, out, err = self.run_cli("init", str(self.project), "--providers", "gemini,codex")
        self.assertEqual(code, 0, out + err)

    def test_init_leaves_git_status_clean(self) -> None:
        self.init_local()
        self.assertEqual(git(self.project, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual((self.project / "CLAUDE.md").read_text(encoding="utf-8"), "# Instrucciones propias del proyecto\n")
        for name in ("AGENTS.md", "GEMINI.md", ".gitignore"):
            self.assertFalse((self.project / name).exists(), name)
        exclude = (self.project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".hefesto/", exclude.splitlines())
        settings = json.loads((self.project / ".hefesto" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["install_mode"], "local")

    def test_entry_points_exist_and_are_excluded(self) -> None:
        self.init_local()
        self.assertTrue((self.project / ".hefesto" / "AGENTS.md").exists())
        for rel in GENERATED:
            self.assertTrue((self.project / rel).exists(), rel)
            self.assertEqual(git(self.project, "check-ignore", rel, check=False), rel, f"{rel} no está excluido")
        hooks = json.loads((self.project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0]["command"], "hefesto status --protocol")

    def test_repeated_init_does_not_duplicate_excludes(self) -> None:
        self.init_local()
        self.init_local()
        lines = (self.project / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count(".hefesto/"), 1)
        self.assertEqual(git(self.project, "status", "--porcelain", "--untracked-files=all"), "")

    def test_status_protocol_prints_protocol(self) -> None:
        self.init_local()
        code, out, _ = self.cli("status", "--protocol")
        self.assertEqual(code, 0)
        self.assertIn("hefesto: IDLE", out)
        self.assertIn("Protocolo Hefesto", out)

    def test_task_commits_contain_only_task_files(self) -> None:
        self.use_fakes("claude", "codex", "gemini")
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({"gemini": {"mode": "ok", "write": {"src/a.py": "a = 1\n"}},
                                        "*": {"mode": "ok", "summary": "VEREDICTO: pass"}}), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(scenario)
        self.init_local()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        self.assertEqual(self.cli("trust", tty_value=True, answers=["s"])[0], 0)
        main = git(self.project, "symbolic-ref", "--short", "HEAD")
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        self.assertEqual(self.cli("run", "runner", "implementa")[0], 0)
        _, out, _ = self.cli("run", "reviewer", "revisa")
        run_id = json.loads(out)["run_id"]
        self.assertEqual(self.cli("review", "TASK-001", "--verdict", "pass", "--run-id", run_id)[0], 0)
        code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 0, out + err)
        branch_files = git(self.project, "log", "--name-only", "--format=", f"{main}..hefesto/fase-1-nucleo")
        self.assertEqual(sorted({f for f in branch_files.splitlines() if f}), ["src/a.py"])
        self.assertEqual(self.cli("evidence", "verify")[0], 0)
        code, out, err = self.cli("finish", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(self.project, "show", "--name-only", "--format=", "HEAD").split(), ["src/a.py"])
        self.assertEqual(git(self.project, "status", "--porcelain", "--untracked-files=all"), "")

    def test_shared_mode_keeps_previous_behaviour(self) -> None:
        code, out, err = self.run_cli("init", str(self.project), "--shared", "--providers", "gemini")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.project / "AGENTS.md").exists())
        self.assertIn(".hefesto/state.json", (self.project / ".gitignore").read_text(encoding="utf-8"))
        settings = config.read_settings(config.project_settings_path(self.project))
        self.assertEqual(settings["install_mode"], "shared")
