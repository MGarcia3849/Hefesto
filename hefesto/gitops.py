"""Operaciones git: rama por fase, aislamiento (branch/worktree) y checkpoint protegido por guard.

Reglas: commits solo en `hefesto/*`; `git add` solo de las rutas de la tarea; nunca `add -A` global,
`reset --hard`, `checkout -- .` ni borrar worktrees con cambios.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hefesto import config, guard, roadmap
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_UNAVAILABLE, HefestoError

# Ficheros de control de Hefesto que viajan en los commits de la tarea.
CONTROL_PATHS = (".hefesto/ROADMAP.md", ".hefesto/DECISIONS.md", ".hefesto/LEARNINGS.md", ".hefesto/evidence")
WORKTREE_REL = ".hefesto/worktree"


def git(cwd: Path, *args: str, check: bool = True, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    if shutil.which("git") is None:
        raise HefestoError("git no está instalado.", EXIT_UNAVAILABLE)
    kwargs: dict[str, Any] = {"cwd": cwd, "capture_output": True}
    if not binary:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(["git", *args], **kwargs)
    if check and result.returncode != 0:
        err = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        raise HefestoError(f"git {' '.join(args)} falló: {err.strip()}", EXIT_CONFLICT)
    return result


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def current_branch(cwd: Path) -> str:
    return git(cwd, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()


def head(cwd: Path) -> str | None:
    result = git(cwd, "rev-parse", "-q", "--verify", "HEAD", check=False)
    return result.stdout.strip() or None


def branch_exists(cwd: Path, branch: str) -> bool:
    return git(cwd, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


def dirty_paths(cwd: Path, include_control: bool = False) -> list[str]:
    """Rutas con cambios (incluidas las no versionadas). Por defecto ignora `.hefesto/`."""
    raw = git(cwd, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    paths: list[str] = []
    entries = raw.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # el siguiente campo es la ruta de origen
        if not include_control and (path == ".hefesto" or path.startswith(".hefesto/")):
            continue
        paths.append(path)
    return paths


def main_branch(project: Path, cfg: config.Config) -> str:
    configured = cfg.get("execution", "main_branch")
    if configured and configured != "auto":
        return str(configured)
    return str(cfg.settings.get("main_branch") or "main")


def branch_for(task: roadmap.Task, cfg: config.Config) -> str:
    if cfg.get("execution", "branch_mode") == "task":
        return f"hefesto/{task.phase_slug}-{task.id.lower()}"
    return f"hefesto/{task.phase_slug}"


def choose_isolation(project: Path, cfg: config.Config) -> str:
    mode = cfg.get("execution", "isolation")
    dirty = dirty_paths(project)
    if mode == "branch":
        if dirty:
            raise HefestoError("isolation=branch exige la copia de trabajo limpia; hay cambios tuyos sin commitear: "
                               + ", ".join(dirty[:10]) + ". Hefesto no los toca.", EXIT_BLOCKED)
        return "branch"
    if mode == "worktree":
        return "worktree"
    if dirty:
        return "worktree"
    current = current_branch(project)
    if current.startswith("hefesto/") or current == main_branch(project, cfg) or not current:
        return "branch"
    return "worktree"  # el usuario trabaja en otra rama suya: no se la cambiamos


def _base_for_new_branch(project: Path, cfg: config.Config, phases: dict[str, Any], rm: roadmap.Roadmap,
                         task: roadmap.Task) -> str:
    """Una fase nueva parte de la rama abierta de la fase anterior (si sigue sin integrar) o de la principal."""
    previous: list[str] = []
    for t in rm.tasks:
        if t.phase_slug == task.phase_slug:
            break
        if t.phase_slug not in previous:
            previous.append(t.phase_slug)
    for slug in reversed(previous):
        info = phases.get(slug)
        if info and branch_exists(project, info["branch"]):
            return info["branch"]
    main = main_branch(project, cfg)
    if not branch_exists(project, main):
        raise HefestoError(f"La rama principal `{main}` no existe (¿falta el primer commit?).", EXIT_CONFLICT)
    return main


def prepare_workdir(project: Path, cfg: config.Config, rm: roadmap.Roadmap, task: roadmap.Task,
                    phases: dict[str, Any]) -> dict[str, Any]:
    """Sitúa el trabajo en la rama de la fase con el aislamiento elegido. No toca cambios ajenos."""
    if head(project) is None:
        raise HefestoError("El repositorio no tiene ningún commit; haz un commit inicial antes de empezar.",
                           EXIT_CONFLICT)
    branch = branch_for(task, cfg)
    slug = task.phase_slug if cfg.get("execution", "branch_mode") == "phase" else f"{task.phase_slug}-{task.id.lower()}"
    info = dict(phases.get(slug) or {})
    isolation = info.get("isolation") if info.get("isolation") and branch_exists(project, branch) else None
    if isolation == "branch" and dirty_paths(project) and current_branch(project) != branch:
        isolation = "worktree"
    isolation = isolation or choose_isolation(project, cfg)
    if not branch_exists(project, branch):
        base_ref = _base_for_new_branch(project, cfg, phases, rm, task)
        info["base"] = out(project, "rev-parse", base_ref)
        git(project, "branch", branch, info["base"])
    info.update({"branch": branch, "isolation": isolation})
    if isolation == "branch":
        if current_branch(project) != branch:
            git(project, "checkout", "-q", branch)
        workdir = project
    else:
        workdir = project / WORKTREE_REL
        listed = out(project, "worktree", "list", "--porcelain")
        registered = any(Path(line[len("worktree "):]).resolve() == workdir.resolve()
                         for line in listed.splitlines() if line.startswith("worktree "))
        if not registered:
            git(project, "worktree", "prune")
            git(project, "worktree", "add", "-q", str(workdir), branch)
        elif current_branch(workdir) != branch:
            if dirty_paths(workdir, include_control=True):
                raise HefestoError(f"El worktree de Hefesto tiene cambios sin commitear en otra rama "
                                   f"({current_branch(workdir)}); revísalo antes de seguir.", EXIT_BLOCKED)
            git(workdir, "checkout", "-q", branch)
    info["workdir"] = str(workdir)
    phases[slug] = info
    return info


def sync_control_files(project: Path, workdir: Path, cfg: config.Config | None = None) -> None:
    """En modo worktree, copia los ficheros de control del proyecto al worktree antes del commit."""
    if cfg is not None and getattr(cfg, "settings", None) and cfg.settings.get("install_mode") == "local":
        return
    if cfg is None:
        try:
            settings = config.read_settings(config.project_settings_path(project))
            if settings.get("install_mode") == "local":
                return
        except Exception:
            pass
    if workdir.resolve() == project.resolve():
        return
    for rel in CONTROL_PATHS:
        src, dest = project / rel, workdir / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            for item in src.rglob("*"):
                if item.is_file():
                    target = dest / item.relative_to(src)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item, target)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


def _in_scope(path: str, scopes: list[str]) -> bool:
    for scope in scopes:
        scope = scope.replace("\\", "/").rstrip("/")
        if path == scope or path.startswith(scope + "/"):
            return True
    return False


def changed_paths(workdir: Path, base: str | None) -> list[str]:
    """Ficheros cambiados respecto a `base` (commits, índice, copia de trabajo y no versionados)."""
    paths: set[str] = set()
    if base:
        paths.update(p for p in out(workdir, "diff", "--name-only", base).splitlines() if p)
    paths.update(dirty_paths(workdir, include_control=True))
    return sorted(paths)


def checkpoint(project: Path, workdir: Path, cfg: config.Config, task: roadmap.Task, message: str,
               extra_paths: tuple[str, ...] = ()) -> str:
    """Commit protegido: solo rutas de la tarea + control, sin secretos ni binarios grandes. Devuelve el hash."""
    branch = current_branch(workdir)
    if not guard.branch_allowed(branch):
        raise HefestoError(f"Commit bloqueado: la rama `{branch or '(detached)'}` no es `hefesto/*`.", EXIT_BLOCKED)
    is_local = bool(getattr(cfg, "settings", None) and cfg.settings.get("install_mode") == "local")
    if not is_local:
        sync_control_files(project, workdir, cfg)
        scopes = task.files + list(CONTROL_PATHS) + list(extra_paths)
    else:
        scopes = task.files + list(extra_paths)
    to_add = [p for p in scopes if (workdir / p).exists() or out(workdir, "ls-files", "--", p)]
    if to_add:
        git(workdir, "add", "--", *to_add)
    staged = [p for p in git(workdir, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]
    problems: list[str] = []
    protected = cfg.get("guard", "protected_paths", default=list(guard.DEFAULT_PROTECTED))
    max_bytes = int(cfg.get("guard", "max_file_mb")) * 1024 * 1024
    for path in staged:
        if not _in_scope(path, scopes):
            problems.append(f"{path}: fuera de `files` de {task.id}")
        if guard.is_protected(path, protected):
            problems.append(f"{path}: ruta protegida")
        size = git(workdir, "cat-file", "-s", f":{path}", check=False).stdout.strip()
        if size.isdigit() and int(size) > max_bytes:
            problems.append(f"{path}: binario de {int(size) // (1024 * 1024)} MB (máx. {max_bytes // (1024 * 1024)})")
    diff = git(workdir, "diff", "--cached", "-U0", "--no-color", "--text").stdout
    added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    for label in guard.find_secrets(added):
        problems.append(f"secreto detectado por contenido ({label})")
    if problems:
        if staged:
            git(workdir, "reset", "-q", "--", *staged)
        raise HefestoError("Checkpoint bloqueado:\n  - " + "\n  - ".join(problems), EXIT_BLOCKED)
    git(workdir, "commit", "-q", "--allow-empty", "-m", message)
    return out(workdir, "rev-parse", "HEAD")


def file_hashes(workdir: Path, commit: str, paths: list[str]) -> list[dict[str, str | None]]:
    result: list[dict[str, str | None]] = []
    for path in sorted(paths):
        blob = git(workdir, "cat-file", "blob", f"{commit}:{path}", check=False, binary=True)
        digest = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None
        result.append({"path": path, "sha256": digest})
    return result


def commit_files(workdir: Path, commit: str) -> list[str]:
    return [p for p in out(workdir, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit).splitlines() if p]
