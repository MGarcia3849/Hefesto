"""Preflight de cuota antes de delegar (sección 2.2).

1. Estado conocido (gratis): cooldown, auth_error, sin binario, tope de llamadas.
2. Uso expuesto por la CLI (gratis): ninguna CLI instalada lo expone de forma legible → `unknown`.
3. Sonda opcional (una llamada mínima): solo con `quota.probe = true` o `providers check --probe`;
   el resultado se cachea en `~/.hefesto/providers.json` durante `probe_max_age_minutes`.

Una sonda confirma que el proveedor responde *ahora*; no garantiza cuota para toda la tarea.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from hefesto import adapters, config, providers, quota
from hefesto.adapters.base import Call
from hefesto.lock import Lock
from hefesto.util import atomic_write_json, now_iso, parse_iso, state_home, utcnow

PROBE_PROMPT = "Responde únicamente con la palabra: ok"
PROBE_TIMEOUT = 90


@dataclass
class Check:
    provider: str
    available: bool
    reason: str
    usage: str
    probe: str


def cached_probe(provider: str) -> dict[str, object] | None:
    entry = quota.load()["providers"].get(provider) or {}
    probe = entry.get("probe")
    return probe if isinstance(probe, dict) else None


def probe_is_fresh(cfg: config.Config, provider: str) -> bool:
    cached = cached_probe(provider)
    if not cached or not cached.get("at"):
        return False
    age = utcnow() - parse_iso(str(cached["at"]))
    return age < dt.timedelta(minutes=int(cfg.get("quota", "probe_max_age_minutes")))


def probe(project: Path, cfg: config.Config, provider: str) -> dict[str, object]:
    """Una llamada mínima en solo lectura. Aplica cooldown/auth igual que una delegación."""
    from hefesto.workflow import new_run_id

    call = Call(role="reviewer", prompt=PROBE_PROMPT, task_id=None, run_id=f"probe-{new_run_id()}", model="",
                workdir=project, project=project, timeout=PROBE_TIMEOUT)
    result = adapters.get(cfg, provider).run(call)
    if result.error_type in ("quota", "rate_limit"):
        until = quota.cooldown_until(result.reset_hint or result.summary,
                                     int(cfg.get("quota", "cooldown_default_minutes")))
        quota.mark_cooldown(provider, until, result.error_type, result.summary)
    elif result.error_type == "auth":
        quota.mark_auth_error(provider, result.summary)
    record = {"at": now_iso(), "ok": result.success, "error_type": result.error_type}
    with Lock(state_home() / ".lock", timeout=30):
        data = quota.load()
        data["providers"].setdefault(provider, {"status": "ok"})["probe"] = record
        atomic_write_json(quota.providers_path(), data)
    return record


def ensure(project: Path, cfg: config.Config, provider: str) -> tuple[bool, str]:
    """Preflight completo para una delegación: niveles 1 y 3 (si `quota.probe` está activo)."""
    ok, reason = providers.available(cfg, provider)
    if not ok:
        return ok, reason
    from hefesto import limits

    threshold_val = cfg.get("quota", "near_limit_threshold", default=0.9)
    try:
        threshold = float(threshold_val) if threshold_val is not None else 0.9
    except (ValueError, TypeError):
        threshold = 0.9
    try:
        limit_reason = limits.near_limit(provider, threshold)
    except Exception:
        limit_reason = None
    if limit_reason:
        return False, limit_reason
    if not cfg.get("quota", "probe"):
        return ok, reason
    if not probe_is_fresh(cfg, provider):
        record = probe(project, cfg, provider)
        if not record["ok"]:
            return False, f"la sonda falló ({record.get('error_type') or 'error'})"
    elif not (cached_probe(provider) or {}).get("ok"):
        return False, "la última sonda (reciente) falló"
    return True, "ok"


def check_all(project: Path, force_probe: bool) -> list[Check]:
    cfg = config.load(project)
    names = ["claude", "codex", "gemini"] + (["local"] if cfg.get("adapters", "local", "enabled") else [])
    out: list[Check] = []
    for name in names:
        ok, reason = providers.available(cfg, name)
        probe_txt = "no"
        if ok and (force_probe or (cfg.get("quota", "probe") and not probe_is_fresh(cfg, name))):
            record = probe(project, cfg, name)
            probe_txt = "ok" if record["ok"] else f"falló ({record.get('error_type')})"
            ok, reason = providers.available(cfg, name)
            ok = ok and bool(record["ok"])
        elif cached_probe(name):
            cached = cached_probe(name) or {}
            probe_txt = f"caché {'ok' if cached.get('ok') else 'fallida'} ({cached.get('at')})"
        out.append(Check(name, ok, reason, "unknown", probe_txt))
    return out


def render(checks: list[Check]) -> str:
    rows = [("proveedor", "disponible", "motivo", "uso (CLI)", "sonda")]
    rows += [(c.provider, "sí" if c.available else "no", c.reason, c.usage, c.probe) for c in checks]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    table = "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return table + ("\n\nUna sonda confirma que el proveedor responde ahora; no garantiza cuota para toda la tarea.\n"
                    "`uso (CLI)` es `unknown` porque ninguna CLI instalada expone su cuota restante.")
