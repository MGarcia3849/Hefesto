"""Adaptadores de proveedores. Todos comparten la interfaz de `base.Adapter`."""

from __future__ import annotations

from hefesto import config
from hefesto.adapters.base import Adapter
from hefesto.adapters.claude import ClaudeAdapter
from hefesto.adapters.codex import CodexAdapter
from hefesto.adapters.gemini import GeminiAdapter
from hefesto.adapters.local import LocalAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "local": LocalAdapter,
}


def get(cfg: config.Config, provider: str) -> Adapter:
    return ADAPTERS[provider](cfg)
