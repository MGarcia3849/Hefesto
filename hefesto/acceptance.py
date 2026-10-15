"""Puertas de aceptación: `review` (independencia del revisor y contrato), `done` y `finish`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hefesto import config, evidence, gitops, log, providers, roadmap, state, trust, util, workflow
from hefesto.lock import project_lock
from hefesto.util import (
    EXIT_BLOCKED,
    EXIT_CONFLICT,
    EXIT_INPUT,
    EXIT_OK,
    HefestoError,
    atomic_write_json,
    now_iso,
    read_jsonl,
)

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
            match = [r for r in runs if r.get("run_id") == run_id and r.get("role") == "reviewer"]
            if not match:
                raise HefestoError(f"run_id {run_id} no es una revisión registrada de esta tarea.", EXIT_INPUT)
            successful = [r for r in match if r.get("success")]
            if not successful:
                raise HefestoError(f"run_id {run_id} no tiene ningún registro con éxito.", EXIT_INPUT)
            reviewer = successful[0]["model_id"]
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
            auto_safe = bool(cfg.get("trust", "auto_safe", default=True))
            extra = cfg.get("trust", "extra_safe_prefixes", default=[])
            if auto_safe and trust.is_safe_verify(task.verify, extra_prefixes=extra):
                tdata = trust.load(project)
                tdata["verify"][task_id] = {
                    "sha256": trust.verify_hash(task.verify),
                    "approved_at": now_iso(),
                    "auto": True,
                }
                atomic_write_json(trust.trust_path(project), tdata)
                trusted = "trusted"
            else:
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
        tentry = trust.load(project)["verify"].get(task_id) or {}
        trust_mode = "auto" if tentry.get("auto") else "human"
        record = {
            "task_id": task_id,
            "title": task.title,
            "phase": task.phase,
            "contract": list(task.contract),
            "verify": {"command": task.verify, "sha256": trust.verify_hash(task.verify), "exit_code": result.exit_code,
                       "duration_seconds": result.duration, "output_summary": result.summary,
                       "log_path": result.log_path, "trust": trust_mode},
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
        is_local = bool(getattr(cfg, "settings", None) and cfg.settings.get("install_mode") == "local")
        if not is_local:
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

        graph_msg = ""
        from hefesto import packs
        if packs.is_pack_enabled(project, "context"):
            _, graph_msg = packs.run_graphify(workdir, ["update", "."])

    msg = f"{task_id} completada: commit {commit[:10]}, evidencia {ev_path.name}."
    if graph_msg:
        msg = f"{msg} {graph_msg}".strip()
    return EXIT_OK, msg


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


def _check_control_files(project: Path, branch: str) -> None:
    for rel in gitops.dirty_paths(project, include_control=True):
        if not gitops.is_control_path(rel):
            continue
        local = (project / rel).read_bytes().replace(b"\r\n", b"\n") if (project / rel).is_file() else None
        theirs = gitops.git(project, "show", f"{branch}:{rel}", check=False, binary=True)
        if local is None or theirs.returncode != 0 or theirs.stdout.replace(b"\r\n", b"\n") != local:
            raise HefestoError(f"{rel} tiene cambios en la copia principal que no están en {branch}; revísalo.",
                               EXIT_CONFLICT)


def _yield_control_files(project: Path, branch: str) -> list[str]:
    """Modo worktree: los ficheros de control modificados en la principal deben coincidir con la rama; se
    devuelven a su versión de HEAD para que la integración los traiga (su contenido vive en la rama)."""
    _check_control_files(project, branch)
    yielded: list[str] = []
    for rel in gitops.dirty_paths(project, include_control=True):
        if not gitops.is_control_path(rel):
            continue
        tracked = gitops.git(project, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0
        if tracked:
            gitops.git(project, "restore", "--staged", "--worktree", "--source=HEAD", "--", rel)
        else:
            (project / rel).unlink(missing_ok=True)
        yielded.append(rel)
    return yielded


def _restore_control_files(project: Path, branch_or_ref: str, files: list[str]) -> None:
    for rel in files:
        theirs = gitops.git(project, "show", f"{branch_or_ref}:{rel}", check=False, binary=True)
        if theirs.returncode == 0:
            target = project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(theirs.stdout)


def _cleanup_temp_finish(project: Path, temp_worktree: Path, temp_branch: str) -> None:
    listed = gitops.git(project, "worktree", "list", "--porcelain", check=False).stdout
    if any(str(Path(line[len("worktree "):]).resolve()).lower() == str(temp_worktree.resolve()).lower()
           for line in listed.splitlines() if line.startswith("worktree ")):
        gitops.git(project, "worktree", "remove", "--force", str(temp_worktree), check=False)
    if temp_worktree.exists():
        shutil.rmtree(temp_worktree, ignore_errors=True)
    gitops.git(project, "worktree", "prune", check=False)
    if gitops.branch_exists(project, temp_branch):
        gitops.git(project, "branch", "-D", temp_branch, check=False)


def _is_contract_task(project: Path, workdir: Path, task_id: str) -> bool:
    try:
        ev = evidence.show(project, task_id)
    except Exception:
        return False
    if "contract" in ev:
        return bool(ev["contract"])
    commit = ev.get("commit")
    if not commit:
        return False
    proc = gitops.git(workdir, "show", f"{commit}:.hefesto/ROADMAP.md", check=False)
    if proc.returncode != 0:
        return False
    try:
        old_rm = roadmap.parse_text(proc.stdout)
        old_task = old_rm.get(task_id)
        return bool(old_task.contract)
    except Exception:
        return False


def finish(project: Path, phase: str | None, pr: bool, yes: bool = False) -> tuple[int, str]:
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
    missing = [t.id for t in tasks if evidence.latest_evidence(project, t.id) is None]
    chain_errors = evidence.verify_chain(project, data.get("evidence_head"))
    if missing or chain_errors:
        return EXIT_BLOCKED, "Evidencias inválidas: " + "; ".join([f"falta {m}" for m in missing] + chain_errors)
    if pr and not cfg.get("github", "pr"):
        return EXIT_BLOCKED, "`finish --pr` requiere `github.pr = true` en la configuración."
    if yes:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    else:
        util.require_tty("hefesto finish")

    main = gitops.main_branch(project, cfg)
    is_local = bool(getattr(cfg, "settings", None) and cfg.settings.get("install_mode") == "local")

    diff_proc = gitops.git(project, "diff", "--name-only", f"{main}...{branch}", check=False)
    branch_diff = [p.strip() for p in diff_proc.stdout.splitlines() if p.strip()]
    untracked_proc = gitops.git(project, "ls-files", "--others", "--exclude-standard", check=False)
    untracked_main = [p.strip() for p in untracked_proc.stdout.splitlines() if p.strip()]
    collisions = sorted(p for p in set(branch_diff) & set(untracked_main) if not (p == ".hefesto" or p.startswith(".hefesto/")))
    if collisions:
        return EXIT_CONFLICT, ("Ficheros sin versionar en la copia principal que el merge sobrescribiría:\n  - "
                               + "\n  - ".join(collisions))

    try:
        if info.get("isolation") == "worktree":
            if gitops.current_branch(project) != main:
                return EXIT_CONFLICT, f"La copia principal no está en {main}; no se integra."
            if not is_local:
                _check_control_files(project, branch)
    except HefestoError as exc:
        return exc.code, str(exc)

    temp_branch = f"hefesto/_finish/{slug}"
    temp_worktree = gitops.worktree_root(project) / "_finish"
    _cleanup_temp_finish(project, temp_worktree, temp_branch)

    try:
        gitops.git(project, "branch", temp_branch, branch)
        gitops.worktree_root(project).mkdir(parents=True, exist_ok=True)
        gitops.git(project, "worktree", "add", "-q", str(temp_worktree), temp_branch)

        base = info.get("base") or gitops.out(project, "merge-base", main, branch)
        rebase = gitops.git(temp_worktree, "rebase", "--onto", main, base, temp_branch, check=False)
        if rebase.returncode != 0:
            conflicts = gitops.out(temp_worktree, "diff", "--name-only", "--diff-filter=U")
            gitops.git(temp_worktree, "rebase", "--abort", check=False)
            return EXIT_BLOCKED, ("Conflictos al rebasar sobre " + main + "; no se resuelven automáticamente:\n  "
                                  + "\n  ".join(conflicts.splitlines() or [rebase.stderr.strip()]))

        timeout = int(cfg.get("limits", "verify_timeout_seconds"))
        test_command = str(cfg.get("execution", "test_command") or "")
        checks: list[tuple[str, str]] = ([("suite", test_command)] if test_command else [])
        skipped_contract: list[str] = []
        for task in tasks:
            if trust.status(project, task) != "trusted":
                return EXIT_BLOCKED, f"El verify de {task.id} ya no coincide con el aprobado: `hefesto trust`."
            if _is_contract_task(project, temp_worktree, task.id):
                skipped_contract.append(task.id)
                continue
            checks.append((task.id, task.verify))
        for label, command in checks:
            result = evidence.run_verify(project, temp_worktree, label, command, timeout, label="finish")
            if result.exit_code != 0:
                return EXIT_INPUT, f"`{label}` falla tras el rebase (exit {result.exit_code}); no se integra.\n{result.summary}"

        commits = gitops.out(temp_worktree, "log", "--oneline", f"{main}..{temp_branch}")
        stat = gitops.out(temp_worktree, "diff", "--stat", f"{main}...{temp_branch}")
        used = sorted({p for t in tasks for p in evidence.show(project, t.id).get("providers", [])})
        omitted = f"\n\nVerify de contrato omitidos: {', '.join(skipped_contract)}" if skipped_contract else ""
        summary = (f"hefesto: integra {branch}\n\nTareas:\n" + "\n".join(f"- {t.id}: {t.title}" for t in tasks)
                   + omitted
                   + f"\n\nProveedores: {', '.join(used) or 'ninguno registrado'}\n\nCommits:\n{commits}\n\n{stat}\n")
        print(summary)
        if pr:
            if not yes and not util.confirm(f"¿Subir {branch} a origin y abrir un PR en borrador?", default=False):
                return EXIT_OK, "Cancelado: no se ha subido nada."
            push = gitops.git(workdir if workdir.exists() else project, "push", "-u", "origin", branch, check=False)
            if push.returncode != 0:
                return EXIT_CONFLICT, f"git push falló: {push.stderr.strip()}"
            gh = subprocess.run(["gh", "pr", "create", "--draft", "--base", main, "--head", branch, "--title",
                                 f"hefesto: {slug}", "--body", summary], cwd=workdir if workdir.exists() else project,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            return (EXIT_OK if gh.returncode == 0 else EXIT_CONFLICT), (gh.stdout or gh.stderr).strip()

        strategy = cfg.get("execution", "finish_strategy")
        if not yes and not util.confirm(f"¿Integrar {branch} en {main} con {strategy}?", default=False):
            return EXIT_OK, "Cancelado: la rama sigue intacta."

        with project_lock(project):
            yielded_control_files: list[str] = []
            if info.get("isolation") == "worktree":
                if not is_local:
                    yielded_control_files = _yield_control_files(project, branch)
            else:
                gitops.git(project, "checkout", "-q", main)

            merge_err: str | None = None
            if strategy == "squash":
                proc = gitops.git(project, "merge", "--squash", temp_branch, check=False)
                if proc.returncode != 0:
                    merge_err = proc.stderr.strip() or proc.stdout.strip() or "merge --squash falló"
                else:
                    proc = gitops.git(project, "commit", "-q", "-m", summary, check=False)
                    if proc.returncode != 0:
                        merge_err = proc.stderr.strip() or proc.stdout.strip() or "commit falló"
            else:
                proc = gitops.git(project, "merge", "--no-ff", "-m", summary, temp_branch, check=False)
                if proc.returncode != 0:
                    merge_err = proc.stderr.strip() or proc.stdout.strip() or "merge --no-ff falló"

            if merge_err is not None:
                gitops.git(project, "merge", "--abort", check=False)
                _restore_control_files(project, temp_branch, yielded_control_files)
                if info.get("isolation") != "worktree":
                    gitops.git(project, "checkout", "-q", branch, check=False)
                return EXIT_CONFLICT, f"Error al integrar {branch} en {main}: {merge_err}"

            merged = gitops.out(project, "rev-parse", "HEAD")
            if info.get("isolation") == "worktree":
                gitops.git(project, "worktree", "remove", "--force", str(workdir), check=False)
                if workdir.exists():
                    shutil.rmtree(workdir, ignore_errors=True)
                gitops.git(project, "worktree", "prune", check=False)
            gitops.git(project, "branch", "-D", branch, check=False)

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
    finally:
        _cleanup_temp_finish(project, temp_worktree, temp_branch)
