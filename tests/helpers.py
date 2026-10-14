"""Utilidades de test: proyectos temporales aislados, CLI en proceso y TTY simulada."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"

# Identidad de git fija para los repositorios temporales.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Hefesto Test",
    "GIT_AUTHOR_EMAIL": "test@hefesto.invalid",
    "GIT_COMMITTER_NAME": "Hefesto Test",
    "GIT_COMMITTER_EMAIL": "test@hefesto.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=dict(os.environ, **GIT_ENV))
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {result.stderr}")
    return result.stdout.strip()


@contextlib.contextmanager
def tty(value: bool = True, answers: list[str] | None = None) -> Iterator[None]:
    """Simula (o niega) una terminal interactiva y responde a `input()` con `answers`."""
    replies = iter(answers or [])
    with mock.patch("hefesto.util.is_tty", return_value=value), \
            mock.patch("builtins.input", side_effect=lambda *_a: next(replies, "")):
        yield


class HefestoTestCase(unittest.TestCase):
    """Cada test tiene HEFESTO_HOME, HEFESTO_STATE_DIR y proyecto propios en un temporal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hefesto-test-")
        self.tmp = Path(self._tmp.name)
        self.env_patch = mock.patch.dict(os.environ, {
            "HEFESTO_HOME": str(self.tmp / "home"),
            "HEFESTO_STATE_DIR": str(self.tmp / "state"),
            **GIT_ENV,
        })
        self.env_patch.start()
        for key in [k for k in os.environ if k.upper().endswith("_API_KEY")]:
            os.environ.pop(key, None)
        self.project = self.tmp / "proyecto"
        self.use_fakes()

    def use_fakes(self, *names: str) -> None:
        """Sustituye el PATH: solo los fakes indicados, nunca las CLIs reales del usuario."""
        os.environ["PATH"] = path_with_fakes(self.tmp / "bin", *names)

    def tearDown(self) -> None:
        self.env_patch.stop()
        self._tmp.cleanup()

    def run_cli(self, *args: str, tty_value: bool = False, answers: list[str] | None = None,
                stdin: str = "") -> tuple[int, str, str]:
        from hefesto import cli

        out, err = io.StringIO(), io.StringIO()
        with tty(tty_value, answers), mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def init_project(self, providers: str = "gemini,codex") -> Path:
        code, out, err = self.run_cli("init", str(self.project), "--providers", providers)
        self.assertEqual(code, 0, out + err)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "inicial")
        return self.project

    def cli(self, *args: str, **kw: object) -> tuple[int, str, str]:
        return self.run_cli("-C", str(self.project), *args, **kw)  # type: ignore[arg-type]


def path_with_fakes(target: Path, *names: str) -> str:
    """PATH con `target` (que recibe solo los fakes pedidos) más git, bash y python."""
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for name in names:
        src = FAKES / name
        if os.name == "nt":
            (target / f"{name}.cmd").write_text(
                f'@echo off\r\n"{sys.executable}" "{src}" %*\r\n', encoding="utf-8")
        else:
            dest = target / name
            dest.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{src}" "$@"\n', encoding="utf-8")
            dest.chmod(0o755)
    keep = [str(target)]
    for tool in ("git", "bash"):
        found = shutil.which(tool)
        if found:
            keep.append(str(Path(found).parent))
    keep.append(str(Path(sys.executable).parent))
    return os.pathsep.join(keep)


def smoke_flow(project: Path, env: dict[str, str],
               hefesto: Callable[..., subprocess.CompletedProcess[str]]) -> int:
    """init → TASK-001 (verify: true) → trust → start → runner falso → reviewer falso → done.

    Todo con procesos reales de `hefesto` y CLIs falsas; `trust` (humano, TTY) se simula en proceso.
    """
    import json

    project.mkdir(parents=True)
    work = project.parent
    scenario = work / "scenario.json"
    scenario.write_text(json.dumps({
        "gemini": {"mode": "ok", "summary": "creado src/hola.py", "write": {"src/hola.py": "print('hola')\n"}},
        "claude": {"mode": "ok", "summary": "VEREDICTO: pass\nsin observaciones"},
    }), encoding="utf-8")
    env = dict(env, **GIT_ENV, HEFESTO_FAKE_SCENARIO=str(scenario), HEFESTO_FAKE_LOG=str(work / "calls.jsonl"),
               PATH=path_with_fakes(work / "bin", "claude", "codex", "gemini"))

    def step(*args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        result = hefesto(project, *args, env=env)
        print(f"$ hefesto {' '.join(args)}  → {result.returncode}\n{result.stdout}{result.stderr}".rstrip() + "\n")
        if result.returncode != expect:
            raise AssertionError(f"`hefesto {' '.join(args)}` devolvió {result.returncode} (esperado {expect})")
        return result

    try:
        step("init", str(project), "--providers", "gemini,codex")
        (project / ".hefesto" / "ROADMAP.md").write_text(
            "# ROADMAP\n\n## Fase 1 — Humo\n- [ ] TASK-001: Saludo\n  - verify: `true`\n  - files: src/\n"
            "  - role: runner\n  - size: S\n", encoding="utf-8")
        for args in (("add", "-A"), ("commit", "-q", "-m", "inicial")):
            subprocess.run(["git", *args], cwd=project, env=env, check=True, capture_output=True)
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            from hefesto import cli

            with tty(True, ["s"]), contextlib.redirect_stdout(io.StringIO()):
                if cli.main(["-C", str(project), "trust"]) != 0:
                    raise AssertionError("trust falló")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        step("-C", str(project), "start", "TASK-001")
        runner = json.loads(step("-C", str(project), "run", "runner", "Crea src/hola.py").stdout)
        reviewer = json.loads(step("-C", str(project), "run", "reviewer", "Revisa el diff").stdout)
        if (runner["provider"], reviewer["provider"]) != ("gemini", "claude"):
            raise AssertionError(f"enrutado inesperado: {runner['provider']} / {reviewer['provider']}")
        step("-C", str(project), "review", "TASK-001", "--verdict", "pass", "--run-id", reviewer["run_id"])
        step("-C", str(project), "done", "TASK-001")
        step("-C", str(project), "evidence", "verify")
        status = step("-C", str(project), "status").stdout
        log = subprocess.run(["git", "log", "--format=%s", "-2"], cwd=project, capture_output=True, text=True).stdout
        if "COMPLETED" not in status or "hefesto(TASK-001): Saludo [verified]" not in log:
            raise AssertionError(f"estado o commit incoherente:\n{status}\n{log}")
    except AssertionError as exc:
        print(f"smoke: FALLO — {exc}")
        return 1
    print("smoke: OK")
    return 0
