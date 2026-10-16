"""Paquete de tarea determinista (sin LLM): lo que recibe cualquier trabajador, sea cual sea el proveedor.

Mismo estado → mismo paquete. Si supera `packet_max_tokens` (4 caracteres ≈ 1 token) se recortan primero
las secciones menos prioritarias y se anota qué se omitió.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hefesto import config, guard, packs, roadmap, skill_select, skills
from hefesto.util import FRAMEWORK_ROOT

CHARS_PER_TOKEN = 4
MAX_FILE_BYTES = 200_000
PROMPTS_DIR = FRAMEWORK_ROOT / "prompts"

# Prioridad: 0 nunca se recorta; cuanto mayor, antes se recorta.
P_ESSENTIAL, P_RETRY, P_CONTRACT, P_FILES, P_SKILLS, P_MEMORY, P_GRAPH = 0, 1, 1, 2, 3, 3, 3

SKILL_INTRO = (
    "Sigue estas skills en esta tarea; si alguna contradice las reglas del rol o del proyecto, mandan las reglas."
)


@dataclass
class Section:
    title: str
    priority: int
    parts: list[tuple[str, str]]  # (etiqueta, texto); se recortan de la última a la primera

    def render(self) -> str:
        body = "\n\n".join(f"{label}\n{text}" if label else text for label, text in self.parts)
        return f"## {self.title}\n\n{body}\n"


@dataclass
class Packet:
    text: str
    omitted: list[str]
    max_tokens: int
    digest: str

    @property
    def tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN


def max_tokens_for(cfg: config.Config, model_id: str) -> int:
    table = cfg.get("limits", "packet_max_tokens", default={}) or {}
    provider = model_id.split(":", 1)[0]
    for key in (model_id, f"{provider}:*", "default"):
        if isinstance(table.get(key), int):
            return int(table[key])
    return 24000


def role_rules(role: str) -> str:
    path = PROMPTS_DIR / f"{role}.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else f"Rol: {role}."


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw[:8000]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _expand(workdir: Path, rels: list[str], protected: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Contenido de los ficheros (o directorios) indicados, en orden estable y redactado."""
    found: list[tuple[str, str]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for rel in rels:
        base = workdir / rel
        candidates = sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else ([base] if base.is_file() else [])
        for path in candidates:
            name = path.relative_to(workdir).as_posix()
            if name in seen or "/.git/" in f"/{name}" or name.startswith(".hefesto/"):
                continue
            seen.add(name)
            if guard.is_protected(name, protected):
                skipped.append(f"{name} (ruta protegida)")
                continue
            text = _read_text(path)
            if text is None:
                skipped.append(f"{name} (binario o mayor de {MAX_FILE_BYTES // 1000} KB)")
                continue
            found.append((name, guard.redact(text)))
    return found, skipped


def _memory_entries(project: Path, task: roadmap.Task) -> list[tuple[str, str]]:
    """Entradas de DECISIONS/LEARNINGS que mencionan rutas de la tarea."""
    keys = {f.rstrip("/") for f in task.files} | {f.split("/")[0] for f in task.files if "/" in f}
    keys.discard("")
    out: list[tuple[str, str]] = []
    for name in ("DECISIONS.md", "LEARNINGS.md"):
        path = project / ".hefesto" / name
        if not path.exists():
            continue
        text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
        blocks = re.split(r"\n(?=## )|\n(?=- )", text)
        for block in blocks:
            if any(k in block for k in keys):
                out.append((f"({name})", guard.redact(block.strip())))
    return out


def _clean(text: str) -> str:
    return skills.sanitize_text(guard.redact(text))


def _skill_parts(project: Path, cfg: config.Config, role: str, task: roadmap.Task) -> list[tuple[str, str]]:
    choices = skill_select.select(project, cfg, role, task)
    if not choices:
        return []
    max_chars = int(cfg.get("skills_selection", "max_chars_per_skill", default=6000))
    parts: list[tuple[str, str]] = [("", SKILL_INTRO)]
    for c in choices:
        clean_name = _clean(c.name)
        clean_source = _clean(c.source)
        clean_body = _clean(c.body)
        if len(clean_body) > max_chars:
            clean_body = clean_body[:max_chars] + "\n[skill recortada]"
        lines: list[str] = []
        if c.description:
            lines.append(_clean(c.description))
        lines.append("Motivo: " + "; ".join(_clean(r) for r in c.reasons))
        if clean_body:
            lines.append(clean_body)
        parts.append((f"### {clean_name} ({clean_source})", "\n".join(lines)))
    return parts


def build(project: Path, cfg: config.Config, role: str, instruction: str, task: roadmap.Task | None,
          rm: roadmap.Roadmap | None, data: dict[str, Any] | None, model_id: str, workdir: Path) -> Packet:
    protected = list(cfg.get("guard", "protected_paths", default=list(guard.DEFAULT_PROTECTED)))
    sections: list[Section] = []
    if task is not None:
        meta = [f"- id: {task.id}", f"- título: {task.title}", f"- fase: {task.phase}", f"- rol: {role}",
                f"- tamaño: {task.size}", f"- ficheros permitidos: {', '.join(task.files)}",
                f"- verify (debe salir con 0): `{task.verify}`"]
        if task.accept:
            meta.append(f"- criterios de aceptación: {task.accept}")
        if task.depends_on:
            meta.append(f"- depende de: {', '.join(task.depends_on)}")
        sections.append(Section("Tarea", P_ESSENTIAL, [("", "\n".join(meta))]))
    sections.append(Section("Instrucción del orquestador", P_ESSENTIAL, [("", guard.redact(instruction.strip()))]))
    sections.append(Section("Reglas del rol", P_ESSENTIAL, [("", role_rules(role))]))
    skipped: list[str] = []
    if task is not None and data and data.get("current_task_id") == task.id and data.get("attempts"):
        history = data.get("error_history") or []
        lines = [f"Intento {h.get('attempt')} ({h.get('role')}): {h.get('error')}" for h in history]
        sections.append(Section("Intentos anteriores (no repitas el mismo enfoque)", P_RETRY,
                                [("", guard.redact("\n".join(lines) or str(data.get("last_error"))))]))
    if task is not None and rm is not None:
        contract = sorted(set(rm.contract_paths(task)) | set(task.contract))
        if contract:
            files, skip = _expand(workdir, contract, protected)
            skipped += skip
            sections.append(Section("Tests de contrato (no los modifiques)", P_CONTRACT,
                                    [(f"### {n}", f"```\n{t}\n```") for n, t in files]))
        files, skip = _expand(workdir, task.files, protected)
        skipped += skip
        contract_set = set(contract)
        files = [(n, t) for n, t in files if n not in contract_set]
        if files:
            sections.append(Section("Ficheros de la tarea", P_FILES, [(f"### {n}", f"```\n{t}\n```") for n, t in files]))
        skill_parts = _skill_parts(project, cfg, role, task)
        if skill_parts:
            sections.append(Section("Skills que aplican (síguelas)", P_SKILLS, skill_parts))
        memory = _memory_entries(project, task)
        from hefesto import memory as mem_mod
        for note in mem_mod.relevant(project, task=task):
            memory.append(("(memoria global)", guard.redact(note)))
        if memory:
            sections.append(Section("Decisiones y lecciones relevantes", P_MEMORY, memory))
        if packs.is_pack_enabled(project, "context"):
            graph_extract = packs.query_graph(project, workdir, task, cfg)
            if graph_extract:
                clean_graph = skills.sanitize_text(guard.redact(graph_extract))
                sections.append(Section("Grafo de código", P_GRAPH, [("", clean_graph)]))
    max_tokens = max_tokens_for(cfg, model_id)
    omitted = _fit(sections, max_tokens * CHARS_PER_TOKEN)
    omitted += [f"{s} — omitido" for s in skipped]
    text = "# Paquete de tarea Hefesto\n\n" + "\n".join(s.render() for s in sections if s.parts)
    if omitted:
        text += "\n## Omitido del paquete\n\n" + "\n".join(f"- {o}" for o in omitted) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Packet(text=text, omitted=omitted, max_tokens=max_tokens, digest=digest)


def _size(sections: list[Section]) -> int:
    return sum(len(s.render()) for s in sections if s.parts) + 200


def _fit(sections: list[Section], budget: int) -> list[str]:
    omitted: list[str] = []
    for priority in sorted({s.priority for s in sections if s.priority > 0}, reverse=True):
        for section in reversed([s for s in sections if s.priority == priority]):
            while section.parts and _size(sections) > budget:
                label, _text = section.parts.pop()
                omitted.append(f"{section.title}: {label.lstrip('# ').strip() or 'contenido'} (por tamaño)")
                if len(section.parts) == 1 and not section.parts[0][0]:
                    section.parts.clear()
            if _size(sections) <= budget:
                return omitted
    if _size(sections) > budget:
        for section in sections:
            if section.priority == 0 and section.title.startswith("Instrucción"):
                label, text = section.parts[0]
                keep = max(200, len(text) - (_size(sections) - budget))
                section.parts[0] = (label, text[:keep] + "\n[instrucción truncada por tamaño]")
                omitted.append("Instrucción del orquestador: truncada (por tamaño)")
    return omitted


def save(project: Path, packet: Packet, task_id: str | None, role: str) -> Path:
    path = project / ".hefesto" / "logs" / "packets" / f"{task_id or 'adhoc'}-{role}-{packet.digest[:10]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(packet.text, encoding="utf-8", newline="\n")
    return path
