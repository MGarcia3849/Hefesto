"""CLI de Google: `gemini -p` (prompt por stdin) o Antigravity `agy -p` (paquete por fichero).

`adapters.gemini.binary` elige `gemini`, `agy` o el primero instalado (`auto`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hefesto.adapters.base import ORCHESTRATOR_SHELL, Adapter, AdapterResult, Call, last_json_object, loads_or_none

STDIN_INSTRUCTION = "Sigue exactamente las instrucciones del paquete recibido por la entrada estándar."


def _find_agy_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        obj = loads_or_none(line)
        if isinstance(obj, dict) and obj.get("event") == "result":
            res = obj.get("result")
            if isinstance(res, dict):
                return res
            return obj
    return None


class GeminiAdapter(Adapter):
    provider = "gemini"
    patterns = {
        "auth": re.compile(
            r"unauthenticated|auth method|not signed in|login required|sign in|\b401\b|"
            r"permission_denied|ineligibletiererror|unsupported_client|no longer supported",
            re.I,
        ),
        "quota": re.compile(
            r"quota|resource_exhausted.*(?:day|daily)|exhausted your|daily limit",
            re.I,
        ),
        "rate_limit": re.compile(
            r"\b429\b|too many requests|ratelimitexceeded|rate[_ ]?limit|resource_exhausted|"
            r"\b503\b|unavailable|service is currently unavailable",
            re.I,
        ),
    }

    def is_agy(self, binary: str) -> bool:
        return Path(binary).name.lower().split(".")[0] == "agy"

    def command(self, binary: str, call: Call) -> list[str]:
        model = call.model if call.model and call.model != "default" else ""
        if self.is_agy(binary):
            packet = call.packet_path
            instruction = (f"Lee el paquete de la tarea en {packet} y sigue exactamente sus instrucciones."
                           if packet else STDIN_INSTRUCTION)
            instruction += " Estás en modo headless: no puedes ejecutar comandos de shell; solo debes leer y editar ficheros."
            argv = [binary, "-p", instruction, "--output-format", "stream-json",
                    "--mode", "plan" if call.read_only else "accept-edits"]
            if packet:
                argv += ["--add-dir", str(packet.parent)]
            return argv + (["--model", model] if model else [])
        argv = [binary, "-p", STDIN_INSTRUCTION, "-o", "json",
                "--approval-mode", "plan" if call.read_only else "auto_edit"]
        if call.orchestrator:
            argv += ["--allowed-tools", *(f"run_shell_command({c})" for c in ORCHESTRATOR_SHELL)]
        return argv + (["-m", model] if model else [])

    def stdin_for(self, call: Call) -> str:
        binary = self.binary()
        if binary and self.is_agy(binary) and call.packet_path:
            return ""
        return call.prompt

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        data: Any = None
        binary = self.binary()
        if binary and self.is_agy(binary):
            data = _find_agy_result(stdout)
        if data is None:
            data = last_json_object(stdout)
            if isinstance(data, dict) and data.get("event") == "result" and isinstance(data.get("result"), dict):
                data = data["result"]
        if not isinstance(data, dict):
            return self.failure_from_text(call, stdout + "\n" + stderr, returncode)

        _record_usage(data, call)

        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            details = f"{error}\n{message}" if isinstance(error, dict) else message
            return self.failure_from_text(call, f"{details}\n{stderr}", returncode or 1)
        denied = data.get("denied_actions")
        response_val = data.get("response")
        response_empty = response_val is None or (isinstance(response_val, str) and not response_val.strip())
        if str(data.get("status") or "").upper() == "SUCCESS" and response_empty and denied:
            count = len(denied) if isinstance(denied, (list, dict)) else 1
            summary = f"acciones denegadas ({count}): {denied}"
            return self.result(call, False, summary, None, _usage(data), call.extra.get("log_path", ""))
        summary = next((data[k] for k in ("response", "result", "text", "content") if isinstance(data.get(k), str)), None)
        if summary is None:
            return self.failure_from_text(call, stdout + "\n" + stderr, 0 if returncode == 0 else returncode)
        if returncode != 0:
            failure = self.failure_from_text(call, f"{summary}\n{stderr}", returncode)
            return failure if failure.error_type else self.result(call, False, summary, None, _usage(data), call.extra.get("log_path", ""))
        return self.result(call, True, summary, None, _usage(data), call.extra.get("log_path", ""))


def _usage(data: dict[str, Any]) -> dict[str, Any] | str:
    stats = data.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(models, dict) and models:
        prompt = candidates = 0
        found = False
        for entry in models.values():
            tokens = entry.get("tokens") if isinstance(entry, dict) else None
            if isinstance(tokens, dict):
                found = True
                prompt += int(tokens.get("prompt") or 0)
                candidates += int(tokens.get("candidates") or 0)
        if found:
            return {"input_tokens": prompt, "output_tokens": candidates}
    usage = data.get("usage")
    if isinstance(usage, dict):
        inp = usage.get("input_tokens") if usage.get("input_tokens") is not None else usage.get("prompt_tokens")
        out = usage.get("output_tokens") if usage.get("output_tokens") is not None else usage.get("completion_tokens")
        if inp is not None or out is not None:
            return {"input_tokens": int(inp or 0), "output_tokens": int(out or 0)}
    return "unknown"


def _record_usage(data: dict[str, Any], call: Call) -> None:
    """Registra el consumo reportado por Gemini sin afectar al resultado del adaptador."""
    try:
        usage = data.get("usage")
        total: Any = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(total, bool) or not isinstance(total, int):
            measured = _usage(data)
            if isinstance(measured, dict):
                input_tokens = measured.get("input_tokens", 0)
                output_tokens = measured.get("output_tokens", 0)
                if (isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and
                        isinstance(output_tokens, int) and not isinstance(output_tokens, bool)):
                    total = input_tokens + output_tokens
        if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            return
        from hefesto import quota

        model_id = call.model_id or f"gemini:{call.model or 'default'}"
        quota.record_usage(model_id, total)
    except Exception:
        # Telemetría de uso no debe convertir una respuesta válida en error de parseo.
        return
