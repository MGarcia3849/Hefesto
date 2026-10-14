"""Tareas de desarrollo (equivalen a los objetivos del Makefile; útiles donde no hay `make`).

    python tools/dev.py lint | test | smoke | smoke-live
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    commands = {"lint": lint, "test": test, "smoke": smoke, "smoke-live": smoke_live}
    if len(argv) != 1 or argv[0] not in commands:
        print(__doc__)
        return 1
    return commands[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
