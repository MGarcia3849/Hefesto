"""Fase 4: adaptadores, clasificación de errores, enrutado por cadena, cooldowns, paquete y modelo local."""

from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import os
import threading
from pathlib import Path

from hefesto import adapters, config, packet, quota, roadmap, state
from hefesto.adapters.base import Call
from hefesto.util import read_jsonl
from tests.helpers import HefestoTestCase, git

ROADMAP = """# ROADMAP

## Fase 1 — Núcleo
- [ ] TASK-001: Crear suma
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""


class AdapterCase(HefestoTestCase):
    providers = "gemini,codex"

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes("claude", "codex", "gemini", "agy")
        self.scenario_path = self.tmp / "scenario.json"
        self.calls_path = self.tmp / "calls.jsonl"
        os.environ["HEFESTO_FAKE_SCENARIO"] = str(self.scenario_path)
        os.environ["HEFESTO_FAKE_LOG"] = str(self.calls_path)
        self.init_project(self.providers)
        (self.project / ".hefesto" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")

    def here(self, *args: str, **kw: object) -> tuple[int, str, str]:
        with contextlib.chdir(self.project):
            return self.run_cli(*args, **kw)  # type: ignore[arg-type]

    def scenario(self, **entries: object) -> None:
        self.scenario_path.write_text(json.dumps(entries), encoding="utf-8")

    def calls(self, name: str | None = None) -> list[dict[str, object]]:
        return [c for c in read_jsonl(self.calls_path) if name is None or c["name"] == name]

    def call(self, provider: str, role: str = "runner", timeout: int = 60) -> object:
        cfg = config.load(self.project)
        return adapters.get(cfg, provider).run(Call(role=role, prompt="haz algo", task_id=None, run_id="r1",
                                                    model="", workdir=self.project, project=self.project,
                                                    timeout=timeout))

    def run_json(self, role: str = "runner", *extra: str) -> tuple[int, dict[str, object]]:
        code, out, err = self.cli("run", role, "instrucción", *extra)
        return code, (json.loads(out) if out.strip().startswith("{") else {"stderr": err, "stdout": out})


class ClassificationTests(AdapterCase):
    def test_error_types_for_each_cli(self) -> None:
        for provider in ("claude", "codex", "gemini"):
            for mode in ("quota", "rate_limit", "auth"):
                self.scenario(**{provider: {"mode": mode}})
                result = self.call(provider)
                self.assertFalse(result.success, f"{provider}/{mode}")
                self.assertEqual(result.error_type, mode, f"{provider}/{mode}: {result.summary}")

    def test_success_parses_summary_and_usage(self) -> None:
        for provider in ("claude", "codex", "gemini"):
            self.scenario(**{provider: {"mode": "ok", "summary": f"listo {provider}"}})
            result = self.call(provider)
            self.assertTrue(result.success, result.summary)
            self.assertEqual(result.summary, f"listo {provider}")
            self.assertIsInstance(result.usage, dict)

    def test_corrupt_output_is_failure(self) -> None:
        for provider in ("claude", "codex", "gemini"):
            self.scenario(**{provider: {"mode": "garbage"}})
            result = self.call(provider)
            self.assertFalse(result.success)
            self.assertEqual(result.error_type, "invalid_output", provider)

    def test_timeout(self) -> None:
        self.scenario(codex={"mode": "timeout", "sleep": 10})
        result = self.call("codex", timeout=1)
        self.assertEqual(result.error_type, "timeout")

    def test_missing_binary(self) -> None:
        self.use_fakes()
        result = self.call("codex")
        self.assertEqual(result.error_type, "missing_binary")

    def test_reviewer_is_read_only(self) -> None:
        self.scenario(codex={"mode": "ok"}, claude={"mode": "ok"}, gemini={"mode": "ok"})
        for provider, flag in (("codex", "read-only"), ("claude", "plan"), ("gemini", "plan")):
            self.call(provider, role="reviewer")
            self.assertIn(flag, self.calls(provider)[-1]["argv"])
        for provider in ("codex", "claude", "gemini"):
            for forbidden in ("--dangerously-skip-permissions", "--yolo", "danger-full-access"):
                self.assertNotIn(forbidden, " ".join(self.calls(provider)[-1]["argv"]))

    def test_api_keys_are_not_passed_to_children(self) -> None:
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GEMINI_API_KEY"] = "x"
        self.scenario(codex={"mode": "ok"})
        self.call("codex")
        self.assertEqual(self.calls("codex")[-1]["api_keys"], [])

    def test_agy_binary_and_unknown_usage(self) -> None:
        settings = config.project_settings_path(self.project)
        config.set_setting(settings, ("adapters", "gemini", "binary"), "agy")
        self.scenario(agy={"mode": "ok", "summary": "hecho por agy"})
        code, data = self.run_json()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"], "hecho por agy")
        self.assertEqual(len(self.calls("agy")), 1)
        usage = read_jsonl(self.project / ".hefesto" / "logs" / "usage.jsonl")[-1]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), ("unknown", "unknown"))


class RoutingTests(AdapterCase):
    def test_dry_run_calls_nothing(self) -> None:
        code, out, _ = self.cli("run", "runner", "x", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("elegido", out)
        code, out, _ = self.cli("route", "TASK-001", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("gemini:default", out)
        self.assertEqual(self.calls(), [])

    def test_quota_moves_to_next_without_spending_attempts(self) -> None:
        self.cli("start", "TASK-001")
        self.scenario(gemini={"mode": "quota"}, codex={"mode": "ok", "summary": "codex lo hizo"})
        code, data = self.run_json()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(state.load(self.project)["attempts"], 0)
        self.assertEqual(quota.provider_state("gemini", model="default")["status"], "cooldown")
        before = len(self.calls("gemini"))
        code, data = self.run_json()
        self.assertEqual(len(self.calls("gemini")), before)
        self.assertEqual(data["provider"], "codex")

    def test_expired_cooldown_is_used_again(self) -> None:
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        quota.mark_cooldown("gemini", past, "quota", "x")
        self.scenario(gemini={"mode": "ok"})
        code, data = self.run_json()
        self.assertEqual(data["provider"], "gemini")

    def test_whole_chain_exhausted_pauses_without_blocking(self) -> None:
        self.cli("start", "TASK-001")
        self.scenario(**{"*": {"mode": "rate_limit"}})
        code, data = self.run_json()
        self.assertEqual(code, 3)
        self.assertIn("next_available", data)
        current = state.load(self.project)
        self.assertEqual((current["status"], current["attempts"]), ("IN_PROGRESS", 0))
        self.assertIn("- [~] TASK-001", (self.project / ".hefesto" / "ROADMAP.md").read_text(encoding="utf-8"))

    def test_auth_error_marks_and_skips_until_reset(self) -> None:
        self.scenario(gemini={"mode": "auth"}, codex={"mode": "ok"})
        code, data = self.run_json()
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(quota.provider_state("gemini")["status"], "auth_error")
        code, out, _ = self.cli("providers", "status")
        self.assertIn("auth_error", out)
        self.here("providers", "reset", "gemini")
        self.assertEqual(quota.provider_state("gemini")["status"], "ok")

    def test_call_cap_per_loop(self) -> None:
        config.set_setting(config.project_settings_path(self.project),
                           ("quota", "max_calls_per_provider_per_loop"), {"gemini": 1, "codex": 5, "claude": 5})
        quota.start_loop("loop-1")
        self.scenario(gemini={"mode": "ok"}, codex={"mode": "ok"})
        self.assertEqual(self.run_json()[1]["provider"], "gemini")
        self.assertEqual(self.run_json()[1]["provider"], "codex")
        quota.end_loop()

    def test_unapproved_providers_are_never_called(self) -> None:
        settings = config.project_settings_path(self.project)
        data = config.read_settings(settings)
        data["approved_external_providers"] = []
        config.write_settings(settings, data)
        self.scenario(**{"*": {"mode": "ok"}})
        code, data = self.run_json()
        self.assertEqual(data["provider"], "claude")
        self.assertEqual(self.calls("gemini") + self.calls("codex"), [])
        outcomes = [h["outcome"] for h in data["hops"]]
        self.assertIn("saltado: no autorizado en este proyecto", outcomes)

    def test_runs_are_recorded_for_review_independence(self) -> None:
        self.cli("start", "TASK-001")
        self.scenario(gemini={"mode": "ok", "write": {"src/a.py": "a = 1\n"}}, claude={"mode": "ok"})
        _, runner = self.run_json()
        _, reviewer = self.run_json("reviewer")
        code, out, err = self.cli("review", "TASK-001", "--verdict", "pass", "--run-id", str(reviewer["run_id"]))
        self.assertEqual(code, 0, err)
        self.assertEqual(state.load(self.project)["review"]["authors"], ["gemini:default"])
        code, _, err = self.cli("review", "TASK-001", "--verdict", "pass", "--run-id", str(runner["run_id"]))
        self.assertEqual(code, 1)

    def test_secret_in_instruction_is_denied(self) -> None:
        code, _, err = self.cli("run", "runner", "usa la clave sk-ant-" + "a" * 30)
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])


class PacketTests(AdapterCase):
    def build(self, max_tokens: int | None = None) -> packet.Packet:
        if max_tokens:
            config.set_setting(config.project_settings_path(self.project), ("limits", "packet_max_tokens"),
                               {"default": max_tokens})
        cfg = config.load(self.project)
        rm = roadmap.load_valid(self.project)
        return packet.build(self.project, cfg, "runner", "implementa", rm.get("TASK-001"), rm, state.load(self.project),
                            "gemini:default", self.project)

    def test_packet_is_deterministic_and_excludes_secrets(self) -> None:
        (self.project / "src").mkdir()
        (self.project / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.project / "src" / ".env").write_text("TOKEN=1", encoding="utf-8")
        first, second = self.build(), self.build()
        self.assertEqual(first.text, second.text)
        self.assertIn("def a():", first.text)
        self.assertNotIn("TOKEN=1", first.text)
        self.assertIn("src/.env (ruta protegida)", first.text)

    def test_packet_respects_max_tokens_and_reports_omissions(self) -> None:
        (self.project / "src").mkdir()
        for i in range(5):
            (self.project / "src" / f"m{i}.py").write_text("x = 1\n" * 400, encoding="utf-8")
        pkt = self.build(max_tokens=900)
        self.assertLessEqual(len(pkt.text), 900 * packet.CHARS_PER_TOKEN + 600)
        self.assertTrue(any("por tamaño" in o for o in pkt.omitted))
        self.assertIn("Omitido del paquete", pkt.text)
        self.assertIn("Reglas del rol", pkt.text)

    def test_retry_includes_previous_error(self) -> None:
        self.cli("start", "TASK-001")
        self.cli("fail", "TASK-001", "AssertionError: suma(2, 2) == 5")
        self.assertIn("AssertionError: suma(2, 2) == 5", self.build().text)


class QuotaParsingTests(HefestoTestCase):
    def test_reset_times(self) -> None:
        now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(quota.parse_reset("limit reached|1767225600", now),
                         dt.datetime.fromtimestamp(1767225600, dt.timezone.utc))
        self.assertEqual(quota.parse_reset("try again in 2 hours", now), now + dt.timedelta(hours=2))
        self.assertEqual(quota.parse_reset("Please retry in 30s", now), now + dt.timedelta(seconds=30))
        self.assertIsNone(quota.parse_reset("algo falló", now))


class _FakeOpenAI(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (API de http.server)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": "pong"}}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


class LocalTests(AdapterCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_local_enable_requires_tty(self) -> None:
        code, _, _ = self.here("local", "enable", "--base-url", self.url, "--model", "m")
        self.assertEqual(code, 2)
        self.assertFalse(config.load(self.project).get("adapters", "local", "enabled"))

    def test_local_enable_and_disable(self) -> None:
        code, out, err = self.here("local", "enable", "--base-url", self.url, "--model", "qwen", "--role", "runner",
                                   "--first", tty_value=True, answers=["s"])
        self.assertEqual(code, 0, out + err)
        cfg = config.load(self.project)
        self.assertTrue(cfg.get("adapters", "local", "enabled"))
        self.assertIn("local", cfg.approved_providers())
        self.assertEqual(cfg.role_chain("runner")[0], "local")
        code, data = self.run_json()
        self.assertEqual((data["provider"], data["summary"]), ("local", "pong"))
        code, out, err = self.here("local", "disable", tty_value=True)
        self.assertEqual(code, 0, err)
        cfg = config.load(self.project)
        self.assertFalse(cfg.get("adapters", "local", "enabled"))
        self.assertNotIn("local", cfg.approved_providers())
        self.assertNotIn("local", cfg.role_chain("runner"))
        self.assertEqual(config.validate(cfg), [])

    def test_unreachable_endpoint_changes_nothing(self) -> None:
        code, _, err = self.here("local", "enable", "--base-url", "http://127.0.0.1:9/v1", "--model", "m",
                                 tty_value=True, answers=["s"])
        self.assertEqual(code, 3)
        self.assertFalse(config.load(self.project).get("adapters", "local", "enabled"))
        self.assertTrue(Path(config.project_settings_path(self.project)).exists())


class RealCliLearningsTests(AdapterCase):
    def test_ineligible_tier_classified_as_auth(self) -> None:
        self.scenario(gemini={"mode": "ineligible"})
        result = self.call("gemini")
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "auth")

        cfg = config.load(self.project)
        adapter = adapters.get(cfg, "gemini")
        for sample in ("IneligibleTierError: tier not supported", "UNSUPPORTED_CLIENT error", "model is no longer supported"):
            self.assertEqual(adapter.classify(sample), "auth", sample)

    def test_unavailable_and_503_classified_as_rate_limit(self) -> None:
        self.scenario(gemini={"mode": "unavailable"})
        result = self.call("gemini")
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "rate_limit")

        cfg = config.load(self.project)
        adapter = adapters.get(cfg, "gemini")
        for sample in ("UNAVAILABLE: service temporary down", "code 503", "HTTP 503 error", "the service is currently unavailable"):
            self.assertEqual(adapter.classify(sample), "rate_limit", sample)

    def test_agy_success_with_empty_response_and_denied_actions_is_failure(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("adapters", "gemini", "binary"), "agy")
        self.scenario(agy={"mode": "denied"})
        result = self.call("gemini")
        self.assertFalse(result.success)
        self.assertIsNone(result.error_type)
        self.assertIn("denegad", result.summary.lower())

    def test_agy_usage_tokens_read_when_no_stats(self) -> None:
        config.set_setting(config.project_settings_path(self.project), ("adapters", "gemini", "binary"), "agy")
        self.scenario(agy={"mode": "ok", "summary": "tarea completada", "usage": {"input_tokens": 120, "output_tokens": 45}})
        result = self.call("gemini")
        self.assertTrue(result.success)
        self.assertEqual(result.usage, {"input_tokens": 120, "output_tokens": 45})

    def test_agy_instruction_always_specifies_headless_and_file_editing_only(self) -> None:
        cfg = config.load(self.project)
        adapter = adapters.get(cfg, "gemini")
        call_with_packet = Call(role="runner", prompt="haz algo", task_id="TASK-001", run_id="r1",
                                model="", workdir=self.project, project=self.project,
                                packet_path=self.project / "packet.md")
        cmd_pkt = adapter.command("agy", call_with_packet)
        prompt_arg_pkt = cmd_pkt[cmd_pkt.index("-p") + 1]
        self.assertIn("headless", prompt_arg_pkt.lower())
        self.assertIn("shell", prompt_arg_pkt.lower())
        self.assertIn("editar ficheros", prompt_arg_pkt.lower())

        call_no_packet = Call(role="runner", prompt="haz algo", task_id="TASK-001", run_id="r1",
                              model="", workdir=self.project, project=self.project)
        cmd_no_pkt = adapter.command("agy", call_no_packet)
        prompt_arg_no_pkt = cmd_no_pkt[cmd_no_pkt.index("-p") + 1]
        self.assertIn("headless", prompt_arg_no_pkt.lower())
        self.assertIn("shell", prompt_arg_no_pkt.lower())
        self.assertIn("editar ficheros", prompt_arg_no_pkt.lower())
