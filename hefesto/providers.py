"""Identidad y disponibilidad de proveedores (binario, habilitado, autorizado en el proyecto)."""

from __future__ import annotations

from hefesto import config
from hefesto.util import which


def split_entry(entry: str) -> tuple[str, str]:
    provider, _, model = entry.partition(":")
    return provider.strip(), model.strip()


def model_id(cfg: config.Config, entry: str) -> str:
    """Identificador estable `proveedor:modelo` (modelo vacío → el configurado o `default`)."""
    provider, model = split_entry(entry)
    if not model:
        model = str(cfg.get("adapters", provider, "model", default="") or "") or "default"
    return f"{provider}:{model}"


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


def available(cfg: config.Config, provider: str) -> tuple[bool, str]:
    """Disponible para recibir trabajo ahora."""
    return static_available(cfg, provider)


def tier(cfg: config.Config, mid: str) -> int | None:
    tiers = cfg.get("reviewer", "tiers", default={}) or {}
    value = tiers.get(mid)
    return int(value) if isinstance(value, int) else None
