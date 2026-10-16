"""Operaciones git: rama por fase, aislamiento (branch/worktree) y checkpoint protegido por guard.

Reglas: commits solo en `hefesto/*`; `git add` solo de las rutas de la tarea; nunca `add -A` global,
`reset --hard`, `checkout -- .` ni borrar worktrees con cambios.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hefesto import config, guard, roadmap, util
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_UNAVAILABLE, HefestoError, atomic_write_json, read_json

# Ficheros de control de Hefesto que viajan en los commits de la tarea.
CONTROL_PATHS = (".hefesto/ROADMAP.md", ".hefesto/DECISIONS.md", ".hefesto/LEARNINGS.md", ".hefesto/evidence")
WORKTREE_REL = ".hefesto/worktree"
_LINKS_FILENAME = "hefesto-links.json"


def is_control_path(path: str | Path) -> bool:
    norm = str(path).replace("\\", "/")
    for cp in CONTROL_PATHS:
        scope = cp.replace("\\", "/").rstrip("/")
        if norm == scope or norm.startswith(scope + "/"):
            return True
    return False


def project_key(project: Path) -> str:
    resolved = str(project.resolve()).lower().encode("utf-8")
    digest = hashlib.sha256(resolved).hexdigest()[:12]
    return f"{project.name}-{digest}"


def worktree_root(project: Path) -> Path:
    return (util.state_home() / "worktrees" / project_key(project)).resolve()


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
        workdir = None
        legacy_candidates: list[Path] = []
        if info.get("workdir"):
            cand = Path(info["workdir"])
            if project.resolve() in cand.resolve().parents or cand.resolve() == (project / WORKTREE_REL).resolve():
                legacy_candidates.append(cand)
        for ph_info in phases.values():
            if isinstance(ph_info, dict) and ph_info.get("workdir"):
                cand = Path(ph_info["workdir"])
                if project.resolve() in cand.resolve().parents or cand.resolve() == (project / WORKTREE_REL).resolve():
                    if cand not in legacy_candidates:
                        legacy_candidates.append(cand)
        inner_default = project / WORKTREE_REL
        if inner_default not in legacy_candidates and inner_default.exists():
            legacy_candidates.append(inner_default)

        for legacy in legacy_candidates:
            if legacy.exists():
                has_changes = False
                try:
                    has_changes = bool(dirty_paths(legacy, include_control=True))
                except Exception:
                    pass
                if has_changes:
                    sys.stderr.write(f"Aviso: worktree antiguo en {legacy} tiene cambios sin commitear; se reutiliza.\n")
                    workdir = legacy
                    break
                else:
                    remove_worktree(project, legacy)
                    if legacy.exists():
                        shutil.rmtree(legacy, ignore_errors=True)
                    git(project, "worktree", "prune", check=False)

        if workdir is None:
            root = worktree_root(project)
            root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(root / "project.json", {"project": str(project.resolve())})
            workdir = root / branch.replace("/", "__")
            listed = out(project, "worktree", "list", "--porcelain")
            registered = any(str(Path(line[len("worktree "):]).resolve()).lower() == str(workdir.resolve()).lower()
                             for line in listed.splitlines() if line.startswith("worktree "))
            if not registered:
                git(project, "worktree", "prune", check=False)
                git(project, "worktree", "add", "-q", str(workdir), branch)
                link_worktree(project, workdir, cfg.get("execution", "worktree_links", default=[]) or [])
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
               extra_paths: tuple[str, ...] = (), include_control: bool = True) -> str:
    """Commit protegido: solo rutas de la tarea + control, sin secretos ni binarios grandes. Devuelve el hash."""
    branch = current_branch(workdir)
    if not guard.branch_allowed(branch):
        raise HefestoError(f"Commit bloqueado: la rama `{branch or '(detached)'}` no es `hefesto/*`.", EXIT_BLOCKED)
    is_local = bool(getattr(cfg, "settings", None) and cfg.settings.get("install_mode") == "local")
    if not is_local and include_control:
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


def _is_link(path: Path) -> bool:
    """True si `path` es un enlace simbólico o una junction de Windows."""
    if os.path.islink(path):
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _create_dir_link(src: Path, dest: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(src.resolve()), os.path.abspath(dest))
    else:
        os.symlink(str(src.resolve()), dest, target_is_directory=True)


def _unlink_only(path: Path) -> None:
    """Elimina solo el enlace (junction o symlink), nunca su contenido."""
    if not _is_link(path):
        return
    if os.name == "nt" and not os.path.islink(path):
        os.rmdir(path)
    else:
        try:
            os.unlink(path)
        except OSError:
            if os.name == "nt":
                os.rmdir(path)
            else:
                raise


def _prune_empty_parents(path: Path, root: Path) -> None:
    root_res = root.resolve()
    parent = path.parent
    while True:
        try:
            if _is_link(parent) or not _is_within(parent, root) or parent.resolve() == root_res:
                break
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _clean_link_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HefestoError(f"Ruta de worktree_links inválida: {raw!r}")
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":") or Path(raw).is_absolute():
        raise HefestoError(f"Ruta de worktree_links no puede ser absoluta: {raw!r}")
    parts = [p for p in raw.replace("\\", "/").strip("/").split("/") if p]
    if not parts or any(p.casefold() in (".", "..", ".git", ".hefesto") for p in parts):
        raise HefestoError(f"Ruta de worktree_links insegura: {raw!r}")
    return "/".join(parts)


def _has_link_ancestor(path: Path, root: Path) -> bool:
    """True si `root` o algún ancestro entre `root` y `path.parent` es un enlace o junction."""
    if _is_link(root):
        return True
    try:
        rel_parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return True
    cur = root
    for part in rel_parts:
        cur = cur / part
        if _is_link(cur):
            return True
    return False


def _is_within(child: Path, root: Path) -> bool:
    try:
        return child.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _worktree_links_registry(worktree: Path) -> Path | None:
    if not worktree.exists():
        return None
    res = git(worktree, "rev-parse", "--git-dir", check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    git_dir = Path(res.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir / _LINKS_FILENAME


def link_worktree(project: Path, worktree: Path, paths: list[str] | tuple[str, ...]) -> list[str]:
    """Enlaza carpetas ignoradas de `project` en `worktree` y registra los enlaces creados."""
    cleaned = [_clean_link_path(raw) for raw in paths]
    if not cleaned or not worktree.exists() or project.resolve() == worktree.resolve():
        return []
    reg = _worktree_links_registry(worktree)
    if reg is None:
        return []
    existing = read_json(reg, default=[])
    recorded: list[str] = [r for r in existing if isinstance(r, str)] if isinstance(existing, list) else []
    linked: list[str] = []
    for rel in cleaned:
        src = project / rel
        dest = worktree / rel
        if _is_link(src) or _has_link_ancestor(src, project):
            continue
        if not _is_within(src.parent, project) or not _is_within(src, project):
            continue
        if not src.is_dir() or os.path.lexists(dest) or _is_link(dest):
            continue
        if _has_link_ancestor(dest, worktree) or not _is_within(dest.parent, worktree):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _has_link_ancestor(dest, worktree) or not _is_within(dest.parent, worktree):
            continue
        _create_dir_link(src, dest)
        had_rel = rel in recorded
        if not had_rel:
            recorded.append(rel)
        atomic_write_json(reg, recorded)
        ignored = git(worktree, "check-ignore", "-q", "--", rel, check=False).returncode == 0
        dirty = bool(git(worktree, "status", "--porcelain", "--", rel, check=False).stdout.strip())
        if not ignored or dirty:
            _unlink_only(dest)
            _prune_empty_parents(dest, worktree)
            if not had_rel and not _is_link(dest):
                recorded = [r for r in recorded if r != rel]
                atomic_write_json(reg, recorded)
            continue
        linked.append(rel)
    return linked


def remove_worktree(project: Path, worktree: Path, force: bool = False) -> subprocess.CompletedProcess[Any]:
    """Quita los enlaces creados por `link_worktree` (sin tocar su destino) y ejecuta `git worktree remove`."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree))
    if worktree.exists() or os.path.lexists(worktree):
        reg = _worktree_links_registry(worktree)
        recorded: list[Any] = []
        if reg is not None and reg.exists():
            data = read_json(reg, default=[])
            if isinstance(data, list):
                recorded = data
        targets: list[Path] = []
        unsafe: list[str] = []
        for rel in recorded:
            try:
                clean_rel = _clean_link_path(rel)
            except HefestoError as exc:
                unsafe.append(f"{rel!r}: {exc}")
                continue
            target = worktree / clean_rel
            if _has_link_ancestor(target, worktree) or not _is_within(target.parent, worktree):
                unsafe.append(f"{target}: ancestro enlazado o ruta fuera del worktree")
                continue
            targets.append(target)
            if _is_link(target):
                try:
                    _unlink_only(target)
                except OSError as exc:
                    unsafe.append(f"{target}: {exc}")
                    continue
                _prune_empty_parents(target, worktree)
        unsafe.extend(f"{t}: entrada registrada aún presente" for t in targets if os.path.lexists(t) or _is_link(t))
        if unsafe:
            return subprocess.CompletedProcess(
                ["git", *args],
                returncode=1,
                stdout="",
                stderr=f"No se pudieron retirar los enlaces con seguridad; se cancela git worktree remove: {', '.join(unsafe)}",
            )
        if reg is not None and reg.exists():
            try:
                reg.unlink()
            except OSError:
                pass
    return git(project, *args, check=False)
