"""CLI falsa que imita a claude, codex, gemini y agy. SOLO PARA TESTS.

Comportamiento por escenario JSON (`HEFESTO_FAKE_SCENARIO`, ruta a un fichero):
    {"gemini": {"mode": "ok", "summary": "hecho", "write": {"src/a.py": "x = 1\\n"}, "delay": 0}}
Modos: ok | quota | rate_limit | auth | garbage | timeout | fail.
Cada llamada se registra en `HEFESTO_FAKE_LOG` (JSONL): nombre, argv, cwd, stdin y claves de API visibles.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ERRORS = {
    "claude": {
        "quota": "Claude AI usage limit reached|1767225600",
        "rate_limit": "API Error: 429 rate_limit_error: Number of request tokens has exceeded your per-minute rate limit",
        "auth": "Invalid API key · Please run /login",
    },
    "codex": {
        "quota": "You've hit your usage limit. Upgrade to Pro or try again in 2 hours.",
        "rate_limit": "stream error: exceeded retry limit, last status: 429 Too Many Requests",
        "auth": "Not logged in. Run `codex login` to authenticate.",
    },
    "gemini": {
        "quota": "Quota exceeded for quota metric 'Gemini requests per day': RESOURCE_EXHAUSTED",
        "rate_limit": "[API Error: 429 Too Many Requests] rateLimitExceeded",
        "auth": "Please set an Auth method in your settings.json or run gemini to login (UNAUTHENTICATED)",
    },
    "agy": {
        "quota": "Error: quota exhausted for this account (RESOURCE_EXHAUSTED)",
        "rate_limit": "Error: 429 rate limit exceeded",
        "auth": "Error: not signed in. Run agy to sign in.",
    },
}


def scenario(name: str) -> dict[str, object]:
    path = os.environ.get("HEFESTO_FAKE_SCENARIO")
    if not path or not Path(path).exists():
        return {"mode": "ok"}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entry = data.get(name, data.get("*", {"mode": "ok"}))
    if isinstance(entry, list):  # secuencia de respuestas: consume la primera
        counter = Path(path).with_suffix(f".{name}.count")
        index = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(index + 1))
        entry = entry[min(index, len(entry) - 1)]
    return entry


def log_call(name: str, argv: list[str], stdin: str) -> None:
    path = os.environ.get("HEFESTO_FAKE_LOG")
    if not path:
        return
    keys = sorted(k for k in os.environ if k.upper().endswith("_API_KEY"))
    record = {"name": name, "argv": argv, "cwd": os.getcwd(), "stdin": stdin[:5000], "api_keys": keys,
              "t": time.time()}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def output(name: str, summary: str, model: str) -> str:
    if name == "claude":
        return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": summary,
                           "usage": {"input_tokens": 120, "output_tokens": 40}, "modelUsage": {model: {}}})
    if name == "codex":
        events = [{"type": "thread.started", "thread_id": "t1"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": summary}},
                  {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 30}}]
        return "\n".join(json.dumps(e) for e in events)
    if name == "gemini":
        return json.dumps({"response": summary, "stats": {"models": {model: {"tokens": {"prompt": 90, "candidates": 20}}}}})
    return json.dumps({"result": summary})


def error_output(name: str, mode: str) -> tuple[str, str]:
    message = ERRORS.get(name, ERRORS["agy"])[mode]
    if name == "codex":
        return json.dumps({"type": "turn.failed", "error": {"message": message}}), message
    if name == "gemini":
        return json.dumps({"error": {"type": "Error", "message": message, "code": 1}}), message
    return "", message


def main(name: str) -> int:
    argv = sys.argv[1:]
    if "--version" in argv or "-v" in argv:
        print(f"{name} fake 0.0.0")
        return 0
    if name == "codex" and argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT")
        return 0
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    log_call(name, argv, stdin)
    sc = scenario(name)
    time.sleep(float(sc.get("delay", 0) or 0))
    mode = str(sc.get("mode", "ok"))
    model = "fake-model"
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]
    elif "-m" in argv:
        model = argv[argv.index("-m") + 1]
    if mode in ("quota", "rate_limit", "auth"):
        out, err = error_output(name, mode)
        if out:
            print(out)
        print(err, file=sys.stderr)
        return 1
    if mode == "timeout":
        time.sleep(float(sc.get("sleep", 30)))
        return 0
    if mode == "garbage":
        print("<<esto no es JSON>>")
        return 0
    for rel, content in dict(sc.get("write", {}) or {}).items():  # type: ignore[union-attr]
        target = Path(os.getcwd()) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    if mode == "fail":
        print(output(name, str(sc.get("summary", "no he podido completar la tarea")), model))
        return 1
    print(output(name, str(sc.get("summary", "hecho")), model))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[0]).name.split(".")[0]))
