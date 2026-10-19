"""Gestión de proveedores: allowlist del proyecto, estado/cooldowns y el adaptador local."""

from __future__ import annotations

import urllib.error
from pathlib import Path

from hefesto import config, doctor, providers, quota, util
from hefesto.util import EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError


def _settings_path(project: Path, global_scope: bool = False) -> Path:
    return config.global_settings_path() if global_scope else config.project_settings_path(project)


def set_approved(project: Path, name: str, approved: bool) -> list[str]:
    if name not in config.EXTERNAL_PROVIDERS:
        raise HefestoError(f"Solo se autorizan proveedores externos: {', '.join(config.EXTERNAL_PROVIDERS)}.", EXIT_INPUT)
    path = _settings_path(project)
    data = config.read_settings(path)
    current = set(data.get("approved_external_providers", []))
    if approved:
        current.add(name)
    else:
        current.discard(name)
    data["approved_external_providers"] = sorted(current)
    config.write_settings(path, data)
    return data["approved_external_providers"]


def add_or_remove(project: Path, action: str, name: str) -> str:
    util.require_tty(f"hefesto providers {action}")
    if action == "add":
        if name == "local":
            raise HefestoError("El modelo local se autoriza con `hefesto local enable`.", EXIT_INPUT)
        if not util.confirm(f"¿Autorizar que el código de este proyecto se envíe a {name}?", default=True):
            return "Sin cambios."
    approved = set_approved(project, name, action == "add")
    return "Autorizados en este proyecto: " + (", ".join(["claude", *approved]))


def status_table(project: Path) -> str:
    cfg = config.load(project)
    approved = cfg.approved_providers()
    names = ["claude", "codex", "gemini"] + (["local"] if cfg.get("adapters", "local", "enabled") else [])
    rows = [("proveedor", "instalado", "auth", "autorizado", "estado", "hasta", "llamadas(loop)")]
    for name in names:
        binary = providers.binary_for(cfg, name)
        current = quota.provider_state(name)
        rows.append((
            name,
            ("sí (" + Path(binary).name + ")") if binary and name != "local" else ("sí" if binary else "no"),
            doctor.auth_status(name) if binary and name != "local" else "unknown",
            "sí" if name in approved else "no",
            current["status"] if binary else "missing",
            current.get("cooldown_until") or "-",
            str(quota.calls_in_loop(name)),
        ))
        for model in sorted((quota.load()["providers"].get(name) or {}).get("models") or {}):
            model_state = quota.provider_state(name, model=model)
            if model_state.get("status") == "cooldown":
                rows.append((f"  {name}:{model}", "", "", "", "cooldown", model_state.get("cooldown_until") or "-", ""))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)


def local_enable(project: Path, base_url: str, model: str, role: str | None, first: bool, global_scope: bool) -> str:
    util.require_tty("hefesto local enable")
    from hefesto.adapters.local import chat

    print(f"Se hará una llamada mínima a {base_url} (modelo {model}) para comprobar el endpoint.")
    if not util.confirm("¿Continuar?", default=True):
        return "Sin cambios."
    try:
        chat(base_url, model, [{"role": "user", "content": "ping"}], timeout=30, max_tokens=1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HefestoError(f"El endpoint no responde correctamente: {exc}. No se ha activado nada.",
                           EXIT_UNAVAILABLE) from exc
    path = _settings_path(project, global_scope)
    for key, value in (("enabled", True), ("base_url", base_url), ("model", model)):
        config.set_setting(path, ("adapters", "local", key), value)
    set_approved(project, "local", True)
    messages = [f"Modelo local activado en {path}."]
    if role:
        if role not in config.ROLES:
            raise HefestoError(f"Rol desconocido `{role}`.", EXIT_INPUT)
        chain = [e for e in config.load(project).role_chain(role) if providers.split_entry(e)[0] != "local"]
        chain = (["local"] + chain) if first else (chain + ["local"])
        config.set_setting(path, ("roles", role, "use"), chain)
        messages.append(f"roles.{role}.use = {chain}")
    return "\n".join(messages)


def local_disable(project: Path, global_scope: bool) -> str:
    util.require_tty("hefesto local disable")
    path = _settings_path(project, global_scope)
    data = config.read_settings(path)
    roles = ((data.get("config") or {}).get("roles") or {})
    for role, value in list(roles.items()):
        chain = [e for e in value.get("use", []) if providers.split_entry(e)[0] != "local"]
        config.set_setting(path, ("roles", role, "use"), chain or None)
    config.set_setting(path, ("adapters", "local", "enabled"), False)
    set_approved(project, "local", False)
    return "Modelo local desactivado y retirado de la allowlist y de las cadenas de roles."
