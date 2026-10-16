"""Mantenimiento: `clean` (limpieza de worktrees y ramas huérfanos) y `discard` (descarte de tareas o fases)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hefesto import config, evidence, gitops, roadmap, state, util, workflow
from hefesto.lock import project_lock
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_INPUT, HefestoError


def list_worktrees(project: Path) -> list[dict[str, Any]]:
    """Devuelve los worktrees registrados según `git worktree list --porcelain`."""
    proc = gitops.git(project, "worktree", "list", "--porcelain", check=False)
    if proc.returncode != 0:
        return []
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            if current and "worktree" in current:
                worktrees.append(current)
                current = {}
            current["worktree"] = Path(line[len("worktree "):].strip())
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            current["branch"] = ref
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
    if current and "worktree" in current:
        worktrees.append(current)
    return worktrees


def _is_hefesto_worktree(project: Path, wt_path: Path) -> bool:
    """True solo si el worktree está bajo state_home()/worktrees/<clave>/ o en <proyecto>/.hefesto/worktree."""
    try:
        res = wt_path.resolve()
        legacy = (project / ".hefesto" / "worktree").resolve()
        if res == legacy or str(res).lower() == str(legacy).lower():
            return True
        root = (util.state_home() / "worktrees" / gitops.project_key(project)).resolve()
        if res == root or str(res).lower() == str(root).lower():
            return False
        if root in res.parents or any(str(p).lower() == str(root).lower() for p in res.parents):
            return True
        try:
            rel = res.relative_to(root)
            return rel != Path(".")
        except ValueError:
            return False
    except Exception:
        return False


def detect_clean_targets(
    project: Path, include_unmerged: bool = False
) -> tuple[
    list[tuple[Path, str]],
    list[tuple[Path, str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
]:
    """Detecta worktrees y ramas huérfanas de Hefesto para limpiar y conservar.

    Devuelve: (wt_to_remove, wt_to_keep, br_to_remove, br_to_keep).
    """
    data = state.load(project)
    phases: dict[str, Any] = data.get("phases") or {}
    open_branches: set[str] = {
        ph["branch"]
        for ph in phases.values()
        if isinstance(ph, dict) and ph.get("branch")
    }
    open_workdirs: set[Path] = {
        Path(ph["workdir"]).resolve()
        for ph in phases.values()
        if isinstance(ph, dict) and ph.get("workdir")
    }
    if data.get("branch"):
        open_branches.add(data["branch"])
    if data.get("workdir"):
        open_workdirs.add(Path(data["workdir"]).resolve())

    all_wts = list_worktrees(project)
    wt_to_remove: list[tuple[Path, str]] = []
    wt_to_keep: list[tuple[Path, str, str]] = []
    dirty_branches: set[str] = set()
    foreign_branches: dict[str, str] = {}

    for wt in all_wts:
        wt_path: Path = wt["worktree"]
        branch: str = str(wt.get("branch") or "")
        resolved = wt_path.resolve()
        if resolved == project.resolve():
            if branch.startswith("hefesto/") and branch not in open_branches:
                foreign_branches[branch] = "rama de la copia principal"
            continue

        if not _is_hefesto_worktree(project, wt_path):
            if branch.startswith("hefesto/"):
                foreign_branches[branch] = "worktree ajeno conservado"
            continue

        if not branch.startswith("hefesto/"):
            continue
        if branch in open_branches or resolved in open_workdirs:
            continue
        if wt_path.exists():
            dirty = gitops.dirty_paths(wt_path, include_control=True)
            if dirty:
                wt_to_keep.append(
                    (wt_path, branch, f"cambios sin commitear ({', '.join(dirty[:3])})")
                )
                dirty_branches.add(branch)
            else:
                wt_to_remove.append((wt_path, branch))
        else:
            wt_to_remove.append((wt_path, branch))

    cfg = config.load(project)
    main = gitops.main_branch(project, cfg)

    proc = gitops.git(project, "for-each-ref", "--format=%(refname:short)", "refs/heads/", check=False)
    all_local_branches = [b.strip() for b in proc.stdout.splitlines() if b.strip()]
    hefesto_branches = [b for b in all_local_branches if b.startswith("hefesto/")]

    merged_proc = gitops.git(project, "branch", "--merged", main, check=False)
    merged_branches = {
        b.strip().lstrip("*+ ") for b in merged_proc.stdout.splitlines() if b.strip()
    }

    br_to_remove: list[tuple[str, str]] = []
    br_to_keep: list[tuple[str, str]] = []

    for b in hefesto_branches:
        if b in open_branches:
            continue
        if b in dirty_branches:
            br_to_keep.append((b, "worktree sucio conservado"))
            continue
        if b in foreign_branches:
            br_to_keep.append((b, foreign_branches[b]))
            continue
        is_integrated = (
            b in merged_branches
            or gitops.git(project, "merge-base", "--is-ancestor", b, main, check=False).returncode == 0
        )
        if not is_integrated and "--TASK-" in b:
            phase_branch = b.split("--TASK-")[0]
            if gitops.branch_exists(project, phase_branch) and gitops.git(
                project, "merge-base", "--is-ancestor", b, phase_branch, check=False
            ).returncode == 0:
                is_integrated = True
        if is_integrated:
            br_to_remove.append((b, "-d"))
        else:
            if include_unmerged:
                br_to_remove.append((b, "-D"))
            else:
                br_to_keep.append((b, "sin integrar (usa --include-unmerged)"))

    return wt_to_remove, wt_to_keep, br_to_remove, br_to_keep


def plan_clean(project: Path, include_unmerged: bool = False) -> str:
    """Informe previo de lo que se limpiará y conservará."""
    wt_rm, wt_keep, br_rm, br_keep = detect_clean_targets(project, include_unmerged)
    lines = ["== Plan de limpieza de Hefesto =="]
    if wt_rm:
        lines.append("Worktrees a eliminar:")
        for path, br in wt_rm:
            lines.append(f"  - {path} ({br})")
    if wt_keep:
        lines.append("Worktrees conservados:")
        for path, br, reason in wt_keep:
            lines.append(f"  - {path} ({br}): {reason}")
    if br_rm:
        lines.append("Ramas a eliminar:")
        for br, flag in br_rm:
            lines.append(f"  - {br} ({flag})")
    if br_keep:
        lines.append("Ramas conservadas:")
        for br, reason in br_keep:
            lines.append(f"  - {br}: {reason}")
    if not wt_rm and not br_rm:
        lines.append("Nada que eliminar.")
    return "\n".join(lines)


def clean(project: Path, include_unmerged: bool = False) -> str:
    """Elimina worktrees limpios y ramas huérfanas de Hefesto."""
    with project_lock(project):
        wt_rm, wt_keep, br_rm, br_keep = detect_clean_targets(project, include_unmerged)
        removed_wts: list[str] = []
        failed_branches: set[str] = set()
        for path, br in wt_rm:
            if path.exists():
                proc = gitops.remove_worktree(project, path)
                if proc.returncode != 0 or path.exists():
                    reason = proc.stderr.strip() or "no se pudo eliminar el worktree"
                    wt_keep.append((path, br, reason))
                    failed_branches.add(br)
                    continue
            removed_wts.append(f"{path} ({br})")
        gitops.git(project, "worktree", "prune", check=False)

        new_br_rm: list[tuple[str, str]] = []
        for br, flag in br_rm:
            if br in failed_branches:
                br_keep.append((br, "worktree conservado"))
            else:
                new_br_rm.append((br, flag))
        br_rm = new_br_rm

        removed_brs: list[str] = []
        for br, flag in br_rm:
            proc = gitops.git(project, "branch", flag, br, check=False)
            if proc.returncode != 0 and flag == "-d" and "--TASK-" in br:
                phase_branch = br.split("--TASK-")[0]
                if gitops.branch_exists(project, phase_branch) and gitops.git(
                    project, "merge-base", "--is-ancestor", br, phase_branch, check=False
                ).returncode == 0:
                    gitops.git(project, "branch", f"--set-upstream-to={phase_branch}", br, check=False)
                    proc = gitops.git(project, "branch", "-d", br, check=False)
            if proc.returncode != 0:
                reason = proc.stderr.strip() or f"falló git branch {flag}"
                br_keep.append((br, reason))
            else:
                removed_brs.append(f"{br} ({flag})")

        lines = ["== Limpieza de Hefesto completada =="]
        if removed_wts:
            lines.append("Worktrees eliminados:")
            for item in removed_wts:
                lines.append(f"  - {item}")
        if wt_keep:
            lines.append("Worktrees conservados:")
            for path, br, reason in wt_keep:
                lines.append(f"  - {path} ({br}): {reason}")
        if removed_brs:
            lines.append("Ramas eliminadas:")
            for item in removed_brs:
                lines.append(f"  - {item}")
        if br_keep:
            lines.append("Ramas conservadas:")
            for br, reason in br_keep:
                lines.append(f"  - {br}: {reason}")
        if not removed_wts and not removed_brs and not wt_keep and not br_keep:
            lines.append("Nada que limpiar.")
        return "\n".join(lines)


def resolve_target(project: Path, target: str) -> tuple[str, Any]:
    """Determina si target es una tarea ('task', task) o una fase ('phase', phase_slug)."""
    rm = roadmap.load_valid(project)
    data = state.load(project)
    by_id = rm.by_id()
    if target in by_id:
        return "task", by_id[target]
    target_slug = roadmap.slugify(target)
    for p in rm.phases:
        if roadmap.slugify(p) == target_slug or p == target:
            return "phase", roadmap.slugify(p)
    phases = data.get("phases") or {}
    for slug in phases:
        if slug == target_slug or slug == target:
            return "phase", slug
    raise HefestoError(
        f"'{target}' no es una tarea ni una fase conocida en el ROADMAP o en el estado.",
        EXIT_INPUT,
    )


def preview_discard(project: Path, target: str) -> str:
    """Previsualización de lo que se descartará."""
    kind, obj = resolve_target(project, target)
    data = state.load(project)
    if kind == "task":
        task: roadmap.Task = obj
        if data.get("status") == "IN_PROGRESS" and data.get("current_task_id") == task.id:
            return (
                f"Se descartará la tarea en curso {task.id} y se restaurarán sus ficheros "
                f"({', '.join(task.files)})."
            )
        elif task.marker == "x":
            return f"Se revertirá el commit de la tarea completada {task.id} y su marcador volverá a [ ]."
        else:
            return f"La tarea {task.id} tiene marcador [{task.marker}]."
    else:
        phase_slug: str = obj
        return (
            f"Se descartará la fase {phase_slug}, eliminando su worktree y su rama, "
            f"y reseteando sus tareas a [ ]."
        )


def _discard_in_progress_task(project: Path, task: roadmap.Task, data: dict[str, Any]) -> str:
    workdir = Path(data.get("workdir") or project)
    task_base = data.get("task_base") or "HEAD"

    for ruta in task.files:
        ruta_norm = ruta.rstrip("/\\")
        proc = gitops.git(workdir, "ls-tree", task_base, "--", ruta_norm, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            restore_proc = gitops.git(
                workdir, "restore", f"--source={task_base}", "--staged", "--worktree", "--", ruta_norm, check=False
            )
            if restore_proc.returncode != 0:
                err = restore_proc.stderr.strip() or f"git restore falló para {ruta_norm}"
                raise HefestoError(err, EXIT_BLOCKED)
        else:
            proc_idx = gitops.git(workdir, "ls-files", "--", ruta_norm, check=False)
            if proc_idx.returncode != 0:
                err = proc_idx.stderr.strip() or f"git ls-files falló para {ruta_norm}"
                raise HefestoError(err, EXIT_BLOCKED)
            if proc_idx.stdout.strip():
                restore_proc = gitops.git(workdir, "restore", "--staged", "--", ruta_norm, check=False)
                if restore_proc.returncode != 0:
                    err = restore_proc.stderr.strip() or f"git restore falló para {ruta_norm}"
                    raise HefestoError(err, EXIT_BLOCKED)

        proc_others = gitops.git(workdir, "ls-files", "--others", "--exclude-standard", "--", ruta_norm, check=False)
        if proc_others.returncode != 0:
            err = proc_others.stderr.strip() or f"git ls-files falló para {ruta_norm}"
            raise HefestoError(err, EXIT_BLOCKED)
        for rel in proc_others.stdout.splitlines():
            rel = rel.strip()
            if rel:
                (workdir / rel).unlink(missing_ok=True)

    roadmap.set_marker(roadmap.roadmap_path(project), task.id, " ")
    data = state.transition(data, "IDLE")
    data.update({
        "current_task_id": None,
        "active_role": None,
        "run_id": None,
        "review": None,
        "attempts": 0,
        "last_error": None,
        "error_history": [],
        "next_action": "hefesto next",
    })
    state.save(project, data)
    workflow.append_decision(project, f"Descarte de {task.id}", f"Descarte de la tarea en curso {task.id}.")
    cfg = config.load(project)
    gitops.sync_control_files(project, workdir, cfg)
    return f"Tarea en curso {task.id} descartada: ficheros restaurados y estado en IDLE."


def _discard_completed_task(project: Path, task: roadmap.Task, data: dict[str, Any]) -> str:
    cfg = config.load(project)
    latest_ev = evidence.latest_evidence(project, task.id)
    if not latest_ev:
        raise HefestoError(f"No hay evidencia registrada para la tarea {task.id}.", EXIT_INPUT)
    _, ev_data = latest_ev
    commit = ev_data.get("commit") or ev_data.get("last_checkpoint")
    if not commit:
        raise HefestoError(f"La evidencia de {task.id} no contiene commit.", EXIT_INPUT)

    phases = data.get("phases") or {}
    slug = task.phase_slug
    phase_info = phases.get(slug)
    if not phase_info:
        phase_info = phases.get(f"{slug}-{task.id.lower()}")
    if not phase_info:
        for k, v in phases.items():
            if isinstance(v, dict) and (k == slug or k == task.phase or roadmap.slugify(k) == slug):
                phase_info = v
                break

    if phase_info and phase_info.get("workdir"):
        phase_workdir = Path(phase_info["workdir"])
    elif data.get("workdir"):
        phase_workdir = Path(data["workdir"])
    else:
        phase_workdir = project

    if not phase_workdir.exists():
        raise HefestoError(f"El directorio de trabajo {phase_workdir} no existe.", EXIT_BLOCKED)
    dirty = gitops.dirty_paths(phase_workdir, include_control=True)
    if dirty:
        raise HefestoError(
            f"El directorio de trabajo {phase_workdir} tiene cambios sin commitear ({', '.join(dirty)}).",
            EXIT_BLOCKED,
        )

    proc = gitops.git(phase_workdir, "revert", "--no-edit", commit, check=False)
    if proc.returncode != 0:
        gitops.git(phase_workdir, "revert", "--abort", check=False)
        raise HefestoError(f"git revert {commit} falló: {proc.stderr.strip()}", EXIT_CONFLICT)

    roadmap.set_marker(roadmap.roadmap_path(project), task.id, " ")
    workflow.append_decision(
        project,
        f"Descarte de {task.id}",
        f"Descarte de la tarea completada {task.id} (revert del commit {commit[:10]}).",
    )
    gitops.sync_control_files(project, phase_workdir, cfg)

    if data.get("status") == "COMPLETED":
        data = state.transition(data, "IDLE")
        data["next_action"] = "hefesto next"
        state.save(project, data)

    return f"Tarea {task.id} descartada: commit {commit[:10]} revertido y marcador vuelto a [ ]."


def _discard_phase(project: Path, phase_slug: str, data: dict[str, Any]) -> str:
    rm = roadmap.load_valid(project)
    cfg = config.load(project)

    if data.get("status") == "IN_PROGRESS" and data.get("current_task_id"):
        curr_task = rm.get(data["current_task_id"])
        if curr_task.phase_slug == phase_slug or roadmap.slugify(curr_task.phase) == phase_slug or curr_task.phase == phase_slug:
            raise HefestoError(f"La fase {phase_slug} tiene la tarea {curr_task.id} en curso.", EXIT_CONFLICT)

    phases = dict(data.get("phases") or {})
    matched_key: str | None = None
    if phase_slug in phases:
        matched_key = phase_slug
    else:
        for k in phases:
            if k == phase_slug or roadmap.slugify(k) == phase_slug:
                matched_key = k
                break

    phase_info = phases.get(matched_key) if matched_key else None
    branch = (phase_info.get("branch") if phase_info else None) or f"hefesto/{phase_slug}"

    if phase_info and phase_info.get("workdir"):
        ph_workdir = Path(phase_info["workdir"])
    else:
        matching_wts = [wt["worktree"] for wt in list_worktrees(project) if wt.get("branch") == branch]
        ph_workdir = matching_wts[0] if matching_wts else None

    if ph_workdir and ph_workdir.exists():
        dirty = gitops.dirty_paths(ph_workdir, include_control=True)
        if dirty:
            raise HefestoError(
                f"El worktree de la fase {phase_slug} tiene cambios sin commitear ({', '.join(dirty)}).",
                EXIT_BLOCKED,
            )

    if gitops.current_branch(project) == branch:
        main = gitops.main_branch(project, cfg)
        dirty_main = gitops.dirty_paths(project, include_control=False)
        if dirty_main:
            raise HefestoError(
                f"La copia principal tiene cambios sin commitear ({', '.join(dirty_main)}).",
                EXIT_BLOCKED,
            )
        gitops.git(project, "checkout", "-q", main)

    if ph_workdir and ph_workdir.exists() and ph_workdir.resolve() != project.resolve():
        proc = gitops.remove_worktree(project, ph_workdir)
        if proc.returncode != 0 or ph_workdir.exists():
            err_msg = proc.stderr.strip() or (
                "la ruta sigue existiendo" if proc.returncode == 0 else f"código {proc.returncode}"
            )
            raise HefestoError(
                f"No se pudo eliminar el worktree de la fase {phase_slug}: {err_msg}",
                EXIT_BLOCKED,
            )
        gitops.git(project, "worktree", "prune", check=False)

    if gitops.branch_exists(project, branch):
        gitops.git(project, "branch", "-D", branch, check=False)

    phases.pop(phase_slug, None)
    if matched_key:
        phases.pop(matched_key, None)
    data["phases"] = phases
    if data.get("branch") == branch:
        data.update({"branch": None, "workdir": None, "isolation": None})
    if data.get("status") == "COMPLETED":
        data = state.transition(data, "IDLE")
    data["next_action"] = "hefesto next"
    state.save(project, data)

    for t in rm.tasks:
        if t.phase_slug == phase_slug or roadmap.slugify(t.phase) == phase_slug or t.phase == phase_slug:
            roadmap.set_marker(roadmap.roadmap_path(project), t.id, " ")

    workflow.append_decision(
        project,
        f"Descarte de la fase {phase_slug}",
        f"Descarte de la fase {phase_slug} y eliminación de su rama {branch}.",
    )

    return f"Fase {phase_slug} descartada: worktree y rama eliminados, tareas devueltas a [ ]."


def discard(project: Path, target: str) -> str:
    """Descarta una tarea en curso, una tarea completada o una fase completa."""
    with project_lock(project):
        kind, obj = resolve_target(project, target)
        data = state.load(project)
        if kind == "task":
            task: roadmap.Task = obj
            if data.get("status") == "IN_PROGRESS" and data.get("current_task_id") == task.id:
                return _discard_in_progress_task(project, task, data)
            elif task.marker == "x":
                return _discard_completed_task(project, task, data)
            else:
                raise HefestoError(
                    f"La tarea {task.id} no está en curso ni completada (marcador [{task.marker}]).",
                    EXIT_INPUT,
                )
        else:
            phase_slug: str = obj
            return _discard_phase(project, phase_slug, data)
