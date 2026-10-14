"""Catálogo curado de packs externos y gestión de `hefesto pack`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
import unicodedata
from pathlib import Path
from typing import Any

from hefesto import config, guard, skills, util
from hefesto.util import (
    EXIT_BLOCKED,
    EXIT_INPUT,
    EXIT_UNAVAILABLE,
    FRAMEWORK_ROOT,
    HefestoError,
)

CATALOG_PATH: Path = FRAMEWORK_ROOT / "catalog" / "packs.toml"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))")


class PackResult(str):
    has_conflicts: bool = False


def is_pack_enabled(project: Path, name: str) -> bool:
    """Devuelve True si el pack indicado está habilitado en el proyecto."""
    try:
        settings = config.read_settings(config.project_settings_path(project))
        return bool(settings.get("packs", {}).get(name, {}).get("enabled", False))
    except Exception:
        return False


def run_graphify(cwd: Path, argv: list[str], timeout: int | None = None) -> tuple[int, str]:
    """Ejecuta graphify con entorno seguro, timeout y argumentos en lista.

    Devuelve (código, salida_o_aviso). Nunca lanza excepción; si graphify no está o falla,
    devuelve un aviso y un código de salida distinto de cero.
    """
    if any(cmd in argv for cmd in ("install", "hook")):
        return 1, "Aviso: subcomando de graphify no permitido."

    binary = util.which("graphify")
    if not binary:
        return 1, "Aviso: graphify no está disponible en el PATH."

    if timeout is None:
        timeout = 60 if (argv and argv[0] == "query") else 300

    env = util.filtered_env(extra={"GRAPHIFY_QUERY_LOG_DISABLE": "1"})

    try:
        proc = subprocess.run(
            [binary, *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            return proc.returncode, f"Aviso: graphify {' '.join(argv)} falló (exit {proc.returncode}): {err}"
        return 0, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 1, f"Aviso: graphify {' '.join(argv)} superó el tiempo límite ({timeout}s)."
    except Exception as exc:
        return 1, f"Aviso: error al ejecutar graphify: {exc}"


def normalize_query(text: str) -> str:
    """Normaliza el texto de consulta para `graphify query`:
    sin caracteres de control, recortado a 300 caracteres y, si empieza por guion,
    se le antepone la palabra 'tarea' y un espacio, de modo que ningún argumento
    de consulta empiece por guion.
    """
    if not text:
        return ""
    stripped = ANSI_RE.sub("", text)
    cleaned = "".join(
        c for c in stripped
        if not (unicodedata.category(c).startswith("C") or ord(c) < 32 or (127 <= ord(c) <= 159))
    ).strip()
    if cleaned.startswith("-"):
        cleaned = f"tarea {cleaned}"
    if len(cleaned) > 300:
        cleaned = cleaned[:300].strip()
    if cleaned.startswith("-"):
        cleaned = f"tarea {cleaned}"
    return cleaned


def sanitize_graph_extract(text: str) -> str:
    """Pasa el extracto del grafo por guard.redact y skills.sanitize_text."""
    if not text:
        return ""
    return skills.sanitize_text(guard.redact(text))


def query_graph(project: Path, workdir: Path, task: Any, cfg: config.Config) -> str:
    """Extrae el subgrafo para la tarea dada si el pack context está activo y hay grafo."""
    if task is None:
        return ""
    if not is_pack_enabled(project, "context"):
        return ""
    if not util.which("graphify"):
        return ""
    graph_path = workdir / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        proj_graph = project / "graphify-out" / "graph.json"
        if proj_graph.is_file():
            try:
                dest_dir = workdir / "graphify-out"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(proj_graph, graph_path)
            except Exception:
                return ""
        else:
            return ""

    files_list = getattr(task, "files", []) or []
    files_str = " ".join(files_list)
    title = getattr(task, "title", "")
    query_str = normalize_query(f"{title} {files_str}".strip())
    if not query_str:
        return ""
    budget = int(cfg.get("packs_context", "query_budget", default=1500))
    argv = ["query", query_str, "--budget", str(budget), "--graph", str(graph_path)]
    code, out = run_graphify(workdir, argv, timeout=60)
    if code != 0 or not out:
        return ""
    return sanitize_graph_extract(out.strip())



def load_catalog() -> dict[str, dict[str, Any]]:
    """Carga y valida el catálogo de packs desde CATALOG_PATH.
    
    Lanza HefestoError con código 1 si el catálogo no existe, no es TOML válido
    o contiene claves/tipos incorrectos.
    """
    if not CATALOG_PATH.is_file():
        raise HefestoError(f"No existe el catálogo de packs en {CATALOG_PATH}.", EXIT_INPUT)

    try:
        with open(CATALOG_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise HefestoError(f"Catálogo de packs inválido ({CATALOG_PATH}): {exc}", EXIT_INPUT) from exc

    if not isinstance(data, dict) or "packs" not in data or not isinstance(data["packs"], dict):
        raise HefestoError("El catálogo debe contener una tabla [packs].", EXIT_INPUT)

    packs_table: dict[str, Any] = data["packs"]
    validated: dict[str, dict[str, Any]] = {}

    for name, entry in packs_table.items():
        if not isinstance(entry, dict):
            raise HefestoError(f"Entrada inválida para pack '{name}'.", EXIT_INPUT)

        kind = entry.get("kind")
        if kind not in ("tool", "skills"):
            raise HefestoError(f"Pack '{name}': 'kind' debe ser 'tool' o 'skills', no {kind!r}.", EXIT_INPUT)

        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise HefestoError(f"Pack '{name}': 'source' debe ser un texto no vacío.", EXIT_INPUT)

        license_str = entry.get("license")
        if not isinstance(license_str, str) or not license_str.strip():
            raise HefestoError(f"Pack '{name}': 'license' debe ser un texto no vacío.", EXIT_INPUT)

        default_val = entry.get("default")
        if default_val not in ("recommended", "proposed", "off"):
            raise HefestoError(f"Pack '{name}': 'default' debe ser 'recommended', 'proposed' u 'off'.", EXIT_INPUT)

        extra_cost = entry.get("extra_cost")
        if not isinstance(extra_cost, bool):
            raise HefestoError(f"Pack '{name}': 'extra_cost' debe ser un booleano.", EXIT_INPUT)

        requires = entry.get("requires")
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise HefestoError(f"Pack '{name}': 'requires' debe ser una lista de textos.", EXIT_INPUT)

        if kind == "tool":
            binary = entry.get("binary")
            if not isinstance(binary, str) or not binary.strip():
                raise HefestoError(f"Pack '{name}': 'binary' debe ser un texto no vacío.", EXIT_INPUT)

            version = entry.get("version")
            if not isinstance(version, str) or not VERSION_RE.match(version.strip()):
                raise HefestoError(f"Pack '{name}': 'version' debe tener formato exactamente X.Y.Z.", EXIT_INPUT)

            install = entry.get("install")
            if not isinstance(install, str) or not install.strip():
                raise HefestoError(f"Pack '{name}': 'install' debe ser un texto no vacío.", EXIT_INPUT)
        else:
            skills_list = entry.get("skills")
            if not isinstance(skills_list, list) or not skills_list or not all(isinstance(s, str) for s in skills_list):
                raise HefestoError(f"Pack '{name}': 'skills' debe ser una lista no vacía de nombres de skill.", EXIT_INPUT)

            overlay = entry.get("overlay")
            if not isinstance(overlay, str) or not overlay.strip():
                raise HefestoError(f"Pack '{name}': 'overlay' debe ser un texto no vacío.", EXIT_INPUT)

        if "confirm_changes" in entry:
            cc = entry["confirm_changes"]
            if not isinstance(cc, list) or not all(isinstance(c, str) for c in cc):
                raise HefestoError(f"Pack '{name}': 'confirm_changes' debe ser una lista de textos.", EXIT_INPUT)

        if "policy_block" in entry:
            pb = entry["policy_block"]
            if not isinstance(pb, list) or not all(isinstance(p, str) for p in pb):
                raise HefestoError(f"Pack '{name}': 'policy_block' debe ser una lista de expresiones regulares.", EXIT_INPUT)

        validated[name] = entry

    return validated


def list_packs(project: Path | None = None) -> list[dict[str, Any]]:
    """Devuelve la lista de packs con su estado para el proyecto dado."""
    cat = load_catalog()
    settings: dict[str, Any] = {}
    if project is not None:
        try:
            settings = config.read_settings(config.project_settings_path(project))
        except Exception:
            settings = {}

    packs_settings = settings.get("packs", {}) if isinstance(settings, dict) else {}
    result: list[dict[str, Any]] = []

    for name, entry in cat.items():
        enabled = bool(packs_settings.get(name, {}).get("enabled", False))
        missing: list[str] = []
        if entry.get("kind") == "tool":
            binary = entry.get("binary")
            if binary and not util.which(binary):
                missing.append(binary)

        result.append({
            "name": name,
            "kind": entry.get("kind"),
            "default": entry.get("default"),
            "enabled": enabled,
            "extra_cost": bool(entry.get("extra_cost", False)),
            "missing": missing,
        })

    return result


def enable(project: Path, name: str, yes: bool = False, accept_extra_cost: bool = False) -> str:
    """Activa un pack en el proyecto."""
    cat = load_catalog()
    if name not in cat:
        raise HefestoError(f"Pack desconocido: '{name}'.", EXIT_INPUT)

    entry = cat[name]

    if entry.get("extra_cost") and not accept_extra_cost:
        raise HefestoError(
            f"El pack '{name}' genera costes adicionales fuera de las tres suscripciones.\n"
            f"Para activarlo, usa: hefesto pack enable {name} --accept-extra-cost",
            EXIT_BLOCKED,
        )

    if not yes:
        util.require_tty("hefesto pack enable")
    else:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)

    # Muestra licencia, coste y confirm_changes antes de pedir confirmación
    print(f"Pack: {name}")
    print(f"Licencia: {entry.get('license', 'unknown')}")
    cost_msg = "sí (coste adicional fuera de las suscripciones)" if entry.get("extra_cost") else "no"
    print(f"Coste adicional: {cost_msg}")
    if entry.get("confirm_changes"):
        print("Cambios fuera de Hefesto:")
        for change in entry["confirm_changes"]:
            print(f"  - {change}")

    if not yes:
        if not util.confirm(f"¿Activar el pack '{name}' en el proyecto?", default=False):
            return f"Activación del pack '{name}' cancelada."

    # kind == "tool": verificar binario
    if entry["kind"] == "tool":
        binary = entry.get("binary") or ""
        if not util.which(binary):
            install_cmd = entry.get("install", "")
            raise HefestoError(
                f"El binario '{binary}' no está disponible en el PATH.\n"
                f"Instalación manual (Hefesto nunca la ejecuta automáticamente):\n  {install_cmd}",
                EXIT_UNAVAILABLE,
            )

    # kind == "skills": fetch y promote de cada skill sin preguntar
    if entry["kind"] == "skills":
        source = entry.get("source", "")
        overlay = entry.get("overlay")
        for skill in entry.get("skills", []):
            spec = f"{source}@{skill}"
            proposal_dir = skills.fetch(spec)
            skills.promote(proposal_dir.name, yes=True, pack=name, overlay=overlay)

    # Registrar en settings.json
    settings_path = config.project_settings_path(project)
    settings = config.read_settings(settings_path)
    packs_data = settings.setdefault("packs", {})
    packs_data[name] = {
        "enabled": True,
        "enabled_at": util.now_iso(),
    }
    config.write_settings(settings_path, settings)

    # Si es el pack context, construir el grafo inicial
    if name == "context":
        code, out = run_graphify(project, ["extract", ".", "--code-only"])
        if out:
            print(out)

    # Ejecutar sync
    from hefesto import sync
    changes = sync.run(project)
    print(sync.summary(changes))

    res = PackResult(f"Pack '{name}' activado con éxito en el proyecto.")
    res.has_conflicts = any(c.action == "conflict" for c in changes)
    return res


def disable(project: Path, name: str) -> str:
    """Desactiva un pack en el proyecto."""
    cat = load_catalog()
    if name not in cat:
        raise HefestoError(f"Pack desconocido: '{name}'.", EXIT_INPUT)

    settings_path = config.project_settings_path(project)
    settings = config.read_settings(settings_path)
    packs_data = settings.setdefault("packs", {})
    packs_data.setdefault(name, {})["enabled"] = False
    config.write_settings(settings_path, settings)

    from hefesto import sync
    changes = sync.run(project)
    print(sync.summary(changes))

    res = PackResult(f"Pack '{name}' desactivado en el proyecto.")
    res.has_conflicts = any(c.action == "conflict" for c in changes)
    return res


def update(project: Path, name: str) -> list[Path]:
    """Descarga la última versión de las skills de un pack a propuestas, o informa de la versión fijada."""
    cat = load_catalog()
    if name not in cat:
        raise HefestoError(f"Pack desconocido: '{name}'.", EXIT_INPUT)

    entry = cat[name]
    if entry["kind"] == "skills":
        updated_paths: list[Path] = []
        for skill in entry.get("skills", []):
            path = skills.update(skill)
            updated_paths.append(path)
        return updated_paths
    else:
        version = entry.get("version", "desconocida")
        print(f"Pack tool '{name}': versión fijada en catálogo {version}.")
        return []
