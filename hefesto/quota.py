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


def usage_path() -> Path:
    return state_home() / "usage.json"


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


def _load_usage() -> dict[str, Any]:
    data = read_json(usage_path(), default=None)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("usage"), dict):
        data["usage"] = {}
    if not isinstance(data.get("learned_limits"), dict):
        data["learned_limits"] = {}
    return data


def _as_utc(value: dt.datetime | None) -> dt.datetime:
    value = value or utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _usage_total(rows: Any, model_id: str, since: dt.datetime, now: dt.datetime) -> int:
    total = 0
    if not isinstance(rows, list):
        return total
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            continue
        timestamp, tokens = row
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0 or not isinstance(timestamp, str):
            continue
        try:
            recorded = _as_utc(parse_iso(timestamp))
        except (TypeError, ValueError, OverflowError):
            continue
        if since <= recorded <= now:
            total += tokens
    return total


def record_usage(model_id: str, tokens: int, at: dt.datetime | None = None) -> None:
    """Registra tokens consumidos por modelo y conserva como máximo ocho días."""
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0 or not model_id:
        return
    recorded_at = _as_utc(at)
    cutoff = recorded_at - dt.timedelta(days=8)
    with _lock():
        data = _load_usage()
        usage = data["usage"]
        for key, rows in list(usage.items()):
            if not isinstance(rows, list):
                usage.pop(key, None)
                continue
            retained = []
            for row in rows:
                if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str):
                    continue
                try:
                    when = _as_utc(parse_iso(row[0]))
                except (TypeError, ValueError, OverflowError):
                    continue
                if when >= cutoff:
                    retained.append(row)
            usage[key] = retained
        usage.setdefault(model_id, []).append([recorded_at.isoformat(), tokens])
        atomic_write_json(usage_path(), data)


def usage_window(model_id: str, hours: float, now: dt.datetime | None = None) -> int:
    """Suma el uso de un modelo dentro de las últimas `hours` horas."""
    try:
        window = float(hours)
        if not model_id or not window > 0:
            return 0
        end = _as_utc(now)
        start = end - dt.timedelta(hours=window)
        with _lock():
            data = _load_usage()
        return _usage_total(data["usage"].get(model_id), model_id, start, end)
    except (OSError, TypeError, ValueError, OverflowError):
        return 0


def _since_last_limit(data: dict[str, Any], model_id: str, now: dt.datetime) -> dt.datetime:
    """Inicio de la ventana: hace 7 días, o el último error de cuota si es posterior (el reinicio vino después)."""
    start = now - dt.timedelta(hours=168)
    raw = (data.get("learned_at") or {}).get(model_id)
    try:
        last = _as_utc(parse_iso(raw)) if isinstance(raw, str) else None
    except (TypeError, ValueError, OverflowError):
        last = None
    return max(start, last) if last else start


def usage_since_limit(model_id: str, now: dt.datetime | None = None) -> int:
    """Tokens consumidos desde el último error de cuota del modelo (como mucho, 7 días)."""
    end = _as_utc(now)
    with _lock():
        data = _load_usage()
    return _usage_total(data["usage"].get(model_id), model_id, _since_last_limit(data, model_id, end), end)


def learned_limit(model_id: str) -> int | None:
    try:
        with _lock():
            value = _load_usage()["learned_limits"].get(model_id)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def _entry(data: dict[str, Any], provider: str) -> dict[str, Any]:
    return data["providers"].setdefault(provider, {"status": "ok", "cooldown_until": None,
                                                    "last_error_type": None, "last_error": None})


def _effective(entry: dict[str, Any]) -> dict[str, Any]:
    state = dict(entry)
    until = state.get("cooldown_until")
    if state.get("status") == "cooldown" and until and parse_iso(until) <= utcnow():
        state["status"] = "ok"
        state["cooldown_until"] = None
    return state


def provider_state(provider: str, model: str | None = None) -> dict[str, Any]:
    """Estado efectivo; auth y cooldown de proveedor prevalecen sobre el modelo."""
    entry = _entry(load(), provider)
    provider_status = _effective(entry)
    if provider_status.get("status") in ("cooldown", "auth_error") or not model:
        return provider_status
    model_state = (entry.get("models") or {}).get(model)
    if not isinstance(model_state, dict):
        return {"status": "ok", "cooldown_until": None, "last_error_type": None, "last_error": None}
    return _effective(model_state)


def _update(provider: str, **fields: Any) -> dict[str, Any]:
    with _lock():
        data = load()
        entry = _entry(data, provider)
        entry.update(fields)
        entry["updated_at"] = now_iso()
        atomic_write_json(providers_path(), data)
        return entry


def mark_cooldown(provider: str, until: dt.datetime, error_type: str, message: str,
                  model: str | None = None) -> dict[str, Any]:
    fields = {"status": "cooldown", "cooldown_until": until.replace(microsecond=0).isoformat(),
              "last_error_type": error_type, "last_error": message[:300]}
    if model is None:
        return _update(provider, **fields)
    learned = usage_since_limit(f"{provider}:{model}") if error_type == "quota" else None
    with _lock():
        data = load()
        entry = _entry(data, provider)
        entry.setdefault("models", {})[model] = fields
        entry["updated_at"] = now_iso()
        atomic_write_json(providers_path(), data)
        if learned is not None:
            usage_data = _load_usage()
            if learned > 0:
                usage_data["learned_limits"][f"{provider}:{model}"] = learned
            usage_data.setdefault("learned_at", {})[f"{provider}:{model}"] = now_iso()
            atomic_write_json(usage_path(), usage_data)
        return dict(fields)


def mark_auth_error(provider: str, message: str) -> dict[str, Any]:
    return _update(provider, status="auth_error", cooldown_until=None, last_error_type="auth", last_error=message[:300])


def mark_ok(provider: str, model: str | None = None) -> None:
    if model is not None:
        with _lock():
            data = load()
            entry = _entry(data, provider)
            models = entry.get("models") or {}
            if model in models:
                models.pop(model, None)
                entry["updated_at"] = now_iso()
                atomic_write_json(providers_path(), data)
        return
    current = provider_state(provider)
    if current.get("status") != "ok" or current.get("cooldown_until"):
        _update(provider, status="ok", cooldown_until=None)


def reset(provider: str) -> dict[str, Any]:
    with _lock():
        data = load()
        entry = _entry(data, provider)
        entry.update(status="ok", cooldown_until=None, last_error_type=None, last_error=None)
        entry.pop("models", None)
        entry["updated_at"] = now_iso()
        atomic_write_json(providers_path(), data)
        return entry


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
_PART = r"\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)(?![a-z])"
_COMPOUND = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)", re.I)
_IN = re.compile(r"(?:try again|retry|resets?|available again)\s+(?:in|after)\s+(?:about\s+)?"
                 rf"((?:{_PART}\s*)+)", re.I)
_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_reset(message: str, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or utcnow()
    match = _EPOCH.search(message)
    if match:
        return dt.datetime.fromtimestamp(int(match.group(1)), dt.timezone.utc)
    match = _IN.search(message)
    if match:
        seconds = sum(float(part.group("value")) * _UNITS[part.group("unit").lower()[0]]
                      for part in _COMPOUND.finditer(match.group(1)))
        return now + dt.timedelta(seconds=seconds)
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
