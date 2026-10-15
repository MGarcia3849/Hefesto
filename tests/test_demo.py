"""Contrato (TASK-1001 → TASK-1002): `python tools/dev.py demo`, la demostración con fakes de la entrega final.

Sección 13 (fase 10) y sección 14 de la especificación: «demo con fakes (incluida una fase con tres tareas en
paralelo, una de ellas con conflicto)» y «demostración de una tarea completa: start → runner → reviewer → verify →
done → commit».

`python tools/dev.py demo`:
- Todo ocurre en un directorio temporal propio que se borra al terminar: proyectos, `HEFESTO_HOME` y
  `HEFESTO_STATE_DIR` temporales. Nunca usa ni crea el `HEFESTO_HOME` del usuario. El PATH solo contiene las CLIs
  falsas de `tests/fakes` (más git, bash y python), así que nunca llama a un proveedor real ni gasta cuota.
  Quita del entorno de sus procesos `HEFESTO_ROLE` y las variables `*_API_KEY`.
- Parte 1, ciclo serie: ejecuta el flujo de `smoke` (init → trust → start → runner → reviewer → review → done →
  evidence verify) mostrando cada comando, y si va bien imprime la línea `demo: ciclo serie OK`.
- Parte 2, fase paralela con conflicto, en otro proyecto:
  - ROADMAP con una fase de tres tareas independientes (TASK-001, TASK-002 y TASK-003) con `files` disjuntos
    (`src/a.py`, `src/b.py`, `src/c.py`) y verify autoaprobable (`test -f …`).
  - Configuración: runner `claude:haiku` con 3 instancias (`limits.max_parallel_per_model`) y reviewer `gemini`.
  - El runner falso de cada tarea escribe su fichero y tarda varios segundos, de modo que las tres trabajan a la vez.
  - Lanza `hefesto parallel` como proceso real. En cuanto el runner de TASK-003 ha arrancado (lo ve en el registro
    del fake), simula un commit humano en la rama de la fase que crea `src/c.py` con otro contenido: el rebase de
    TASK-003 entra en conflicto.
  - Imprime una línea `demo: <TAREA> <estado>` por tarea, con el estado del resultado: `TASK-001 integrated`,
    `TASK-002 integrated` y `TASK-003 conflict`.
  - Comprueba e imprime:
    - `demo: runners solapados: sí`, si los tres runners estuvieron en marcha a la vez;
    - `demo: rama de TASK-003 conservada`, si su rama de tarea existe y la fase no tiene un rebase a medias;
    - `demo: evidencias: OK`, si `hefesto evidence verify` sale con 0.
- Termina con `demo: OK` y código 0 solo si todo lo anterior se cumple; si no, `demo: FALLO — <motivo>` y código 1.
"""

from __future__ import annotations

import os
import subprocess
import sys

from tests.helpers import ROOT, HefestoTestCase

EXPECTED = [
    "demo: ciclo serie OK",
    "demo: TASK-001 integrated",
    "demo: TASK-002 integrated",
    "demo: TASK-003 conflict",
    "demo: runners solapados: sí",
    "demo: rama de TASK-003 conservada",
    "demo: evidencias: OK",
    "demo: OK",
]


class DemoTests(HefestoTestCase):
    def test_demo_runs_both_parts_with_fakes_and_leaves_no_trace(self) -> None:
        user_home = self.tmp / "home-del-usuario"
        user_state = self.tmp / "state-del-usuario"
        env = dict(os.environ, HEFESTO_HOME=str(user_home), HEFESTO_STATE_DIR=str(user_state),
                   HEFESTO_ROLE="runner", ANTHROPIC_API_KEY="sk-no-se-usa")
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "dev.py"), "demo"], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        lines = [line.strip() for line in result.stdout.splitlines()]
        for expected in EXPECTED:
            self.assertIn(expected, lines, f"falta «{expected}» en la salida:\n{output[-4000:]}")
        self.assertLess(lines.index("demo: ciclo serie OK"), lines.index("demo: TASK-001 integrated"))
        self.assertEqual(lines[-1] if lines else "", "demo: OK")
        self.assertIn("hefesto parallel", result.stdout)
        self.assertFalse(user_home.exists(), "la demo no debe tocar el HEFESTO_HOME del usuario")
        self.assertFalse(user_state.exists(), "la demo no debe tocar el HEFESTO_STATE_DIR del usuario")
        self.assertNotIn("sk-no-se-usa", output)

    def test_unknown_command_still_shows_usage(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "dev.py"), "no-existe"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("demo", result.stdout, "el uso de tools/dev.py debe listar `demo`")
