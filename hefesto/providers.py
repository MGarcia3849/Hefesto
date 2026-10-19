"""Identidad y disponibilidad de proveedores (binario, habilitado, autorizado en el proyecto)."""

from __future__ import annotations

from hefesto import config, util
from hefesto.util import which

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def split_entry(entry: str) -> tuple[str, str]:
    provider, _, model = entry.partition(":")
    if provider.strip() == "codex" and model.count(":") == 1:
        model = model.split(":", 1)[0]
    return provider.strip(), model.strip()


def entry_effort(entry: str) -> str | None:
    """Nivel de razonamiento opcional de una entrada Codex."""
    provider, separator, remainder = entry.partition(":")
    if provider.strip() != "codex" or not separator or remainder.count(":") != 1:
        return None
    return remainder.rsplit(":", 1)[1].strip()


def model_id(cfg: config.Config, entry: str) -> str:
    """Identificador estable `proveedor:modelo` (modelo vacío → el configurado o `default`)."""
    provider, model = split_entry(entry)
    if not model:
        model = str(cfg.get("adapters", provider, "model", default="") or "") or "default"
    effort = entry_effort(entry)
    if effort is not None:
        model = f"{model}:{effort}"
    return f"{provider}:{model}"


def entry_model(cfg: config.Config, entry: str) -> str:
    """Modelo efectivo de la entrada sin el proveedor: la clave de su cooldown."""
    provider, model = split_entry(entry)
    return model or str(cfg.get("adapters", provider, "model", default="") or "default")


def binary_for(cfg: config.Config, provider: str) -> str | None:
    """Ruta del ejecutable del proveedor, o None. `local` no usa binario (devuelve su URL)."""
    if provider == "local":
        return str(cfg.get("adapters", "local", "base_url") or "") or None
    if provider == "gemini":
        choice = cfg.get("adapters", "gemini", "binary", default="auto")
        names = ["gemini", "agy"] if choice == "auto" else [choice]
        for name in names:
            path = which(name)
            if path:
                return path
        return None
    return which(provider)


def static_available(cfg: config.Config, provider: str) -> tuple[bool, str]:
    """Disponible sin mirar la cuota: habilitado, autorizado e instalado."""
    if not cfg.get("adapters", provider, "enabled", default=False):
        return False, "desactivado"
    if provider not in cfg.approved_providers():
        return False, "no autorizado en este proyecto"
    if binary_for(cfg, provider) is None:
        return False, "no instalado" if provider != "local" else "sin base_url"
    return True, "ok"


def available(cfg: config.Config, provider: str, model: str | None = None) -> tuple[bool, str]:
    """Disponible para recibir trabajo ahora: estático + sin cooldown, sin error de auth y bajo su tope."""
    from hefesto import limits, quota

    ok, reason = static_available(cfg, provider)
    if not ok:
        return ok, reason
    current = quota.provider_state(provider, model=model)
    if current["status"] == "cooldown":
        return False, f"en cooldown hasta {current['cooldown_until']}"
    if current["status"] == "auth_error":
        return False, "error de autenticación (inicia sesión y `hefesto providers reset " + provider + "`)"
    cap = (cfg.get("quota", "max_calls_per_provider_per_loop", default={}) or {}).get(provider)
    effective_cap = cap
    if isinstance(cap, int) and cfg.get("quota", "adaptive_caps", default=False):
        effective_cap = limits.adaptive_cap(provider, cap, util.utcnow())
    if isinstance(effective_cap, int) and quota.calls_in_loop(provider) >= effective_cap:
        return False, f"tope de {effective_cap} llamadas en este loop alcanzado"
    threshold_val = cfg.get("quota", "near_limit_threshold", default=0.9)
    threshold = float(threshold_val) if threshold_val is not None else 0.9
    limit_reason = limits.near_limit(provider, threshold, model=model)
    if limit_reason:
        return False, limit_reason
    return True, "ok"


def tier(cfg: config.Config, mid: str) -> int | None:
    tiers = cfg.get("reviewer", "tiers", default={}) or {}
    value = tiers.get(mid)
    return int(value) if isinstance(value, int) else None
