"""Modelo local con API compatible con OpenAI (`/chat/completions`) vía urllib. Desactivado por defecto."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from hefesto import log
from hefesto.adapters.base import Adapter, AdapterResult, Call, _rel


def chat(base_url: str, model: str, messages: list[dict[str, str]], timeout: int, max_tokens: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens:
        body["max_tokens"] = max_tokens
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL configurada por el usuario)
        return json.loads(response.read().decode("utf-8"))


class LocalAdapter(Adapter):
    provider = "local"

    def run(self, call: Call) -> AdapterResult:
        rel_log = f"runs/{call.run_id}-local.log"
        base_url = str(self.cfg.get("adapters", "local", "base_url") or "")
        model = call.model if call.model and call.model != "default" else str(self.cfg.get("adapters", "local", "model") or "")
        if not base_url or not model:
            path = log.write_text(call.project, rel_log, "local: falta base_url o model")
            return self.result(call, False, "adaptador local sin base_url o model", "missing_binary", "unknown",
                               _rel(call.project, path))
        start = time.monotonic()
        error_type: str | None = None
        detail = ""
        data: dict[str, Any] = {}
        try:
            data = chat(base_url, model, [{"role": "user", "content": call.prompt}], call.timeout)
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
            error_type = {401: "auth", 403: "auth", 429: "rate_limit"}.get(exc.code, "invalid_output")
        except (TimeoutError, socket.timeout):
            error_type, detail = "timeout", f"timeout tras {call.timeout}s"
        except (urllib.error.URLError, ConnectionError) as exc:
            error_type, detail = "missing_binary", f"endpoint no disponible: {exc}"
        except json.JSONDecodeError as exc:
            error_type, detail = "invalid_output", f"respuesta no JSON: {exc}"
        duration = round(time.monotonic() - start, 3)
        path = log.write_text(call.project, rel_log, f"POST {base_url}/chat/completions model={model}\n"
                                                     f"[{duration}s]\n{detail or json.dumps(data, ensure_ascii=False)}")
        rel = _rel(call.project, path)
        if error_type:
            return self.result(call, False, detail, error_type, "unknown", rel)
        try:
            content = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return self.result(call, False, "respuesta sin choices[0].message.content", "invalid_output", "unknown", rel)
        usage_raw = data.get("usage")
        usage: dict[str, Any] | str = ({"input_tokens": usage_raw.get("prompt_tokens", "unknown"),
                                        "output_tokens": usage_raw.get("completion_tokens", "unknown")}
                                       if isinstance(usage_raw, dict) else "unknown")
        result = self.result(call, True, content, None, usage, rel)
        result.duration = duration
        return result
