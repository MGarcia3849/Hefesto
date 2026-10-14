"""Claude Code en modo headless: `claude -p --output-format stream-json --verbose`, prompt por stdin."""

from __future__ import annotations

import re
from typing import Any

from hefesto.adapters.base import ORCHESTRATOR_SHELL, Adapter, AdapterResult, Call, last_json_object, loads_or_none


def _find_claude_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        obj = loads_or_none(line)
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    obj = last_json_object(stdout)
    if isinstance(obj, dict):
        return obj
    return None


class ClaudeAdapter(Adapter):
    provider = "claude"
    patterns = {
        "auth": re.compile(r"invalid api key|please run /login|not logged in|oauth token (?:has )?expired|"
                           r"authentication_error|\b401\b", re.I),
        "quota": re.compile(r"usage limit|hit your (?:usage )?limit|limit (?:will )?reset|out of (?:extra )?usage|"
                            r"weekly limit|session limit", re.I),
        "rate_limit": re.compile(r"\b429\b|rate[_ ]?limit|overloaded|\b529\b", re.I),
    }

    def command(self, binary: str, call: Call) -> list[str]:
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if call.model and call.model != "default":
            argv += ["--model", call.model]
        argv += ["--permission-mode", "plan" if call.read_only else "acceptEdits"]
        if call.orchestrator:
            argv += ["--allowedTools", " ".join(f"Bash({c}:*)" for c in ORCHESTRATOR_SHELL)]
            if call.max_turns:
                argv += ["--max-turns", str(call.max_turns)]
        return argv

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        data = _find_claude_result(stdout)
        text = f"{stdout}\n{stderr}"
        if not isinstance(data, dict) or "result" not in data and "is_error" not in data:
            return self.failure_from_text(call, text, returncode)
        summary = str(data.get("result") or "")
        if data.get("is_error") or returncode != 0 or data.get("subtype") not in (None, "success"):
            failure = self.failure_from_text(call, summary + "\n" + stderr, returncode or 1)
            return failure if failure.error_type else self.result(call, False, summary or failure.summary, None,
                                                                  _usage(data), call.extra.get("log_path", ""))
        return self.result(call, True, summary, None, _usage(data), call.extra.get("log_path", ""))


def _usage(data: dict[str, object]) -> dict[str, object] | str:
    usage = data.get("usage")
    if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
        return {"input_tokens": usage.get("input_tokens", "unknown"), "output_tokens": usage.get("output_tokens", "unknown")}
    return "unknown"
