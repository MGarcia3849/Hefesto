"""Exportación Markdown de solo lectura del estado del proyecto a Obsidian."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hefesto import guard, memory, project as project_mod, roadmap, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

VAULT_DIR = ".hefesto/vault"
OBSIDIAN_BINARY = "obsidian"
GENERATED_KEY = "generado_por"
GENERATED_VALUE = "hefesto"
MEMORY_INDEX_DIR = "_indices"
REVIEW_KEY = "revision"
REVIEW_ACTIONS = ("aprobar", "descartar")

CANVAS_COLORS: dict[str, str] = {
    "pending": "6",
    "in_progress": "3",
    "done": "4",
    "blocked": "1",
}


@dataclass
class BuildResult:
    root: Path
    written: list[Path]
    removed: list[Path]


@dataclass
class SearchResult:
    source: str
    hits: list[str]


def search(project: Path, term: str) -> SearchResult:
    """Busca en la bóveda con la CLI oficial si existe y, si falla, en Markdown local."""
    query = term.strip()
    if not query:
        raise HefestoError("Indica un término de búsqueda no vacío.", EXIT_INPUT)
    root = (Path(project).resolve() / VAULT_DIR).resolve()
    binary = util.which(OBSIDIAN_BINARY)
    if binary:
        try:
            result = subprocess.run(
                [str(binary), "search", query, "path", str(root)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if result.returncode == 0:
                hits = [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
                return SearchResult("obsidian", hits)
        except (OSError, subprocess.TimeoutExpired):
            pass
    hits: list[str] = []
    if root.is_dir():
        for path in sorted(root.rglob("*.md")):
            try:
                if query.casefold() in path.read_text(encoding="utf-8", errors="replace").casefold():
                    hits.append(path.relative_to(root).as_posix())
            except OSError:
                continue
    return SearchResult("ficheros", hits)


def open_vault(project: Path) -> str:
    root = (Path(project).resolve() / VAULT_DIR).resolve()
    binary = util.which(OBSIDIAN_BINARY)
    if not binary:
        return f"CLI de Obsidian no disponible; abre la bóveda manualmente en {root}."
    try:
        subprocess.run([str(binary), "open", str(root)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, check=True)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        return f"No se pudo abrir Obsidian ({exc}); abre la bóveda manualmente en {root}."
    return f"Bóveda abierta en Obsidian: {root}"


def _safe(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _front(meta: dict[str, Any], body: str) -> str:
    clean = {str(k): guard.redact(str(v)).replace("\n", " ").replace("\r", " ") for k, v in meta.items()}
    return guard.redact(memory.format_note(clean, body))


def _link(path: str) -> str:
    return f"[[{path.removesuffix('.md')}]]"


def _safe_slug(text: str) -> str:
    return roadmap.slugify(text)


def memory_index() -> list[Path]:
    """Genera índices de las notas aprobadas sin modificar sus fuentes."""
    store = memory.memory_dir()
    index_dir = store / MEMORY_INDEX_DIR
    memory._check_memory_path(index_dir)
    groups: dict[str, set[str]] = {}
    for note in memory._approved_notes():
        try:
            meta, _ = memory.parse_note_content(note.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        link = _link(note.relative_to(store).as_posix())
        for field in ("stack", "tipo", "proyecto", "confianza"):
            value = memory._stack_slug(guard.redact(meta.get(field, "") or "general"))
            groups.setdefault(f"{field}-{value}.md", set()).add(link)

    outputs = {index_dir / name: _front(
        {GENERATED_KEY: GENERATED_VALUE},
        f"# {name.removesuffix('.md')}\n\n" + "\n".join(f"- {link}" for link in sorted(links)),
    ) for name, links in sorted(groups.items())}
    # Validate every destination before writing; user notes are never overwritten.
    for path in outputs:
        memory._check_memory_path(path)
        if path.exists():
            meta, _ = memory.parse_note_content(path.read_text(encoding="utf-8"))
            if meta.get(GENERATED_KEY) != GENERATED_VALUE:
                raise HefestoError("Una nota manual ocupa la ruta de un índice de memoria.", EXIT_BLOCKED)
    index_dir.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        memory._check_memory_path(path)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
    for path in sorted(index_dir.glob("*.md")):
        memory._check_memory_path(path)
        if path in outputs:
            continue
        try:
            meta, _ = memory.parse_note_content(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if meta.get(GENERATED_KEY) == GENERATED_VALUE:
            path.unlink()
    return list(outputs)


def review_from_vault(project: Path | None = None) -> str:
    """Aplica las marcas humanas de las candidatas tras una confirmación TTY."""
    if "HEFESTO_ROLE" in os.environ:
        raise HefestoError("La revisión desde la bóveda requiere un humano, no un trabajador delegado.", EXIT_BLOCKED)
    util.require_tty("hefesto memory review --from-vault")
    proposals: list[tuple[Path, str, bytes]] = []
    for candidate in memory.pending():
        note = memory._read_candidate(candidate)
        if note is None:
            continue
        action = note[0].get(REVIEW_KEY, "")
        if action not in REVIEW_ACTIONS:
            continue
        memory._check_memory_path(candidate)
        raw = candidate.read_bytes()
        # Capture the action and the bytes from the same version of the note.
        if memory.parse_note_content(raw.decode("utf-8"))[0].get(REVIEW_KEY) != action:
            continue
        proposals.append((candidate, action, raw))
        print(f"{guard.redact(candidate.name)} → {action}")
    if not proposals:
        return "No hay candidatas con una revisión válida en la bóveda."
    if not util.confirm("¿Aplicar estas revisiones de memoria?", default=False):
        return "Revisión cancelada; no se ha cambiado ninguna candidata."
    approved = discarded = skipped = 0
    for candidate, action, raw in proposals:
        memory._check_memory_path(candidate)
        if not candidate.is_file() or candidate.read_bytes() != raw or memory._read_candidate(candidate) is None:
            skipped += 1
            continue
        if action == "aprobar":
            if memory._approve_candidate(candidate, project, preserve_existing=True):
                approved += 1
            else:
                skipped += 1
        else:
            memory._check_path(memory.pending_dir(), candidate)
            candidate.unlink()
            discarded += 1
    return f"Revisión completada: {approved} aprobadas, {discarded} descartadas, {skipped} saltadas."


def build(project: Path, out: Path | str | None = None) -> BuildResult:
    project = Path(project).resolve()
    root = (project / VAULT_DIR if out is None else (project / out if not Path(out).is_absolute() else Path(out))).absolute()
    if not _safe(project, root):
        raise HefestoError("La bóveda debe permanecer dentro del proyecto.", EXIT_BLOCKED)
    # Validate existing ancestor links before creating anything.
    cursor = root
    while cursor != project:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise HefestoError("No se permiten enlaces en la ruta de la bóveda.", EXIT_BLOCKED)
        cursor = cursor.parent
    rm = roadmap.parse(roadmap.roadmap_path(project))
    evidence_dir = project / ".hefesto" / "evidence"
    ev_by_task: dict[str, tuple[Path, dict[str, Any]]] = {}
    ev_notes: dict[str, str] = {}
    if evidence_dir.is_dir():
        for path in sorted(evidence_dir.glob("TASK-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    task_id = str(data.get("task_id", path.stem))
                    ev_by_task[task_id] = (path, data)
                    rel = f"evidencia/{path.stem}.md"
                    ev_notes[task_id] = rel
            except (OSError, ValueError):
                continue
    decision_sections: list[tuple[str, str]] = []
    decision_path = project / ".hefesto" / "DECISIONS.md"
    if decision_path.is_file():
        raw = guard.redact(decision_path.read_text(encoding="utf-8", errors="replace"))
        chunks = re.split(r"(?m)^##\s+", raw)[1:]
        for i, chunk in enumerate(chunks):
            head, _, body = chunk.partition("\n")
            title = head.strip()
            decision_sections.append((f"decisiones/{i+1:03d}-{_safe_slug(title)}.md", f"# {title}\n\n{body.strip()}"))
    learning_lines: list[str] = []
    learning_path = project / ".hefesto" / "LEARNINGS.md"
    if learning_path.is_file():
        learning_lines = [line.strip()[2:] for line in guard.redact(learning_path.read_text(encoding="utf-8", errors="replace")).splitlines()
                          if line.startswith("- ")]
    outputs: dict[str, str] = {}
    phase_paths = {phase: f"fases/{roadmap.slugify(phase)}.md" for phase in rm.phases}
    tasks_by_phase: dict[str, list[roadmap.Task]] = {}
    for task in rm.tasks:
        tasks_by_phase.setdefault(task.phase, []).append(task)
    for phase, rel in phase_paths.items():
        links = "\n".join(f"- {_link('tareas/' + t.id)}" for t in tasks_by_phase.get(phase, []))
        outputs[rel] = _front({GENERATED_KEY: GENERATED_VALUE, "fase": phase}, f"# {guard.redact(phase)}\n\n{links}")
    decision_links_by_task: dict[str, list[str]] = {}
    for rel, content in decision_sections:
        for task in rm.tasks:
            if task.id in content:
                decision_links_by_task.setdefault(task.id, []).append(rel)
                content += f"\n\nRelacionado: {_link('tareas/' + task.id)}"
        outputs[rel] = _front({GENERATED_KEY: GENERATED_VALUE}, content)
    for i, line in enumerate(learning_lines, 1):
        rel = f"lecciones/{i:03d}.md"
        outputs[rel] = _front({GENERATED_KEY: GENERATED_VALUE}, f"# Lección {i}\n\n{line}")
    for task in rm.tasks:
        ev_pair = ev_by_task.get(task.id)
        ev = ev_pair[1] if ev_pair else {}
        review = ev.get("review") if isinstance(ev.get("review"), dict) else {}
        providers = ev.get("providers", [])
        if not isinstance(providers, list): providers = [providers]
        meta: dict[str, Any] = {GENERATED_KEY: GENERATED_VALUE, "estado": task.status, "fase": task.phase,
                                "intentos": ev.get("attempts", ""), "proveedor": ", ".join(map(str, providers)),
                                "veredicto": review.get("verdict", ""), "fecha": review.get("at", "")}
        links = [_link(phase_paths[task.phase])]
        links.extend(_link("tareas/" + dep) for dep in task.depends_on)
        if task.id in ev_notes: links.append(_link(ev_notes[task.id]))
        links.extend(_link(p) for p in decision_links_by_task.get(task.id, []))
        body = f"# {guard.redact(task.id + ': ' + task.title)}\n\n" + "\n".join(f"- {item}" for item in links)
        if ev:
            visual = ev.get("visual") if isinstance(ev.get("visual"), dict) else {}
            captures = visual.get("captures", [])
            for cap in captures if isinstance(captures, list) else []:
                if isinstance(cap, dict) and cap.get("path"):
                    body += f"\n- Captura: `{guard.redact(str(cap['path']))}`"
        outputs[f"tareas/{task.id}.md"] = _front(meta, body)
    for task_id, rel in ev_notes.items():
        data = ev_by_task[task_id][1]
        body = f"# Evidencia {guard.redact(task_id)}\n\n{_link('tareas/' + task_id)}\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
        outputs[rel] = _front({GENERATED_KEY: GENERATED_VALUE}, body)
    research = evidence_dir / "research"
    if research.is_dir():
        for src in sorted(research.glob("*.md")):
            outputs[f"investigacion/{src.stem}.md"] = _front({GENERATED_KEY: GENERATED_VALUE}, src.read_text(encoding="utf-8", errors="replace"))
    # Ensure the selected root cannot redirect writes through existing nested links.
    for rel in outputs:
        target = root / rel
        if not _safe(root, target):
            raise HefestoError("Ruta de nota fuera de la bóveda.", EXIT_BLOCKED)
        cursor = target.parent
        while cursor != root:
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise HefestoError("No se permiten enlaces en la bóveda.", EXIT_BLOCKED)
            cursor = cursor.parent
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for rel, content in outputs.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)
    removed: list[Path] = []
    for target in sorted(root.rglob("*.md")):
        if not _safe(root, target) or target.is_symlink() or target.relative_to(root).as_posix() in outputs:
            continue
        try:
            meta, _ = memory.parse_note_content(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if meta.get(GENERATED_KEY) == GENERATED_VALUE:
            target.unlink(); removed.append(target)
    project_mod.update_git_exclude(project)
    exclude_path = project_mod.git_exclude_path(project)
    existing = exclude_path.read_text(encoding="utf-8").splitlines() if exclude_path.exists() else []
    if ".hefesto/vault/" not in {line.strip() for line in existing}:
        existing.append(".hefesto/vault/")
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("\n".join(existing) + "\n", encoding="utf-8", newline="\n")
    return BuildResult(root, written, removed)


def canvas(project: Path, out: Path | str | None = None) -> Path:
    res = build(project, out=out)
    root = res.root
    rm = roadmap.parse(roadmap.roadmap_path(project))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    padding = 40
    header = 60
    task_width = 260
    task_height = 140
    gap_y = 40
    gap_x = 80

    tasks_by_phase: dict[str, list[roadmap.Task]] = {}
    for task in rm.tasks:
        tasks_by_phase.setdefault(task.phase, []).append(task)

    current_x = 0
    group_y = 0
    group_width = task_width + 2 * padding

    for phase in rm.phases:
        phase_tasks = tasks_by_phase.get(phase, [])
        num_tasks = len(phase_tasks)
        if num_tasks > 0:
            group_height = header + num_tasks * task_height + (num_tasks - 1) * gap_y + padding
        else:
            group_height = header + padding

        group_id = f"group-{roadmap.slugify(phase)}"
        nodes.append({
            "id": group_id,
            "type": "group",
            "label": phase,
            "x": current_x,
            "y": group_y,
            "width": group_width,
            "height": group_height,
        })

        for i, task in enumerate(phase_tasks):
            task_x = current_x + padding
            task_y = group_y + header + i * (task_height + gap_y)
            task_id = f"task-{task.id}"
            nodes.append({
                "id": task_id,
                "type": "file",
                "file": f"tareas/{task.id}.md",
                "color": CANVAS_COLORS.get(task.status, "6"),
                "x": task_x,
                "y": task_y,
                "width": task_width,
                "height": task_height,
            })

        current_x += group_width + gap_x

    task_map = rm.by_id()
    for task in rm.tasks:
        for dep in task.depends_on:
            if dep in task_map:
                edges.append({
                    "id": f"edge-{dep}-{task.id}",
                    "fromNode": f"task-{dep}",
                    "toNode": f"task-{task.id}",
                })

    canvas_file = root / "roadmap.canvas"
    content = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2) + "\n"
    canvas_file.write_text(content, encoding="utf-8", newline="\n")
    return canvas_file

