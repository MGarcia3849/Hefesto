"""Fase 2: roadmap, lock, escritura atómica, transiciones, circuit breaker, trust y unblock."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

from hefesto import lock, roadmap, state, trust
from hefesto.util import EXIT_CONFLICT, HefestoError
from tests.helpers import ROOT, HefestoTestCase

ROADMAP = """# ROADMAP

## Fase 1 — Setup y tipos base
- [ ] TASK-001: Contrato de suma
  - verify: `true`
  - files: src/, tests/test_suma.py
  - role: core
  - size: S
  - contract: tests/test_suma.py
- [ ] TASK-002: Implementar suma
  - verify: `true`
  - files: src/
  - role: runner
  - depends_on: TASK-001
  - size: M
- [ ] TASK-003: Rehacer todo
  - verify: `true`
  - files: lib/
  - role: forge
  - size: L
"""


def write_roadmap(project: Path, text: str = ROADMAP) -> None:
    (project / ".hefesto" / "ROADMAP.md").write_text(text, encoding="utf-8")


class RoadmapTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()

    def errors_for(self, text: str) -> list[str]:
        write_roadmap(self.project, text)
        return roadmap.validate(roadmap.parse(roadmap.roadmap_path(self.project)), self.project)

    def test_valid_roadmap(self) -> None:
        self.assertEqual(self.errors_for(ROADMAP), [])
        rm = roadmap.parse(roadmap.roadmap_path(self.project))
        self.assertEqual([t.id for t in rm.tasks], ["TASK-001", "TASK-002", "TASK-003"])
        self.assertEqual(rm.get("TASK-001").phase_slug, "fase-1-setup-y-tipos-base")
        self.assertEqual(rm.contract_paths(rm.get("TASK-002")), ["tests/test_suma.py"])

    def test_rejects_missing_fields_duplicates_unknown_deps_cycles_and_paths(self) -> None:
        text = """## Fase 1
- [ ] TASK-001: a
  - files: src/
  - role: runner
- [ ] TASK-001: b
  - verify: `true`
  - files: ../fuera
  - role: runner
  - depends_on: TASK-009
- [ ] TASK-002: c
  - verify: `true`
  - files: /etc
  - role: wizard
  - depends_on: TASK-003
- [ ] TASK-003: d
  - verify: `true`
  - files: src/
  - role: runner
  - depends_on: TASK-002
- [ ] T-1: e
  - verify: `true`
  - files: src/
  - role: runner
"""
        errors = "\n".join(self.errors_for(text))
        for expected in ("falta `verify`", "ID duplicado", "`TASK-009`, que no existe", "fuera del proyecto",
                         "rol `wizard` inválido", "ciclo de dependencias", "ID inválido"):
            self.assertIn(expected, errors)

    def test_next_respects_dependencies_and_skips_large(self) -> None:
        write_roadmap(self.project)
        rm = roadmap.parse(roadmap.roadmap_path(self.project))
        task, _ = roadmap.next_task(rm)
        self.assertEqual(task.id if task else None, "TASK-001")
        roadmap.set_marker(rm.path, "TASK-001", "x")
        rm = roadmap.parse(rm.path)
        task, _ = roadmap.next_task(rm)
        self.assertEqual(task.id if task else None, "TASK-002")
        roadmap.set_marker(rm.path, "TASK-002", "x")
        task, reason = roadmap.next_task(roadmap.parse(rm.path))
        self.assertIsNone(task)
        self.assertIn("tamaño L", reason)

    def test_set_marker_preserves_crlf(self) -> None:
        path = roadmap.roadmap_path(self.project)
        path.write_bytes(ROADMAP.replace("\n", "\r\n").encode("utf-8"))
        roadmap.set_marker(path, "TASK-002", "~")
        raw = path.read_bytes().decode("utf-8")
        self.assertIn("- [~] TASK-002", raw)
        self.assertEqual(raw.count("\r\n"), ROADMAP.count("\n"))


class StartFailTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        write_roadmap(self.project)

    def test_start_rejects_pending_deps_large_and_second_task(self) -> None:
        code, _, err = self.cli("start", "TASK-002")
        self.assertEqual(code, 1)
        self.assertIn("depende de tareas sin completar", err)
        code, _, err = self.cli("start", "TASK-003")
        self.assertEqual(code, 2)
        self.assertIn("tamaño L", err)
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, err)
        self.assertIn("- [~] TASK-001", roadmap.roadmap_path(self.project).read_text(encoding="utf-8"))
        code, _, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 4)
        code, _, err = self.cli("start", "TASK-999")
        self.assertEqual(code, 1)

    def test_circuit_breaker_exactly_on_third_failure(self) -> None:
        write_roadmap(self.project, ROADMAP.replace("role: core", "role: runner"))
        self.assertEqual(self.cli("start", "TASK-001")[0], 0)
        code, out, _ = self.cli("fail", "TASK-001", "tests en rojo")
        self.assertEqual(code, 0)
        self.assertIn("runner", out)
        self.assertEqual(state.load(self.project)["attempts"], 1)
        code, out, _ = self.cli("fail", "TASK-001", "otra vez")
        self.assertEqual(code, 0)
        self.assertIn("forge", out)
        code, out, _ = self.cli("fail", "TASK-001", "y otra")
        self.assertEqual(code, 2)
        data = state.load(self.project)
        self.assertEqual(data["status"], "BLOCKED_NEED_ARCHITECT_REVIEW")
        self.assertEqual(data["attempts"], 3)
        self.assertIn("- [!] TASK-001", roadmap.roadmap_path(self.project).read_text(encoding="utf-8"))
        report = (self.project / ".hefesto" / "logs" / "circuit_breaker.log").read_text(encoding="utf-8")
        self.assertIn("intento 3", report)
        self.assertEqual(self.cli("fail", "TASK-001", "cuarto")[0], 4)
        self.assertEqual(self.cli("start", "TASK-002")[0], 2)

    def test_unblock_requires_tty_and_resets(self) -> None:
        self.cli("start", "TASK-001")
        for _ in range(3):
            self.cli("fail", "TASK-001", "x")
        code, _, err = self.cli("unblock", "TASK-001", "cambio de enfoque")
        self.assertEqual(code, 2)
        self.assertIn("terminal interactiva", err)
        code, out, err = self.cli("unblock", "TASK-001", "cambio de enfoque", tty_value=True)
        self.assertEqual(code, 0, err)
        data = state.load(self.project)
        self.assertEqual((data["status"], data["attempts"]), ("IDLE", 0))
        self.assertIn("cambio de enfoque", (self.project / ".hefesto" / "DECISIONS.md").read_text(encoding="utf-8"))

    def test_concurrent_starts_only_one_wins(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        cmd = [sys.executable, "-m", "hefesto", "-C", str(self.project), "start", "TASK-001"]
        procs = [subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        codes = sorted(p.wait(timeout=60) for p in procs)
        for p in procs:
            p.stdout.close()  # type: ignore[union-attr]
            p.stderr.close()  # type: ignore[union-attr]
        self.assertEqual(codes, [0, 4])

    def test_illegal_transitions(self) -> None:
        data = state.load(self.project)
        with self.assertRaises(HefestoError):
            state.transition(data, "BLOCKED_NEED_ARCHITECT_REVIEW")
        with self.assertRaises(HefestoError):
            state.transition(data, "NOPE")


class LockTests(HefestoTestCase):
    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def write_owner(self, path: Path, pid: int, host: str | None = None) -> None:
        path.mkdir(parents=True)
        (path / "owner").write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(),
                                                "created": "x", "token": "otro"}), encoding="utf-8")

    def test_orphan_lock_is_recovered(self) -> None:
        path = self.tmp / "l" / ".lock"
        self.write_owner(path, self.dead_pid())
        recovered: list[dict[str, object]] = []
        with lock.Lock(path, timeout=2, on_recover=recovered.append):
            self.assertTrue(path.exists())
        self.assertEqual(len(recovered), 1)
        self.assertFalse(path.exists())

    def test_live_lock_is_never_touched(self) -> None:
        path = self.tmp / "l" / ".lock"
        self.write_owner(path, os.getpid())
        with self.assertRaises(HefestoError) as ctx:
            lock.Lock(path, timeout=0.3).acquire()
        self.assertEqual(ctx.exception.code, EXIT_CONFLICT)
        self.assertTrue((path / "owner").exists())

    def test_lock_from_other_host_is_not_recovered(self) -> None:
        path = self.tmp / "l" / ".lock"
        self.write_owner(path, self.dead_pid(), host="otra-maquina")
        with self.assertRaises(HefestoError):
            lock.Lock(path, timeout=0.3).acquire()

    def test_pid_alive(self) -> None:
        self.assertTrue(lock.pid_alive(os.getpid()))
        self.assertFalse(lock.pid_alive(self.dead_pid()))


class AtomicWriteTests(HefestoTestCase):
    def test_failure_mid_write_leaves_valid_state(self) -> None:
        self.init_project()
        before = state.state_path(self.project).read_text(encoding="utf-8")
        data = state.load(self.project)
        data["attempts"] = 2
        with mock.patch("hefesto.util.os.replace", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                state.save(self.project, data)
        self.assertEqual(state.state_path(self.project).read_text(encoding="utf-8"), before)
        state.load(self.project)
        leftovers = [p.name for p in (self.project / ".hefesto").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_invalid_state_is_never_written(self) -> None:
        self.init_project()
        data = state.load(self.project)
        data["status"] = "IN_PROGRESS"
        with self.assertRaises(HefestoError):
            state.save(self.project, data)
        self.assertEqual(state.load(self.project)["status"], "IDLE")


class TrustTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        write_roadmap(self.project)

    def test_trust_requires_tty(self) -> None:
        code, _, err = self.cli("trust")
        self.assertEqual(code, 2)

    def test_trust_approves_and_detects_changes(self) -> None:
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, err)
        rm = roadmap.parse(roadmap.roadmap_path(self.project))
        self.assertEqual(trust.status(self.project, rm.get("TASK-001")), "trusted")
        write_roadmap(self.project, ROADMAP.replace("verify: `true`", "verify: `echo cambiado`", 1))
        rm = roadmap.parse(roadmap.roadmap_path(self.project))
        self.assertEqual(trust.status(self.project, rm.get("TASK-001")), "changed")
        self.assertEqual(trust.status(self.project, rm.get("TASK-002")), "trusted")

    def test_trust_declined_stores_nothing(self) -> None:
        self.cli("trust", tty_value=True, answers=["n"])
        self.assertFalse(trust.trust_path(self.project).exists())
