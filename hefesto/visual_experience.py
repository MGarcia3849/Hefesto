"""Propuestas de experiencia visual: el add-on propone, no implementa.

`inspect` y `propose` solo leen el proyecto y escriben documentos en `.hefesto/visual-experience/`: nunca cambian
código, nunca añaden efectos y nunca generan movimiento. Una propuesta es un documento estático (JSON y markdown)
con un storyboard en fotogramas fijos, un coste estimado, un fallback estático y la variante
`prefers-reduced-motion`. Se registra con `approval.propose` y muestra su alcance exacto; registrarla no autoriza
nada. `apply` exige una aprobación puntual de esa propuesta (`approval.find`) y reutiliza `approval.gate`;
`verify` reutiliza el mismo gate sin barreras nuevas.

Límites: el coste es una estimación heurística por efecto, no mide rendimiento real. La aprobación depende de
`approval.human_channel()`; la TTY y la ausencia de variables de agente son condiciones necesarias, no son
autenticación robusta. La barrera real es el registro protegido de `hefesto/approval.py`, la huella y los gates.
Ninguna skill externa se instala desde aquí: solo se declaran como opcionales y no evaluadas.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from hefesto import approval, gitops, guard, util, visual
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

SCHEMA_VERSION = 1
EFFECTS = approval.EFFECTS
KINDS = approval.KINDS
SCOPE_KEYS = approval.SCOPE_KEYS
PAGE_SUFFIXES = (*visual.INTERFACE_SUFFIXES, ".js", ".ts", ".dart")
INSPECTION_KEYS = ("schema_version", "kind", "pages", "existing_motion", "reduced_motion")
PROPOSAL_KEYS = ("schema_version", "id", "scope", "description", "storyboard", "cost", "fallback")
FRAME_KEYS = ("step", "description", "static")
MIN_FRAMES = 2
COST_KEYS = ("performance", "accessibility", "notes")
COST_LEVELS = ("bajo", "medio", "alto")
EFFECT_COST = {
    "animation": ("bajo", "medio"),
    "video": ("alto", "alto"),
    "motion-graphic": ("medio", "medio"),
    "3d": ("alto", "alto"),
}
_COST_NOTES = {
    "animation": "Estimación heurística: transiciones y fotogramas clave baratos si solo mueven opacidad o "
                 "transform; puede marear o distraer.",
    "video": "Estimación heurística: carga de red, decodificación y batería elevadas; requiere pausa y subtítulos.",
    "motion-graphic": "Estimación heurística: coste medio de CPU y de peso del recurso; requiere pausa visible.",
    "3d": "Estimación heurística: GPU, memoria y peso del modelo elevados; requiere alternativa sin WebGL.",
}
FALLBACK_KEYS = ("static", "reduced_motion")
STATUSES = ("pendiente", "aprobada", "usada", "revocada")
APPLY_KEYS = ("proposal", "approval", "scope", "instruction", "findings")
APPLY_STAGE = "delegation"
VERIFY_STAGE = "design-pass"
OPTIONAL_SKILLS = ("web-animation-design", "design-motion-principles")
SKILL_STATUS = "no evaluada"

_REDUCED = "prefers-reduced-motion"
_ID_RE = re.compile(r"PROP-[A-Za-z0-9]{1,32}")


def experience_dir(project: Path) -> Path:
    return Path(project) / ".hefesto" / "visual-experience"


def inspection_path(project: Path) -> Path:
    return experience_dir(project) / "inspection.json"


def proposal_paths(project: Path, proposal_id: str) -> tuple[Path, Path]:
    folder = experience_dir(project)
    return folder / f"{proposal_id}.json", folder / f"{proposal_id}.md"


# --- Inspección -------------------------------------------------------------------------------------------------


def _candidate_files(workdir: Path) -> list[str]:
    try:
        out = gitops.git(workdir, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
        names = [n for n in out.split("\0") if n]
    except (HefestoError, OSError):
        names = [p.relative_to(workdir).as_posix() for p in workdir.rglob("*") if p.is_file()]
    return names


def _new_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    head = [f"diff --git a/{path} b/{path}", "new file mode 100644", "index 0000000..1111111", "--- /dev/null",
            f"+++ b/{path}"]
    if not lines:
        return "\n".join(head) + "\n"
    return "\n".join(head + [f"@@ -0,0 +1,{len(lines)} @@"] + ["+" + line for line in lines]) + "\n"


def inspect(project: Path, kind: str = "web", workdir: Path | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise HefestoError(f"kind no válido: {kind!r} (usa {', '.join(KINDS)}).", EXIT_INPUT)
    root = Path(workdir) if workdir is not None else Path(project)
    pages: list[str] = []
    for name in set(_candidate_files(root)):
        rel = PurePosixPath(name)
        if rel.parts[0] in (".git", ".hefesto") or rel.suffix.lower() not in PAGE_SUFFIXES:
            continue
        if (root / name).is_file():
            pages.append(rel.as_posix())
    pages.sort()
    motion: set[tuple[str, str]] = set()
    reduced: list[str] = []
    for page in pages:
        try:
            text = (root / page).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _REDUCED in text:
            reduced.append(page)
        for finding in approval.detect_motion(_new_file_diff(page, text)):
            motion.add((finding["path"], finding["effect"]))
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "pages": pages,
        "existing_motion": [{"path": p, "effect": e} for p, e in sorted(motion)],
        "reduced_motion": reduced,
    }
    util.atomic_write_json(inspection_path(project), result)
    return result


# --- Propuestas -------------------------------------------------------------------------------------------------


def _default_steps(page: str, effect: str) -> list[str]:
    return [f"Estado inicial de {page}: reposo, sin {effect}, tal como está hoy.",
            f"Estado final de {page}: resultado fijo del efecto {effect}, con contraste y foco visibles."]


def _markdown(proposal: dict[str, Any]) -> str:
    scope, cost, fallback = proposal["scope"], proposal["cost"], proposal["fallback"]
    lines = [f"# Propuesta visual {proposal['id']}", "",
             "Documento estático: describe estados fijos y no genera movimiento ni recursos.", "",
             "## Alcance", ""]
    lines += [f"- {key}: {scope[key]}" for key in SCOPE_KEYS]
    lines += ["", "## Descripción", "", proposal["description"], "", "## Storyboard estático", ""]
    lines += [f"{f['step']}. {f['description']}" for f in proposal["storyboard"]]
    lines += ["", "## Coste estimado", "", f"- rendimiento: {cost['performance']}",
              f"- accesibilidad: {cost['accessibility']}", f"- notas: {cost['notes']}", "",
              "## Fallback", "", f"- estático: {fallback['static']}",
              f"- reduced motion: {fallback['reduced_motion']}", "",
              "## Aprobación", "",
              "Esta propuesta no autoriza nada. Una persona debe ejecutar desde su terminal:", "",
              f"    hefesto approval approve {proposal['id']}", ""]
    return "\n".join(lines)


def propose(project: Path, page: str, kind: str, effect: str, description: str,
            steps: list[str] | None = None) -> dict[str, Any]:
    if steps is not None:
        if (not isinstance(steps, (list, tuple)) or len(steps) < MIN_FRAMES
                or any(not isinstance(s, str) or not s.strip() for s in steps)):
            raise HefestoError(f"El storyboard necesita al menos {MIN_FRAMES} pasos con texto.", EXIT_INPUT)
    # approval.propose valida página, tipo y efecto antes de escribir nada.
    registered = approval.propose(project, page, kind, effect, description)
    texts = [guard.redact(s.strip()) for s in steps] if steps is not None else _default_steps(
        registered["page"], effect)
    performance, accessibility = EFFECT_COST[effect]
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "id": registered["id"],
        "scope": {"project": registered["project"], "page": registered["page"], "kind": registered["kind"],
                  "effect": registered["effect"], "proposal": registered["id"], "sha256": registered["sha256"]},
        "description": registered["description"],
        "storyboard": [{"step": i, "description": t, "static": True} for i, t in enumerate(texts, 1)],
        "cost": {"performance": performance, "accessibility": accessibility, "notes": _COST_NOTES[effect]},
        "fallback": {"static": f"Versión estática de {registered['page']}: el contenido final sin {effect}.",
                     "reduced_motion": f"Con {_REDUCED}: reduce, desactiva el efecto y muestra el estado final "
                                       "fijo."},
    }
    json_path, md_path = proposal_paths(project, registered["id"])
    util.atomic_write_json(json_path, proposal)
    util.atomic_write_text(md_path, _markdown(proposal))
    return proposal


def load(project: Path, proposal_id: str) -> dict[str, Any] | None:
    if not isinstance(proposal_id, str) or not _ID_RE.fullmatch(proposal_id):
        return None
    try:
        data = util.read_json(proposal_paths(project, proposal_id)[0])
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or not set(PROPOSAL_KEYS) <= data.keys()
            or not isinstance(data.get("scope"), dict) or not set(SCOPE_KEYS) <= data["scope"].keys()):
        return None
    return data


def status(project: Path, proposal_id: str) -> str:
    mine = [r for r in approval.records(project) if r.get("proposal") == proposal_id]
    live = {r["id"]: r for r in approval.active(project) if r.get("proposal") == proposal_id}
    if live:
        return "usada" if any(r["used_for"] for r in live.values()) else "aprobada"
    if any(r.get("revoked_at") for r in mine):
        return "revocada"
    return "pendiente"


# --- Aplicación y verificación ----------------------------------------------------------------------------------


def _instruction(proposal: dict[str, Any]) -> str:
    scope, fallback = proposal["scope"], proposal["fallback"]
    frames = "; ".join(f"{f['step']}) {f['description']}" for f in proposal["storyboard"])
    return (f"Implementa solo la propuesta {proposal['id']}: efecto {scope['effect']} en {scope['page']}. "
            f"Storyboard: {frames}. Fallback estático: {fallback['static']} {fallback['reduced_motion']} "
            f"Respeta {_REDUCED}. {approval.STATIC_RULE}")


def apply(project: Path, proposal_id: str, workdir: Path | None = None, base: str | None = None) -> dict[str, Any]:
    doc = load(project, proposal_id)
    if doc is None:
        raise HefestoError(f"Propuesta desconocida: {proposal_id}.", EXIT_INPUT)
    scope = doc["scope"]
    registered = approval.load_proposal(project, proposal_id)
    if (registered is None or doc["id"] != proposal_id or scope["proposal"] != proposal_id
            or registered["sha256"] != approval.proposal_hash(registered)
            or any(scope[k] != registered[k] for k in ("project", "page", "kind", "effect", "sha256"))
            or scope["project"] != approval.project_id(project)):
        raise HefestoError(f"El documento de {proposal_id} no coincide con la propuesta registrada.", EXIT_BLOCKED)
    page, kind, effect = scope["page"], scope["kind"], scope["effect"]
    record = approval.find(project, page, kind, effect)
    if record is None or record.get("proposal") != proposal_id:
        record = next((r for r in approval.active(project) if r.get("proposal") == proposal_id
                       and not r["used_for"]), None)
    if record is None:
        raise HefestoError(f"Sin aprobación puntual vigente para {proposal_id}; una persona debe ejecutar "
                           f"`hefesto approval approve {proposal_id}`.", EXIT_BLOCKED)
    findings = approval.gate(project, Path(workdir) if workdir is not None else Path(project),
                             base or "HEAD", kind, APPLY_STAGE, consume=False)
    return {"proposal": proposal_id, "approval": record["id"], "scope": scope,
            "instruction": _instruction(doc), "findings": findings}


def verify(project: Path, workdir: Path | None = None, base: str | None = None,
           kind: str = "web") -> list[dict[str, Any]]:
    return approval.gate(project, Path(workdir) if workdir is not None else Path(project),
                         base or "HEAD", kind, VERIFY_STAGE, consume=False)


def render_inspection(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
