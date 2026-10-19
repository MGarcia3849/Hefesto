"""Codex CLI no interactivo: `codex exec --json -s <sandbox> -`, prompt por stdin, eventos JSONL."""

from __future__ import annotations

import re
from typing import Any

from hefesto import providers
from hefesto.adapters.base import Adapter, AdapterResult, Call, loads_or_none


class CodexAdapter(Adapter):
    provider = "codex"
    patterns = {
        "auth": re.compile(r"not logged in|codex login|401 unauthorized|\bunauthorized\b|refresh token", re.I),
        "quota": re.compile(r"usage limit|usage_limit_reached|hit your usage limit|insufficient_quota|quota", re.I),
        "rate_limit": re.compile(r"\b429\b|too many requests|rate[_ ]?limit", re.I),
    }

    def command(self, binary: str, call: Call) -> list[str]:
        argv = [binary, "exec", "--json", "-s", "read-only" if call.read_only else "workspace-write"]
        if call.model and call.model != "default":
            argv += ["-m", call.model]
        effort = providers.entry_effort(call.model_id)
        if effort in providers.EFFORTS:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        for image in call.extra.get("images", []):
            argv += ["--image", str(image)]
        return argv + ["-"]

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        events = [e for e in (loads_or_none(line) for line in stdout.splitlines() if line.strip()) if isinstance(e, dict)]
        messages: list[str] = []
        usage: dict[str, Any] | str = "unknown"
        errors: list[str] = []
        for event in events:
            kind = event.get("type")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                messages.append(str(item.get("text") or ""))
            elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
                u = event["usage"]
                usage = {"input_tokens": u.get("input_tokens", "unknown"), "output_tokens": u.get("output_tokens", "unknown")}
            elif kind in ("turn.failed", "error"):
                err = event.get("error")
                errors.append(str(err.get("message") if isinstance(err, dict) else event.get("message") or err))
        if errors or (returncode != 0 and not messages):
            return self.failure_from_text(call, "\n".join(errors) + "\n" + stderr, returncode or 1)
        if not events or not messages:
            return self.failure_from_text(call, stdout + "\n" + stderr, 0)
        return self.result(call, returncode == 0, messages[-1], None, usage, call.extra.get("log_path", ""))
