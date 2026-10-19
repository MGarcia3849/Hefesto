"""Fase 19 (contrato): cuotas y coste por modelo.

- `quota.parse_reset` entiende duraciones compuestas («Resets in 126h13m11s», «2d3h», «45m10s»).
- Cooldown por modelo: `quota.mark_cooldown(provider, until, error_type, message, model=None)`,
  `quota.provider_state(provider, model=None)` y `providers.available(cfg, provider, model=None)`. `model` es el
  nombre del modelo sin proveedor ni sufijo de razonamiento (`"default"`, `"gemini-3.8-flash-medium"`). Un cooldown
  con modelo solo bloquea ese modelo; uno sin modelo bloquea el proveedor entero (y todos sus modelos). Un
  `providers.json` antiguo (sin modelos) se sigue leyendo igual.
- Tokens por modelo: `quota.record_usage(model_id, tokens, at=None)`, `quota.usage_window(model_id, hours, now=None)`
  (suma de tokens registrados en las últimas `hours` horas) y `quota.learned_limit(model_id)`: al marcar un cooldown
  con `error_type="quota"` y modelo, se guarda como umbral aprendido de `"provider:model"` la suma de tokens de los
  últimos 7 días (168 h); sin error de cuota es None y otros tipos de error no lo cambian.
- `limits.gemini_limits()` añade `models`: `{model_id: {"tokens_24h", "tokens_7d", "learned_limit"}}` solo con los
  modelos de Gemini que tengan uso registrado.
- Razonamiento por entrada (solo Codex): `codex:modelo:nivel` con nivel en `low|medium|high|xhigh|max`.
  `providers.split_entry` quita el nivel, `providers.entry_effort` lo devuelve (None si no hay) y
  `providers.model_id` lo conserva. El adaptador de Codex añade `-c model_reasoning_effort="nivel"` (tomado de
  `call.model_id`) antes de `-`; sin nivel no añade nada. Otros proveedores no cambian (`local:qwen2.5:7b` sigue
  siendo el modelo `qwen2.5:7b`). `config.validate` rechaza un nivel desconocido en una entrada de Codex.
- `limits.adaptive_cap(provider, base_cap, now)`: con la ventana semanal conocida y sin reiniciar, si el porcentaje
  usado va por delante de la fracción de semana transcurrida (`1 - (resets_at - now) / 7 días`), devuelve
  `max(1, floor(base_cap * transcurrida / usada))`; si no, o si el uso es desconocido, `base_cap`. Lee la ventana
  con la misma función que `near_limit` (`codex_limits`, `claude_limits`, `gemini_limits`). Con
  `[quota] adaptive_caps = true` (por defecto false), `providers.available` aplica ese tope.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from hefesto import config, limits, providers, quota
from hefesto.adapters.base import Call
from hefesto.adapters.codex import CodexAdapter
from tests.helpers import HefestoTestCase

NOW = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


class ResetParsingTests(HefestoTestCase):
    def test_compound_hours_minutes_seconds(self) -> None:
        got = quota.parse_reset("Quota exhausted. Resets in 126h13m11s.", now=NOW)
        self.assertEqual(got, NOW + dt.timedelta(hours=126, minutes=13, seconds=11))

    def test_compound_days_hours(self) -> None:
        self.assertEqual(quota.parse_reset("try again in 2d3h", now=NOW), NOW + dt.timedelta(days=2, hours=3))

    def test_compound_minutes_seconds(self) -> None:
        self.assertEqual(quota.parse_reset("retry after 45m10s", now=NOW), NOW + dt.timedelta(minutes=45, seconds=10))

    def test_simple_forms_still_work(self) -> None:
        self.assertEqual(quota.parse_reset("try again in 30 minutes", now=NOW), NOW + dt.timedelta(minutes=30))
        self.assertEqual(quota.parse_reset("resets in 5h", now=NOW), NOW + dt.timedelta(hours=5))
        self.assertIsNone(quota.parse_reset("algo salió mal", now=NOW))


class ModelCooldownTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.init_project("gemini,codex")
        self.cfg = config.load(self.project)
        self.until = dt.datetime.now(UTC) + dt.timedelta(hours=5)

    def test_model_cooldown_only_blocks_that_model(self) -> None:
        quota.mark_cooldown("gemini", self.until, "quota", "Resets in 5h", model="default")
        self.assertEqual(quota.provider_state("gemini", model="default")["status"], "cooldown")
        self.assertEqual(quota.provider_state("gemini", model="gemini-3.8-flash-medium")["status"], "ok")
        self.assertEqual(quota.provider_state("gemini")["status"], "ok")
        self.assertFalse(providers.available(self.cfg, "gemini", model="default")[0])
        self.assertTrue(providers.available(self.cfg, "gemini", model="gemini-3.8-flash-medium")[0])
        self.assertTrue(providers.available(self.cfg, "gemini")[0])

    def test_provider_cooldown_blocks_every_model(self) -> None:
        quota.mark_cooldown("gemini", self.until, "quota", "Resets in 5h")
        for model in (None, "default", "gemini-3.8-flash-medium"):
            with self.subTest(model=model):
                self.assertEqual(quota.provider_state("gemini", model=model)["status"], "cooldown")
                self.assertFalse(providers.available(self.cfg, "gemini", model=model)[0])

    def test_expired_model_cooldown_is_ok(self) -> None:
        past = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
        quota.mark_cooldown("gemini", past, "quota", "x", model="default")
        self.assertEqual(quota.provider_state("gemini", model="default")["status"], "ok")

    def test_legacy_providers_file_is_read(self) -> None:
        legacy = {"schema_version": 1, "loop": {"id": None, "calls": {}}, "providers": {"codex": {
            "status": "cooldown", "cooldown_until": self.until.replace(microsecond=0).isoformat(),
            "last_error_type": "quota", "last_error": "usage limit"}}}
        path = quota.providers_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(quota.provider_state("codex")["status"], "cooldown")
        self.assertEqual(quota.provider_state("codex", model="gpt-6-luna")["status"], "cooldown")


class UsageAccountingTests(HefestoTestCase):
    def test_usage_window_sums_by_model_and_time(self) -> None:
        mid = "gemini:gemini-3.8-flash-medium"
        quota.record_usage(mid, 100, at=NOW - dt.timedelta(hours=30))
        quota.record_usage(mid, 50, at=NOW - dt.timedelta(hours=1))
        quota.record_usage("gemini:default", 999, at=NOW - dt.timedelta(hours=1))
        self.assertEqual(quota.usage_window(mid, 24, now=NOW), 50)
        self.assertEqual(quota.usage_window(mid, 168, now=NOW), 150)
        self.assertEqual(quota.usage_window("codex:gpt-6-luna", 24, now=NOW), 0)

    def test_learned_limit_set_on_quota_error(self) -> None:
        mid = "gemini:default"
        self.assertIsNone(quota.learned_limit(mid))
        now = dt.datetime.now(UTC)
        quota.record_usage(mid, 1000, at=now - dt.timedelta(hours=2))
        quota.record_usage(mid, 2000, at=now - dt.timedelta(hours=1))
        quota.mark_cooldown("gemini", now + dt.timedelta(hours=1), "rate_limit", "429", model="default")
        self.assertIsNone(quota.learned_limit(mid))
        quota.mark_cooldown("gemini", now + dt.timedelta(hours=126), "quota", "Resets in 126h", model="default")
        self.assertEqual(quota.learned_limit(mid), 3000)
        quota.record_usage(mid, 500)
        self.assertEqual(quota.learned_limit(mid), 3000)
        self.assertIsNone(quota.learned_limit("gemini:gemini-3.8-flash-medium"))

    def test_gemini_limits_report_tokens_per_model(self) -> None:
        quota.record_usage("gemini:gemini-3.8-flash-medium", 1200)
        quota.record_usage("codex:gpt-6-luna", 7)
        info = limits.gemini_limits()
        models = info["models"]
        self.assertEqual(set(models), {"gemini:gemini-3.8-flash-medium"})
        entry = models["gemini:gemini-3.8-flash-medium"]
        self.assertEqual(entry["tokens_24h"], 1200)
        self.assertEqual(entry["tokens_7d"], 1200)
        self.assertIsNone(entry["learned_limit"])


class ReasoningEffortTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.init_project("gemini,codex")
        self.cfg = config.load(self.project)

    def call(self, model: str, mid: str) -> Call:
        return Call(role="runner", prompt="haz algo", task_id=None, run_id="r1", model=model, workdir=self.project,
                    project=self.project, model_id=mid)

    def test_split_and_effort(self) -> None:
        self.assertEqual(providers.split_entry("codex:gpt-6.1-sol:high"), ("codex", "gpt-6.1-sol"))
        self.assertEqual(providers.entry_effort("codex:gpt-6.1-sol:high"), "high")
        self.assertIsNone(providers.entry_effort("codex:gpt-6.1-sol"))
        self.assertIsNone(providers.entry_effort("claude:sonnet"))
        self.assertEqual(providers.model_id(self.cfg, "codex:gpt-6.1-sol:high"), "codex:gpt-6.1-sol:high")

    def test_other_providers_unchanged(self) -> None:
        self.assertEqual(providers.split_entry("local:qwen2.5:7b"), ("local", "qwen2.5:7b"))
        self.assertIsNone(providers.entry_effort("local:qwen2.5:7b"))
        self.assertEqual(providers.split_entry("gemini:gemini-3.8-flash-medium"), ("gemini", "gemini-3.8-flash-medium"))

    def test_codex_command_adds_effort(self) -> None:
        argv = CodexAdapter(self.cfg).command("codex", self.call("gpt-6.1-sol", "codex:gpt-6.1-sol:high"))
        self.assertIn('model_reasoning_effort="high"', argv)
        i = argv.index('model_reasoning_effort="high"')
        self.assertEqual(argv[i - 1], "-c")
        self.assertLess(i, argv.index("-"))
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")

    def test_codex_command_without_effort_inherits(self) -> None:
        argv = CodexAdapter(self.cfg).command("codex", self.call("gpt-6.1-sol", "codex:gpt-6.1-sol"))
        self.assertFalse(any("model_reasoning_effort" in a for a in argv))

    def test_validate_rejects_unknown_effort(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("roles", "runner", "use"),
                           ["codex:gpt-6.1-sol:extreme"])
        errors = config.validate(config.load(self.project))
        self.assertTrue(any("extreme" in e for e in errors), errors)
        config.set_setting(config.project_settings_path(self.project), ("roles", "runner", "use"),
                           ["codex:gpt-6.1-sol:xhigh"])
        self.assertFalse(any("xhigh" in e for e in config.validate(config.load(self.project))))


class AdaptiveCapTests(HefestoTestCase):
    def weekly(self, used: float | None, days_left: float) -> dict[str, object]:
        window: object = "unknown" if used is None else {
            "used_percent": used, "resets_at": (NOW + dt.timedelta(days=days_left)).isoformat()}
        return {"provider": "codex", "five_hour": "unknown", "weekly": window, "observed_at": None, "plan": None}

    def cap(self, used: float | None, days_left: float, base: int = 30) -> int:
        with mock.patch("hefesto.limits.codex_limits", return_value=self.weekly(used, days_left)):
            return limits.adaptive_cap("codex", base, NOW)

    def test_reduces_when_ahead_of_pace(self) -> None:
        self.assertEqual(self.cap(80, 3.5), 18)  # 30 * 0.5 / 0.8 = 18.75

    def test_keeps_base_when_on_pace(self) -> None:
        self.assertEqual(self.cap(40, 3.5), 30)

    def test_never_below_one(self) -> None:
        self.assertEqual(self.cap(100, 6.99), 1)

    def test_unknown_or_reset_keeps_base(self) -> None:
        self.assertEqual(self.cap(None, 3), 30)
        self.assertEqual(self.cap(90, -1), 30)

    def test_available_uses_adaptive_cap_when_enabled(self) -> None:
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.init_project("gemini,codex")
        path = config.project_settings_path(self.project)
        config.set_setting(path, ("quota", "max_calls_per_provider_per_loop"), {"codex": 30})
        quota.start_loop("loop-1")
        for _ in range(2):
            quota.record_call("codex")
        with mock.patch("hefesto.limits.adaptive_cap", return_value=2), \
                mock.patch("hefesto.limits.near_limit", return_value=None):
            self.assertTrue(providers.available(config.load(self.project), "codex")[0])
            config.set_setting(path, ("quota", "adaptive_caps"), True)
            ok, reason = providers.available(config.load(self.project), "codex")
        quota.end_loop()
        self.assertFalse(ok)
        self.assertIn("tope", reason)
