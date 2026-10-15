"""Contrato (TASK-904 → TASK-905): modo paralelo — scheduler, ciclo automático por tarea e integración serial.

Sección 10.1 de la especificación. Todo vive en `hefesto/scheduler.py`; la CLI añade `hefesto parallel` y
`hefesto route --dry-run` sin tarea.

Ramas y worktrees de las tareas paralelas:
- `scheduler.task_branch(phase_branch, task_id) -> str` = `f"{phase_branch}--{task_id}"`. (Desviación de la
  especificación, que dice `hefesto/<fase>/TASK-XXX`: git no admite a la vez `refs/heads/hefesto/fase-1` y
  `refs/heads/hefesto/fase-1/TASK-001`.)
- `scheduler.task_workdir(project, phase_branch, task_id) -> Path` =
  `gitops.worktree_root(project) / task_branch(...).replace("/", "__")`, fuera del proyecto. Se crea desde la punta
  ACTUAL de la rama de la fase en el momento de lanzar la tarea.

Selección (pura, determinista):
- `scheduler.files_overlap(a, b) -> bool`: True si alguna ruta de `a` es igual a una de `b` o está dentro de ella
  (o al revés). Normaliza `./`, `\\` y la barra final. `src` y `src2/x` no se solapan.
- `scheduler.select_batch(rm, running, capacity) -> list[Task]`, en orden del ROADMAP y como mucho `capacity`:
  - solo tareas `[ ]` de la PRIMERA fase que tenga tareas sin `[x]`;
  - con todas sus dependencias `[x]` (una dependencia en curso o pendiente = espera);
  - nunca de rol `core` (las escribe el orquestador);
  - con `files` disjuntos de los de las tareas en marcha (`running`, IDs, más las `[~]` del ROADMAP) y entre sí;
  - una tarea `parallel: false` o de tamaño `L` solo va sola: si hay algo en marcha o ya elegido, espera; si se
    elige, el lote es solo ella.

Concurrencia por modelo (adaptativa):
- `scheduler.Concurrency(maxima: dict[str, int], adaptive: bool = True)`:
  - `maximum(model_id)`: clave exacta, si no `"<proveedor>:*"`, si no 1.
  - `limit(model_id)`: límite actual (al principio, el máximo).
  - `on_rate_limit(model_id) -> int`: lo reduce a la mitad (división entera, mínimo 1) y reinicia la racha.
  - `on_success(model_id) -> int`: tras `scheduler.RECOVER_AFTER` (= 3) éxitos seguidos sube 1, hasta el máximo.
  - Con `adaptive=False`, `limit` es siempre el máximo.
  - `history`: lista de `(model_id, nuevo_límite)`, una entrada por cada cambio.
  - `save()` / `Concurrency.from_config(cfg)`: los límites actuales persisten en
    `util.state_home()/concurrency.json` (acotados al máximo vigente al cargar). `from_config` usa
    `limits.max_parallel_per_model` y `limits.adaptive_concurrency`.
- `scheduler.pick_entry(cfg, role, busy, conc) -> str | None`: la primera entrada de `cfg.role_chain(role)` cuyo
  proveedor está disponible (`providers.available`) y cuyo modelo tiene hueco (`busy[model_id] < conc.limit`).

Ciclo por tarea:
- El trabajo de una tarea lo hace un `worker(job) -> WorkerOutcome`:
  - `scheduler.Job`: `task`, `workdir`, `entry`, `model_id`, `role`, `attempt` (1, 2, 3…) y `last_error`
    (str o None).
  - `scheduler.WorkerOutcome(ok, error_type=None, summary="", review=None, model_id=None)`, donde `review` es
    `{"verdict", "run_id", "model_id"}`.
  - `run_parallel(..., worker=None)` usa el worker por defecto: Runner (o el rol del intento) con `entry`, verify en
    su worktree y Reviewer independiente; `scheduler.parse_verdict(summary)` → `"pass"` solo si una línea dice
    `VEREDICTO: pass` o `VERDICT: pass` (sin distinguir mayúsculas).
  - Los procesos hijo reciben `HEFESTO_TASK_ID` con la tarea.
- `error_type` `rate_limit` o `quota`:
  - halve de la concurrencia del modelo;
  - la tarea vuelve a la cola sin contar intento, y `pick_entry` decide de nuevo.
- Cualquier otro fallo es un intento:
  - excepción del worker, `ok=False`, verify en rojo o revisión que no sea `pass` cuando hay cambios;
  - el siguiente intento usa `workflow.role_for_attempt` y `last_error`;
  - al agotar `limits.max_attempts`, la tarea pasa a `[!]` (resultado `failed`) y el resto sigue.

Integración en la rama de la fase (serial, una tarea cada vez, con lock):
1. Commit del trabajo en la rama de la tarea, solo con rutas de su `files`:
   `hefesto(TASK-XXX): <título> [verified]`.
2. Rebase sobre la punta actual de la rama de la fase.
   - Si hay conflicto: `rebase --abort`, la tarea pasa a `[!]` (resultado `conflict`) y su rama se conserva.
   - Ninguna rebase queda a medias y la rama de la fase no cambia.
3. Vuelve a ejecutar su `verify`.
   - Debe estar confiado; los seguros se autoaprueban como en `done`.
   - Los no confiados no se seleccionan: resultado `skipped` con «trust» en el detalle.
4. Fast-forward de la rama de la fase. Después, `[x]`, evidencia encadenada y commit `hefesto(TASK-XXX): evidence`
   en la rama de la fase.
   - En la evidencia: `branch` = rama de la fase, `review` y `attempts`.
5. Elimina el worktree y la rama de la tarea.

`scheduler.run_parallel(project, max_parallel=None, worker=None, concurrency=None) -> list[TaskResult]`:
- `TaskResult`: `task_id`, `status` (`integrated|failed|conflict|skipped|cancelled`), `detail` y `attempts`.
- Límites:
  - nunca más de `max_parallel` (por defecto `limits.max_parallel_tasks`) tareas a la vez;
  - nunca más instancias de un modelo que su límite actual.
- Con una tarea serie en curso (`state` IN_PROGRESS), `HefestoError` con código 4 sin tocar nada.
- Antes de lanzar ninguna tarea prepara la rama y el worktree de la fase (`gitops.prepare_workdir`) y los guarda
  en `state.phases`.
- Al empezar, los `[~]` que dejó una ejecución paralela interrumpida vuelven a `[ ]`.
  - Sus worktrees limpios se eliminan.
- STOP (`loop.stop_path`):
  - no lanza nada nuevo;
  - con `stop_policy = finish_running`, las tareas en marcha terminan y se integran;
  - con `cancel_running`, nada que termine después de STOP se integra: vuelve a `[ ]` y su worktree limpio se
    elimina.
- Al terminar no quedan marcadores `[~]` ni worktrees de tarea limpios.
- `max_parallel = 1` produce exactamente la misma secuencia de commits que la ejecución en serie.

`maintenance.clean` trata como huérfanos los worktrees y ramas de tarea (`<rama de fase>--TASK-XXX`) que quedan
tras matar el proceso. Una rama de tarea contenida en la rama de su fase cuenta como integrada (se borra con `-d`).

CLI:
- `hefesto parallel [--max-parallel N]`: con `HEFESTO_ROLE` definido, código 2 y no lanza nada.
- `hefesto route --dry-run` sin tarea: muestra el lote que se lanzaría y la entrada de cada tarea, sin llamadas.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from hefesto import config, evidence, gitops, loop, roadmap, state, util
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, git

PHASE_BRANCH = "hefesto/fase-1-nucleo"
PASS = {"verdict": "pass", "run_id": "revision-falsa", "model_id": "gemini:default"}


def sched() -> Any:
    from hefesto import scheduler

    return scheduler


def task_block(task_id: str, files: str, *, verify: str | None = None, role: str = "runner", depends: str = "",
               size: str = "S", parallel: bool | None = None, marker: str = " ") -> str:
    first = files.split(",")[0].strip()
    lines = [f"- [{marker}] {task_id}: Tarea {task_id}",
             f"  - verify: `{verify or f'test -f {first}'}`",
             f"  - files: {files}",
             f"  - role: {role}",
             f"  - size: {size}"]
    if depends:
        lines.append(f"  - depends_on: {depends}")
    if parallel is not None:
        lines.append(f"  - parallel: {'true' if parallel else 'false'}")
    return "\n".join(lines)


def roadmap_text(*blocks: str, extra_phase: str = "") -> str:
    text = "# ROADMAP\n\n## Fase 1 — Núcleo\n" + "\n".join(blocks) + "\n"
    if extra_phase:
        text += "\n## Fase 2 — Extra\n" + extra_phase + "\n"
    return text


def simple(n: int) -> str:
    """`n` tareas independientes con ficheros disjuntos: TASK-001 → src/m1.py, …"""
    return roadmap_text(*(task_block(f"TASK-{i:03d}", f"src/m{i}.py") for i in range(1, n + 1)))


class Recorder:
    """Worker falso: escribe los `files` de la tarea, espera `delay` y mide la concurrencia real."""

    def __init__(self, delay: float = 0.4, behaviour: Callable[[Any, "Recorder"], Any] | None = None) -> None:
        self.delay = delay
        self.behaviour = behaviour
        self.lock = threading.Lock()
        self.active: Counter[str] = Counter()
        self.total = 0
        self.peak_total = 0
        self.peak_model: Counter[str] = Counter()
        self.jobs: list[Any] = []
        self.intervals: dict[str, list[tuple[float, float]]] = {}
        self.started: dict[str, threading.Event] = {}

    def event(self, task_id: str) -> threading.Event:
        with self.lock:
            return self.started.setdefault(task_id, threading.Event())

    def __call__(self, job: Any) -> Any:
        scheduler = sched()
        start = time.monotonic()
        with self.lock:
            self.jobs.append(job)
            self.active[job.model_id] += 1
            self.total += 1
            self.peak_total = max(self.peak_total, self.total)
            self.peak_model[job.model_id] = max(self.peak_model[job.model_id], self.active[job.model_id])
        self.event(job.task.id).set()
        try:
            if self.behaviour is not None:
                custom = self.behaviour(job, self)
                if custom is not None:
                    return custom
            for rel in job.task.files:
                if rel.endswith("/"):  # directorio: un fichero nuevo dentro
                    rel = f"{rel}{job.task.id.lower()}.py"
                target = Path(job.workdir) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# {job.task.id} intento {job.attempt}\n", encoding="utf-8")
            time.sleep(self.delay)
            return scheduler.WorkerOutcome(ok=True, summary="hecho", review=dict(PASS), model_id=job.model_id)
        finally:
            with self.lock:
                self.active[job.model_id] -= 1
                self.total -= 1
                self.intervals.setdefault(job.task.id, []).append((start, time.monotonic()))


class SelectionTests(unittest.TestCase):
    def test_task_branch_and_workdir_live_outside_the_project(self) -> None:
        scheduler = sched()
        self.assertEqual(scheduler.task_branch(PHASE_BRANCH, "TASK-007"), "hefesto/fase-1-nucleo--TASK-007")
        project = Path("proyecto-x").resolve()
        expected = gitops.worktree_root(project) / "hefesto__fase-1-nucleo--TASK-007"
        self.assertEqual(scheduler.task_workdir(project, PHASE_BRANCH, "TASK-007"), expected)

    def test_files_overlap(self) -> None:
        overlap = sched().files_overlap
        self.assertTrue(overlap(["src/"], ["src/a.py"]))
        self.assertTrue(overlap(["src/a.py"], ["./src/a.py"]))
        self.assertTrue(overlap(["docs\\guia.md"], ["docs/"]))
        self.assertTrue(overlap(["a.py", "lib/x"], ["lib/x/y.py"]))
        self.assertFalse(overlap(["src/a.py"], ["src/b.py"]))
        self.assertFalse(overlap(["src"], ["src2/x.py"]))
        self.assertFalse(overlap([], ["src/"]))

    def batch(self, text: str, running: list[str] | None = None, capacity: int = 6) -> list[str]:
        rm = roadmap.parse_text(text)
        return [t.id for t in sched().select_batch(rm, running or [], capacity)]

    def test_disjoint_tasks_fill_capacity_in_roadmap_order(self) -> None:
        self.assertEqual(self.batch(simple(4)), ["TASK-001", "TASK-002", "TASK-003", "TASK-004"])
        self.assertEqual(self.batch(simple(4), capacity=2), ["TASK-001", "TASK-002"])
        self.assertEqual(self.batch(simple(4), running=["TASK-001"], capacity=2), ["TASK-002", "TASK-003"])
        self.assertEqual(self.batch(simple(2), capacity=0), [])

    def test_overlapping_files_never_share_a_batch(self) -> None:
        text = roadmap_text(task_block("TASK-001", "src/"), task_block("TASK-002", "src/b.py"),
                            task_block("TASK-003", "docs/c.md"))
        self.assertEqual(self.batch(text), ["TASK-001", "TASK-003"])
        self.assertEqual(self.batch(text, running=["TASK-001"]), ["TASK-003"])
        in_progress = roadmap_text(task_block("TASK-001", "src/", marker="~"), task_block("TASK-002", "src/b.py"),
                                   task_block("TASK-003", "docs/c.md"))
        self.assertEqual(self.batch(in_progress), ["TASK-003"])

    def test_dependencies_must_be_done(self) -> None:
        text = roadmap_text(task_block("TASK-001", "src/a.py"),
                            task_block("TASK-002", "src/b.py", depends="TASK-001"))
        self.assertEqual(self.batch(text), ["TASK-001"])
        text = roadmap_text(task_block("TASK-001", "src/a.py", marker="~"),
                            task_block("TASK-002", "src/b.py", depends="TASK-001"))
        self.assertEqual(self.batch(text), [])
        text = roadmap_text(task_block("TASK-001", "src/a.py", marker="x"),
                            task_block("TASK-002", "src/b.py", depends="TASK-001"))
        self.assertEqual(self.batch(text), ["TASK-002"])

    def test_non_parallel_and_large_tasks_run_alone(self) -> None:
        text = roadmap_text(task_block("TASK-001", "src/a.py", parallel=False), task_block("TASK-002", "src/b.py"))
        self.assertEqual(self.batch(text), ["TASK-001"])
        text = roadmap_text(task_block("TASK-001", "src/a.py"), task_block("TASK-002", "src/b.py", parallel=False),
                            task_block("TASK-003", "src/c.py"))
        self.assertEqual(self.batch(text), ["TASK-001", "TASK-003"])
        self.assertEqual(self.batch(text, running=["TASK-001"]), ["TASK-003"])
        large = roadmap_text(task_block("TASK-001", "src/a.py", size="L"), task_block("TASK-002", "src/b.py"))
        self.assertEqual(self.batch(large), ["TASK-001"])
        self.assertEqual(self.batch(large, running=["TASK-009"]), ["TASK-002"])

    def test_only_first_open_phase_and_never_core(self) -> None:
        text = roadmap_text(task_block("TASK-001", "tests/test_a.py", role="core"),
                            task_block("TASK-002", "src/b.py"),
                            extra_phase=task_block("TASK-003", "src/c.py"))
        self.assertEqual(self.batch(text), ["TASK-002"])
        blocked = roadmap_text(task_block("TASK-001", "src/a.py", marker="!"),
                               extra_phase=task_block("TASK-002", "src/b.py"))
        self.assertEqual(self.batch(blocked), [])
        finished = roadmap_text(task_block("TASK-001", "src/a.py", marker="x"),
                                extra_phase=task_block("TASK-002", "src/b.py"))
        self.assertEqual(self.batch(finished), ["TASK-002"])

    def test_parse_verdict(self) -> None:
        parse = sched().parse_verdict
        self.assertEqual(parse("VEREDICTO: pass\nsin observaciones"), "pass")
        self.assertEqual(parse("Revisado.\nveredicto: PASS"), "pass")
        self.assertEqual(parse("VERDICT: pass"), "pass")
        self.assertEqual(parse("VEREDICTO: fail\nfalta un test"), "fail")
        self.assertEqual(parse("todo pass, creo"), "fail")
        self.assertEqual(parse(""), "fail")


class ConcurrencyTests(HefestoTestCase):
    def test_maximum_and_initial_limit(self) -> None:
        conc = sched().Concurrency({"claude:haiku": 3, "local:*": 2})
        self.assertEqual(conc.maximum("claude:haiku"), 3)
        self.assertEqual(conc.maximum("local:qwen"), 2)
        self.assertEqual(conc.maximum("codex:default"), 1)
        self.assertEqual(conc.limit("claude:haiku"), 3)

    def test_rate_limit_halves_and_successes_recover(self) -> None:
        scheduler = sched()
        conc = scheduler.Concurrency({"claude:haiku": 4})
        self.assertEqual(scheduler.RECOVER_AFTER, 3)
        self.assertEqual(conc.on_rate_limit("claude:haiku"), 2)
        self.assertEqual(conc.on_rate_limit("claude:haiku"), 1)
        self.assertEqual(conc.on_rate_limit("claude:haiku"), 1)
        conc.on_success("claude:haiku")
        conc.on_success("claude:haiku")
        self.assertEqual(conc.limit("claude:haiku"), 1)
        self.assertEqual(conc.on_success("claude:haiku"), 2)
        conc.on_success("claude:haiku")
        conc.on_rate_limit("claude:haiku")  # reinicia la racha
        self.assertEqual(conc.limit("claude:haiku"), 1)
        for _ in range(3 * 6):
            conc.on_success("claude:haiku")
        self.assertEqual(conc.limit("claude:haiku"), 4)
        self.assertEqual(conc.history[:2], [("claude:haiku", 2), ("claude:haiku", 1)])

    def test_without_adaptive_the_limit_is_fixed(self) -> None:
        conc = sched().Concurrency({"claude:haiku": 3}, adaptive=False)
        conc.on_rate_limit("claude:haiku")
        self.assertEqual(conc.limit("claude:haiku"), 3)

    def test_limits_persist_between_runs(self) -> None:
        scheduler = sched()
        self.init_project()
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("limits", "max_parallel_per_model"), {"claude:haiku": 3})
        cfg = config.load(self.project)
        conc = scheduler.Concurrency.from_config(cfg)
        self.assertEqual(conc.limit("claude:haiku"), 3)
        conc.on_rate_limit("claude:haiku")
        conc.save()
        self.assertTrue((util.state_home() / "concurrency.json").exists())
        self.assertEqual(scheduler.Concurrency.from_config(config.load(self.project)).limit("claude:haiku"), 1)
        config.set_setting(settings, ("limits", "adaptive_concurrency"), False)
        self.assertEqual(scheduler.Concurrency.from_config(config.load(self.project)).limit("claude:haiku"), 3)

    def test_pick_entry_moves_to_the_next_model_when_saturated(self) -> None:
        scheduler = sched()
        self.use_fakes("claude", "gemini")
        self.init_project(providers="gemini")
        config.set_setting(config.project_settings_path(self.project), ("roles", "runner", "use"),
                           ["claude:haiku", "gemini"])
        cfg = config.load(self.project)
        conc = scheduler.Concurrency({"claude:haiku": 3})
        self.assertEqual(scheduler.pick_entry(cfg, "runner", {}, conc), "claude:haiku")
        self.assertEqual(scheduler.pick_entry(cfg, "runner", {"claude:haiku": 2}, conc), "claude:haiku")
        self.assertEqual(scheduler.pick_entry(cfg, "runner", {"claude:haiku": 3}, conc), "gemini")
        self.assertIsNone(scheduler.pick_entry(cfg, "runner", {"claude:haiku": 3, "gemini:default": 1}, conc))
        from hefesto import quota

        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        quota.mark_cooldown("claude", until, "rate_limit", "429")
        self.assertEqual(scheduler.pick_entry(cfg, "runner", {}, conc), "gemini")


class ParallelBase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "gemini")
        self.scenario = self.tmp / "scenario.json"
        self.scenario.write_text(json.dumps({"*": {"mode": "ok", "summary": "VEREDICTO: pass"}}), encoding="utf-8")
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.tmp / "calls.jsonl")
        self.init_project(providers="gemini")
        self.settings = config.project_settings_path(self.project)
        self.configure({
            ("roles", "runner", "use"): ["claude:haiku", "gemini"],
            ("roles", "forge", "use"): ["claude:sonnet"],
            ("roles", "reviewer", "use"): ["gemini"],
            ("limits", "max_parallel_per_model"): {"claude:haiku": 3},
            ("limits", "max_parallel_tasks"): 4,
        })

    def configure(self, values: dict[tuple[str, ...], Any]) -> None:
        for keys, value in values.items():
            config.set_setting(self.settings, keys, value)

    def write_roadmap(self, text: str) -> None:
        (self.project / ".hefesto" / "ROADMAP.md").write_text(text, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def markers(self) -> dict[str, str]:
        return {t.id: t.marker for t in roadmap.parse(roadmap.roadmap_path(self.project)).tasks}

    def run_parallel(self, **kw: Any) -> dict[str, Any]:
        results = sched().run_parallel(self.project, **kw)
        return {r.task_id: r for r in results}

    def phase_file(self, rel: str) -> str:
        return git(self.project, "show", f"{PHASE_BRANCH}:{rel}")

    def phase_subjects(self, count: int) -> list[str]:
        return list(reversed(git(self.project, "log", "--format=%s", f"-{count}", PHASE_BRANCH).splitlines()))

    def task_worktrees(self) -> list[str]:
        listed = git(self.project, "worktree", "list", "--porcelain")
        return [line for line in listed.splitlines() if line.startswith("branch ") and "--TASK-" in line]

    def assert_clean_finish(self) -> None:
        self.assertNotIn("~", self.markers().values())
        self.assertEqual(self.task_worktrees(), [])
        self.assertEqual(evidence.verify_chain(self.project), [])
        self.assertEqual(git(self.project, "rev-list", "--merges", PHASE_BRANCH), "")


class ParallelRunTests(ParallelBase):
    def test_respects_global_and_per_model_limits(self) -> None:
        self.write_roadmap(simple(6))
        rec = Recorder(delay=0.6)
        results = self.run_parallel(worker=rec)
        self.assertEqual({r.status for r in results.values()}, {"integrated"})
        self.assertLessEqual(rec.peak_total, 4)
        self.assertEqual(rec.peak_model["claude:haiku"], 3, "tres instancias de haiku deben trabajar a la vez")
        self.assertLessEqual(rec.peak_model["gemini:default"], 1)
        self.assertEqual(rec.peak_total, 4, "el cuarto hueco va al siguiente modelo de la cadena")
        workdirs = {str(Path(j.workdir).resolve()) for j in rec.jobs}
        self.assertEqual(len(workdirs), 6)
        for job in rec.jobs:
            self.assertEqual(Path(job.workdir), sched().task_workdir(self.project, PHASE_BRANCH, job.task.id))
            self.assertNotIn(self.project.resolve(), [Path(job.workdir).resolve(), *Path(job.workdir).resolve().parents])
        for i in range(1, 7):
            self.assertIn(f"TASK-{i:03d}", self.phase_file(f"src/m{i}.py"))
        self.assertEqual(set(self.markers().values()), {"x"})
        self.assert_clean_finish()

    def test_overlapping_files_never_run_at_the_same_time(self) -> None:
        self.write_roadmap(roadmap_text(task_block("TASK-001", "src/", verify="test -d src"),
                                        task_block("TASK-002", "src/b.py"), task_block("TASK-003", "docs/c.md")))
        rec = Recorder(delay=0.4)
        results = self.run_parallel(worker=rec)
        self.assertEqual({r.status for r in results.values()}, {"integrated"})
        (s1, e1), = rec.intervals["TASK-001"]
        (s2, e2), = rec.intervals["TASK-002"]
        self.assertTrue(e1 <= s2 or e2 <= s1, "TASK-001 y TASK-002 comparten ficheros")
        self.assert_clean_finish()

    def test_dependency_starts_from_the_integrated_phase_tip(self) -> None:
        self.write_roadmap(roadmap_text(task_block("TASK-001", "src/a.py"),
                                        task_block("TASK-002", "src/b.py", depends="TASK-001")))
        seen: dict[str, bool] = {}

        def behaviour(job: Any, _rec: Recorder) -> None:
            if job.task.id == "TASK-002":
                seen["a"] = (Path(job.workdir) / "src" / "a.py").exists()

        rec = Recorder(delay=0.1, behaviour=behaviour)
        results = self.run_parallel(worker=rec)
        self.assertEqual(results["TASK-002"].status, "integrated")
        self.assertTrue(seen.get("a"), "TASK-002 debe partir de la fase con TASK-001 ya integrada")
        (_, end1), = rec.intervals["TASK-001"]
        (start2, _), = rec.intervals["TASK-002"]
        self.assertLessEqual(end1, start2)

    def test_rate_limit_halves_the_model_and_requeues_without_counting(self) -> None:
        scheduler = sched()
        self.write_roadmap(simple(3))
        hit: list[str] = []

        def behaviour(job: Any, _rec: Recorder) -> Any:
            if job.task.id == "TASK-001" and not hit:
                hit.append(job.model_id)
                return scheduler.WorkerOutcome(ok=False, error_type="rate_limit", summary="429",
                                               model_id=job.model_id)
            return None

        conc = scheduler.Concurrency({"claude:haiku": 3})
        rec = Recorder(delay=0.2, behaviour=behaviour)
        results = self.run_parallel(worker=rec, concurrency=conc)
        self.assertEqual(hit, ["claude:haiku"])
        self.assertIn(("claude:haiku", 1), conc.history)
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-001"].attempts, 1)
        record = evidence.latest_evidence(self.project, "TASK-001")
        assert record is not None
        self.assertEqual(record[1]["attempts"], 1)
        self.assert_clean_finish()

    def test_failures_escalate_and_block_only_that_task(self) -> None:
        scheduler = sched()
        self.write_roadmap(simple(3))

        def behaviour(job: Any, _rec: Recorder) -> Any:
            if job.task.id == "TASK-002":
                return scheduler.WorkerOutcome(ok=False, summary=f"fallo {job.attempt}", model_id=job.model_id)
            return None

        rec = Recorder(delay=0.1, behaviour=behaviour)
        results = self.run_parallel(worker=rec)
        jobs = [j for j in rec.jobs if j.task.id == "TASK-002"]
        self.assertEqual([j.role for j in jobs], ["runner", "runner", "forge"])
        self.assertEqual([j.attempt for j in jobs], [1, 2, 3])
        self.assertIsNone(jobs[0].last_error)
        self.assertIn("fallo 1", jobs[1].last_error)
        self.assertEqual(jobs[2].entry, "claude:sonnet")
        self.assertEqual(results["TASK-002"].status, "failed")
        self.assertEqual(self.markers(), {"TASK-001": "x", "TASK-002": "!", "TASK-003": "x"})

    def test_exceptions_and_missing_review_are_failed_attempts(self) -> None:
        scheduler = sched()
        self.write_roadmap(simple(2))

        def behaviour(job: Any, _rec: Recorder) -> Any:
            if job.task.id == "TASK-001":
                raise RuntimeError("el agente se cayó")
            (Path(job.workdir) / "src").mkdir(exist_ok=True)
            (Path(job.workdir) / "src" / "m2.py").write_text("x = 2\n", encoding="utf-8")
            return scheduler.WorkerOutcome(ok=True, summary="sin revisión", review=None, model_id=job.model_id)

        rec = Recorder(delay=0.0, behaviour=behaviour)
        results = self.run_parallel(worker=rec)
        self.assertEqual(results["TASK-001"].status, "failed")
        self.assertEqual(results["TASK-002"].status, "failed")
        self.assertEqual(len([j for j in rec.jobs if j.task.id == "TASK-001"]), 3)
        self.assertIn("el agente se cayó", [j for j in rec.jobs if j.task.id == "TASK-001"][1].last_error)
        self.assertEqual(self.markers(), {"TASK-001": "!", "TASK-002": "!"})
        self.assertNotIn("m2.py", git(self.project, "ls-tree", "-r", "--name-only", PHASE_BRANCH))

    def test_conflict_after_rebase_blocks_only_that_task(self) -> None:
        self.write_roadmap(simple(3))

        def behaviour(job: Any, _rec: Recorder) -> None:
            if job.task.id == "TASK-002":
                # Alguien integra en la fase un src/m2.py distinto mientras TASK-002 trabaja.
                phase_dir = Path(state.load(self.project)["phases"]["fase-1-nucleo"]["workdir"])
                (phase_dir / "src").mkdir(exist_ok=True)
                (phase_dir / "src" / "m2.py").write_text("ajeno = True\n", encoding="utf-8")
                git(phase_dir, "add", "src/m2.py")
                git(phase_dir, "commit", "-q", "-m", "cambio ajeno en la fase")

        rec = Recorder(delay=0.2, behaviour=behaviour)
        results = self.run_parallel(worker=rec)
        self.assertEqual(results["TASK-002"].status, "conflict")
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-003"].status, "integrated")
        self.assertEqual(self.markers()["TASK-002"], "!")
        self.assertEqual(self.phase_file("src/m2.py"), "ajeno = True")
        self.assertTrue(gitops.branch_exists(self.project, sched().task_branch(PHASE_BRANCH, "TASK-002")))
        phase_dir = Path(state.load(self.project)["phases"]["fase-1-nucleo"]["workdir"])
        self.assertEqual(gitops.dirty_paths(phase_dir, include_control=False), [])
        for line in git(self.project, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                wt = Path(line[len("worktree "):])
                for marker in ("rebase-merge", "rebase-apply"):
                    self.assertFalse((wt / git(wt, "rev-parse", "--git-path", marker)).exists(), f"{wt}: {marker}")
        self.assertEqual(evidence.verify_chain(self.project), [])

    def test_max_parallel_one_is_identical_to_serial(self) -> None:
        self.configure({("limits", "max_parallel_tasks"): 1})
        self.write_roadmap(simple(3))
        rec = Recorder(delay=0.1)
        results = self.run_parallel(worker=rec)
        self.assertEqual(list(results), ["TASK-001", "TASK-002", "TASK-003"])
        self.assertEqual(rec.peak_total, 1)
        expected: list[str] = []
        for i in range(1, 4):
            expected += [f"hefesto(TASK-{i:03d}): Tarea TASK-{i:03d} [verified]", f"hefesto(TASK-{i:03d}): evidence"]
        self.assertEqual(self.phase_subjects(6), expected)
        for i in range(1, 4):
            path, record = evidence.latest_evidence(self.project, f"TASK-{i:03d}")  # type: ignore[misc]
            self.assertEqual(path.name, f"TASK-{i:03d}.json")
            self.assertEqual(record["branch"], PHASE_BRANCH)
            self.assertEqual(record["review"]["verdict"], "pass")
        self.assert_clean_finish()

    def test_stop_with_finish_running_integrates_active_and_launches_nothing_new(self) -> None:
        self.configure({("limits", "max_parallel_tasks"): 2, ("limits", "stop_policy"): "finish_running"})
        self.write_roadmap(simple(4))

        def behaviour(job: Any, rec: Recorder) -> None:
            if job.task.id == "TASK-001":
                rec.event("TASK-002").wait(10)
                loop.request_stop(self.project)

        rec = Recorder(delay=0.3, behaviour=behaviour)
        self.run_parallel(worker=rec)
        self.assertEqual(sorted({j.task.id for j in rec.jobs}), ["TASK-001", "TASK-002"])
        self.assertEqual(self.markers(), {"TASK-001": "x", "TASK-002": "x", "TASK-003": " ", "TASK-004": " "})
        self.assert_clean_finish()

    def test_stop_with_cancel_running_integrates_nothing_that_finishes_later(self) -> None:
        self.configure({("limits", "max_parallel_tasks"): 2, ("limits", "stop_policy"): "cancel_running"})
        self.write_roadmap(simple(3))
        scheduler = sched()

        def behaviour(job: Any, rec: Recorder) -> Any:
            if job.task.id == "TASK-001":
                rec.event("TASK-002").wait(10)
                loop.request_stop(self.project)
            else:
                deadline = time.monotonic() + 10
                while not loop.stop_path(self.project).exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            return scheduler.WorkerOutcome(ok=True, summary="sin cambios", review=dict(PASS), model_id=job.model_id)

        rec = Recorder(delay=0.0, behaviour=behaviour)
        results = self.run_parallel(worker=rec)
        self.assertEqual(sorted({j.task.id for j in rec.jobs}), ["TASK-001", "TASK-002"])
        self.assertEqual({results[t].status for t in ("TASK-001", "TASK-002")}, {"cancelled"})
        self.assertEqual(set(self.markers().values()), {" "})
        self.assert_clean_finish()

    def test_refuses_while_a_serial_task_is_in_progress(self) -> None:
        self.write_roadmap(simple(2))
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        rec = Recorder()
        with self.assertRaises(HefestoError) as ctx:
            sched().run_parallel(self.project, worker=rec)
        self.assertEqual(ctx.exception.code, 4)
        self.assertEqual(rec.jobs, [])

    def test_untrusted_verify_is_skipped(self) -> None:
        self.write_roadmap(roadmap_text(task_block("TASK-001", "src/a.py"),
                                        task_block("TASK-002", "src/b.py", verify="make check")))
        rec = Recorder(delay=0.1)
        results = self.run_parallel(worker=rec)
        self.assertEqual(results["TASK-001"].status, "integrated")
        self.assertEqual(results["TASK-002"].status, "skipped")
        self.assertIn("trust", results["TASK-002"].detail)
        self.assertNotIn("TASK-002", [j.task.id for j in rec.jobs])
        self.assertEqual(self.markers()["TASK-002"], " ")

    def test_clean_removes_task_worktrees_left_by_a_killed_run(self) -> None:
        from hefesto import maintenance

        scheduler = sched()
        self.write_roadmap(simple(2))
        self.run_parallel(worker=Recorder(delay=0.0), max_parallel=1)  # crea la rama de la fase y su worktree
        # Restos de un proceso matado: worktree y rama de una tarea, sin cambios y contenidos en la fase.
        tip = git(self.project, "rev-parse", PHASE_BRANCH)
        branch = scheduler.task_branch(PHASE_BRANCH, "TASK-777")
        orphan = scheduler.task_workdir(self.project, PHASE_BRANCH, "TASK-777")
        git(self.project, "worktree", "add", "-q", "-b", branch, str(orphan), tip)
        report = maintenance.clean(self.project)
        self.assertIn("TASK-777", report)
        self.assertFalse(orphan.exists())
        self.assertFalse(gitops.branch_exists(self.project, branch))

    def test_stale_in_progress_markers_from_a_killed_run_are_reset(self) -> None:
        scheduler = sched()
        self.write_roadmap(simple(1))
        self.run_parallel(worker=Recorder(delay=0.0))  # deja preparada la fase con TASK-001 integrada
        # Un proceso matado dejó TASK-002 en `[~]` con su worktree limpio.
        self.write_roadmap(roadmap_text(task_block("TASK-001", "src/m1.py", marker="x"),
                                        task_block("TASK-002", "src/m2.py", marker="~")))
        tip = git(self.project, "rev-parse", PHASE_BRANCH)
        branch = scheduler.task_branch(PHASE_BRANCH, "TASK-002")
        leftover = scheduler.task_workdir(self.project, PHASE_BRANCH, "TASK-002")
        git(self.project, "worktree", "add", "-q", "-b", branch, str(leftover), tip)
        results = self.run_parallel(worker=Recorder(delay=0.0))
        self.assertEqual(results["TASK-002"].status, "integrated")
        self.assert_clean_finish()


class EndToEndTests(ParallelBase):
    """Worker por defecto con CLIs falsas: runner claude:haiku, reviewer gemini."""

    def scenario_for(self, tasks: dict[str, str], delay: float, reviewer: dict[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"*": {"mode": "ok", "summary": "VEREDICTO: pass"},
                                "gemini@reviewer": reviewer or {"mode": "ok", "summary": "VEREDICTO: pass"}}
        for task_id, rel in tasks.items():
            data[f"claude#{task_id}"] = {"mode": "ok", "summary": f"creado {rel}", "delay": delay,
                                         "write": {rel: f"# {task_id}\n"}}
        self.scenario.write_text(json.dumps(data), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        path = self.tmp / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_three_fake_haiku_write_at_once_in_their_own_worktrees(self) -> None:
        scheduler = sched()
        self.write_roadmap(simple(3))
        self.scenario_for({f"TASK-00{i}": f"src/m{i}.py" for i in range(1, 4)}, delay=2.0)
        results = self.run_parallel()
        self.assertEqual({r.status for r in results.values()}, {"integrated"}, results)
        runner_calls = [c for c in self.calls() if c["name"] == "claude"]
        self.assertEqual(len(runner_calls), 3)
        cwds = {str(Path(c["cwd"]).resolve()).lower() for c in runner_calls}
        expected = {str(scheduler.task_workdir(self.project, PHASE_BRANCH, f"TASK-00{i}").resolve()).lower()
                    for i in range(1, 4)}
        self.assertEqual(cwds, expected)
        starts = sorted(c["t"] for c in runner_calls)
        self.assertLess(starts[-1] - starts[0], 2.0, "las tres instancias deben solaparse en el tiempo")
        reviews = [c for c in self.calls() if c["name"] == "gemini"]
        self.assertEqual(len(reviews), 3)
        for i in range(1, 4):
            self.assertEqual(self.phase_file(f"src/m{i}.py"), f"# TASK-00{i}")
            _, record = evidence.latest_evidence(self.project, f"TASK-00{i}")  # type: ignore[misc]
            self.assertEqual(record["review"]["verdict"], "pass")
            self.assertIn("claude:haiku", record["providers"])
        phase_dir = Path(state.load(self.project)["phases"]["fase-1-nucleo"]["workdir"])
        if phase_dir.resolve() != self.project.resolve():  # con aislamiento `branch` la fase ES la copia principal
            self.assertFalse((self.project / "src").exists(), "nada se escribe en la copia principal")
        self.assert_clean_finish()

    def test_negative_review_is_never_integrated(self) -> None:
        self.write_roadmap(simple(1))
        self.scenario_for({"TASK-001": "src/m1.py"}, delay=0.0,
                          reviewer={"mode": "ok", "summary": "VEREDICTO: fail\nfalta un test"})
        results = self.run_parallel()
        self.assertEqual(results["TASK-001"].status, "failed")
        self.assertEqual(self.markers()["TASK-001"], "!")
        self.assertNotIn("src/m1.py", git(self.project, "ls-tree", "-r", "--name-only", PHASE_BRANCH))


class CliTests(ParallelBase):
    def test_route_dry_run_shows_the_batch_without_calls(self) -> None:
        self.write_roadmap(simple(5))
        code, out, err = self.cli("route", "--dry-run")
        self.assertEqual(code, 0, out + err)
        for i in range(1, 5):
            self.assertIn(f"TASK-00{i}", out)
        self.assertNotIn("TASK-005", out)  # max_parallel_tasks = 4
        self.assertIn("claude:haiku", out)
        self.assertIn("gemini", out)
        self.assertFalse((self.tmp / "calls.jsonl").exists())

    def test_parallel_is_refused_inside_an_agent(self) -> None:
        self.write_roadmap(simple(2))
        os.environ["HEFESTO_ROLE"] = "runner"
        code, out, err = self.cli("parallel")
        self.assertEqual(code, 2, out + err)
        self.assertFalse((self.tmp / "calls.jsonl").exists())
        self.assertEqual(set(self.markers().values()), {" "})


if __name__ == "__main__":
    unittest.main()
