"""Modo paralelo: scheduler, ciclo automático por tarea e integración serial."""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hefesto import acceptance, config, evidence, gitops, log, loop, providers, roadmap, router, state, trust, util, workflow
from hefesto.lock import project_lock
from hefesto.util import EXIT_CONFLICT, HefestoError, atomic_write_json, now_iso

RECOVER_AFTER = 3
VERDICT_RE = re.compile(r"^\s*(?:VEREDICTO|VERDICT):\s*pass\s*$", re.I | re.M)


def task_branch(phase_branch: str, task_id: str) -> str:
    """Nombre de la rama de trabajo para una tarea paralela."""
    return f"{phase_branch}--{task_id}"


def task_workdir(project: Path, phase_branch: str, task_id: str) -> Path:
    """Directorio del worktree para una tarea paralela, fuera del proyecto."""
    branch = task_branch(phase_branch, task_id)
    return gitops.worktree_root(project) / branch.replace("/", "__")


def _drop_task_branch(project: Path, phase_branch: str, tb: str) -> bool:
    """Solo borra la rama tb si está contenida en phase_branch; si no, la conserva y devuelve False."""
    if not gitops.branch_exists(project, tb):
        return True
    if not gitops.branch_exists(project, phase_branch):
        return False
    res = gitops.git(project, "merge-base", "--is-ancestor", tb, phase_branch, check=False)
    if res.returncode == 0:
        gitops.git(project, "branch", "-D", tb, check=False)
        return True
    return False


def _norm_parts(path: str) -> tuple[str, ...]:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return tuple(part for part in p.strip("/").split("/") if part and part != ".")


def files_overlap(a: list[str] | tuple[str, ...], b: list[str] | tuple[str, ...]) -> bool:
    """True si alguna ruta de a es igual o está dentro de una de b (o viceversa)."""
    if not a or not b:
        return False
    a_parts = [_norm_parts(p) for p in a if p]
    b_parts = [_norm_parts(p) for p in b if p]
    for p1 in a_parts:
        if not p1:
            return True
        for p2 in b_parts:
            if not p2:
                return True
            if len(p1) <= len(p2) and p2[:len(p1)] == p1:
                return True
            if len(p2) <= len(p1) and p1[:len(p2)] == p2:
                return True
    return False


def select_batch(rm: roadmap.Roadmap, running: list[str] | set[str], capacity: int) -> list[roadmap.Task]:
    """Selecciona un lote determinista de tareas listas para ejecutar en paralelo."""
    if capacity <= 0:
        return []

    by_id = rm.by_id()
    running_ids = set(running) | {t.id for t in rm.tasks if t.marker == "~"}
    has_running = len(running_ids) > 0
    running_files: list[str] = []
    for tid in running_ids:
        if tid in by_id:
            if not by_id[tid].parallel or by_id[tid].size == "L":
                return []
            running_files.extend(by_id[tid].files)

    # Buscar la primera fase que tenga tareas sin [x]
    seen_phases: list[str] = []
    for t in rm.tasks:
        if t.phase not in seen_phases:
            seen_phases.append(t.phase)

    target_phase: str | None = None
    for phase in seen_phases:
        pts = [t for t in rm.tasks if t.phase == phase]
        if any(t.marker != "x" for t in pts):
            target_phase = phase
            break

    if target_phase is None:
        return []

    phase_tasks = [t for t in rm.tasks if t.phase == target_phase]
    chosen: list[roadmap.Task] = []
    chosen_files: list[str] = []

    for task in phase_tasks:
        if task.marker != " ":
            continue
        if task.id in running_ids:
            continue
        if task.role == "core":
            continue
        # Todas las dependencias deben estar completadas ([x])
        if any(by_id.get(dep) is None or by_id[dep].marker != "x" for dep in task.depends_on):
            continue

        is_alone = (not task.parallel) or (task.size == "L")
        if is_alone:
            if has_running or chosen:
                continue
            if files_overlap(task.files, running_files):
                continue
            return [task]

        if files_overlap(task.files, running_files):
            continue
        if files_overlap(task.files, chosen_files):
            continue

        chosen.append(task)
        chosen_files.extend(task.files)
        if len(chosen) >= capacity:
            break

    return chosen


def parse_verdict(summary: str) -> str:
    """Extrae el veredicto del resumen del revisor ('pass' o 'fail')."""
    return "pass" if VERDICT_RE.search(summary) else "fail"


class Concurrency:
    """Gestión de límites de concurrencia globales y por modelo con retroceso adaptativo."""

    def __init__(self, maxima: dict[str, int], adaptive: bool = True) -> None:
        self.maxima = dict(maxima)
        self.adaptive = adaptive
        self._limits: dict[str, int] = {}
        self._streaks: dict[str, int] = {}
        self.history: list[tuple[str, int]] = []

    def maximum(self, model_id: str) -> int:
        if model_id in self.maxima:
            return int(self.maxima[model_id])
        if ":" in model_id:
            prov = model_id.split(":", 1)[0]
            wildcard = f"{prov}:*"
            if wildcard in self.maxima:
                return int(self.maxima[wildcard])
        return 1

    def limit(self, model_id: str) -> int:
        if not self.adaptive:
            return self.maximum(model_id)
        return self._limits.get(model_id, self.maximum(model_id))

    def on_rate_limit(self, model_id: str) -> int:
        if not self.adaptive:
            return self.maximum(model_id)
        self._streaks[model_id] = 0
        current = self.limit(model_id)
        new_limit = max(1, current // 2)
        self._limits[model_id] = new_limit
        if new_limit != current:
            self.history.append((model_id, new_limit))
        return new_limit

    def on_success(self, model_id: str) -> int:
        if not self.adaptive:
            return self.maximum(model_id)
        current = self.limit(model_id)
        max_limit = self.maximum(model_id)
        if current >= max_limit:
            self._streaks[model_id] = 0
            return current
        streak = self._streaks.get(model_id, 0) + 1
        self._streaks[model_id] = streak
        if streak >= RECOVER_AFTER:
            self._streaks[model_id] = 0
            new_limit = min(max_limit, current + 1)
            if new_limit != current:
                self.history.append((model_id, new_limit))
                self._limits[model_id] = new_limit
            return new_limit
        return current

    def save(self) -> None:
        path = util.state_home() / "concurrency.json"
        data = {
            "schema_version": 1,
            "limits": self._limits,
        }
        atomic_write_json(path, data)

    @classmethod
    def from_config(cls, cfg: config.Config) -> Concurrency:
        maxima = dict(cfg.get("limits", "max_parallel_per_model", default={}) or {})
        adaptive = bool(cfg.get("limits", "adaptive_concurrency", default=True))
        conc = cls(maxima=maxima, adaptive=adaptive)
        path = util.state_home() / "concurrency.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                saved = raw.get("limits") or {}
                for mid, val in saved.items():
                    max_val = conc.maximum(mid)
                    conc._limits[mid] = min(max_val, max(1, int(val)))
            except Exception:
                pass
        return conc


def pick_entry(
    cfg: config.Config,
    role: str,
    busy: Counter[str] | dict[str, int],
    conc: Concurrency,
) -> str | None:
    """Elige la primera entrada de la cadena de rol con proveedor disponible y hueco de modelo."""
    for entry in cfg.role_chain(role):
        provider = providers.split_entry(entry)[0]
        mid = providers.model_id(cfg, entry)
        ok, _ = providers.available(cfg, provider)
        if not ok:
            continue
        if busy.get(mid, 0) < conc.limit(mid):
            return entry
    return None


@dataclass
class Job:
    task: roadmap.Task
    workdir: Path
    entry: str
    model_id: str
    role: str
    attempt: int
    last_error: str | None = None


@dataclass
class WorkerOutcome:
    ok: bool
    error_type: str | None = None
    summary: str = ""
    review: dict[str, Any] | None = None
    model_id: str | None = None


@dataclass
class TaskResult:
    task_id: str
    status: str
    detail: str = ""
    attempts: int = 0


class _ModelSlots:
    """Reservas compartidas por autores y revisores, como máximo una por tarea."""

    def __init__(self, cfg: config.Config, conc: Concurrency) -> None:
        self.cfg = cfg
        self.conc = conc
        self.busy: Counter[str] = Counter()
        self.owners: dict[str, str] = {}
        self.condition = threading.Condition()

    def acquire(self, task_id: str, role: str, *, wait: bool = False,
                authors: list[str] | None = None) -> str | None:
        with self.condition:
            while True:
                available = False
                for entry in self.cfg.role_chain(role):
                    mid = providers.model_id(self.cfg, entry)
                    if not providers.available(self.cfg, providers.split_entry(entry)[0])[0]:
                        continue
                    if authors is not None:
                        try:
                            acceptance.check_independence(self.cfg, authors, mid)
                        except HefestoError:
                            continue
                    available = True
                    if self.busy[mid] < self.conc.limit(mid):
                        self.busy[mid] += 1
                        self.owners[task_id] = mid
                        return entry
                if not wait or not available:
                    return None
                self.condition.wait(timeout=0.1)

    def release(self, task_id: str) -> None:
        with self.condition:
            mid = self.owners.pop(task_id, None)
            if mid is not None:
                self.busy[mid] -= 1
                self.condition.notify_all()


def _capacity_failure(result: router.RunOutcome, model_id: str) -> WorkerOutcome | None:
    """El router agota la entrada fijada con result=None y conserva el motivo en hops."""
    if result.result and result.result.error_type in ("rate_limit", "quota"):
        return WorkerOutcome(False, result.result.error_type, result.result.summary, model_id=model_id)
    if result.result is None:
        for hop in reversed(result.hops):
            kind = hop.outcome.partition(":")[0]
            if kind in ("rate_limit", "quota"):
                return WorkerOutcome(False, kind, hop.outcome, model_id=hop.model_id)
    return None


def default_worker(
    project: Path,
    cfg: config.Config,
    job: Job,
    task_run_id: str,
    slots: _ModelSlots | None = None,
) -> WorkerOutcome:
    """Worker por defecto: implementación con router, verify local y revisión independiente."""
    if job.last_error:
        instr = f"Reintento {job.attempt} tras el error anterior:\n{job.last_error}"
    else:
        instr = f"Implementa {job.task.id}: {job.task.title}"

    try:
        runner_res = router.run(
            project,
            job.role,
            instr,
            task_id=job.task.id,
            workdir=job.workdir,
            entries=[job.entry],
            task_run_id=task_run_id,
        )
    finally:
        if slots is not None:
            slots.release(job.task.id)
    capacity_failure = _capacity_failure(runner_res, job.model_id)
    if capacity_failure is not None:
        return capacity_failure
    if not runner_res.result or not runner_res.result.success:
        summary = runner_res.result.summary if runner_res.result else "falló la ejecución del agente"
        return WorkerOutcome(
            ok=False,
            summary=summary,
            model_id=job.model_id,
        )

    verify_timeout = int(cfg.get("limits", "verify_timeout_seconds", default=60))
    v_res = evidence.run_verify(
        project,
        job.workdir,
        job.task.id,
        job.task.verify,
        verify_timeout,
        label=f"p{job.attempt}",
    )
    if v_res.exit_code != 0:
        return WorkerOutcome(
            ok=False,
            summary=f"verify salió con {v_res.exit_code}:\n{v_res.summary}",
            model_id=job.model_id,
        )

    rev_instr = (
        f"Revisa el trabajo de {job.task.id} en {job.workdir}. "
        "Indica si la solución cumple los requisitos y emite una línea con 'VEREDICTO: pass' o 'VEREDICTO: fail'."
    )
    authors = sorted({r["model_id"] for r in acceptance.runs_for_task(project, task_run_id)
                      if r.get("role") in acceptance.AUTHOR_ROLES and r.get("success") and r.get("model_id")}
                     | {runner_res.result.model or job.model_id})
    review_slots = slots if slots is not None else _ModelSlots(cfg, Concurrency.from_config(cfg))
    rev_entry = review_slots.acquire(job.task.id, "reviewer", wait=True, authors=authors)
    if rev_entry is None:
        return WorkerOutcome(False, summary="sin revisor independiente disponible", model_id=job.model_id)
    rev_mid = providers.model_id(cfg, rev_entry)
    try:
        rev_res = router.run(
            project,
            "reviewer",
            rev_instr,
            task_id=job.task.id,
            workdir=job.workdir,
            entries=[rev_entry],
            task_run_id=task_run_id,
        )
    finally:
        review_slots.release(job.task.id)
    capacity_failure = _capacity_failure(rev_res, rev_mid)
    if capacity_failure is not None:
        return capacity_failure
    if not rev_res.result or not rev_res.result.success:
        return WorkerOutcome(
            ok=False,
            summary=rev_res.result.summary if rev_res.result else "falló la revisión",
            model_id=job.model_id,
        )

    verdict = parse_verdict(rev_res.result.summary)
    rev_data = {
        "verdict": verdict,
        "run_id": rev_res.run_id,
        "model_id": rev_res.result.model,
    }
    try:
        acceptance.check_independence(cfg, authors, rev_res.result.model)
    except HefestoError as exc:
        rev_data["verdict"] = "fail"
        return WorkerOutcome(False, summary=str(exc), review=rev_data, model_id=job.model_id)
    if verdict != "pass":
        return WorkerOutcome(
            ok=False,
            summary=rev_res.result.summary,
            review=rev_data,
            model_id=job.model_id,
        )

    return WorkerOutcome(
        ok=True,
        summary=runner_res.result.summary,
        review=rev_data,
        model_id=job.model_id,
    )


def _execute_job(
    custom_worker: Callable[[Job], WorkerOutcome] | None,
    job: Job,
    project: Path,
    cfg: config.Config,
    task_run_id: str,
    slots: _ModelSlots,
) -> WorkerOutcome:
    try:
        if custom_worker is not None:
            return custom_worker(job)
        return default_worker(project, cfg, job, task_run_id, slots)
    except Exception as exc:
        return WorkerOutcome(ok=False, summary=str(exc), model_id=job.model_id)


def run_parallel(
    project: Path,
    max_parallel: int | None = None,
    worker: Callable[[Job], WorkerOutcome] | None = None,
    concurrency: Concurrency | None = None,
) -> list[TaskResult]:
    """Ejecuta el scheduler paralelo procesando tareas independientes concurrentemente."""
    cfg = config.load(project)
    cfg_max_parallel = int(cfg.get("limits", "max_parallel_tasks", default=4))
    effective_max = max_parallel if max_parallel is not None else cfg_max_parallel
    effective_max = max(1, effective_max)

    conc = concurrency if concurrency is not None else Concurrency.from_config(cfg)
    max_attempts = int(cfg.get("limits", "max_attempts", default=3))
    stop_policy = str(cfg.get("limits", "stop_policy", default="finish_running"))

    with project_lock(project):
        st = state.load(project)
        if st.get("status") == "IN_PROGRESS":
            raise HefestoError("Hay una tarea en curso en serie.", EXIT_CONFLICT)

        rm = roadmap.load_valid(project)
        # Limpieza de [~] pendientes de ejecuciones interrumpidas
        for t in rm.tasks:
            if t.marker == "~":
                roadmap.set_marker(roadmap.roadmap_path(project), t.id, " ")
                pb = gitops.branch_for(t, cfg)
                tb = task_branch(pb, t.id)
                tw = task_workdir(project, pb, t.id)
                if tw.exists():
                    if not gitops.dirty_paths(tw, include_control=True):
                        gitops.remove_worktree(project, tw)
                        _drop_task_branch(project, pb, tb)
                else:
                    _drop_task_branch(project, pb, tb)
        gitops.git(project, "worktree", "prune", check=False)

        rm = roadmap.load_valid(project)
        target_phase: str | None = None
        for p in rm.phases:
            pts = [t for t in rm.tasks if t.phase == p]
            if any(t.marker != "x" for t in pts):
                target_phase = p
                break

        if target_phase is None:
            conc.save()
            return []

        rep_task = next(t for t in rm.tasks if t.phase == target_phase)
        phases = dict(st.get("phases") or {})
        info = gitops.prepare_workdir(project, cfg, rm, rep_task, phases)
        phase_branch = info["branch"]
        phase_workdir = Path(info["workdir"])
        st["phases"] = phases
        state.save(project, st)

    results: dict[str, TaskResult] = {}
    active_jobs: dict[str, Job] = {}
    futures: dict[concurrent.futures.Future[WorkerOutcome], tuple[str, Job]] = {}
    slots = _ModelSlots(cfg, conc)
    attempts: Counter[str] = Counter()
    last_errors: dict[str, str | None] = {}
    task_run_ids: dict[str, str] = {}
    task_base_commits: dict[str, str] = {}
    skipped_tasks: set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_max) as executor:
        while True:
            stopped = loop.stop_path(project).exists()

            if not stopped:
                rm = roadmap.load_valid(project)
                capacity = max(0, effective_max - len(active_jobs))
                if capacity > 0:
                    running_ids = list(active_jobs.keys())
                    batch = select_batch(rm, running_ids, capacity)
                    batch = [t for t in batch if t.phase == target_phase
                             and t.id not in skipped_tasks and t.id not in results]
                    for task in batch:
                        if loop.stop_path(project).exists() or len(active_jobs) >= effective_max:
                            break

                        # Comprobar verify trust
                        trusted = trust.status(project, task)
                        if trusted != "trusted":
                            auto_safe = bool(cfg.get("trust", "auto_safe", default=True))
                            extra = cfg.get("trust", "extra_safe_prefixes", default=[])
                            if auto_safe and trust.is_safe_verify(task.verify, extra_prefixes=extra):
                                tdata = trust.load(project)
                                tdata["verify"][task.id] = {
                                    "sha256": trust.verify_hash(task.verify),
                                    "approved_at": now_iso(),
                                    "auto": True,
                                }
                                atomic_write_json(trust.trust_path(project), tdata)
                                trusted = "trusted"
                            else:
                                results[task.id] = TaskResult(
                                    task_id=task.id,
                                    status="skipped",
                                    detail="verify no confiado (trust requerido)",
                                    attempts=attempts[task.id],
                                )
                                skipped_tasks.add(task.id)
                                continue

                        attempt = attempts[task.id] + 1
                        role = workflow.role_for_attempt(task.role, attempt, max_attempts)
                        entry = slots.acquire(task.id, role)
                        if entry is None:
                            continue

                        mid = providers.model_id(cfg, entry)
                        tb = task_branch(phase_branch, task.id)
                        tw = task_workdir(project, phase_branch, task.id)

                        with project_lock(project):
                            if loop.stop_path(project).exists():
                                slots.release(task.id)
                                break
                            tip = gitops.out(project, "rev-parse", phase_branch)
                            task_base_commits.setdefault(task.id, tip)
                            if not tw.exists():
                                if not gitops.branch_exists(project, tb):
                                    gitops.git(project, "worktree", "add", "-q", "-b", tb, str(tw), tip)
                                else:
                                    gitops.git(project, "worktree", "add", "-q", str(tw), tb)
                                gitops.link_worktree(
                                    project, tw, cfg.get("execution", "worktree_links", default=[]) or []
                                )
                            roadmap.set_marker(roadmap.roadmap_path(project), task.id, "~")

                        if loop.stop_path(project).exists():
                            slots.release(task.id)
                            with project_lock(project):
                                roadmap.set_marker(roadmap.roadmap_path(project), task.id, " ")
                                if not gitops.dirty_paths(tw, include_control=True):
                                    gitops.remove_worktree(project, tw)
                                    _drop_task_branch(project, phase_branch, tb)
                            break

                        if task.id not in task_run_ids:
                            task_run_ids[task.id] = workflow.new_run_id()
                        trun = task_run_ids[task.id]

                        job = Job(
                            task=task,
                            workdir=tw,
                            entry=entry,
                            model_id=mid,
                            role=role,
                            attempt=attempt,
                            last_error=last_errors.get(task.id),
                        )
                        active_jobs[task.id] = job

                        fut = executor.submit(
                            _execute_job,
                            worker,
                            job,
                            project,
                            cfg,
                            trun,
                            slots,
                        )
                        futures[fut] = (task.id, job)

            if not futures:
                break

            done_set, _ = concurrent.futures.wait(
                futures.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

            for fut in done_set:
                task_id, job = futures.pop(fut)
                del active_jobs[task_id]
                slots.release(task_id)

                try:
                    outcome = fut.result()
                except Exception as exc:
                    outcome = WorkerOutcome(ok=False, summary=str(exc), model_id=job.model_id)

                tb = task_branch(phase_branch, task_id)

                # Comprobar cancelación por STOP
                if loop.stop_path(project).exists() and stop_policy == "cancel_running":
                    with project_lock(project):
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, " ")
                        if job.workdir.exists():
                            if not gitops.dirty_paths(job.workdir, include_control=True):
                                gitops.remove_worktree(project, job.workdir)
                                _drop_task_branch(project, phase_branch, tb)
                        else:
                            _drop_task_branch(project, phase_branch, tb)
                    results[task_id] = TaskResult(
                        task_id=task_id,
                        status="cancelled",
                        detail="cancelado por STOP",
                        attempts=attempts[task_id],
                    )
                    continue

                # Rate limit o quota: retroceso y reencolar sin contar intento
                if outcome.error_type in ("rate_limit", "quota"):
                    with slots.condition:
                        conc.on_rate_limit(outcome.model_id or job.model_id)
                        slots.condition.notify_all()
                    with project_lock(project):
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, " ")
                        if job.workdir.exists():
                            if not gitops.dirty_paths(job.workdir, include_control=True):
                                gitops.remove_worktree(project, job.workdir)
                                _drop_task_branch(project, phase_branch, tb)
                        else:
                            _drop_task_branch(project, phase_branch, tb)
                    continue

                # Comprobar cambios y validez
                base = task_base_commits.get(task_id)
                changed = [
                    p for p in gitops.changed_paths(job.workdir, base)
                    if not (p == ".hefesto" or p.startswith(".hefesto/"))
                ]
                has_pass_review = bool(outcome.review and outcome.review.get("verdict") == "pass")
                is_success = outcome.ok and (not changed or has_pass_review)

                if not is_success:
                    attempts[task_id] += 1
                    last_errors[task_id] = outcome.summary or "fallo en la tarea"
                    if attempts[task_id] >= max_attempts:
                        with project_lock(project):
                            roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!")
                            if job.workdir.exists() and not gitops.dirty_paths(job.workdir, include_control=True):
                                gitops.remove_worktree(project, job.workdir)
                        results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            detail=outcome.summary,
                            attempts=attempts[task_id],
                        )
                    else:
                        with project_lock(project):
                            roadmap.set_marker(roadmap.roadmap_path(project), task_id, " ")
                    continue

                # Éxito: integrar serialmente con project_lock
                attempts[task_id] += 1

                with project_lock(project):
                    # 1. Commit del trabajo en la rama de la tarea con solo sus files
                    commit_msg = f"hefesto({task_id}): {job.task.title} [verified]"
                    try:
                        gitops.checkpoint(project, job.workdir, cfg, job.task, commit_msg, include_control=False)
                    except (HefestoError, subprocess.CalledProcessError, Exception) as exc:
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!")
                        if job.workdir.exists() and not gitops.dirty_paths(job.workdir, include_control=True):
                            gitops.remove_worktree(project, job.workdir)
                        results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            detail=f"falló checkpoint de la tarea: {exc}",
                            attempts=attempts[task_id],
                        )
                        continue

                    # 2. Rebase sobre la punta actual de la rama de la fase
                    reb = gitops.git(job.workdir, "rebase", phase_branch, check=False)
                    if reb.returncode != 0:
                        gitops.git(job.workdir, "rebase", "--abort", check=False)
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!")
                        gitops.remove_worktree(project, job.workdir)
                        results[task_id] = TaskResult(
                            task_id=task_id,
                            status="conflict",
                            detail="conflicto de rebase con la rama de la fase",
                            attempts=attempts[task_id],
                        )
                        continue

                    # 3. Verify tras el rebase
                    v_timeout = int(cfg.get("limits", "verify_timeout_seconds", default=60))
                    v_res = evidence.run_verify(
                        project,
                        job.workdir,
                        task_id,
                        job.task.verify,
                        v_timeout,
                        label=f"p{attempts[task_id]}",
                    )
                    if v_res.exit_code != 0:
                        last_errors[task_id] = f"verify falló tras rebase: {v_res.summary}"
                        exhausted = attempts[task_id] >= max_attempts
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!" if exhausted else " ")
                        # El rebase terminó: conservar HEAD y el worktree permite corregir
                        # sobre la fase actual sin descartar los commits de intentos previos.
                        if exhausted:
                            if not gitops.dirty_paths(job.workdir, include_control=True):
                                gitops.remove_worktree(project, job.workdir)
                            results[task_id] = TaskResult(task_id, "failed", last_errors[task_id] or "",
                                                          attempts[task_id])
                        continue

                    with slots.condition:
                        conc.on_success(job.model_id)
                        slots.condition.notify_all()

                    # Pasos 4-7: fast-forward, marcador, evidencia, commit de evidencia y limpieza
                    ff_done = False
                    try:
                        # 4. Fast-forward en el worktree de la fase
                        gitops.git(phase_workdir, "merge", "--ff-only", tb, check=True)
                        ff_done = True

                        # 5. Marcador [x]
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "x")

                        # 6. Evidencia y commit de evidencia en la fase
                        phase_commit = gitops.out(phase_workdir, "rev-parse", "HEAD")
                        runs = acceptance.runs_for_task(project, task_run_ids.get(task_id))
                        tentry = trust.load(project)["verify"].get(task_id) or {}
                        trust_mode = "auto" if tentry.get("auto") else "human"
                        provider_list = sorted({r["model_id"] for r in runs if r.get("model_id")})
                        if not provider_list and outcome.model_id:
                            provider_list = [outcome.model_id]

                        record = {
                            "task_id": task_id,
                            "title": job.task.title,
                            "phase": job.task.phase,
                            "contract": list(job.task.contract),
                            "verify": {
                                "command": job.task.verify,
                                "sha256": trust.verify_hash(job.task.verify),
                                "exit_code": v_res.exit_code,
                                "duration_seconds": v_res.duration,
                                "output_summary": v_res.summary,
                                "log_path": v_res.log_path,
                                "trust": trust_mode,
                            },
                            "commit": phase_commit,
                            "base_commit": task_base_commits.get(task_id),
                            "branch": phase_branch,
                            "files": gitops.file_hashes(
                                phase_workdir,
                                phase_commit,
                                [p for p in gitops.commit_files(phase_workdir, phase_commit) if not p.startswith(".hefesto/")],
                            ),
                            "review": outcome.review,
                            "attempts": attempts[task_id],
                            "providers": provider_list,
                            "runs": [
                                {
                                    "run_id": r.get("run_id"),
                                    "role": r.get("role"),
                                    "model_id": r.get("model_id"),
                                    "success": r.get("success"),
                                }
                                for r in runs
                            ],
                        }
                        ev_path, head_ref = evidence.write(project, record)
                        st_cur = state.load(project)
                        st_cur["evidence_head"] = head_ref
                        state.save(project, st_cur)

                        gitops.checkpoint(
                            project,
                            phase_workdir,
                            cfg,
                            job.task,
                            f"hefesto({task_id}): evidence",
                            extra_paths=(ev_path.relative_to(project).as_posix(),),
                        )

                        # 7. Eliminar worktree y rama de la tarea
                        gitops.remove_worktree(project, job.workdir)
                        _drop_task_branch(project, phase_branch, tb)

                        results[task_id] = TaskResult(
                            task_id=task_id,
                            status="integrated",
                            detail="integrada con éxito",
                            attempts=attempts[task_id],
                        )
                    except (HefestoError, subprocess.CalledProcessError, Exception) as exc:
                        roadmap.set_marker(roadmap.roadmap_path(project), task_id, "!")
                        if job.workdir.exists() and not gitops.dirty_paths(job.workdir, include_control=True):
                            gitops.remove_worktree(project, job.workdir)
                        if not ff_done:
                            results[task_id] = TaskResult(
                                task_id=task_id,
                                status="conflict",
                                detail=f"fast-forward falló: {exc}",
                                attempts=attempts[task_id],
                            )
                        else:
                            log.event(project, "error", f"Error tras fast-forward de {task_id}: {exc}", task_id=task_id)
                            results[task_id] = TaskResult(
                                task_id=task_id,
                                status="failed",
                                detail=f"fallo tras fast-forward: {exc}",
                                attempts=attempts[task_id],
                            )
                        continue

    with project_lock(project):
        rm_final = roadmap.load_valid(project)
        for t in rm_final.tasks:
            if t.marker == "~":
                roadmap.set_marker(roadmap.roadmap_path(project), t.id, "!")

    conc.save()
    return list(results.values())
