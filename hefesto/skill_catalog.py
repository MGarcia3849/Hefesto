"""Catálogo único de skills y herramientas por rol, modo y tipo de tarea."""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hefesto import config, roadmap, skills, util
from hefesto.util import EXIT_INPUT, FRAMEWORK_ROOT, HefestoError

CATALOG_PATH: Path = FRAMEWORK_ROOT / "catalog" / "skills.toml"

ROLES: tuple[str, ...] = ("core", "runner", "forge", "visual", "reviewer", "verify")

ALLOWED_LICENSES: frozenset[str] = frozenset({
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "0BSD",
    "CC0-1.0",
})

ALLOWED_KEYS: frozenset[str] = frozenset({
    "kind",
    "source",
    "commit",
    "path",
    "license",
    "roles",
    "modes",
    "kinds",
    "interactive",
    "providers",
    "binary",
})

SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
BINARY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    kind: str
    source: str
    license: str
    roles: tuple[str, ...]
    modes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    kinds: tuple[str, ...] = ()
    interactive: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    commit: str = ""
    path: str = ""
    binary: str = ""


def _validate_str_list(name: str, field_name: str, value: Any, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise HefestoError(
            f"Entrada '{name}' en catálogo: '{field_name}' debe ser una lista de cadenas no vacías.",
            EXIT_INPUT,
        )
    if not allow_empty and not value:
        raise HefestoError(
            f"Entrada '{name}' en catálogo: '{field_name}' no puede estar vacío.",
            EXIT_INPUT,
        )
    return tuple(value)


def _validate_entry(name: str, body: Any) -> CatalogEntry:
    if not isinstance(name, str) or not skills.NAME_RE.fullmatch(name) or name in (".", ".."):
        raise HefestoError(f"Entrada '{name}' en catálogo: nombre inválido.", EXIT_INPUT)
    if not isinstance(body, dict):
        raise HefestoError(f"Entrada '{name}' en catálogo: debe ser una tabla TOML.", EXIT_INPUT)

    unknown = set(body) - ALLOWED_KEYS
    if unknown:
        keys_str = ", ".join(sorted(unknown))
        raise HefestoError(
            f"Entrada '{name}' en catálogo: claves desconocidas o no permitidas ({keys_str}).",
            EXIT_INPUT,
        )

    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in ("skill", "tool"):
        raise HefestoError(
            f"Entrada '{name}' en catálogo: 'kind' debe ser 'skill' o 'tool' (recibido: {kind!r}).",
            EXIT_INPUT,
        )

    source = body.get("source")
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise HefestoError(
            f"Entrada '{name}' en catálogo: 'source' debe tener formato 'owner/repo' (recibido: {source!r}).",
            EXIT_INPUT,
        )
    owner, repo = source.split("/", 1)
    if owner in (".", "..") or repo in (".", ".."):
        raise HefestoError(
            f"Entrada '{name}' en catálogo: 'source' inválido ({source!r}).",
            EXIT_INPUT,
        )

    lic = body.get("license")
    if not isinstance(lic, str) or lic not in ALLOWED_LICENSES:
        raise HefestoError(
            f"Entrada '{name}' en catálogo: licencia no permitida ({lic!r}).",
            EXIT_INPUT,
        )

    if "roles" not in body:
        raise HefestoError(f"Entrada '{name}' en catálogo: falta 'roles'.", EXIT_INPUT)
    roles = _validate_str_list(name, "roles", body["roles"], allow_empty=False)
    for role in roles:
        if role not in ROLES:
            raise HefestoError(
                f"Entrada '{name}' en catálogo: rol desconocido '{role}'.",
                EXIT_INPUT,
            )

    kinds: tuple[str, ...] = ()
    if "kinds" in body:
        kinds = _validate_str_list(name, "kinds", body["kinds"], allow_empty=True)
        for k in kinds:
            if k not in roadmap.TASK_KINDS:
                raise HefestoError(
                    f"Entrada '{name}' en catálogo: kind de tarea desconocido '{k}'.",
                    EXIT_INPUT,
                )

    commit = ""
    if "commit" in body:
        raw_commit = body["commit"]
        if not isinstance(raw_commit, str) or not COMMIT_RE.fullmatch(raw_commit):
            raise HefestoError(
                f"Entrada '{name}' en catálogo: 'commit' debe ser un hash SHA-1 de 40 hex en minúsculas.",
                EXIT_INPUT,
            )
        commit = raw_commit
    elif kind == "skill":
        raise HefestoError(
            f"Entrada '{name}' en catálogo: toda skill exige un 'commit' fijado de 40 hex.",
            EXIT_INPUT,
        )

    path_str = ""
    if "path" in body:
        raw_path = body["path"]
        if not isinstance(raw_path, str) or not skills.is_portable_rel_path(raw_path):
            raise HefestoError(
                f"Entrada '{name}' en catálogo: 'path' debe ser una ruta relativa portable ({raw_path!r}).",
                EXIT_INPUT,
            )
        path_str = raw_path

    binary = ""
    if "binary" in body:
        raw_binary = body["binary"]
        if (
            not isinstance(raw_binary, str)
            or not BINARY_RE.fullmatch(raw_binary)
            or raw_binary in (".", "..")
            or "npx" in raw_binary.lower()
        ):
            raise HefestoError(
                f"Entrada '{name}' en catálogo: 'binary' inválido ({raw_binary!r}).",
                EXIT_INPUT,
            )
        binary = raw_binary
    elif kind == "tool":
        raise HefestoError(
            f"Entrada '{name}' en catálogo: toda tool exige un 'binary'.",
            EXIT_INPUT,
        )

    modes: dict[str, tuple[str, ...]] = {}
    if "modes" in body:
        raw_modes = body["modes"]
        if not isinstance(raw_modes, dict):
            raise HefestoError(
                f"Entrada '{name}' en catálogo: 'modes' debe ser una tabla TOML.",
                EXIT_INPUT,
            )
        for role_key, mode_values in raw_modes.items():
            if role_key not in roles:
                raise HefestoError(
                    f"Entrada '{name}' en catálogo: 'modes.{role_key}' referencia un rol que la entrada no tiene.",
                    EXIT_INPUT,
                )
            modes[role_key] = _validate_str_list(
                name, f"modes.{role_key}", mode_values, allow_empty=False
            )

    interactive: tuple[str, ...] = ()
    if "interactive" in body:
        interactive = _validate_str_list(name, "interactive", body["interactive"], allow_empty=True)
        all_modes = {m for ms in modes.values() for m in ms}
        for mode_name in interactive:
            if mode_name not in all_modes:
                raise HefestoError(
                    f"Entrada '{name}' en catálogo: modo interactivo '{mode_name}' no figura en 'modes'.",
                    EXIT_INPUT,
                )

    providers: tuple[str, ...] = ()
    if "providers" in body:
        providers = _validate_str_list(name, "providers", body["providers"], allow_empty=True)
        for prov in providers:
            if prov not in config.PROVIDERS:
                raise HefestoError(
                    f"Entrada '{name}' en catálogo: proveedor desconocido '{prov}'.",
                    EXIT_INPUT,
                )

    return CatalogEntry(
        name=name,
        kind=kind,
        source=source,
        license=lic,
        roles=roles,
        modes=modes,
        kinds=kinds,
        interactive=interactive,
        providers=providers,
        commit=commit,
        path=path_str,
        binary=binary,
    )


def load(path: Path | None = None) -> dict[str, CatalogEntry]:
    """Carga y valida el catálogo de skills y herramientas desde `catalog/skills.toml`."""
    target = Path(path) if path is not None else CATALOG_PATH
    try:
        raw_text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HefestoError(
            f"El catálogo de skills en '{target}' no es UTF-8 válido: {exc}",
            EXIT_INPUT,
        ) from exc
    except OSError as exc:
        raise HefestoError(f"No se pudo leer el catálogo de skills en '{target}': {exc}", EXIT_INPUT) from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise HefestoError(f"TOML inválido en el catálogo de skills '{target}': {exc}", EXIT_INPUT) from exc

    unknown_top = set(data) - {"skills"}
    if unknown_top:
        raise HefestoError(
            f"Catálogo de skills '{target}': secciones desconocidas ({', '.join(sorted(unknown_top))}).",
            EXIT_INPUT,
        )

    skills_table = data.get("skills")
    if not isinstance(skills_table, dict):
        raise HefestoError(
            f"Catálogo de skills '{target}': falta la tabla '[skills]'.",
            EXIT_INPUT,
        )

    entries: dict[str, CatalogEntry] = {}
    for name, body in skills_table.items():
        entries[name] = _validate_entry(name, body)
    return entries


@dataclass
class SyncResult:
    promoted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _remove_proposal(proposal: Path) -> None:
    try:
        skills.check_confinement(proposal, util.hefesto_home())
        shutil.rmtree(proposal, ignore_errors=True)
    except (HefestoError, OSError):
        pass


def sync(catalog: dict[str, CatalogEntry] | None = None) -> SyncResult:
    """Descarga las skills del catálogo en su commit fijado y promueve las limpias."""
    entries = catalog if catalog is not None else load()
    result = SyncResult()

    for entry in entries.values():
        if entry.kind == "tool":
            result.skipped.append((entry.name, "herramienta: se instala aparte, no se descarga"))
            continue

        spec = f"{entry.source}@{entry.name}"
        try:
            proposal = skills.fetch(spec, commit=entry.commit)
        except (HefestoError, OSError) as exc:
            result.rejected.append((entry.name, str(exc)))
            continue

        try:
            source_data = util.read_json(proposal / "SOURCE.json", default={})
            if not isinstance(source_data, dict):
                source_data = {}
            fetched_commit = source_data.get("commit")
            if fetched_commit != entry.commit:
                _remove_proposal(proposal)
                result.rejected.append(
                    (entry.name, "commit distinto del fijado en el catálogo: actualiza el catálogo")
                )
                continue

            risks: list[dict[str, str]] = []
            raw_risks = source_data.get("risks")
            if isinstance(raw_risks, list):
                risks.extend(r_item for r_item in raw_risks if isinstance(r_item, dict))
            _, id_risks = skills.skill_identity((proposal / "SKILL.md").read_bytes(), entry.name)
            for r_item in id_risks:
                if r_item not in risks:
                    risks.append(r_item)
        except (HefestoError, OSError, ValueError) as exc:
            _remove_proposal(proposal)
            result.rejected.append((entry.name, str(exc)))
            continue

        if risks:
            _remove_proposal(proposal)
            kinds = ", ".join(sorted({str(x.get("kind", "riesgo")) for x in risks}))
            result.rejected.append((entry.name, f"riesgos detectados: {kinds}"))
            continue

        scan_fn = getattr(skills, "agentshield_scan", None)
        if callable(scan_fn):
            findings = scan_fn(proposal)
            if isinstance(findings, list):
                high_findings = [
                    f for f in findings
                    if isinstance(f, dict) and str(f.get("severity", "")).lower() in ("high", "critical")
                ]
                if high_findings:
                    _remove_proposal(proposal)
                    severities = ", ".join(sorted({str(f.get("severity", "")).lower() for f in high_findings}))
                    result.rejected.append((entry.name, f"agentshield detectó hallazgos ({severities})"))
                    continue

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                skills.promote(proposal.name, yes=True)
        except (HefestoError, OSError) as exc:
            _remove_proposal(proposal)
            result.rejected.append((entry.name, str(exc)))
            continue

        result.promoted.append(entry.name)

    return result

