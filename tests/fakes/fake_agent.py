"""CLI falsa que imita a claude, codex, gemini y agy. SOLO PARA TESTS.

Comportamiento por escenario JSON (`HEFESTO_FAKE_SCENARIO`, ruta a un fichero):
    {"gemini": {"mode": "ok", "summary": "hecho", "write": {"src/a.py": "x = 1\\n"}, "delay": 0}}
Claves, de más a menos concreta: `nombre@rol#TAREA`, `nombre#TAREA` (TAREA = `HEFESTO_TASK_ID`),
`nombre@rol`, `nombre` y `*`.
Modos: ok | quota | rate_limit | auth | garbage | timeout | fail.
Cada llamada se registra en `HEFESTO_FAKE_LOG` (JSONL): nombre, argv, cwd, stdin y claves de API visibles.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ERRORS = {
    "claude": {
        "quota": "Claude AI usage limit reached|{reset_epoch}",
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
        "ineligible": "IneligibleTierError: model is no longer supported (UNSUPPORTED_CLIENT)",
        "unavailable": "The service is currently unavailable (UNAVAILABLE, code 503)",
    },
    "agy": {
        "quota": "Error: quota exhausted for this account (RESOURCE_EXHAUSTED)",
        "rate_limit": "Error: 429 rate limit exceeded",
        "auth": "Error: not signed in. Run agy to sign in.",
        "ineligible": "IneligibleTierError: client is no longer supported (UNSUPPORTED_CLIENT)",
        "unavailable": "The service is currently unavailable (UNAVAILABLE, code 503)",
    },
}


def scenario(name: str) -> dict[str, object]:
    path = os.environ.get("HEFESTO_FAKE_SCENARIO")
    if not path or not Path(path).exists():
        return {"mode": "ok"}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    role = os.environ.get("HEFESTO_ROLE", "")
    task = os.environ.get("HEFESTO_TASK_ID", "")
    candidates = ([f"{name}@{role}#{task}", f"{name}#{task}"] if task else []) + [f"{name}@{role}"]
    key = next((k for k in candidates if k in data), name)
    entry = data.get(key, data.get("*", {"mode": "ok"}))
    if isinstance(entry, list):  # secuencia de respuestas: consume la primera
        counter = Path(path).with_suffix(f".{key.replace('@', '-')}.count")
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


STREAM = False  # True si la CLI se invocó con `--output-format stream-json` (Claude y agy)


def emit_steps(name: str, sc: dict[str, object]) -> None:
    """Pasos intermedios en vivo (con flush), en el formato de streaming de cada CLI."""
    steps = list(sc.get("steps") or [])  # type: ignore[call-overload]
    delay = float(sc.get("step_delay", 0) or 0)  # type: ignore[arg-type]
    for step in steps:
        if name == "claude":
            line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": step}]}})
        elif name == "codex":
            line = json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": step}})
        elif name == "agy":
            line = json.dumps({"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response",
                                                                      "text_delta": step}})
        else:
            line = str(step)
        print(line, flush=True)
        time.sleep(delay)


def output(name: str, summary: str, model: str) -> str:
    if name == "claude":
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": summary,
                             "usage": {"input_tokens": 120, "output_tokens": 40}, "modelUsage": {model: {}}})
        if STREAM:
            return json.dumps({"type": "system", "subtype": "init", "model": model}) + "\n" + result
        return result
    if name == "codex":
        events = [{"type": "thread.started", "thread_id": "t1"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": summary}},
                  {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 30}}]
        return "\n".join(json.dumps(e) for e in events)
    if name == "gemini":
        return json.dumps({"response": summary, "stats": {"models": {model: {"tokens": {"prompt": 90, "candidates": 20}}}}})
    if name == "agy" and STREAM:
        return (json.dumps({"event": "init", "conversation_id": "c1"}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": summary}}))
    return json.dumps({"result": summary})


def error_output(name: str, mode: str) -> tuple[str, str]:
    message = ERRORS.get(name, ERRORS["agy"])[mode].replace("{reset_epoch}", str(int(time.time()) + 3600))
    if name == "codex":
        return json.dumps({"type": "turn.failed", "error": {"message": message}}), message
    if name == "gemini":
        return json.dumps({"error": {"type": "Error", "message": message, "code": 1}}), message
    return "", message


def hefesto(*args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, "-m", "hefesto", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def orchestrate(name: str, sc: dict[str, object]) -> int:
    """Orquestador falso: un ciclo `next` real con la CLI, retomando desde `hefesto resume`."""
    info = json.loads(hefesto("resume", "--json").stdout)
    if info["status"] != "IN_PROGRESS":
        nxt = re.search(r"Siguiente: (TASK-\d+)", hefesto("next").stdout)
        if not nxt:
            print(output(name, "no hay tareas", "fake"))
            return 0
        hefesto("start", nxt.group(1))
        info = json.loads(hefesto("resume", "--json").stdout)
    task = info["task"]
    runs = info["runs"]
    if not any(r["success"] and r["role"] != "reviewer" for r in runs):
        hefesto("run", task["role"], "Implementa la tarea")
        hefesto("note", f"{name}: implementación delegada")
        if sc.get("quota_after") == "runner":
            out, err = error_output(name, "quota")
            if out:
                print(out)
            print(err, file=sys.stderr)
            return 1
    reviewer = next((r for r in runs if r["success"] and r["role"] == "reviewer"), None)
    if reviewer is None:
        reviewer = json.loads(hefesto("run", "reviewer", "Revisa el diff").stdout)
        hefesto("note", f"{name}: revisión delegada")
    if not info.get("review"):
        hefesto("review", task["id"], "--verdict", "pass", "--run-id", str(reviewer["run_id"]))
    done = hefesto("done", task["id"])
    hefesto("note", f"{name}: done {task['id']} → {done.returncode}")
    print(output(name, f"ciclo de {task['id']} terminado", "fake"))
    return 0


def main(name: str) -> int:
    global STREAM
    argv = sys.argv[1:]
    STREAM = any(argv[i] in ("--output-format", "-o") and argv[i + 1] == "stream-json" for i in range(len(argv) - 1))
    if "--version" in argv or "-v" in argv:
        print(f"{name} fake 0.0.0")
        return 0
    if name == "codex" and argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT")
        return 0
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode("utf-8", "replace")
    log_call(name, argv, stdin)
    sc = scenario(name)
    time.sleep(float(sc.get("delay", 0) or 0))
    mode = str(sc.get("mode", "ok"))
    model = "fake-model"
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]
    elif "-m" in argv:
        model = argv[argv.index("-m") + 1]
    if mode in ("quota", "rate_limit", "auth", "ineligible", "unavailable"):
        out, err = error_output(name, mode)
        if out:
            print(out)
        print(err, file=sys.stderr)
        return 1
    if mode == "denied":
        denied_payload: dict[str, object] = {
            "status": "SUCCESS",
            "response": "",
            "denied_actions": [{"tool": "run_shell_command", "command": "python -m unittest"}],
        }
        if "usage" in sc:
            denied_payload["usage"] = sc["usage"]
        print(json.dumps(denied_payload))
        return 0
    if mode == "orchestrate":
        return orchestrate(name, sc)
    if mode == "timeout":
        time.sleep(float(sc.get("sleep", 30)))
        return 0
    if mode == "garbage":
        print("<<esto no es JSON>>")
        return 0
    emit_steps(name, sc)
    for rel, content in dict(sc.get("write", {}) or {}).items():  # type: ignore[union-attr]
        target = Path(os.getcwd()) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    if mode == "fail":
        print(output(name, str(sc.get("summary", "no he podido completar la tarea")), model))
        return 1
    raw = output(name, str(sc.get("summary", "hecho")), model)
    if "usage" in sc:
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) > 1:
            try:
                payload = json.loads(lines[-1])
                if isinstance(payload, dict):
                    if name == "agy" and STREAM and isinstance(payload.get("result"), dict):
                        payload["result"]["usage"] = sc["usage"]
                    else:
                        payload["usage"] = sc["usage"]
                    lines[-1] = json.dumps(payload)
                    raw = "\n".join(lines)
            except (json.JSONDecodeError, ValueError):
                pass
        else:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    payload["usage"] = sc["usage"]
                    raw = json.dumps(payload)
            except (json.JSONDecodeError, ValueError):
                pass
    print(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main(str(globals().get("FAKE_NAME") or Path(sys.argv[0]).name.split(".")[0])))
