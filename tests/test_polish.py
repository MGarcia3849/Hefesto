"""Tests para TASK-1003: pulido anotado durante la construcción.

1. Guarda de contratos en `review` exime al rol core.
2. `fail` tras revisión negativa no vuelve a contar el intento sin nueva delegación de autor.
3. `jobs` en formato tabla muestra texto legible y no JSON crudo.
4. `doctor` informa de CPU y RAM y avisa si max_parallel_tasks supera lo recomendado.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from hefesto import doctor, log, state, util
from tests.helpers import HefestoTestCase, git

ROADMAP_CONTRACT = """# ROADMAP

## Fase 1 — Contrato
- [ ] TASK-001: Contrato base
  - verify: `test -f tests/test_calc.py`
  - files: tests/
  - role: core
  - size: S
  - contract: tests/test_calc.py
- [ ] TASK-002: Enmienda contrato
  - verify: `test -f tests/test_calc.py`
  - files: tests/
  - role: core
  - depends_on: TASK-001
  - size: S
- [ ] TASK-003: Implementacion runner
  - verify: `test -f src/calc.py`
  - files: src/, tests/
  - role: runner
  - depends_on: TASK-001
  - size: S
"""

ROADMAP_SIMPLE = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Implementar
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""


class ContractReviewGuardTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_CONTRACT, encoding="utf-8")
        git(self.project, "add", ".hefesto/ROADMAP.md")
        git(self.project, "commit", "-q", "-m", "roadmap")
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)

        # Completar TASK-001 (que establece el contrato tests/test_calc.py)
        code, out, err = self.cli("start", "TASK-001")
        self.assertEqual(code, 0, out + err)
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "tests").mkdir(parents=True, exist_ok=True)
        (workdir / "tests" / "test_calc.py").write_text("def test_calc(): pass\n", encoding="utf-8")
        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("done", "TASK-001")
        self.assertEqual(code, 0, out + err)

    def test_core_role_modifying_contract_receives_pass(self) -> None:
        # TASK-002 es rol core y depende de TASK-001 (cuyo contrato es tests/test_calc.py)
        code, out, err = self.cli("start", "TASK-002")
        self.assertEqual(code, 0, out + err)
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "tests" / "test_calc.py").write_text("def test_calc(): assert True\n", encoding="utf-8")

        code, out, err = self.cli("review", "TASK-002", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pass registrada", out)
        st = state.load(self.project)
        self.assertEqual(st["review"]["verdict"], "pass")
        self.assertNotIn("Revisión negativa automática", st["review"].get("notes", ""))

    def test_runner_role_modifying_contract_receives_fail(self) -> None:
        # TASK-003 es rol runner y depende de TASK-001 (cuyo contrato es tests/test_calc.py)
        code, out, err = self.cli("start", "TASK-003")
        self.assertEqual(code, 0, out + err)
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (workdir / "tests" / "test_calc.py").write_text("def test_calc(): assert True\n", encoding="utf-8")

        code, out, err = self.cli("review", "TASK-003", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertNotEqual(code, 0)
        st = state.load(self.project)
        self.assertEqual(st["attempts"], 1)
        reviews = (self.project / ".hefesto" / "logs" / "reviews.jsonl").read_text(encoding="utf-8")
        self.assertIn("modificó tests de contrato", reviews)

    def test_runner_started_task_with_roadmap_changed_to_core_receives_fail(self) -> None:
        # TASK-003 se inicia con rol runner
        code, out, err = self.cli("start", "TASK-003")
        self.assertEqual(code, 0, out + err)
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (workdir / "tests" / "test_calc.py").write_text("def test_calc(): assert True\n", encoding="utf-8")

        # Se cambia el ROADMAP a mitad de tarea para poner role: core en TASK-003
        rm_path = self.project / ".hefesto" / "ROADMAP.md"
        rm_text = rm_path.read_text(encoding="utf-8").replace("role: runner", "role: core")
        rm_path.write_text(rm_text, encoding="utf-8")
        if (workdir / ".hefesto" / "ROADMAP.md").is_file():
            (workdir / ".hefesto" / "ROADMAP.md").write_text(rm_text, encoding="utf-8")

        code, out, err = self.cli("review", "TASK-003", "--verdict", "pass", "--author", "gemini",
                                  "--reviewer", "claude:sonnet")
        self.assertNotEqual(code, 0)
        st = state.load(self.project)
        self.assertEqual(st["attempts"], 1)
        reviews = (self.project / ".hefesto" / "logs" / "reviews.jsonl").read_text(encoding="utf-8")
        self.assertIn("modificó tests de contrato", reviews)


class WorkflowFailDeduplicationTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP_SIMPLE, encoding="utf-8")
        git(self.project, "add", ".hefesto/ROADMAP.md")
        git(self.project, "commit", "-q", "-m", "roadmap")
        code, out, err = self.cli("trust", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)

    def test_review_fail_then_fail_counts_one_attempt(self) -> None:
        self.cli("start", "TASK-001")
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        # Intento 1 registrado vía revisión negativa
        code, out, err = self.cli("review", "TASK-001", "--verdict", "fail", "--notes", "fallo de estilo",
                                  "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertNotEqual(code, 0)
        self.assertEqual(state.load(self.project)["attempts"], 1)

        # Llamar a hefesto fail sin nuevas delegaciones de autor no debe sumar otro intento
        code, out, err = self.cli("fail", "TASK-001", "fallo manual")
        self.assertEqual(code, 0, out + err)
        self.assertIn("el intento ya se contó con la revisión negativa", out.lower())
        self.assertEqual(state.load(self.project)["attempts"], 1)

    def test_review_fail_then_runner_run_then_fail_counts_additional_attempt(self) -> None:
        self.cli("start", "TASK-001")
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        # Intento 1 vía revisión negativa
        self.cli("review", "TASK-001", "--verdict", "fail", "--notes", "fallo",
                 "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertEqual(state.load(self.project)["attempts"], 1)

        # Simular delegación con éxito del runner (autor)
        task_run_id = state.load(self.project)["run_id"]
        log.record(self.project, "runs", {
            "run_id": "r1",
            "task_id": "TASK-001",
            "task_run_id": task_run_id,
            "role": "runner",
            "provider": "gemini",
            "model_id": "gemini:default",
            "success": True,
        })

        # Ahora fail sí debe sumar el segundo intento (+1 adicional)
        code, out, err = self.cli("fail", "TASK-001", "segundo fallo tras implementar")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(state.load(self.project)["attempts"], 2)
        self.assertIn("Intento registrado", out)

    def test_review_fail_with_prior_author_run_deduplicates_until_new_author_run(self) -> None:
        self.cli("start", "TASK-001")
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        task_run_id = state.load(self.project)["run_id"]
        log.record(self.project, "runs", {
            "run_id": "r0",
            "task_id": "TASK-001",
            "task_run_id": task_run_id,
            "role": "runner",
            "provider": "gemini",
            "model_id": "gemini:default",
            "success": True,
        })

        # Intento 1 vía revisión negativa
        self.cli("review", "TASK-001", "--verdict", "fail", "--notes", "fallo",
                 "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertEqual(state.load(self.project)["attempts"], 1)

        # fail sin nueva delegación -> no incrementa
        code, out, _ = self.cli("fail", "TASK-001", "reintento sin cambios")
        self.assertEqual(code, 0)
        self.assertIn("el intento ya se contó con la revisión negativa", out.lower())
        self.assertEqual(state.load(self.project)["attempts"], 1)

        # Nueva delegación del autor con éxito
        log.record(self.project, "runs", {
            "run_id": "r1",
            "task_id": "TASK-001",
            "task_run_id": task_run_id,
            "role": "runner",
            "provider": "gemini",
            "model_id": "gemini:default",
            "success": True,
        })

        # fail tras nueva delegación -> incrementa
        code, out, _ = self.cli("fail", "TASK-001", "fallo del nuevo intento")
        self.assertEqual(code, 0)
        self.assertEqual(state.load(self.project)["attempts"], 2)

    def test_fail_then_fail_counts_two_attempts(self) -> None:
        self.cli("start", "TASK-001")
        code, out, err = self.cli("fail", "TASK-001", "primer fallo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(state.load(self.project)["attempts"], 1)

        code, out, err = self.cli("fail", "TASK-001", "segundo fallo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(state.load(self.project)["attempts"], 2)

    def test_review_fail_then_failing_runner_run_then_fail_counts_additional_attempt(self) -> None:
        self.cli("start", "TASK-001")
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        # Intento 1 vía revisión negativa
        self.cli("review", "TASK-001", "--verdict", "fail", "--notes", "fallo",
                 "--author", "gemini", "--reviewer", "claude:sonnet")
        self.assertEqual(state.load(self.project)["attempts"], 1)

        # Simular delegación del runner que falla (success=False)
        task_run_id = state.load(self.project)["run_id"]
        log.record(self.project, "runs", {
            "run_id": "r1",
            "task_id": "TASK-001",
            "task_run_id": task_run_id,
            "role": "runner",
            "provider": "gemini",
            "model_id": "gemini:default",
            "success": False,
        })

        # Ahora fail sí debe sumar el segundo intento (+1 adicional)
        code, out, err = self.cli("fail", "TASK-001", "segundo fallo tras runner fallido")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(state.load(self.project)["attempts"], 2)
        self.assertIn("Intento registrado", out)

    def test_three_cycles_reach_circuit_breaker(self) -> None:
        # Configurar max_attempts = 6 para 3 ciclos completos de review fail -> runner fail -> fail
        (self.project / ".hefesto" / "hefesto.toml").write_text("[limits]\nmax_attempts = 6\n", encoding="utf-8")
        self.cli("start", "TASK-001")
        workdir = Path(state.load(self.project).get("workdir") or self.project)
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        task_run_id = state.load(self.project)["run_id"]
        for i in range(1, 4):
            # Paso 1: revisión negativa
            self.cli("review", "TASK-001", "--verdict", "fail", "--notes", f"fallo {i}",
                     "--author", "gemini", "--reviewer", "claude:sonnet")
            # Paso 2: delegación runner que falla
            log.record(self.project, "runs", {
                "run_id": f"r{i}",
                "task_id": "TASK-001",
                "task_run_id": task_run_id,
                "role": "runner",
                "provider": "gemini",
                "model_id": "gemini:default",
                "success": False,
            })
            # Paso 3: fail
            code, out, _ = self.cli("fail", "TASK-001", f"fallo intento {i}")
            if i < 3:
                self.assertEqual(code, 0)
                self.assertEqual(state.load(self.project)["attempts"], i * 2)
            else:
                self.assertNotEqual(code, 0)
                self.assertIn("circuit breaker", out.lower())

        st = state.load(self.project)
        self.assertEqual(st["status"], "BLOCKED_NEED_ARCHITECT_REVIEW")
        self.assertEqual(st["attempts"], 6)


class JobsFormattingTests(HefestoTestCase):
    def test_jobs_table_shows_summarized_text_not_raw_json(self) -> None:
        self.init_project()
        active_dir = self.project / ".hefesto" / "logs" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.project / ".hefesto" / "logs" / "runs" / "test_run.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Escribir línea stream-json en el log del agente activo
        stream_json_line = (
            '{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", '
            '"text": "editando fichero principal de la tarea"}}\n'
        )
        log_file.write_text(stream_json_line, encoding="utf-8")

        active_record = {
            "run_id": "test_run",
            "kind": "worker",
            "pid": os.getpid(),
            "role": "runner",
            "provider": "gemini",
            "model_id": "gemini:default",
            "task_id": "TASK-001",
            "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/test_run.log",
        }
        (active_dir / "test_run.json").write_text(json.dumps(active_record), encoding="utf-8")

        code, out, err = self.cli("jobs")
        self.assertEqual(code, 0, out + err)
        self.assertIn("editando fichero principal de la tarea", out)
        self.assertNotIn('{"type"', out)

    def test_jobs_json_flag_works(self) -> None:
        self.init_project()
        active_dir = self.project / ".hefesto" / "logs" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.project / ".hefesto" / "logs" / "runs" / "json_run.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("ejecutando tarea\n", encoding="utf-8")

        active_record = {
            "run_id": "json_run",
            "kind": "worker",
            "pid": os.getpid(),
            "role": "runner",
            "provider": "codex",
            "model_id": "codex:default",
            "task_id": "TASK-002",
            "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/json_run.log",
        }
        (active_dir / "json_run.json").write_text(json.dumps(active_record), encoding="utf-8")

        code, out, err = self.cli("jobs", "--json")
        self.assertEqual(code, 0, out + err)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["run_id"], "json_run")

    def test_jobs_table_handles_input_json_delta_without_raw_json(self) -> None:
        self.init_project()
        active_dir = self.project / ".hefesto" / "logs" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.project / ".hefesto" / "logs" / "runs" / "tool_delta_run.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        stream_json_line = (
            '{"type": "content_block_delta", "index": 1, "name": "edit_file", '
            '"delta": {"type": "input_json_delta", "partial_json": "{\\"target\\": \\"src/main.py\\"}"}}\n'
        )
        log_file.write_text(stream_json_line, encoding="utf-8")

        active_record = {
            "run_id": "tool_delta_run",
            "kind": "worker",
            "pid": os.getpid(),
            "role": "runner",
            "provider": "claude",
            "model_id": "claude:sonnet",
            "task_id": "TASK-001",
            "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/tool_delta_run.log",
        }
        (active_dir / "tool_delta_run.json").write_text(json.dumps(active_record), encoding="utf-8")

        code, out, err = self.cli("jobs")
        self.assertEqual(code, 0, out + err)
        self.assertIn("(llamada a herramienta: edit_file)", out)
        self.assertNotIn("partial_json", out)
        self.assertNotIn('{"target"', out)

    def test_jobs_table_handles_input_json_delta_unnamed_partial_json(self) -> None:
        self.init_project()
        active_dir = self.project / ".hefesto" / "logs" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.project / ".hefesto" / "logs" / "runs" / "tool_delta_run2.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        stream_json_line = (
            '{"type": "content_block_delta", "delta": {"type": "input_json_delta", '
            '"partial_json": "{\\"target\\": \\"src/calc.py\\"}"}}\n'
        )
        log_file.write_text(stream_json_line, encoding="utf-8")

        active_record = {
            "run_id": "tool_delta_run2",
            "kind": "worker",
            "pid": os.getpid(),
            "role": "runner",
            "provider": "claude",
            "model_id": "claude:sonnet",
            "task_id": "TASK-001",
            "started_at": util.now_iso(),
            "log_path": ".hefesto/logs/runs/tool_delta_run2.log",
        }
        (active_dir / "tool_delta_run2.json").write_text(json.dumps(active_record), encoding="utf-8")

        code, out, err = self.cli("jobs")
        self.assertEqual(code, 0, out + err)
        self.assertIn("(llamada a herramienta)", out)
        self.assertNotIn("partial_json", out)
        self.assertNotIn('{"target"', out)


class DoctorResourcesTests(HefestoTestCase):
    def test_doctor_shows_cpu_and_ram_and_ok(self) -> None:
        self.init_project()
        with mock.patch("hefesto.doctor.cpu_count", return_value=8), \
                mock.patch("hefesto.doctor.available_ram", return_value=16 * 1024**3):
            code, out, err = self.cli("doctor")
            self.assertEqual(code, 0, out + err)
            self.assertIn("recursos", out)
            self.assertIn("CPU", out)
            self.assertIn("RAM", out)
            self.assertIn("[ok]", out)

    def test_doctor_warns_when_max_parallel_exceeds_cpu_or_ram(self) -> None:
        self.init_project()
        # Con max_parallel_tasks por defecto = 4, y CPU=2 / RAM=2 GB, debe emitir aviso [!!]
        with mock.patch("hefesto.doctor.cpu_count", return_value=2), \
                mock.patch("hefesto.doctor.available_ram", return_value=2 * 1024**3):
            code, out, err = self.cli("doctor")
            self.assertEqual(code, 0, out + err)
            self.assertIn("recursos", out)
            self.assertIn("[!!]", out)
            self.assertIn("supera", out.lower())

    def test_doctor_never_fails_when_measurement_fails(self) -> None:
        self.init_project()
        with mock.patch("hefesto.doctor.cpu_count", return_value=None), \
                mock.patch("hefesto.doctor.available_ram", return_value=None):
            code, out, err = self.cli("doctor")
            self.assertEqual(code, 0, out + err)
            self.assertIn("recursos", out)
            self.assertIn("[··]", out)

    def test_doctor_available_ram_macos_vm_stat(self) -> None:
        fake_vm_stat = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               1000.\n"
            "Pages active:                            50000.\n"
            "Pages inactive:                           2000.\n"
        )
        proc_mock = mock.Mock(returncode=0, stdout=fake_vm_stat, stderr="")
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("platform.system", return_value="Darwin"), \
                mock.patch("subprocess.run", return_value=proc_mock):
            ram = doctor.available_ram()
            # (1000 + 2000) * 16384 = 49152000 bytes
            self.assertEqual(ram, 49152000)

    def test_doctor_invalid_max_parallel_tasks_warns_and_continues_diagnostics(self) -> None:
        self.init_project()
        toml_path = self.project / ".hefesto" / "hefesto.toml"
        toml_path.write_text('[limits]\nmax_parallel_tasks = "invalido"\n', encoding="utf-8")
        code, out, err = self.cli("doctor")
        self.assertIn("recursos", out)
        self.assertIn("valor inválido de limits.max_parallel_tasks", out)
        self.assertIn("[!!] config", out)
        self.assertIn("Configuración inválida", out)
        self.assertIn("python", out)
        self.assertIn("estado", out)
