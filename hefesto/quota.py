"""Estado global de proveedores (`~/.hefesto/providers.json`): cooldowns, errores de auth y llamadas por loop.

La cuota es de la cuenta, no del proyecto: por eso este estado es global y tiene su propio lock.
Hefesto no estima cuota restante: si una CLI no la expone, el dato es `unknown`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from hefesto.lock import Lock
from hefesto.util import atomic_write_json, now_iso, parse_iso, read_json, state_home, utcnow

STATUSES = ("ok", "cooldown", "auth_error", "missing")
_THREAD_LOCK = threading.Lock()


def providers_path() -> Path:
    return state_home() / "providers.json"


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    # Evita que un hilo lea owner mientras otro lo elimina (sharing violation
    # en Windows). El lock de disco sigue protegiendo frente a otros procesos.
    with _THREAD_LOCK, Lock(state_home() / ".lock", timeout=30):
        yield


def load() -> dict[str, Any]:
    data = read_json(providers_path(), default=None) or {}
    data.setdefault("schema_version", 1)
    data.setdefault("providers", {})
    data.setdefault("loop", {"id": None, "calls": {}})
    return data


def _entry(data: dict[str, Any], provider: str) -> dict[str, Any]:
    return data["providers"].setdefault(provider, {"status": "ok", "cooldown_until": None,
                                                    "last_error_type": None, "last_error": None})


def provider_state(provider: str) -> dict[str, Any]:
    """Estado efectivo: un cooldown vencido vuelve a `ok` sin intervención."""
    entry = dict(_entry(load(), provider))
    until = entry.get("cooldown_until")
    if entry.get("status") == "cooldown" and until and parse_iso(until) <= utcnow():
        entry["status"] = "ok"
        entry["cooldown_until"] = None
    return entry


def _update(provider: str, **fields: Any) -> dict[str, Any]:
    with _lock():
        data = load()
        entry = _entry(data, provider)
        entry.update(fields)
        entry["updated_at"] = now_iso()
        atomic_write_json(providers_path(), data)
        return entry


def mark_cooldown(provider: str, until: dt.datetime, error_type: str, message: str) -> dict[str, Any]:
    return _update(provider, status="cooldown", cooldown_until=until.replace(microsecond=0).isoformat(),
                   last_error_type=error_type, last_error=message[:300])


def mark_auth_error(provider: str, message: str) -> dict[str, Any]:
    return _update(provider, status="auth_error", cooldown_until=None, last_error_type="auth", last_error=message[:300])


def mark_ok(provider: str) -> None:
    current = provider_state(provider)
    if current.get("status") != "ok" or current.get("cooldown_until"):
        _update(provider, status="ok", cooldown_until=None)


def reset(provider: str) -> dict[str, Any]:
    return _update(provider, status="ok", cooldown_until=None, last_error_type=None, last_error=None)


def record_call(provider: str) -> int:
    """Cuenta una llamada en el loop actual. Fuera de un loop no hay tope que contar."""
    with _lock():
        data = load()
        loop = data["loop"]
        if not loop.get("id"):
            return 0
        calls = loop.setdefault("calls", {})
        calls[provider] = int(calls.get(provider, 0)) + 1
        atomic_write_json(providers_path(), data)
        return calls[provider]


def calls_in_loop(provider: str) -> int:
    loop = load()["loop"]
    return int((loop.get("calls") or {}).get(provider, 0)) if loop.get("id") else 0


def start_loop(loop_id: str) -> None:
    with _lock():
        data = load()
        data["loop"] = {"id": loop_id, "calls": {}, "started_at": now_iso()}
        atomic_write_json(providers_path(), data)


def end_loop() -> None:
    with _lock():
        data = load()
        data["loop"] = {"id": None, "calls": {}}
        atomic_write_json(providers_path(), data)


# ---------------------------------------------------------------------------
# Hora de reinicio a partir de los mensajes de las CLIs.

_EPOCH = re.compile(r"\|(\d{10})\b")
_IN = re.compile(r"(?:try again|retry|resets?|available again)\s+(?:in|after)\s+(?:about\s+)?(\d+(?:\.\d+)?)\s*"
                 r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", re.I)
_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_reset(message: str, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or utcnow()
    match = _EPOCH.search(message)
    if match:
        return dt.datetime.fromtimestamp(int(match.group(1)), dt.timezone.utc)
    match = _IN.search(message)
    if match:
        unit = match.group(2).lower()[0]
        return now + dt.timedelta(seconds=float(match.group(1)) * _UNITS[unit])
    match = _AT.search(message)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
        minute = int(match.group(2) or 0)
        local_now = now.astimezone()
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += dt.timedelta(days=1)
        return candidate.astimezone(dt.timezone.utc)
    return None


def cooldown_until(message: str, default_minutes: int) -> dt.datetime:
    return parse_reset(message) or utcnow() + dt.timedelta(minutes=default_minutes)
