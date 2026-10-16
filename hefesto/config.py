"""Configuración en capas: TOML por defecto → TOML global → settings global → TOML del proyecto → settings del proyecto.

Hefesto lee TOML (lo edita el usuario) y escribe solo `settings.json`, nunca TOML.
"""

from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hefesto import __version__
from hefesto.util import (
    EXIT_INPUT,
    FRAMEWORK_ROOT,
    HefestoError,
    atomic_write_json,
    hefesto_home,
    read_json,
)

ROLES = ("core", "runner", "forge", "visual", "reviewer")
PROVIDERS = ("claude", "codex", "gemini", "local")
EXTERNAL_PROVIDERS = ("codex", "gemini", "local")
ORCHESTRATORS = ("claude", "codex", "gemini")

# Tablas cuyas claves son libres (nombres de modelo o de proveedor); el valor debe ser del tipo indicado.
FREE_TABLES: dict[tuple[str, ...], type] = {
    ("quota", "max_calls_per_provider_per_loop"): int,
    ("quota", "near_limit_tokens"): int,
    ("limits", "max_parallel_per_model"): int,
    ("limits", "packet_max_tokens"): int,
    ("reviewer", "tiers"): int,
}

ENUMS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("orchestrator", "default"): ORCHESTRATORS + ("auto",),
    ("adapters", "gemini", "binary"): ("auto", "gemini", "agy"),
    ("limits", "stop_policy"): ("finish_running", "cancel_running"),
    ("execution", "branch_mode"): ("phase", "task"),
    ("execution", "isolation"): ("auto", "branch", "worktree"),
    ("execution", "finish_strategy"): ("squash", "merge"),
}

POSITIVE_INTS: tuple[tuple[str, ...], ...] = (
    ("limits", "max_parallel_tasks"),
    ("limits", "max_attempts"),
    ("limits", "max_tasks_per_loop"),
    ("limits", "max_turns_per_task"),
    ("limits", "timeout_seconds"),
    ("limits", "verify_timeout_seconds"),
    ("limits", "orchestrator_timeout_seconds"),
    ("quota", "cooldown_default_minutes"),
    ("quota", "probe_max_age_minutes"),
    ("guard", "max_file_mb"),
    ("packs_context", "query_budget"),
)

DEFAULTS_PATH = FRAMEWORK_ROOT / "hefesto.toml"


def global_toml_path() -> Path:
    return hefesto_home() / "hefesto.toml"


def global_settings_path() -> Path:
    return hefesto_home() / "settings.json"


def project_toml_path(project: Path) -> Path:
    return project / ".hefesto" / "hefesto.toml"


def project_settings_path(project: Path) -> Path:
    return project / ".hefesto" / "settings.json"


@dataclass
class Layer:
    name: str
    path: Path
    data: dict[str, Any]


@dataclass
class Config:
    data: dict[str, Any]
    layers: list[Layer] = field(default_factory=list)
    project: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def role_chain(self, role: str) -> list[str]:
        return list(self.get("roles", role, "use", default=[]))

    def approved_providers(self) -> list[str]:
        """Proveedores externos autorizados en este proyecto. Claude siempre lo está."""
        return ["claude"] + [p for p in self.settings.get("approved_external_providers", []) if p in EXTERNAL_PROVIDERS]

    def origin(self, *keys: str) -> str:
        found = "default"
        for layer in self.layers:
            node: Any = layer.data
            ok = True
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    ok = False
                    break
                node = node[key]
            if ok:
                found = layer.name
        return found


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise HefestoError(f"TOML mal formado en {path}: {exc}", EXIT_INPUT) from exc


def _load_settings(path: Path) -> dict[str, Any]:
    import json

    try:
        data = read_json(path, default={})
    except json.JSONDecodeError as exc:
        raise HefestoError(f"JSON mal formado en {path}: {exc}", EXIT_INPUT) from exc
    if not isinstance(data, dict):
        raise HefestoError(f"{path} debe contener un objeto JSON.", EXIT_INPUT)
    return data


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(project: Path | None = None, validate_layers: bool = True) -> Config:
    layers: list[Layer] = [Layer("default", DEFAULTS_PATH, _load_toml(DEFAULTS_PATH))]
    gtoml = global_toml_path()
    if gtoml.exists() and gtoml.resolve() != DEFAULTS_PATH.resolve():
        layers.append(Layer("global", gtoml, _load_toml(gtoml)))
    gsettings = _load_settings(global_settings_path())
    if gsettings.get("config"):
        layers.append(Layer("global-settings", global_settings_path(), gsettings["config"]))
    psettings: dict[str, Any] = {}
    if project is not None:
        ptoml = project_toml_path(project)
        if ptoml.exists():
            layers.append(Layer("project", ptoml, _load_toml(ptoml)))
        psettings = _load_settings(project_settings_path(project))
        if psettings.get("config"):
            layers.append(Layer("project-settings", project_settings_path(project), psettings["config"]))
    data: dict[str, Any] = {}
    for layer in layers:
        data = deep_merge(data, layer.data)
    cfg = Config(data=data, layers=layers, project=project, settings=psettings)
    if validate_layers:
        errors = validate(cfg)
        if errors:
            raise HefestoError("Configuración inválida:\n  - " + "\n  - ".join(errors), EXIT_INPUT)
    return cfg


def _check_unknown(node: dict[str, Any], schema: dict[str, Any], prefix: tuple[str, ...], errors: list[str], where: str) -> None:
    for key, value in node.items():
        path = prefix + (key,)
        dotted = ".".join(path)
        if prefix in FREE_TABLES:
            expected = FREE_TABLES[prefix]
            if not isinstance(value, expected) or isinstance(value, bool):
                errors.append(f"{where}: {dotted} debe ser {expected.__name__}")
            elif expected is int and value < 0:
                errors.append(f"{where}: {dotted} no puede ser negativo")
            continue
        if path in FREE_TABLES:
            if not isinstance(value, dict):
                errors.append(f"{where}: {dotted} debe ser una tabla")
            else:
                _check_unknown(value, {}, path, errors, where)
            continue
        if key not in schema:
            errors.append(f"{where}: clave desconocida `{dotted}`")
            continue
        ref = schema[key]
        if isinstance(ref, dict):
            if not isinstance(value, dict):
                errors.append(f"{where}: `{dotted}` debe ser una tabla")
            else:
                _check_unknown(value, ref, path, errors, where)
        elif isinstance(ref, bool):
            if not isinstance(value, bool):
                errors.append(f"{where}: `{dotted}` debe ser booleano")
        elif isinstance(ref, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{where}: `{dotted}` debe ser numérico")
            elif value < 0:
                errors.append(f"{where}: `{dotted}` no puede ser negativo")
        elif isinstance(ref, str):
            if not isinstance(value, str):
                errors.append(f"{where}: `{dotted}` debe ser texto")
        elif isinstance(ref, list):
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                errors.append(f"{where}: `{dotted}` debe ser una lista de textos")


def _chain_errors(chain: Any, label: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(chain, list) or not chain:
        return [f"{label} debe ser una lista no vacía"]
    for entry in chain:
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"{label}: entrada inválida {entry!r}")
            continue
        provider = entry.split(":", 1)[0]
        if provider not in PROVIDERS:
            errors.append(f"{label}: proveedor desconocido `{provider}` en `{entry}`")
    return errors


def validate(cfg: Config) -> list[str]:
    errors: list[str] = []
    schema = _load_toml(DEFAULTS_PATH)
    schema.setdefault("quota", {})
    for layer in cfg.layers[1:]:
        _check_unknown(layer.data, schema, (), errors, str(layer.path))
    data = cfg.data
    if data.get("schema_version") != 1:
        errors.append("schema_version debe ser 1")
    for role in ROLES:
        errors += _chain_errors(cfg.get("roles", role, "use"), f"roles.{role}.use")
    errors += _chain_errors(cfg.get("orchestrator", "use"), "orchestrator.use")
    for entry in cfg.get("orchestrator", "use", default=[]) or []:
        if isinstance(entry, str) and entry.split(":", 1)[0] not in ORCHESTRATORS:
            errors.append(f"orchestrator.use: `{entry}` no puede orquestar")
    for keys, allowed in ENUMS.items():
        value = cfg.get(*keys)
        if value not in allowed:
            errors.append(f"{'.'.join(keys)} = {value!r}; valores válidos: {', '.join(allowed)}")
    for keys in POSITIVE_INTS:
        value = cfg.get(*keys)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"{'.'.join(keys)} debe ser un entero ≥ 1")
    threshold = cfg.get("quota", "near_limit_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        errors.append("quota.near_limit_threshold debe estar en (0, 1]")
    if cfg.get("adapters", "local", "enabled"):
        if not cfg.get("adapters", "local", "base_url"):
            errors.append("adapters.local está activo pero falta base_url")
        if not cfg.get("adapters", "local", "model"):
            errors.append("adapters.local está activo pero falta model")
    for role in ROLES:
        chain = cfg.get("roles", role, "use") or []
        enabled = [e for e in chain if isinstance(e, str) and cfg.get("adapters", e.split(":", 1)[0], "enabled")]
        if chain and not enabled:
            errors.append(f"roles.{role}.use no tiene ningún adaptador habilitado")
    approved = cfg.settings.get("approved_external_providers", [])
    if not isinstance(approved, list) or any(p not in EXTERNAL_PROVIDERS for p in approved):
        errors.append(f"settings.approved_external_providers solo admite {', '.join(EXTERNAL_PROVIDERS)}")
    if not isinstance(cfg.get("trust", "auto_safe", default=True), bool):
        errors.append("trust.auto_safe debe ser booleano")
    extra = cfg.get("trust", "extra_safe_prefixes", default=[])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        errors.append("trust.extra_safe_prefixes debe ser una lista de textos")
    wt_links = cfg.get("execution", "worktree_links", default=[])
    if not isinstance(wt_links, list) or not all(isinstance(x, str) for x in wt_links):
        errors.append("execution.worktree_links debe ser una lista de textos")
    else:
        for entry in wt_links:
            parts = [p for p in entry.replace("\\", "/").strip("/").split("/") if p]
            if (not entry.strip() or entry.startswith(("/", "\\"))
                    or (len(entry) > 1 and entry[1] == ":") or Path(entry).is_absolute()
                    or not parts or any(p.casefold() in (".", "..", ".git", ".hefesto") for p in parts)):
                errors.append(
                    f"execution.worktree_links: ruta inválida o insegura `{entry}` "
                    "(debe ser relativa, sin `..`, `.git` ni `.hefesto`)"
                )
    npm_version = cfg.get("skills_sources", "npm_version")
    if not isinstance(npm_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", npm_version):
        errors.append("skills_sources.npm_version debe ser exactamente X.Y.Z (solo dígitos y puntos)")
    trusted = cfg.get("skills_sources", "trusted_owners")
    if trusted is not None and (not isinstance(trusted, list) or not all(isinstance(x, str) for x in trusted)):
        errors.append("skills_sources.trusted_owners debe ser una lista de textos")
    git_base = cfg.get("skills_sources", "git_base")
    if git_base is not None and not isinstance(git_base, str):
        errors.append("skills_sources.git_base debe ser texto")
    sel_enabled = cfg.get("skills_selection", "enabled", default=True)
    if not isinstance(sel_enabled, bool):
        errors.append("skills_selection.enabled debe ser booleano")
    for key in ("max_skills", "min_score", "max_chars_per_skill"):
        val = cfg.get("skills_selection", key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < 0):
            errors.append(f"skills_selection.{key} debe ser un entero ≥ 0")
    for key in ("user_roots", "exclude"):
        val = cfg.get("skills_selection", key)
        if val is not None and (not isinstance(val, list) or not all(isinstance(x, str) for x in val)):
            errors.append(f"skills_selection.{key} debe ser una lista de textos")
    auto_enabled = cfg.get("skills_auto", "enabled", default=True)
    if not isinstance(auto_enabled, bool):
        errors.append("skills_auto.enabled debe ser booleano")
    for key in ("min_installs", "min_stars", "max_per_task", "good_score", "search_ttl_hours"):
        val = cfg.get("skills_auto", key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < 0):
            errors.append(f"skills_auto.{key} debe ser un entero ≥ 0")
    return errors


def flatten(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    out: list[tuple[tuple[str, ...], Any]] = []
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict) and path not in FREE_TABLES:
            out += flatten(value, path)
        else:
            out.append((path, value))
    return out


# ---------------------------------------------------------------------------
# settings.json: lo único que Hefesto escribe.

def read_settings(path: Path) -> dict[str, Any]:
    data = _load_settings(path)
    data.setdefault("schema_version", 1)
    return data


def write_settings(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def init_project_settings(project: Path) -> dict[str, Any]:
    path = project_settings_path(project)
    data = read_settings(path)
    data.setdefault("hefesto_version", __version__)
    data.setdefault("approved_external_providers", [])
    write_settings(path, data)
    return data


def set_setting(path: Path, keys: tuple[str, ...], value: Any) -> None:
    """Fija `config.<keys>` en un settings.json (o lo elimina si value es None)."""
    data = read_settings(path)
    node = data.setdefault("config", {})
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    if value is None:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value
    write_settings(path, data)
