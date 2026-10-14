"""Puertas de aceptación: `review` (independencia del revisor y contrato), `done` y `finish`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from hefesto import config, evidence, gitops, log, providers, roadmap, state, trust, util, workflow
from hefesto.lock import project_lock
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_INPUT, EXIT_OK, HefestoError, now_iso, read_jsonl

AUTHOR_ROLES = ("runner", "forge", "visual", "core")


def runs_for_task(project: Path, task_run_id: str | None) -> list[dict[str, Any]]:
    """Delegaciones registradas por `hefesto run` para la ejecución actual de la tarea."""
    if not task_run_id:
        return []
    return [r for r in read_jsonl(project / ".hefesto" / "logs" / "runs.jsonl") if r.get("task_run_id") == task_run_id]


def code_changes(workdir: Path, base: str | None) -> list[str]:
    return [p for p in gitops.changed_paths(workdir, base) if not (p == ".hefesto" or p.startswith(".hefesto/"))]


def check_independence(cfg: config.Config, authors: list[str], reviewer: str) -> str | None:
    """Regla de la sección 2. Devuelve la nota de excepción (mismo proveedor permitido) o lanza error."""
    rprov = reviewer.split(":", 1)[0]
    if reviewer in authors:
        raise HefestoError(f"El revisor {reviewer} es el mismo modelo que escribió el cambio.", EXIT_BLOCKED)
    same = [a for a in authors if a.split(":", 1)[0] == rprov]
    if not same:
        return None
    alternatives = []
    for entry in cfg.role_chain("reviewer"):
        prov = providers.split_entry(entry)[0]
        if prov != rprov and prov not in {a.split(":", 1)[0] for a in authors}:
            ok, _ = providers.available(cfg, prov)
            if ok:
                alternatives.append(prov)
    if alternatives:
        raise HefestoError(f"El revisor {reviewer} es del mismo proveedor que el autor ({', '.join(same)}) y hay "
                           f"otro proveedor disponible: {', '.join(sorted(set(alternatives)))}.", EXIT_BLOCKED)
    rtier = providers.tier(cfg, reviewer)
    for author in same:
        atier = providers.tier(cfg, author)
        if rtier is None or atier is None or rtier <= atier:
            raise HefestoError(f"Revisor {reviewer} del mismo proveedor que {author} sin ser un modelo superior "
                               f"(niveles en [reviewer].tiers).", EXIT_BLOCKED)
    return (f"mismo proveedor ({rprov}) sin alternativa disponible; revisor {reviewer} superior a "
            f"{', '.join(same)}")


def review(project: Path, task_id: str, verdict: str, notes: str, run_id: str | None,
           reviewer_flag: str | None, author_flags: list[str]) -> tuple[int, str]:
    with project_lock(project):
        cfg = config.load(project)
        data = state.load(project)
        workflow.require_current(data, task_id)
        rm = roadmap.load_valid(project)
        task = rm.get(task_id)
        runs = runs_for_task(project, data.get("run_id"))
        authors = sorted({r["model_id"] for r in runs if r.get("role") in AUTHOR_ROLES and r.get("success")}
                         | {providers.model_id(cfg, a) for a in author_flags})
        reviewer = None
        if run_id:
            match = [r for r in runs if r.get("run_id") == run_id]
            if not match or match[0].get("role") != "reviewer":
                raise HefestoError(f"run_id {run_id} no es una revisión registrada de esta tarea.", EXIT_INPUT)
            reviewer = match[0]["model_id"]
        elif reviewer_flag:
            reviewer = providers.model_id(cfg, reviewer_flag)
        if not reviewer:
            raise HefestoError("Indica la revisión: `--run-id` de `hefesto run reviewer` o `--reviewer proveedor:modelo`.",
                               EXIT_INPUT)
        workdir = Path(data.get("workdir") or project)
        changed = code_changes(workdir, data.get("task_base"))
        if changed and not authors:
            raise HefestoError("No consta quién escribió el cambio: delega con `hefesto run` o indica "
                               "`--author proveedor:modelo`.", EXIT_INPUT)
        exception = check_independence(cfg, authors, reviewer)
        touched_contract = [p for p in rm.contract_paths(task) if any(
            c == p or c.startswith(p.rstrip("/") + "/") for c in changed)]
        if touched_contract:
            verdict = "fail"
            notes = (f"Revisión negativa automática: la implementación modificó tests de contrato "
                     f"({', '.join(touched_contract)}). " + notes).strip()
        record = {"verdict": verdict, "notes": notes, "run_id": run_id, "reviewer": reviewer, "authors": authors,
                  "attempt": data["attempts"], "exception": exception, "at": now_iso()}
        log.record(project, "reviews", {"task_id": task_id, **record})
        if verdict == "fail":
            code, message = workflow.register_failure(project, task_id, f"revisión negativa: {notes}")
            return (code if code else EXIT_INPUT), (message if code else f"Revisión negativa registrada. "
                                                    f"Rol del siguiente intento: {message}")
        data = state.load(project)
        data["review"] = record
        data["next_action"] = f"hefesto done {task_id}"
        state.save(project, data)
        suffix = f" (excepción: {exception})" if exception else ""
        return EXIT_OK, f"Revisión pass registrada para {task_id} por {reviewer}{suffix}."


def done(project: Path, task_id: str) -> tuple[int, str]:
    with project_lock(project):
        cfg = config.load(project)
        data = state.load(project)
        workflow.require_current(data, task_id)
        rm = roadmap.load_valid(project)
        task = rm.get(task_id)
        trusted = trust.status(project, task)
        if trusted != "trusted":
            return EXIT_BLOCKED, (f"El verify de {task_id} es {'nuevo' if trusted == 'new' else 'distinto del aprobado'}"
                                  f": un humano debe ejecutar `hefesto trust`.")
        workdir = Path(data.get("workdir") or project)
        timeout = int(cfg.get("limits", "verify_timeout_seconds"))
        result = evidence.run_verify(project, workdir, task_id, task.verify, timeout, label=f"a{data['attempts'] + 1}")
        if result.exit_code != 0:
            code, message = workflow.register_failure(project, task_id,
                                                      f"verify salió con {result.exit_code}:\n{result.summary}")
            if code:
                return code, f"verify en rojo.\n{message}"
            return EXIT_INPUT, f"verify en rojo (exit {result.exit_code}). Rol del siguiente intento: {message}\n{result.summary}"
        changed = code_changes(workdir, data.get("task_base"))
        review_rec = data.get("review")
        if changed and (not review_rec or review_rec.get("verdict") != "pass" or review_rec.get("attempt") != data["attempts"]):
            return EXIT_BLOCKED, (f"{task_id} cambia código ({len(changed)} ficheros) y no tiene revisión `pass` del "
                                  f"intento actual: `hefesto review {task_id} --verdict pass|fail …`.")
        path = roadmap.roadmap_path(project)
        roadmap.set_marker(path, task_id, "x")
        try:
            commit = gitops.checkpoint(project, workdir, cfg, task, f"hefesto({task_id}): {task.title} [verified]")
        except HefestoError:
            roadmap.set_marker(path, task_id, "~")
            raise
        runs = runs_for_task(project, data.get("run_id"))
        record = {
            "task_id": task_id,
            "title": task.title,
            "phase": task.phase,
            "verify": {"command": task.verify, "sha256": trust.verify_hash(task.verify), "exit_code": result.exit_code,
                       "duration_seconds": result.duration, "output_summary": result.summary,
                       "log_path": result.log_path},
            "commit": commit,
            "base_commit": data.get("task_base"),
            "branch": data.get("branch"),
            "files": gitops.file_hashes(workdir, commit, [p for p in gitops.commit_files(workdir, commit)
                                                          if not p.startswith(".hefesto/")]),
            "review": review_rec,
            "attempts": data["attempts"] + 1,
            "providers": sorted({r["model_id"] for r in runs if r.get("model_id")}),
            "runs": [{"run_id": r.get("run_id"), "role": r.get("role"), "model_id": r.get("model_id"),
                      "success": r.get("success")} for r in runs],
        }
        ev_path, head = evidence.write(project, record)
        try:
            gitops.checkpoint(project, workdir, cfg, task, f"hefesto({task_id}): evidence",
                              extra_paths=(ev_path.relative_to(project).as_posix(),))
        except HefestoError as exc:
            log.event(project, "evidence", f"evidencia de {task_id} escrita pero sin commit: {exc}")
            raise
        all_done = all(t.marker == "x" or t.id == task_id for t in rm.tasks)
        data = state.transition(data, "COMPLETED" if all_done else "IDLE")
        data.update({"current_task_id": None, "active_role": None, "attempts": 0, "last_error": None,
                     "error_history": [], "review": None, "last_checkpoint": commit, "evidence_head": head,
                     "next_action": "hefesto finish" if all_done else "hefesto next"})
        state.save(project, data)
        log.event(project, "done", f"{task_id} completada", commit=commit)
    return EXIT_OK, f"{task_id} completada: commit {commit[:10]}, evidencia {ev_path.name}."


# ---------------------------------------------------------------------------
# finish


def _phase_info(project: Path, data: dict[str, Any], rm: roadmap.Roadmap, phase: str | None) -> tuple[str, dict[str, Any]]:
    phases: dict[str, Any] = data.get("phases") or {}
    if phase:
        slug = roadmap.slugify(phase)
        matches = [s for s in phases if s == slug or s.startswith(slug + "-") or s.startswith(slug)]
        if not matches:
            raise HefestoError(f"No hay rama abierta para la fase `{phase}`.", EXIT_INPUT)
        slug = sorted(matches, key=len)[0]
    else:
        if not phases:
            raise HefestoError("No hay ninguna fase abierta.", EXIT_INPUT)
        order = [t.phase_slug for t in rm.tasks]
        slug = sorted(phases, key=lambda s: order.index(s) if s in order else len(order))[0]
    return slug, phases[slug]


def _yield_control_files(project: Path, branch: str) -> None:
    """Modo worktree: los ficheros de control modificados en la principal deben coincidir con la rama; se
    devuelven a su versión de HEAD para que la integración los traiga (su contenido vive en la rama)."""
    for rel in gitops.dirty_paths(project, include_control=True):
        if not rel.startswith(".hefesto/"):
            continue
        local = (project / rel).read_bytes().replace(b"\r\n", b"\n") if (project / rel).is_file() else None
        theirs = gitops.git(project, "show", f"{branch}:{rel}", check=False, binary=True)
        if local is None or theirs.returncode != 0 or theirs.stdout.replace(b"\r\n", b"\n") != local:
            raise HefestoError(f"{rel} tiene cambios en la copia principal que no están en {branch}; revísalo.",
                               EXIT_CONFLICT)
        tracked = gitops.git(project, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0
        if tracked:
            gitops.git(project, "restore", "--staged", "--worktree", "--source=HEAD", "--", rel)
        else:
            (project / rel).unlink()


def finish(project: Path, phase: str | None, pr: bool) -> tuple[int, str]:
    cfg = config.load(project)
    data = state.load(project)
    rm = roadmap.load_valid(project)
    slug, info = _phase_info(project, data, rm, phase)
    branch, workdir = info["branch"], Path(info["workdir"])
    if data["status"] == "IN_PROGRESS" and data.get("branch") == branch:
        return EXIT_CONFLICT, f"Hay una tarea en curso en {branch}: termínala antes de integrar."
    tasks = [t for t in rm.tasks if t.phase_slug == slug or f"{t.phase_slug}-{t.id.lower()}" == slug]
    pending = [t.id for t in tasks if t.marker != "x"]
    if pending:
        return EXIT_BLOCKED, f"Tareas sin completar en la fase: {', '.join(pending)}."
    missing = [t.id for t in tasks if not (evidence.evidence_dir(project) / f"{t.id}.json").exists()]
    chain_errors = evidence.verify_chain(project, data.get("evidence_head"))
    if missing or chain_errors:
        return EXIT_BLOCKED, "Evidencias inválidas: " + "; ".join([f"falta {m}" for m in missing] + chain_errors)
    if pr and not cfg.get("github", "pr"):
        return EXIT_BLOCKED, "`finish --pr` requiere `github.pr = true` en la configuración."
    util.require_tty("hefesto finish")
    main = gitops.main_branch(project, cfg)
    base = info.get("base") or gitops.out(project, "merge-base", main, branch)
    rebase = gitops.git(workdir, "rebase", "--onto", main, base, branch, check=False)
    if rebase.returncode != 0:
        conflicts = gitops.out(workdir, "diff", "--name-only", "--diff-filter=U")
        gitops.git(workdir, "rebase", "--abort", check=False)
        return EXIT_BLOCKED, ("Conflictos al rebasar sobre " + main + "; no se resuelven automáticamente:\n  "
                              + "\n  ".join(conflicts.splitlines() or [rebase.stderr.strip()]))
    info["base"] = gitops.out(project, "rev-parse", main)
    with project_lock(project):
        fresh = state.load(project)
        fresh_phases = dict(fresh.get("phases") or {})
        fresh_phases[slug] = info
        fresh["phases"] = fresh_phases
        state.save(project, fresh)
    timeout = int(cfg.get("limits", "verify_timeout_seconds"))
    test_command = str(cfg.get("execution", "test_command") or "")
    checks: list[tuple[str, str]] = ([("suite", test_command)] if test_command else [])
    for task in tasks:
        if trust.status(project, task) != "trusted":
            return EXIT_BLOCKED, f"El verify de {task.id} ya no coincide con el aprobado: `hefesto trust`."
        checks.append((task.id, task.verify))
    for label, command in checks:
        result = evidence.run_verify(project, workdir, label, command, timeout, label="finish")
        if result.exit_code != 0:
            return EXIT_INPUT, f"`{label}` falla tras el rebase (exit {result.exit_code}); no se integra.\n{result.summary}"
    commits = gitops.out(workdir, "log", "--oneline", f"{main}..{branch}")
    stat = gitops.out(workdir, "diff", "--stat", f"{main}...{branch}")
    used = sorted({p for t in tasks for p in evidence.show(project, t.id).get("providers", [])})
    summary = (f"hefesto: integra {branch}\n\nTareas:\n" + "\n".join(f"- {t.id}: {t.title}" for t in tasks)
               + f"\n\nProveedores: {', '.join(used) or 'ninguno registrado'}\n\nCommits:\n{commits}\n\n{stat}\n")
    print(summary)
    if pr:
        if not util.confirm(f"¿Subir {branch} a origin y abrir un PR en borrador?", default=False):
            return EXIT_OK, "Cancelado: no se ha subido nada."
        push = gitops.git(workdir, "push", "-u", "origin", branch, check=False)
        if push.returncode != 0:
            return EXIT_CONFLICT, f"git push falló: {push.stderr.strip()}"
        gh = subprocess.run(["gh", "pr", "create", "--draft", "--base", main, "--head", branch, "--title",
                             f"hefesto: {slug}", "--body", summary], cwd=workdir, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        return (EXIT_OK if gh.returncode == 0 else EXIT_CONFLICT), (gh.stdout or gh.stderr).strip()
    strategy = cfg.get("execution", "finish_strategy")
    if not util.confirm(f"¿Integrar {branch} en {main} con {strategy}?", default=False):
        return EXIT_OK, "Cancelado: la rama sigue intacta."
    with project_lock(project):
        if info["isolation"] == "worktree":
            if gitops.current_branch(project) != main:
                return EXIT_CONFLICT, f"La copia principal no está en {main}; no se integra."
            _yield_control_files(project, branch)
        else:
            if gitops.dirty_paths(project, include_control=True):
                return EXIT_CONFLICT, "La copia de trabajo tiene cambios sin commitear; no se integra."
            gitops.git(project, "checkout", "-q", main)
        if strategy == "squash":
            gitops.git(project, "merge", "--squash", branch)
            gitops.git(project, "commit", "-q", "-m", summary)
        else:
            gitops.git(project, "merge", "--no-ff", "-m", summary, branch)
        merged = gitops.out(project, "rev-parse", "HEAD")
        if info["isolation"] == "worktree":
            gitops.git(project, "worktree", "remove", str(workdir))
        gitops.git(project, "branch", "-D" if strategy == "squash" else "-d", branch)
        data = state.load(project)
        phases = dict(data.get("phases") or {})
        phases.pop(slug, None)
        data["phases"] = phases
        if data.get("branch") == branch:
            data.update({"branch": None, "workdir": None, "isolation": None})
        data["next_action"] = "hefesto next"
        state.save(project, data)
        log.event(project, "finish", f"{branch} integrada en {main}", commit=merged)
    return EXIT_OK, f"{branch} integrada en {main} ({strategy}): {merged[:10]}."
