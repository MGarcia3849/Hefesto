"""CLI de Google: `gemini -p` (prompt por stdin) o Antigravity `agy -p` (paquete por fichero).

`adapters.gemini.binary` elige `gemini`, `agy` o el primero instalado (`auto`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hefesto.adapters.base import ORCHESTRATOR_SHELL, Adapter, AdapterResult, Call, last_json_object

STDIN_INSTRUCTION = "Sigue exactamente las instrucciones del paquete recibido por la entrada estándar."


class GeminiAdapter(Adapter):
    provider = "gemini"
    patterns = {
        "auth": re.compile(r"unauthenticated|auth method|not signed in|login required|sign in|\b401\b|"
                           r"permission_denied", re.I),
        "quota": re.compile(r"quota|resource_exhausted.*(?:day|daily)|exhausted your|daily limit", re.I),
        "rate_limit": re.compile(r"\b429\b|too many requests|ratelimitexceeded|rate[_ ]?limit|resource_exhausted", re.I),
    }

    def is_agy(self, binary: str) -> bool:
        return Path(binary).name.lower().split(".")[0] == "agy"

    def command(self, binary: str, call: Call) -> list[str]:
        model = call.model if call.model and call.model != "default" else ""
        if self.is_agy(binary):
            packet = call.packet_path
            instruction = (f"Lee el paquete de la tarea en {packet} y sigue exactamente sus instrucciones."
                           if packet else STDIN_INSTRUCTION)
            argv = [binary, "-p", instruction, "--output-format", "json",
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
        data = last_json_object(stdout)
        if not isinstance(data, dict):
            return self.failure_from_text(call, stdout + "\n" + stderr, returncode)
        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            return self.failure_from_text(call, f"{message}\n{stderr}", returncode or 1)
        summary = next((data[k] for k in ("response", "result", "text", "content") if isinstance(data.get(k), str)), None)
        if summary is None:
            return self.failure_from_text(call, stdout + "\n" + stderr, 0 if returncode == 0 else returncode)
        return self.result(call, returncode == 0, summary, None, _usage(data), call.extra.get("log_path", ""))


def _usage(data: dict[str, Any]) -> dict[str, Any] | str:
    stats = data.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict) or not models:
        return "unknown"
    prompt = candidates = 0
    found = False
    for entry in models.values():
        tokens = entry.get("tokens") if isinstance(entry, dict) else None
        if isinstance(tokens, dict):
            found = True
            prompt += int(tokens.get("prompt") or 0)
            candidates += int(tokens.get("candidates") or 0)
    return {"input_tokens": prompt, "output_tokens": candidates} if found else "unknown"
