"""Tareas de desarrollo (equivalen a los objetivos del Makefile; útiles donde no hay `make`).

    python tools/dev.py lint | test | smoke | smoke-live | demo
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY_DIRS = ("hefesto", "tests", "tools")
FORBIDDEN_MARKERS = re.compile(r"\b(?:TODO|FIXME|XXX)\b")


def _py_files(*dirs: str) -> list[Path]:
    return sorted(p for d in dirs for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


def lint() -> int:
    failures: list[str] = []
    result = subprocess.run([sys.executable, "-W", "error", "-m", "compileall", "-q", "-f", *PY_DIRS], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        failures.append("compileall:\n" + result.stdout + result.stderr)
    stdlib = set(sys.stdlib_module_names)
    for path in _py_files("hefesto", "tools") + [p for p in _py_files("tests") if "fakes" not in p.parts]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top not in stdlib and top not in ("hefesto", "tests"):
                    failures.append(f"{path.relative_to(ROOT)}: importa un paquete que no es de la librería estándar: {name}")
    for path in _py_files("hefesto"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN_MARKERS.search(line):
                failures.append(f"{path.relative_to(ROOT)}:{number}: marcador pendiente prohibido")
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        result = subprocess.run([shellcheck, "install.sh", "bin/hefesto"], cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            failures.append("shellcheck:\n" + result.stdout)
    else:
        print("lint: shellcheck no está instalado; se omite el análisis de install.sh")
    if failures:
        print("\n".join(failures))
        return 1
    print(f"lint: OK ({len(_py_files(*PY_DIRS))} ficheros Python)")
    return 0


def test() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def _hefesto(cwd: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "hefesto", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def smoke() -> int:
    from tests.helpers import smoke_flow

    with tempfile.TemporaryDirectory(prefix="hefesto-smoke-") as tmp:
        env = dict(os.environ, PYTHONPATH=str(ROOT), HEFESTO_HOME=str(Path(tmp) / "home"),
                   HEFESTO_STATE_DIR=str(Path(tmp) / "state"))
        return smoke_flow(Path(tmp) / "proyecto", env, _hefesto)


def smoke_live() -> int:
    """Llamadas reales y mínimas a los proveedores. Gasta cuota: exige confirmación en terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("smoke-live: exige una terminal interactiva (gasta cuota real).")
        return 2
    print("smoke-live hará una llamada mínima REAL por proveedor autorizado (runner y reviewer). Gasta cuota.")
    if input("¿Continuar? [s/N] ").strip().lower() not in ("s", "si", "sí", "y", "yes"):
        print("Cancelado.")
        return 0
    with tempfile.TemporaryDirectory(prefix="hefesto-live-") as tmp:
        project = Path(tmp) / "proyecto"
        project.mkdir()
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        steps = [("init", str(project), "--providers", "gemini,codex"),
                 ("-C", str(project), "providers", "status"),
                 ("-C", str(project), "run", "runner", "Responde solo con la palabra: listo"),
                 ("-C", str(project), "run", "reviewer", "Responde solo con: VEREDICTO: pass")]
        for step in steps:
            result = _hefesto(project, *step, env=env)
            print(f"$ hefesto {' '.join(step)} → {result.returncode}\n{result.stdout}{result.stderr}")
            if result.returncode != 0:
                return 1
    print("smoke-live: OK")
    return 0


def _terminate_demo_tree(proc: subprocess.Popen[str]) -> None:
    from hefesto.adapters.base import _terminate_tree

    # Los adaptadores abren sesiones propias: también hay que matar esos grupos.
    if os.name != "nt" and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGSTOP)
            snapshot = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid="], capture_output=True,
                                      text=True, check=True, timeout=5)
            processes = [tuple(map(int, line.split())) for line in snapshot.stdout.splitlines()]
            descendants = {proc.pid}
            while True:
                children = {pid for pid, parent, _ in processes if parent in descendants}
                if children <= descendants:
                    break
                descendants.update(children)
            groups = {group for pid, _, group in processes if pid in descendants and group in descendants}
            for group in groups - {proc.pid}:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            _terminate_tree(proc)
    else:
        # taskkill /T /F en Windows (ruta absoluta y fallback Toolhelp), killpg en POSIX;
        # esta utilidad espera al padre con wait() tras terminar el árbol.
        _terminate_tree(proc)


@contextlib.contextmanager
def _demo_process(args: list[str], **kwargs: Any) -> Iterator[subprocess.Popen[str]]:
    proc = None
    try:
        proc = subprocess.Popen(args, start_new_session=os.name != "nt", **kwargs)
        yield proc
    finally:
        if proc is not None:
            try:
                _terminate_demo_tree(proc)
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()


def _demo_run(args: list[str], *, timeout: float = 300, check: bool = False,
              capture_output: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _demo_process(args, **kwargs) as proc:
        out, err = proc.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(args, proc.returncode, out, err)
        if check:
            result.check_returncode()
        return result


def _demo_hefesto(cwd: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _demo_run([sys.executable, "-m", "hefesto", *args], cwd=cwd, text=True,
                     encoding="utf-8", errors="replace", env=env)


def demo() -> int:
    saved_env = dict(os.environ)
    try:
        result = _demo()
        if result == 0:
            print("demo: OK")
        return result
    except (Exception, KeyboardInterrupt) as exc:
        print(f"demo: FALLO — {type(exc).__name__}: {exc}")
        return 1
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def _demo() -> int:
    import json
    import time
    from hefesto import config, scheduler, state
    from tests.helpers import GIT_ENV, path_with_fakes, smoke_flow

    with tempfile.TemporaryDirectory(prefix="hefesto-demo-") as tmp:
        tmp_path = Path(tmp)
        home_dir = tmp_path / "home"
        state_dir = tmp_path / "state"
        home_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        os.environ["HEFESTO_HOME"] = str(home_dir)
        os.environ["HEFESTO_STATE_DIR"] = str(state_dir)
        for k in ("HEFESTO_ROLE", "HEFESTO_ORCHESTRATOR", "HEFESTO_LOOP_ID", "CLAUDECODE", "GEMINI_CLI",
                  "HEFESTO_FAKE_SCENARIO", "HEFESTO_FAKE_LOG"):
            os.environ.pop(k, None)
        for k in list(os.environ.keys()):
            if k.upper().endswith("_API_KEY"):
                os.environ.pop(k, None)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env.update(GIT_ENV)
        env["PATH"] = path_with_fakes(tmp_path / "bin", "claude", "codex", "gemini")

        # Parte 1: ciclo serie
        serie_project = tmp_path / "serie"
        if smoke_flow(serie_project, env, _demo_hefesto, run=_demo_run) != 0:
            print("demo: FALLO — ciclo serie")
            return 1
        print("demo: ciclo serie OK")

        # Parte 2: fase paralela con conflicto
        paralela = tmp_path / "paralela"
        paralela.mkdir(parents=True, exist_ok=True)
        init_res = _demo_hefesto(paralela, "init", str(paralela), "--shared", "--providers", "gemini,codex", env=env)
        if init_res.returncode != 0:
            err = (init_res.stderr or init_res.stdout).strip()
            print(f"demo: FALLO — init en proyecto paralelo falló: {err}")
            return 1

        roadmap_content = (
            "# ROADMAP\n\n"
            "## Fase 1 — Paralela\n"
            "- [ ] TASK-001: Tarea TASK-001\n"
            "  - verify: `test -f src/a.py`\n"
            "  - files: src/a.py\n"
            "  - role: runner\n"
            "  - size: S\n"
            "- [ ] TASK-002: Tarea TASK-002\n"
            "  - verify: `test -f src/b.py`\n"
            "  - files: src/b.py\n"
            "  - role: runner\n"
            "  - size: S\n"
            "- [ ] TASK-003: Tarea TASK-003\n"
            "  - verify: `test -f src/c.py`\n"
            "  - files: src/c.py\n"
            "  - role: runner\n"
            "  - size: S\n"
        )
        (paralela / ".hefesto" / "ROADMAP.md").write_text(roadmap_content, encoding="utf-8")

        settings_path = config.project_settings_path(paralela)
        config.set_setting(settings_path, ("roles", "runner", "use"), ["claude:haiku"])
        config.set_setting(settings_path, ("roles", "reviewer", "use"), ["gemini"])
        config.set_setting(settings_path, ("limits", "max_parallel_per_model"), {"claude:haiku": 3})
        config.set_setting(settings_path, ("limits", "max_parallel_tasks"), 3)

        add_res = _demo_run(["git", "add", "-A"], cwd=paralela, env=env, capture_output=True, text=True)
        if add_res.returncode != 0:
            print(f"demo: FALLO — git add inicial falló: {add_res.stderr.strip()}")
            return 1
        commit_res = _demo_run(["git", "commit", "-m", "inicial"], cwd=paralela, env=env, capture_output=True, text=True)
        if commit_res.returncode != 0:
            print(f"demo: FALLO — git commit inicial falló: {commit_res.stderr.strip()}")
            return 1

        delay = 6.0
        scenario_file = tmp_path / "scenario_paralela.json"
        calls_file = tmp_path / "calls_paralela.jsonl"
        if calls_file.exists():
            calls_file.unlink()

        scenario_data = {
            "*": {"mode": "ok", "summary": "VEREDICTO: pass"},
            "gemini@reviewer": {"mode": "ok", "summary": "VEREDICTO: pass"},
            "claude#TASK-001": {
                "mode": "ok",
                "summary": "creado src/a.py",
                "delay": delay,
                "write": {"src/a.py": "# TASK-001\n"},
            },
            "claude#TASK-002": {
                "mode": "ok",
                "summary": "creado src/b.py",
                "delay": delay,
                "write": {"src/b.py": "# TASK-002\n"},
            },
            "claude#TASK-003": {
                "mode": "ok",
                "summary": "creado src/c.py",
                "delay": delay,
                "write": {"src/c.py": "# TASK-003\n"},
            },
        }
        scenario_file.write_text(json.dumps(scenario_data), encoding="utf-8")

        env_parallel = dict(env)
        env_parallel.pop("HEFESTO_ROLE", None)
        env_parallel["HEFESTO_FAKE_SCENARIO"] = str(scenario_file)
        env_parallel["HEFESTO_FAKE_LOG"] = str(calls_file)

        with _demo_process(
            [sys.executable, "-m", "hefesto", "-C", str(paralela), "parallel"],
            cwd=paralela,
            env=env_parallel,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as proc:

            phase_dir: Path | None = None
            target_tw = scheduler.task_workdir(paralela, "hefesto/fase-1-paralela", "TASK-003")
            try:
                target_cwd = os.path.normcase(str(target_tw.resolve()))
            except Exception:
                target_cwd = os.path.normcase(str(target_tw))

            deadline = time.monotonic() + 120.0
            task3_started = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    proc_out, _ = proc.communicate(timeout=5)
                    print(f"demo: FALLO — hefesto parallel terminó prematuramente ({proc.returncode}):\n{proc_out}")
                    return 1

                if phase_dir is None:
                    try:
                        st = state.load(paralela)
                        phases = st.get("phases") or {}
                        phase_info = phases.get("fase-1-paralela")
                        if not phase_info and phases:
                            phase_info = next(iter(phases.values()))
                        if phase_info and phase_info.get("workdir"):
                            cand = Path(phase_info["workdir"])
                            if cand.is_dir():
                                phase_dir = cand
                    except Exception:
                        pass

                if not task3_started and calls_file.exists():
                    try:
                        content = calls_file.read_text(encoding="utf-8")
                        for line in content.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            if rec.get("name") == "claude":
                                raw_cwd = rec.get("cwd", "")
                                try:
                                    norm_cwd = os.path.normcase(str(Path(raw_cwd).resolve()))
                                except Exception:
                                    norm_cwd = os.path.normcase(raw_cwd)
                                if norm_cwd == target_cwd or norm_cwd.endswith(os.path.normcase("--TASK-003")):
                                    task3_started = True
                                    break
                    except Exception:
                        pass

                if task3_started and phase_dir is not None:
                    break
                time.sleep(0.05)

            if not task3_started:
                print("demo: FALLO — timeout esperando inicio de TASK-003 en registro fake")
                return 1

            if phase_dir is None:
                print("demo: FALLO — no se encontró el worktree de la fase en state.json")
                return 1

            (phase_dir / "src").mkdir(parents=True, exist_ok=True)
            (phase_dir / "src" / "c.py").write_text("cambio humano\n", encoding="utf-8")
            add_res = _demo_run(["git", "add", "src/c.py"], cwd=phase_dir, env=env_parallel, capture_output=True, text=True)
            if add_res.returncode != 0:
                print(f"demo: FALLO — error en git add commit humano: {add_res.stderr.strip()}")
                return 1

            commit_res = _demo_run(["git", "commit", "-m", "commit humano simulado"], cwd=phase_dir, env=env_parallel, capture_output=True, text=True)
            if commit_res.returncode != 0:
                print(f"demo: FALLO — error en commit humano: {commit_res.stderr.strip()}")
                return 1

            out, _ = proc.communicate(timeout=300)
        print("$ hefesto parallel")
        if out:
            print(out.rstrip())

        # Solo el resumen real de la CLI acredita el resultado; [!] también significa failed.
        for task_id, expected in (("TASK-001", "integrated"), ("TASK-002", "integrated"),
                                  ("TASK-003", "conflict")):
            statuses = re.findall(rf"^{task_id}: (\w+) \(intentos: \d+\)(?: - [^\r\n]*)?$",
                                  out, flags=re.MULTILINE)
            if statuses != [expected]:
                print(f"demo: FALLO — {task_id}: resultado esperado {expected}, recibido {statuses}")
                return 1
            print(f"demo: {task_id} {expected}")

        calls: list[dict[str, Any]] = []
        if calls_file.exists():
            for line in calls_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    calls.append(json.loads(line))
                except Exception:
                    continue

        claude_calls = [c for c in calls if c.get("name") == "claude"]
        if len(claude_calls) < 3:
            print(f"demo: FALLO — llamadas insuficientes de claude ({len(claude_calls)})")
            return 1

        starts = sorted(c["t"] for c in claude_calls)
        if starts[-1] >= starts[0] + delay:
            print("demo: FALLO — los runners no se solaparon en el tiempo")
            return 1
        print("demo: runners solapados: sí")

        phase_branch = "hefesto/fase-1-paralela"
        task_3_branch = scheduler.task_branch(phase_branch, "TASK-003")
        branch_res = _demo_run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{task_3_branch}"],
                               cwd=paralela, env=env_parallel)
        if branch_res.returncode != 0:
            print(f"demo: FALLO — la rama {task_3_branch} no existe")
            return 1

        rebase_active = False
        for marker in ("rebase-merge", "rebase-apply"):
            res = _demo_run(["git", "rev-parse", "--git-path", marker], cwd=phase_dir,
                                 capture_output=True, text=True, env=env_parallel)
            if res.returncode == 0:
                p_str = res.stdout.strip()
                p = Path(p_str)
                if not p.is_absolute():
                    p = phase_dir / p
                if p.exists():
                    rebase_active = True
                    break

        if rebase_active:
            print("demo: FALLO — rebase a medias en el worktree de la fase")
            return 1

        print("demo: rama de TASK-003 conservada")

        ev_res = _demo_hefesto(paralela, "-C", str(paralela), "evidence", "verify", env=env_parallel)
        if ev_res.returncode != 0:
            print(f"demo: FALLO — evidence verify falló:\n{ev_res.stdout}\n{ev_res.stderr}")
            return 1
        print("demo: evidencias: OK")

        return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    commands = {"lint": lint, "test": test, "smoke": smoke, "smoke-live": smoke_live, "demo": demo}
    if len(argv) != 1 or argv[0] not in commands:
        print(__doc__)
        return 1
    return commands[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
