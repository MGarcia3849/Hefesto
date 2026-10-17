"""Paquete de tarea determinista (sin LLM): lo que recibe cualquier trabajador, sea cual sea el proveedor.

Mismo estado → mismo paquete. Si supera `packet_max_tokens` (4 caracteres ≈ 1 token) se recortan primero
las secciones menos prioritarias y se anota qué se omitió.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hefesto import config, distribution, guard, packs, roadmap, skill_catalog, skill_select, skills
from hefesto.util import FRAMEWORK_ROOT, HefestoError

CHARS_PER_TOKEN = 4
MAX_FILE_BYTES = 200_000
PROMPTS_DIR = FRAMEWORK_ROOT / "prompts"

# Prioridad: 0 nunca se recorta; cuanto mayor, antes se recorta.
P_ESSENTIAL, P_RETRY, P_CONTRACT, P_FILES, P_SKILLS, P_MEMORY, P_GRAPH = 0, 1, 1, 2, 3, 3, 3

SKILL_INTRO = (
    "Sigue estas skills en esta tarea; si alguna contradice las reglas del rol o del proyecto, mandan las reglas."
)

PACKAGE_JSON_STACKS: tuple[tuple[str, str], ...] = (
    ("@angular/core", "angular"),
    ("react", "react"),
    ("vue", "vue"),
    ("typescript", "typescript"),
    ("next", "nextjs"),
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
    """Entradas de DECISIONS/LEARNINGS puntuadas por rutas, términos del título y etiquetas."""
    try:
        max_chars = int(config.load(project).get("memory", "max_chars", default=2000))
    except Exception:
        max_chars = 2000
    if max_chars <= 0:
        return []
    files = list(getattr(task, "files", None) or [])
    routes: set[str] = set()
    route_names: set[str] = set()
    for raw_f in files:
        clean_f = raw_f.strip().rstrip("/")
        if not clean_f:
            continue
        routes.add(clean_f.lower())
        if "/" in clean_f:
            base = clean_f.rsplit("/", 1)[-1].lower()
            if len(base) >= 3:
                routes.add(base)
        for part in re.split(r"[/\\]+", clean_f):
            if part:
                route_names.add(skill_select._normalize(part))
                stem = part.split(".")[0]
                if stem:
                    route_names.add(skill_select._normalize(stem))
    title_terms = {
        tok for tok in skill_select._tokenize(getattr(task, "title", "") or "")
        if len(tok) >= 4 and tok not in skill_select.STOPWORDS
    }
    tag_targets = title_terms | route_names
    scored: list[tuple[int, int, str, str]] = []
    idx = 0
    for name in ("DECISIONS.md", "LEARNINGS.md"):
        path = project / ".hefesto" / name
        if not path.exists():
            continue
        text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
        blocks = re.split(r"\n(?=## )|\n(?=- )", text)
        for block in blocks:
            stripped = block.strip()
            if not stripped or stripped.startswith("# "):
                continue
            low = stripped.lower()
            score = 0
            for route in routes:
                if route in low:
                    score += 4
            block_tokens = set(skill_select._tokenize(stripped))
            for term in title_terms:
                if term in block_tokens:
                    score += 1
            for raw_tag in re.findall(r"(?<![#\w])#([a-zA-Z0-9_\u00c0-\u024f-]+)", stripped):
                norm_tag = skill_select._normalize(raw_tag)
                if not norm_tag:
                    continue
                if norm_tag in tag_targets or (
                    len(norm_tag) >= 4
                    and any(norm_tag.rstrip("s") == t.rstrip("s") for t in tag_targets if len(t) >= 4)
                ):
                    score += 2
            if score > 0:
                scored.append((score, idx, f"({name})", guard.redact(stripped)))
                idx += 1
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[tuple[str, str]] = []
    total_chars = 0
    for _score, _idx, label, redacted in scored:
        if total_chars + len(redacted) > max_chars:
            continue
        out.append((label, redacted))
        total_chars += len(redacted)
    return out


def _clean(text: str) -> str:
    return skills.sanitize_text(guard.redact(text))


def _detected_rule_folders(project: Path) -> list[str]:
    folders: list[str] = ["common"]
    try:
        detected = distribution.detect_stack(project).get("stacks", [])
    except Exception:
        detected = []
    for st in detected:
        if isinstance(st, str) and st and st not in folders:
            folders.append(st)
    pkg_path = project / "package.json"
    if pkg_path.is_file() and not pkg_path.is_symlink():
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            deps: set[str] = set()
            for key in ("dependencies", "devDependencies"):
                sec = data.get(key)
                if isinstance(sec, dict):
                    deps.update(str(k) for k in sec.keys())
            for pkg_name, folder_name in PACKAGE_JSON_STACKS:
                if pkg_name in deps and folder_name not in folders:
                    folders.append(folder_name)
    return folders


def _is_link(path: Path) -> bool:
    return (
        path.is_symlink()
        or os.path.islink(path)
        or (hasattr(os.path, "isjunction") and os.path.isjunction(path))
    )


def _has_link_below(resolved_root: Path, target: Path) -> bool:
    """True si algún componente entre `resolved_root` (excluido) y `target` (incluido) es enlace o junction."""
    try:
        rel_parts = target.relative_to(resolved_root).parts
    except ValueError:
        return True
    cur = resolved_root
    for part in rel_parts:
        cur = cur / part
        if _is_link(cur):
            return True
    return False


def stack_rules(project: Path, cfg: config.Config) -> list[tuple[str, str]]:
    """Reglas .md de las raíces configuradas para `common` y los stacks detectados.

    Las raíces relativas se resuelven contra `project` (nunca contra el `cwd`) y las que
    empiezan por `~` o son absolutas se expanden con `expanduser`.
    Política de enlaces: cada raíz configurada la elige el usuario y se resuelve una sola
    vez con `resolve()`; dentro de la raíz resuelta ninguna subcarpeta ni fichero puede
    ser enlace simbólico ni junction (se comprueba cada componente entre la raíz resuelta
    y el fichero) y la ruta resuelta de cada fichero debe quedar dentro de la raíz
    resuelta. Se descartan los ficheros que cumplan `guard.is_protected`.
    """
    raw_roots = cfg.get("packet", "rules_roots", default=[]) or []
    if not isinstance(raw_roots, list) or not raw_roots:
        return []
    max_chars = int(cfg.get("packet", "rules_max_chars", default=4000))
    if max_chars <= 0:
        return []
    raw_protected = cfg.get("guard", "protected_paths", default=list(guard.DEFAULT_PROTECTED))
    protected = list(raw_protected) if isinstance(raw_protected, (list, tuple)) else list(guard.DEFAULT_PROTECTED)
    folders = _detected_rule_folders(project)
    out: list[tuple[str, str]] = []
    total_chars = 0
    for raw_root in raw_roots:
        if not isinstance(raw_root, str) or not raw_root.strip():
            continue
        raw_str = raw_root.strip()
        if raw_str.startswith("~"):
            root_path = Path(os.path.expanduser(raw_str))
            if not root_path.is_absolute():
                continue
        else:
            expanded = Path(os.path.expanduser(raw_str))
            root_path = expanded if expanded.is_absolute() else (project / expanded)
        try:
            resolved_root = root_path.resolve()
            if not resolved_root.is_dir():
                continue
        except OSError:
            continue
        for folder in folders:
            sub = resolved_root / folder
            try:
                if _has_link_below(resolved_root, sub) or not sub.is_dir():
                    continue
                if not sub.resolve().is_relative_to(resolved_root):
                    continue
            except OSError:
                continue
            for dirpath, dirnames, filenames in os.walk(sub, followlinks=False):
                parent = Path(dirpath)
                safe_dirs: list[str] = []
                for d in sorted(dirnames):
                    d_path = parent / d
                    try:
                        rel_dir = d_path.relative_to(resolved_root).as_posix()
                        if (
                            not _has_link_below(resolved_root, d_path)
                            and not guard.is_protected(rel_dir, protected)
                            and d_path.resolve().is_relative_to(resolved_root)
                        ):
                            safe_dirs.append(d)
                    except (OSError, ValueError):
                        continue
                dirnames[:] = safe_dirs
                for fname in sorted(filenames):
                    md_path = parent / fname
                    if md_path.suffix.lower() != ".md":
                        continue
                    try:
                        rel_name = md_path.relative_to(resolved_root).as_posix()
                        if guard.is_protected(rel_name, protected):
                            continue
                        if _has_link_below(resolved_root, md_path) or not md_path.is_file():
                            continue
                        if not md_path.resolve().is_relative_to(resolved_root):
                            continue
                        if md_path.stat().st_size > skill_select.MAX_SKILL_BYTES:
                            continue
                        raw_bytes = md_path.read_bytes()
                        if len(raw_bytes) > skill_select.MAX_SKILL_BYTES or b"\0" in raw_bytes[:8000]:
                            continue
                        raw_text = raw_bytes.decode("utf-8").replace("\r\n", "\n").strip()
                    except (OSError, UnicodeDecodeError, ValueError):
                        continue
                    clean_text = _clean(raw_text)
                    if not clean_text:
                        continue
                    if total_chars + len(clean_text) > max_chars:
                        continue
                    out.append((f"### {_clean(rel_name)}", clean_text))
                    total_chars += len(clean_text)
    return out


def _skill_parts(project: Path, cfg: config.Config, role: str, task: roadmap.Task,
                 provider: str | None = None) -> list[tuple[str, str]]:
    raw_choices = skill_select.select(project, cfg, role, task)
    try:
        catalog = skill_catalog.load()
        catalog_choices = skill_select.from_catalog(catalog, role, task, provider)
    except HefestoError:
        catalog = {}
        catalog_choices = []
    pinned_names = {name.casefold() for name in skill_select._pinned_names(task)}
    catalog_names = {name.casefold() for name in catalog}
    chosen_catalog_names = {c.name.casefold() for c in catalog_choices}
    selected_by_name = {c.name.casefold(): c for c in raw_choices}
    choices = [
        c for c in raw_choices
        if c.name.casefold() not in chosen_catalog_names
        and (c.name.casefold() not in catalog_names or c.name.casefold() in pinned_names)
    ]
    if not choices and not catalog_choices:
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
    if catalog_choices:
        installed = {info.name.casefold(): info for info in
                     skill_select.discover(skill_select.default_roots(project, cfg))}
        for choice in catalog_choices:
            key = choice.name.casefold()
            modes = ", ".join(choice.modes) if choice.modes else "toda la skill"
            lines = [_clean(f"modos: {modes}")]
            info = installed.get(key) or selected_by_name.get(key)
            # Se añade el aviso de modos prohibidos cuando hay cuerpo instalado (que describe todos
            # los modos) o cuando el rol entra con toda la skill; sin cuerpo instalado y con modos
            # explícitos se omiten del texto (contrato SelectionTests: assertNotIn sobre pulidor).
            if choice.forbidden and (info is not None or not choice.modes):
                forb = ", ".join(choice.forbidden)
                lines.append(
                    _clean(
                        f"modos prohibidos para este rol (exigen un humano o son de verify): {forb} — no los ejecutes"
                    )
                )
            reasons = list(choice.reasons)
            prev = selected_by_name.get(key)
            if prev is not None:
                for r in prev.reasons:
                    if r not in reasons:
                        reasons.append(r)
            lines.append(_clean("Motivo: " + "; ".join(reasons)))
            if info is not None:
                clean_body = _clean(info.body)
                if len(clean_body) > max_chars:
                    clean_body = clean_body[:max_chars] + "\n[skill recortada]"
                if clean_body:
                    lines.append(clean_body)
            parts.append((_clean(f"### {choice.name} (catálogo)"), "\n".join(lines)))
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
        skill_parts = _skill_parts(project, cfg, role, task, model_id.split(":", 1)[0])
        if skill_parts:
            sections.append(Section("Skills que aplican (síguelas)", P_SKILLS, skill_parts))
        rules = stack_rules(project, cfg)
        if rules:
            sections.append(Section("Reglas del stack", P_SKILLS, rules))
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
