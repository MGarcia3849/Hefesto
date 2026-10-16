"""Contrato (TASK-1301 → TASK-1302/1303): dependencias compartidas en las copias temporales (worktrees).

Motivo (2026-10-16, `finish` de SENTINEL): `finish` pasa la suite en un worktree temporal recién creado, que no
tiene las carpetas ignoradas por git de la copia principal (`node_modules`), y `ng build` falla sin que el código
tenga nada mal. Reinstalar dependencias en cada worktree es lento y necesita red, así que se enlazan.

Hallazgo que manda en el diseño (probado en Windows con git 2.x): `git worktree remove`, con o sin `--force`,
ENTRA por una junction y borra el contenido real de la carpeta enlazada (el `node_modules` de la copia principal).
`shutil.rmtree` sí respeta las junctions. Por eso ningún código de Hefesto puede llamar a `git worktree remove`
directamente: siempre `gitops.remove_worktree`, que quita antes los enlaces.

Configuración: `[execution] worktree_links = []` en hefesto.toml (lista de rutas relativas a la raíz del proyecto,
p. ej. `["sentinel-app/node_modules"]`). `config.validate` da error si no es una lista de textos, o si alguna
entrada es absoluta, contiene `..`, o es (o está dentro de) `.git` o `.hefesto`.

API (hefesto/gitops.py):
- `link_worktree(project, worktree, paths) -> list[str]`: por cada ruta, si `project/ruta` es un directorio
  existente y `worktree/ruta` no existe, crea `worktree/ruta` como junction (Windows) o enlace simbólico a
  directorio (resto), creando los directorios padre. Solo conserva el enlace si git lo ignora en el worktree (no
  aparece en `git status --porcelain -- ruta`); si aparece, quita el enlace (solo el enlace) y omite la ruta: así
  nunca ensucia el worktree ni acaba en un commit, sea cual sea la forma de la regla (`deps` o `deps/`: git ignora
  una junction como directorio, pero un symlink POSIX como fichero) y la plataforma. Devuelve las rutas enlazadas (en formato posix, en el orden recibido). Una fuente que no existe, una ruta no
  ignorada o un destino que ya existe se omiten sin error (el destino existente no se toca). Una ruta insegura
  (absoluta, con `..`, `.git` o `.hefesto`) lanza `HefestoError`. Deja constancia de los enlaces creados para que
  `remove_worktree` los encuentre aunque la configuración cambie después.
- `remove_worktree(project, worktree, force=False) -> subprocess.CompletedProcess`: quita primero los enlaces que
  creó `link_worktree` en ese worktree (solo el enlace, nunca su contenido), aunque la configuración haya cambiado
  desde entonces, y después ejecuta `git worktree remove [--force] <worktree>` sin `check`, devolviendo su
  resultado (nunca lanza por un error de git: un worktree inexistente da un código distinto de 0).

Uso: `finish` enlaza `execution.worktree_links` en su worktree temporal antes de pasar la suite y los verify; el
worktree de una fase (`ensure_phase_branch`) y los worktrees de tareas en paralelo (`scheduler`) también, así que
`acceptance.py`, `gitops.py` y `scheduler.py` llaman a `link_worktree`. Ningún módulo de `hefesto/` salvo
`gitops.py` contiene la llamada `"worktree", "remove"`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from hefesto import config, gitops, state
from hefesto.util import HefestoError

from tests.helpers import ROOT, HefestoTestCase, git
from tests.test_isolation import IsolationCase


def is_link(path: Path) -> bool:
    """Enlace simbólico o junction, también en Python < 3.12 (sin `os.path.isjunction`)."""
    if os.path.islink(path):
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


class RepoCase(HefestoTestCase):
    """Repositorio con una carpeta de dependencias ignorada y un worktree recién creado."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text("deps\napp/node_modules\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# repo\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "inicial")
        for rel in ("deps/pkg", "app/node_modules/lib"):
            (self.repo / rel).mkdir(parents=True)
            (self.repo / rel / "marca.txt").write_text("ok\n", encoding="utf-8")
        self.wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "hefesto/prueba", str(self.wt))

    def registered(self) -> list[str]:
        listed = git(self.repo, "worktree", "list", "--porcelain")
        return [line[len("worktree "):] for line in listed.splitlines() if line.startswith("worktree ")]


class LinkTests(RepoCase):
    def test_links_ignored_dirs_and_shares_content(self) -> None:
        linked = gitops.link_worktree(self.repo, self.wt, ["deps", "app/node_modules"])
        self.assertEqual(linked, ["deps", "app/node_modules"])
        for rel in ("deps", "app/node_modules"):
            self.assertTrue(is_link(self.wt / rel), rel)
        self.assertEqual((self.wt / "deps" / "pkg" / "marca.txt").read_text(encoding="utf-8"), "ok\n")
        self.assertEqual((self.wt / "app/node_modules/lib/marca.txt").read_text(encoding="utf-8"), "ok\n")
        self.assertEqual(git(self.wt, "status", "--porcelain"), "", "un enlace ignorado no ensucia el worktree")

    def test_skips_missing_unignored_and_existing(self) -> None:
        (self.repo / "visible").mkdir()  # existe pero no está ignorada: se commitearía, no se enlaza
        (self.wt / "deps").mkdir()
        (self.wt / "deps" / "propio.txt").write_text("mío\n", encoding="utf-8")
        linked = gitops.link_worktree(self.repo, self.wt, ["no-existe", "visible", "deps"])
        self.assertEqual(linked, [])
        self.assertFalse(is_link(self.wt / "deps"))
        self.assertEqual((self.wt / "deps" / "propio.txt").read_text(encoding="utf-8"), "mío\n")
        self.assertFalse((self.wt / "no-existe").exists())
        self.assertFalse((self.wt / "visible").exists())

    def test_rejects_unsafe_paths(self) -> None:
        for bad in (str(self.repo / "deps"), "../fuera", "deps/../..", ".git", ".git/hooks", ".hefesto"):
            with self.subTest(bad=bad), self.assertRaises(HefestoError):
                gitops.link_worktree(self.repo, self.wt, [bad])


class RemoveTests(RepoCase):
    def test_remove_keeps_link_targets(self) -> None:
        for force in (False, True):
            with self.subTest(force=force):
                if not self.wt.exists():
                    git(self.repo, "worktree", "add", "-q", str(self.wt), "hefesto/prueba")
                gitops.link_worktree(self.repo, self.wt, ["deps", "app/node_modules"])
                proc = gitops.remove_worktree(self.repo, self.wt, force=force)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(self.wt.exists())
                self.assertNotIn(str(self.wt.resolve()).lower(),
                                 [str(Path(p).resolve()).lower() for p in self.registered()])
                self.assertEqual((self.repo / "deps/pkg/marca.txt").read_text(encoding="utf-8"), "ok\n")
                self.assertEqual((self.repo / "app/node_modules/lib/marca.txt").read_text(encoding="utf-8"), "ok\n")

    def test_remove_without_links(self) -> None:
        proc = gitops.remove_worktree(self.repo, self.wt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.wt.exists())

    def test_remove_missing_worktree_returns_error_code(self) -> None:
        proc = gitops.remove_worktree(self.repo, self.tmp / "no-existe")
        self.assertNotEqual(proc.returncode, 0)

    def test_worktree_creators_link_dependencies(self) -> None:
        for name in ("acceptance.py", "gitops.py", "scheduler.py"):
            with self.subTest(module=name):
                self.assertIn("link_worktree(", (ROOT / "hefesto" / name).read_text(encoding="utf-8"))

    def test_no_raw_worktree_remove_outside_gitops(self) -> None:
        pattern = re.compile(r"""["']worktree["']\s*,\s*["']remove["']""")
        offenders = [p.name for p in (ROOT / "hefesto").glob("*.py")
                     if p.name != "gitops.py" and pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [], "usa gitops.remove_worktree: git worktree remove borra a través de los enlaces")


class ConfigTests(HefestoTestCase):
    def test_default_is_empty_list(self) -> None:
        self.assertEqual(config.load(None).get("execution", "worktree_links"), [])

    def test_validate_rejects_bad_entries(self) -> None:
        self.init_project()
        settings = config.project_settings_path(self.project)
        for bad in ("node_modules", [1], ["/abs/node_modules"], ["../x"], [".git"], [".hefesto/logs"]):
            with self.subTest(bad=bad):
                config.set_setting(settings, ("execution", "worktree_links"), bad)
                errors = config.validate(config.load(self.project, validate_layers=False))
                self.assertTrue(any("worktree_links" in e for e in errors), errors)
        config.set_setting(settings, ("execution", "worktree_links"), ["app/node_modules"])
        errors = config.validate(config.load(self.project, validate_layers=False))
        self.assertFalse(any("worktree_links" in e for e in errors), errors)


class FinishTests(IsolationCase):
    def setUp(self) -> None:
        super().setUp()
        (self.project / ".gitignore").write_text("deps\n", encoding="utf-8")
        git(self.project, "add", ".gitignore")
        git(self.project, "commit", "-q", "-m", "ignora deps")
        (self.project / "deps").mkdir()
        (self.project / "deps" / "marca.txt").write_text("ok\n", encoding="utf-8")
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("execution", "worktree_links"), ["deps"])

    def test_phase_worktree_gets_links(self) -> None:
        self.dirty_main()  # fuerza el aislamiento por worktree
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        self.assertNotEqual(workdir.resolve(), self.project.resolve())
        self.assertTrue(is_link(workdir / "deps"))
        self.assertEqual((workdir / "deps" / "marca.txt").read_text(encoding="utf-8"), "ok\n")

    def test_finish_suite_sees_linked_dir_and_target_survives(self) -> None:
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("execution", "test_command"), "test -f deps/marca.txt")
        self.phase_done()
        code, out, err = self.here("finish", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.project / "deps" / "marca.txt").read_text(encoding="utf-8"), "ok\n")
        temp = gitops.worktree_root(self.project) / "_finish"
        self.assertFalse(temp.exists())

    def test_finish_failure_cleans_up_without_touching_target(self) -> None:
        settings = config.project_settings_path(self.project)
        # exit 3 solo si la suite vio la carpeta enlazada; sin enlace, `test -f` daría exit 1.
        config.set_setting(settings, ("execution", "test_command"), "test -f deps/marca.txt && exit 3")
        self.phase_done()
        code, out, err = self.here("finish", "--yes")
        self.assertEqual(code, 1, out + err)
        self.assertIn("exit 3", out + err)
        self.assertEqual((self.project / "deps" / "marca.txt").read_text(encoding="utf-8"), "ok\n")
        self.assertFalse((gitops.worktree_root(self.project) / "_finish").exists())
