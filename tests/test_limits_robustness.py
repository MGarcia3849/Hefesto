"""Tests de robustez para límites de uso y registro de actividad (TASK-816).

Verifica que:
1. codex_limits lee solo los últimos 512 KB de cada rollout (descartando la primera línea parcial).
2. Errores de E/S, de stat, JSON corrupto o conversión numérica se tratan como dato ausente (unknown) sin lanzar excepciones.
3. _parse_window rechaza used_percent no finito o fuera de [0, 100] y resets_at no finito, negativo o mayor que el año 2100.
4. near_limit nunca lanza excepción y claude_limits/update_claude_cache toleran una caché corrupta.
5. activity.list_active ignora y borra entradas JSON corruptas, y register/finish no fallan por carreras de ficheros.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from unittest import mock

from hefesto import activity, limits
from tests.test_limits_watch import LimitsCase, codex_event


class LimitsRobustnessTests(LimitsCase):
    def test_codex_reads_only_last_512kb_of_large_rollout(self) -> None:
        """Un rollout de más de 1 MB lee el evento del final y descarta la primera línea parcial."""
        # Generar ~1.2 MB de líneas antes del evento válido
        padding_line = json.dumps({"timestamp": "2026-10-14T10:00:00Z", "type": "heartbeat", "payload": "x" * 200})
        num_lines = 5000  # ~1.1 MB
        lines = [padding_line] * num_lines
        # Evento válido dentro de los últimos 512 KB (al final)
        valid_event = codex_event(75.0, 25.0, resets_in=3600, ts="2026-10-14T18:00:00Z")
        lines.append(valid_event)

        session_path = self.write_session("grande", *lines)
        file_size = session_path.stat().st_size
        self.assertGreater(file_size, 1024 * 1024, "El fichero de prueba debe superar 1 MB")

        data = limits.codex_limits()
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(data["five_hour"]["used_percent"], 75.0)
        self.assertEqual(data["weekly"]["used_percent"], 25.0)
        self.assertEqual(data["observed_at"], "2026-10-14T18:00:00Z")

    def test_codex_ignores_events_outside_last_512kb(self) -> None:
        """Un evento en el primer tercio de un fichero de 1.2 MB queda fuera de los 512 KB y da unknown."""
        early_event = codex_event(88.0, 33.0, resets_in=3600)
        padding_line = json.dumps({"timestamp": "2026-10-14T10:00:00Z", "type": "heartbeat", "payload": "y" * 300})
        # 3000 líneas de ~350 bytes = ~1 MB de relleno posterior
        lines = [early_event] + [padding_line] * 3000
        session_path = self.write_session("evento_al_inicio", *lines)
        self.assertGreater(session_path.stat().st_size, 1024 * 1024)

        data = limits.codex_limits()
        self.assertEqual(data["five_hour"], "unknown")
        self.assertEqual(data["weekly"], "unknown")

    def test_codex_tolerates_stat_and_io_errors(self) -> None:
        """Cualquier error de stat entre rglob y sort o de lectura en codex_limits no lanza excepción."""
        self.write_session("valido", codex_event(50.0, 20.0))
        # Simular que stat lanza OSError
        original_stat = Path.stat

        def broken_stat(p: Path, *args: object, **kwargs: object) -> object:
            if "rollout-valido" in p.name:
                raise OSError("Disco no disponible")
            return original_stat(p, *args, **kwargs)

        with mock.patch.object(Path, "stat", side_effect=broken_stat):
            data = limits.codex_limits()
            self.assertEqual(data["five_hour"], "unknown")
            self.assertEqual(data["weekly"], "unknown")

    def test_codex_tolerates_corrupt_json_lines(self) -> None:
        """Líneas truncadas o no-JSON en rollouts no provocan excepciones."""
        self.write_session("corrupto", "{json no valido", "", "   ", "{\"type\": \"desconocido\"}")
        data = limits.codex_limits()
        self.assertEqual(data["five_hour"], "unknown")
        self.assertEqual(data["weekly"], "unknown")

    def test_parse_window_rejects_non_finite_or_out_of_range_used_percent(self) -> None:
        """_parse_window rechaza used_percent no finito, inf, nan, <0, >100 o tipos inválidos."""
        now = int(time.time()) + 3600
        # Valores inválidos de used_percent
        for invalid_used in (float("inf"), float("-inf"), float("nan"), 105.0, -0.5, "inf", "nan", True, False, None, []):
            win = {"used_percent": invalid_used, "resets_at": now}
            self.assertEqual(limits._parse_window(win), "unknown", f"Falló rechazar used_percent={invalid_used!r}")

        # Valores válidos de used_percent en los límites [0, 100]
        self.assertEqual(limits._parse_window({"used_percent": 0.0, "resets_at": now})["used_percent"], 0.0)
        self.assertEqual(limits._parse_window({"used_percent": 100.0, "resets_at": now})["used_percent"], 100.0)
        self.assertEqual(limits._parse_window({"used_percent": "50.5", "resets_at": now})["used_percent"], 50.5)

    def test_parse_window_rejects_non_finite_negative_or_post_2100_resets_at(self) -> None:
        """_parse_window rechaza resets_at no finito, negativo o posterior al año 2100."""
        # Negativos, no finitos o tipos no permitidos
        for invalid_reset in (-1, -100.0, "-50", float("inf"), float("-inf"), float("nan"), "inf", "nan", True, False, []):
            win = {"used_percent": 50.0, "resets_at": invalid_reset}
            self.assertEqual(limits._parse_window(win), "unknown", f"Falló rechazar resets_at={invalid_reset!r}")

        # Posterior al año 2100 (año 2101 timestamp o string)
        self.assertEqual(limits._parse_window({"used_percent": 50.0, "resets_at": 4133980801}), "unknown")
        self.assertEqual(limits._parse_window({"used_percent": 50.0, "resets_at": "2105-01-01T00:00:00Z"}), "unknown")
        self.assertEqual(limits._parse_window({"used_percent": 50.0, "resets_at": "9999-12-31T23:59:59Z"}), "unknown")

        # Año 2100 es válido
        res = limits._parse_window({"used_percent": 50.0, "resets_at": "2100-12-31T23:59:59Z"})
        self.assertIsInstance(res, dict)
        self.assertEqual(res["used_percent"], 50.0)

    def test_near_limit_never_raises_and_claude_tolerates_corrupt_cache(self) -> None:
        """near_limit devuelve None y claude_limits tolera una caché corrupta, sobrescribiéndola al actualizar."""
        cache_file = limits.claude_cache_path()
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        # 1. Caché corrupta con JSON roto
        cache_file.write_text("{este json esta roto!", encoding="utf-8")

        # claude_limits no debe lanzar y debe devolver unknown
        claude_data = limits.claude_limits()
        self.assertEqual(claude_data["five_hour"], "unknown")
        self.assertEqual(claude_data["weekly"], "unknown")

        # near_limit no debe lanzar con caché corrupta
        self.assertIsNone(limits.near_limit("claude", 0.9))
        self.assertIsNone(limits.near_limit("desconocido", 0.9))
        self.assertIsNone(limits.near_limit("claude", float("nan")))

        # 2. update_claude_cache tolera la caché rota y la sobrescribe con datos válidos
        resets = int(time.time()) + 3600
        limits.update_claude_cache({"five_hour": {"used_percentage": 95, "resets_at": resets}})

        # Ahora claude_limits lee los datos válidos
        updated_data = limits.claude_limits()
        self.assertEqual(updated_data["five_hour"]["used_percent"], 95.0)

        # near_limit ahora detecta que está cerca del límite
        reason = limits.near_limit("claude", 0.9)
        self.assertIsNotNone(reason)
        self.assertIn("cerca del límite", str(reason))


class ActivityRobustnessTests(LimitsCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        self.active_dir = activity.active_dir(self.project)
        self.active_dir.mkdir(parents=True, exist_ok=True)

    def test_list_active_ignores_and_deletes_corrupt_json(self) -> None:
        """list_active ignora y elimina ficheros JSON corruptos o que no sean diccionarios."""
        # Fichero JSON sintácticamente corrupto
        corrupt_file = self.active_dir / "corrupto.json"
        corrupt_file.write_text("{json roto", encoding="utf-8")

        # Fichero JSON válido pero no diccionario
        list_file = self.active_dir / "lista.json"
        list_file.write_text("[1, 2, 3]", encoding="utf-8")

        # Fichero diccionario válido pero con PID de proceso vivo actual
        valid_file = self.active_dir / "valido.json"
        valid_file.write_text(json.dumps({
            "run_id": "valido", "kind": "worker", "pid": os.getpid(), "role": "runner",
            "provider": "gemini", "model_id": "gemini:default", "task_id": "TASK-816",
            "started_at": "2026-10-14T20:00:00Z", "log_path": "test.log",
        }), encoding="utf-8")

        active = activity.list_active(self.project)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["run_id"], "valido")

        # Los ficheros corruptos deben haber sido eliminados del disco
        self.assertFalse(corrupt_file.exists(), "corrupto.json debe ser borrado por list_active")
        self.assertFalse(list_file.exists(), "lista.json debe ser borrado por list_active")
        self.assertTrue(valid_file.exists())

    def test_register_and_finish_race_conditions_do_not_fail(self) -> None:
        """register y finish toleran directorios inexistentes o ficheros ya eliminados sin lanzar."""
        # finish sobre id inexistente
        activity.finish(self.project, "inexistente_123")

        # register crea directorio si hiciera falta y registra sin error
        entry = activity.register(
            self.project, "run_test", "worker", "runner", "claude", "claude:sonnet",
            "TASK-816", ".hefesto/logs/runs/test.log",
        )
        self.assertTrue(entry.is_file())

        # finish elimina la entrada
        activity.finish(self.project, "run_test")
        self.assertFalse(entry.exists())

        # segunda llamada a finish sobre la misma entrada ya borrada no falla
        activity.finish(self.project, "run_test")
