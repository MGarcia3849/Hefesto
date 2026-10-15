"""Contrato (TASK-901 → TASK-902/903): aislamiento real, finish atómico, clean y discard (sección 10).

Lecciones que motivan este contrato (LEARNINGS, integración de la fase 8): un trabajador (agy) creó ficheros en
la copia principal porque el worktree vivía DENTRO del repositorio (`.hefesto/worktree`), y `finish` empezó a
preparar los ficheros de control antes de comprobar que el merge saldría bien.

Ubicación de los worktrees (TASK-902):
- Clave del proyecto: `gitops.project_key(project) -> str` = `<nombre de la carpeta>-<12 primeros hex del sha256 de
  la ruta resuelta en minúsculas>`.
- El worktree de una fase vive en `util.state_home()/worktrees/<clave>/<rama con "/" sustituido por "__">`, fuera
  del proyecto. Junto a esa carpeta, `util.state_home()/worktrees/<clave>/project.json` guarda
  `{"project": "<ruta del proyecto>"}`.
- `project.find_project(start)` devuelve el proyecto principal cuando `start` está dentro de uno de esos worktrees
  (leyendo `project.json`), además de los casos actuales.
- Un worktree antiguo en `.hefesto/worktree` sin cambios se elimina (`git worktree remove`) y se recrea fuera la
  próxima vez que se necesite; si tiene cambios, se sigue usando y `start` avisa.

finish atómico (TASK-902): `finish` comprueba TODAS las condiciones antes de modificar la copia principal
(ficheros, índice, HEAD) o la rama de la fase:
- tareas `[x]` con evidencia y cadena íntegra;
- ficheros sin versionar de la copia principal que el merge sobrescribiría → código 4 con la lista;
- ficheros de control modificados que no coinciden con la rama → código 4;
- el rebase se hace sobre una rama temporal (`hefesto/_finish/<fase>`) creada desde la punta de la fase; si hay
  conflicto (código 2) o falla la suite o un verify (código 1), la temporal se elimina y la rama de la fase y su
  worktree quedan exactamente como estaban; solo si todo pasa, la rama de la fase avanza a la temporal.
En cualquier fallo, la copia principal (ficheros, índice y HEAD) y la punta de la rama de la fase no cambian.

clean (TASK-903), `hefesto clean [--yes] [--include-unmerged]`:
- Huérfanos: worktrees de Hefesto (en `state_home()/worktrees/<clave>/` o el antiguo `.hefesto/worktree`) de una
  rama `hefesto/*` que no corresponde a ninguna fase abierta de `state.json`, y ramas `hefesto/*` sin fase abierta.
- Elimina los worktrees huérfanos sin cambios; NUNCA uno con cambios sin commitear (lo lista como conservado), ni el
  de una fase abierta, ni un worktree de una rama que no empiece por `hefesto/`.
- Elimina ramas huérfanas ya integradas en la rama principal; las no integradas solo con `--include-unmerged`.
  Nunca una rama de una fase abierta ni una rama que no empiece por `hefesto/`.
- Sin `--yes` exige TTY y confirmación (si no se confirma, no cambia nada); `--yes` con las barreras de siempre
  (`HEFESTO_ROLE` → 2, `-C` → 1).

discard (TASK-903), `hefesto discard TASK|FASE [--yes]` (mismas barreras; sin `--yes`, TTY y confirmación):
- Tarea en curso: cada ruta de su `files` vuelve exactamente a su estado en `task_base` (ficheros versionados
  modificados o en el índice, y nuevos sin versionar bajo esas rutas, que se eliminan); nada fuera de `files`
  cambia; marcador `[ ]`, estado IDLE.
- Tarea completada: `git revert --no-edit` de su commit de código en la rama de su fase (sin reescribir la
  historia), marcador `[ ]`; su evidencia se conserva intacta. Al rehacerla, `done` escribe `TASK-NNN.2.json` (luego
  `.3`…), encadenada como cualquier otra; `evidence verify` acepta `<tarea>.json` y `<tarea>.<n>.json` y sigue
  detectando la edición o el borrado de cualquiera de ellas.
- Fase: si alguna tarea de la fase está en curso, código 4 sin tocar nada; si su worktree tiene cambios sin
  commitear, código 2 sin tocar nada; si no, elimina su worktree y su rama y vuelve a `[ ]` los marcadores de sus
  tareas (sus evidencias se conservan; rehacer la fase escribe evidencias numeradas).
- Toda acción añade una entrada a `.hefesto/DECISIONS.md` («Descarte de …»).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

from hefesto import config, evidence, gitops, project, roadmap, state, util
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear suma
  - verify: `test -f src/suma.py`
  - files: src/
  - role: runner
  - size: S
- [ ] TASK-002: Crear resta
  - verify: `test -f src/resta.py`
  - files: src/
  - role: runner
  - depends_on: TASK-001
  - size: S
"""

BRANCH = "hefesto/fase-1-nucleo"


class IsolationCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        (self.project / "README.md").write_text("# Proyecto\n", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        self.main = git(self.project, "symbolic-ref", "--short", "HEAD")

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]

    def headless(self, *args: str) -> tuple[int, str]:
        os.environ["HEFESTO_ROLE"] = "orchestrator"
        try:
            code, _, err = self.here(*args)
        finally:
            os.environ.pop("HEFESTO_ROLE")
        return code, err

    def dirty_main(self) -> None:
        """Un cambio del usuario sin commitear fuerza el aislamiento por worktree."""
        (self.project / "notas-del-usuario.txt").write_text("mío\n", encoding="utf-8")

    def workdir(self) -> Path:
        return Path(state.load(self.project).get("workdir") or self.project)

    def complete(self, task: str, rel: str) -> None:
        self.assertEqual(self.cli("start", task)[0], 0)
        target = self.workdir() / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
        self.cli("review", task, "--verdict", "pass", "--author", "gemini", "--reviewer", "claude:sonnet")
        code, out, err = self.cli("done", task)
        self.assertEqual(code, 0, out + err)

    def snapshot(self) -> dict[str, str]:
        """Ficheros de la copia principal (sin .git ni logs) + índice + HEAD + punta de la rama de la fase."""
        out: dict[str, str] = {}
        for item in sorted(self.project.rglob("*")):
            rel = item.relative_to(self.project).as_posix()
            if item.is_file() and not rel.startswith(".git/") and "/logs/" not in f"/{rel}" \
                    and not rel.startswith(".hefesto/.lock") and rel != ".hefesto/state.json":
                out[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
        out["<index>"] = git(self.project, "ls-files", "-s")
        out["<HEAD>"] = git(self.project, "rev-parse", "HEAD")
        out["<branch>"] = git(self.project, "rev-parse", BRANCH, check=False)
        return out

    def phase_done(self) -> None:
        self.dirty_main()
        self.complete("TASK-001", "src/suma.py")
        self.complete("TASK-002", "src/resta.py")


class WorktreeLocationTests(IsolationCase):
    def test_project_key(self) -> None:
        key = gitops.project_key(self.project)
        digest = hashlib.sha256(str(self.project.resolve()).lower().encode("utf-8")).hexdigest()[:12]
        self.assertEqual(key, f"{self.project.name}-{digest}")

    def test_phase_worktree_lives_outside_the_project(self) -> None:
        self.dirty_main()
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        workdir = self.workdir().resolve()
        base = (util.state_home() / "worktrees" / gitops.project_key(self.project)).resolve()
        self.assertEqual(workdir, base / BRANCH.replace("/", "__"))
        self.assertNotIn(self.project.resolve(), workdir.parents)
        registry = json.loads((base / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(registry["project"]).resolve(), self.project.resolve())
        self.assertFalse((self.project / ".hefesto" / "worktree").exists())

    def test_find_project_from_outside_worktree(self) -> None:
        self.dirty_main()
        self.cli("start", "TASK-001")
        sub = self.workdir() / "src"
        sub.mkdir(exist_ok=True)
        self.assertEqual(project.find_project(sub).resolve(), self.project.resolve())
        with contextlib.chdir(sub):
            code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("TASK-001", out)

    def test_full_cycle_and_finish_remove_outside_worktree(self) -> None:
        self.phase_done()
        workdir = self.workdir()
        code, out, err = self.here("finish", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(workdir.exists())
        self.assertTrue((self.project / "src" / "resta.py").exists())
        self.assertEqual((self.project / "notas-del-usuario.txt").read_text(encoding="utf-8"), "mío\n")

    def test_legacy_clean_inner_worktree_is_replaced(self) -> None:
        legacy = self.project / ".hefesto" / "worktree"
        git(self.project, "branch", BRANCH)
        git(self.project, "worktree", "add", "-q", str(legacy), BRANCH)
        data = state.load(self.project)
        data["phases"] = {"fase-1-nucleo": {"branch": BRANCH, "isolation": "worktree", "workdir": str(legacy),
                                            "base": git(self.project, "rev-parse", "HEAD")}}
        state.save(self.project, data)
        self.dirty_main()
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        self.assertFalse(legacy.exists())
        self.assertNotIn(self.project.resolve(), self.workdir().resolve().parents)


class AtomicFinishTests(IsolationCase):
    def assert_untouched(self, before: dict[str, str], code: int, expected: int, out: str) -> None:
        self.assertEqual(code, expected, out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(git(self.project, "branch", "--list", "hefesto/_finish/*"), "")
        self.assertEqual(evidence.verify_chain(self.project, state.load(self.project).get("evidence_head")), [])

    def test_untracked_collision_aborts_before_touching_anything(self) -> None:
        self.phase_done()
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "suma.py").write_text("borrador del usuario\n", encoding="utf-8")
        before = self.snapshot()
        code, out, err = self.here("finish", "--yes")
        self.assertIn("src/suma.py", out + err)
        self.assert_untouched(before, code, 4, out + err)

    def test_mismatching_control_file_aborts_before_touching_anything(self) -> None:
        self.phase_done()
        decisions = self.project / ".hefesto" / "DECISIONS.md"
        decisions.write_text(decisions.read_text(encoding="utf-8") + "\nNota a mano no commiteada.\n", encoding="utf-8")
        before = self.snapshot()
        code, out, err = self.here("finish", "--yes")
        self.assert_untouched(before, code, 4, out + err)

    def test_rebase_conflict_leaves_branch_and_main_intact(self) -> None:
        self.phase_done()
        (self.project / "README.md").write_text("# Proyecto\nCambio en la principal.\n", encoding="utf-8")
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "suma.py").write_text("conflicto = True\n", encoding="utf-8")
        git(self.project, "add", "README.md", "src/suma.py")
        git(self.project, "commit", "-q", "-m", "cambio en la principal")
        before = self.snapshot()
        workdir_head = git(self.workdir(), "rev-parse", "HEAD")
        code, out, err = self.here("finish", "--yes")
        self.assert_untouched(before, code, 2, out + err)
        self.assertEqual(git(self.workdir(), "rev-parse", "HEAD"), workdir_head)
        self.assertEqual(git(self.workdir(), "status", "--porcelain"), "")

    def test_failing_suite_leaves_branch_and_main_intact(self) -> None:
        self.phase_done()
        # Un commit compatible en la principal obliga a que el rebase cambie los commits de la fase.
        (self.project / "README.md").write_text("# Proyecto\nCambio compatible en la principal.\n", encoding="utf-8")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-q", "-m", "cambio compatible")
        config.set_setting(config.project_settings_path(self.project), ("execution", "test_command"), "false")
        before = self.snapshot()
        workdir_head = git(self.workdir(), "rev-parse", "HEAD")
        workdir_files = {p.relative_to(self.workdir()).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in self.workdir().rglob("*") if p.is_file() and ".git" not in p.parts}
        code, out, err = self.here("finish", "--yes")
        self.assert_untouched(before, code, 1, out + err)
        self.assertEqual(git(self.workdir(), "rev-parse", "HEAD"), workdir_head)
        self.assertEqual(git(self.workdir(), "status", "--porcelain"), "")
        self.assertEqual(git(self.workdir(), "ls-files", "-s"), git(self.workdir(), "ls-files", "-s"))
        after_files = {p.relative_to(self.workdir()).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in self.workdir().rglob("*") if p.is_file() and ".git" not in p.parts}
        self.assertEqual(after_files, workdir_files)


class CleanTests(IsolationCase):
    def make_orphans(self) -> tuple[Path, Path, Path]:
        """Worktrees huérfanos (limpio y con cambios), una rama sin integrar y una rama y un worktree ajenos."""
        base = util.state_home() / "worktrees" / gitops.project_key(self.project)
        clean_wt, dirty_wt = base / "hefesto__huerfana-limpia", base / "hefesto__huerfana-sucia"
        git(self.project, "worktree", "add", "-q", "-b", "hefesto/huerfana-limpia", str(clean_wt))
        git(self.project, "worktree", "add", "-q", "-b", "hefesto/huerfana-sucia", str(dirty_wt))
        (dirty_wt / "trabajo.txt").write_text("sin commitear\n", encoding="utf-8")
        git(self.project, "branch", "hefesto/sin-integrar")
        git(self.project, "checkout", "-q", "hefesto/sin-integrar")
        (self.project / "extra.txt").write_text("x\n", encoding="utf-8")
        git(self.project, "add", "extra.txt")
        git(self.project, "commit", "-q", "-m", "trabajo sin integrar")
        git(self.project, "checkout", "-q", self.main)
        foreign_wt = self.tmp / "wt-usuario"
        git(self.project, "worktree", "add", "-q", "-b", "usuario/mi-rama", str(foreign_wt))
        return clean_wt, dirty_wt, foreign_wt

    def test_clean_removes_only_safe_orphans(self) -> None:
        clean_wt, dirty_wt, foreign_wt = self.make_orphans()
        self.dirty_main()
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        open_wt = self.workdir()
        code, out, err = self.here("clean", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(clean_wt.exists())
        self.assertTrue((dirty_wt / "trabajo.txt").exists())
        self.assertFalse(gitops.branch_exists(self.project, "hefesto/huerfana-limpia"))
        self.assertTrue(gitops.branch_exists(self.project, "hefesto/sin-integrar"))
        self.assertIn("huerfana-sucia", out)
        code, out, err = self.here("clean", "--yes", "--include-unmerged")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(gitops.branch_exists(self.project, "hefesto/sin-integrar"))
        self.assertTrue((dirty_wt / "trabajo.txt").exists())
        self.assertTrue(gitops.branch_exists(self.project, "hefesto/huerfana-sucia"))
        self.assertTrue(foreign_wt.exists())
        self.assertTrue(gitops.branch_exists(self.project, "usuario/mi-rama"))
        self.assertTrue(open_wt.exists())
        self.assertTrue(gitops.branch_exists(self.project, BRANCH))

    def test_clean_needs_confirmation_and_barriers(self) -> None:
        clean_wt, _, _ = self.make_orphans()
        self.assertEqual(self.here("clean")[0], 2)
        code, _, _ = self.here("clean", tty_value=True, answers=["n"])
        self.assertEqual(code, 0)
        self.assertTrue(clean_wt.exists())
        code, _, err = self.cli("clean", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("-C", err)
        code, err = self.headless("clean", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("HEFESTO_ROLE", err)
        self.assertTrue(clean_wt.exists())
        self.assertTrue(gitops.branch_exists(self.project, "hefesto/huerfana-limpia"))


class DiscardTests(IsolationCase):
    def test_discard_in_progress_task_restores_exactly_its_files(self) -> None:
        (self.project / "src").mkdir()
        (self.project / "src" / "base.py").write_text("base = 1\n", encoding="utf-8")
        (self.project / "src" / "otra.py").write_text("otra = 1\n", encoding="utf-8")
        git(self.project, "add", "src")
        git(self.project, "commit", "-q", "-m", "base")
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        wd = self.workdir()
        (wd / "src" / "base.py").write_text("base = 999\n", encoding="utf-8")
        (wd / "src" / "otra.py").write_text("otra = 999\n", encoding="utf-8")
        git(wd, "add", "src/otra.py")
        (wd / "src" / "nuevo.py").write_text("nuevo = 1\n", encoding="utf-8")
        (wd / "README.md").write_text("# Proyecto\nCambio ajeno a la tarea.\n", encoding="utf-8")
        (wd / "fuera.txt").write_text("ajeno\n", encoding="utf-8")
        code, out, err = self.here("discard", "TASK-001", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual((wd / "src" / "base.py").read_text(encoding="utf-8"), "base = 1\n")
        self.assertEqual((wd / "src" / "otra.py").read_text(encoding="utf-8"), "otra = 1\n")
        self.assertFalse((wd / "src" / "nuevo.py").exists())
        self.assertEqual(git(wd, "diff", "--cached", "--name-only", "--", "src"), "")
        self.assertEqual((wd / "README.md").read_text(encoding="utf-8"), "# Proyecto\nCambio ajeno a la tarea.\n")
        self.assertEqual((wd / "fuera.txt").read_text(encoding="utf-8"), "ajeno\n")
        self.assertEqual(state.load(self.project)["status"], "IDLE")
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, " ")
        self.assertIn("Descarte de TASK-001", (self.project / ".hefesto" / "DECISIONS.md").read_text(encoding="utf-8"))

    def test_discard_completed_task_reverts_keeps_evidence_and_allows_redo(self) -> None:
        self.complete("TASK-001", "src/suma.py")
        wd = self.workdir()
        first = self.project / ".hefesto" / "evidence" / "TASK-001.json"
        first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        commits_before = int(git(wd, "rev-list", "--count", "HEAD"))
        code, out, err = self.here("discard", "TASK-001", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((wd / "src" / "suma.py").exists())
        self.assertGreater(int(git(wd, "rev-list", "--count", "HEAD")), commits_before)
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, " ")
        self.complete("TASK-001", "src/suma.py")
        second = self.project / ".hefesto" / "evidence" / "TASK-001.2.json"
        self.assertTrue(second.exists())
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), first_hash)
        self.assertEqual(self.cli("evidence", "verify")[0], 0)
        data = json.loads(second.read_text(encoding="utf-8"))
        data["verify"]["exit_code"] = 0
        data["title"] = "manipulada"
        second.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.cli("evidence", "verify")[0], 2)

    def test_deleting_an_old_evidence_after_redo_is_detected(self) -> None:
        self.complete("TASK-001", "src/suma.py")
        self.here("discard", "TASK-001", "--yes")
        self.complete("TASK-001", "src/suma.py")
        (self.project / ".hefesto" / "evidence" / "TASK-001.json").unlink()
        self.assertEqual(self.cli("evidence", "verify")[0], 2)

    def test_discard_phase_and_redo(self) -> None:
        self.dirty_main()
        self.complete("TASK-001", "src/suma.py")
        workdir = self.workdir()
        code, out, err = self.here("discard", "fase-1-nucleo", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(gitops.branch_exists(self.project, BRANCH))
        self.assertFalse(workdir.exists())
        self.assertEqual(roadmap.load_valid(self.project).get("TASK-001").marker, " ")
        self.assertIn("Descarte de", (self.project / ".hefesto" / "DECISIONS.md").read_text(encoding="utf-8"))
        self.complete("TASK-001", "src/suma.py")
        self.assertTrue((self.project / ".hefesto" / "evidence" / "TASK-001.2.json").exists())
        self.assertEqual(self.cli("evidence", "verify")[0], 0)

    def test_discard_phase_refusals_change_nothing(self) -> None:
        self.dirty_main()
        self.complete("TASK-001", "src/suma.py")
        self.assertEqual(self.cli("start", "TASK-002")[0], 0)
        before = self.snapshot()
        code, _, _ = self.here("discard", "fase-1-nucleo", "--yes")
        self.assertEqual(code, 4)
        self.assertEqual(self.snapshot(), before)
        self.here("discard", "TASK-002", "--yes")
        workdir = self.workdir() if state.load(self.project).get("workdir") else \
            Path(state.load(self.project)["phases"]["fase-1-nucleo"]["workdir"])
        (workdir / "src" / "pendiente.py").write_text("y = 2\n", encoding="utf-8")
        code, _, _ = self.here("discard", "fase-1-nucleo", "--yes")
        self.assertEqual(code, 2)
        self.assertTrue((workdir / "src" / "pendiente.py").exists())
        self.assertTrue(gitops.branch_exists(self.project, BRANCH))

    def test_discard_confirmation_and_barriers(self) -> None:
        self.cli("start", "TASK-001")
        self.assertEqual(self.here("discard", "TASK-001")[0], 2)
        code, _, _ = self.here("discard", "TASK-001", tty_value=True, answers=["n"])
        self.assertEqual(code, 0)
        self.assertEqual(state.load(self.project)["status"], "IN_PROGRESS")
        code, _, err = self.cli("discard", "TASK-001", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("-C", err)
        code, err = self.headless("discard", "TASK-001", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("HEFESTO_ROLE", err)
        self.assertEqual(state.load(self.project)["status"], "IN_PROGRESS")
