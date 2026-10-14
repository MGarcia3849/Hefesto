"""Parser, validación y marcadores de `.hefesto/ROADMAP.md`."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from hefesto.util import EXIT_CONFLICT, EXIT_INPUT, HefestoError, atomic_write_text, rel_inside

PHASE_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
TASK_RE = re.compile(r"^- \[(?P<marker>[ ~x!])\]\s+(?P<id>[A-Za-z]+-\d+):\s*(?P<title>.*?)\s*$")
FIELD_RE = re.compile(r"^\s+-\s+(?P<key>[a-z_]+):\s*(?P<value>.*?)\s*$")
VALID_ID = re.compile(r"^TASK-\d{3,}$")

MARKERS = {" ": "pending", "~": "in_progress", "x": "done", "!": "blocked"}
TASK_ROLES = ("core", "runner", "forge", "visual")
SIZES = ("S", "M", "L")
REQUIRED = ("verify", "files", "role")
KNOWN_FIELDS = REQUIRED + ("depends_on", "size", "parallel", "accept", "contract")


@dataclass
class Task:
    id: str
    title: str
    marker: str
    phase: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def verify(self) -> str:
        value = self.fields.get("verify", "").strip()
        if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
            value = value[1:-1].strip()
        return value

    @property
    def files(self) -> list[str]:
        return _split_list(self.fields.get("files", ""))

    @property
    def role(self) -> str:
        return self.fields.get("role", "").strip().lower()

    @property
    def depends_on(self) -> list[str]:
        deps = _split_list(self.fields.get("depends_on", ""))
        return [] if deps in ([], ["none"], ["-"]) else deps

    @property
    def size(self) -> str:
        return (self.fields.get("size") or "M").strip().upper()

    @property
    def parallel(self) -> bool:
        return self.fields.get("parallel", "true").strip().lower() in ("true", "yes", "sí", "si", "1")

    @property
    def accept(self) -> str:
        return self.fields.get("accept", "").strip()

    @property
    def contract(self) -> list[str]:
        return _split_list(self.fields.get("contract", ""))

    @property
    def phase_slug(self) -> str:
        return slugify(self.phase)

    @property
    def status(self) -> str:
        return MARKERS[self.marker]


@dataclass
class Roadmap:
    path: Path
    phases: list[str]
    tasks: list[Task]
    parse_errors: list[str]

    def get(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise HefestoError(f"La tarea {task_id} no existe en el ROADMAP.", EXIT_INPUT)

    def by_id(self) -> dict[str, Task]:
        return {t.id: t for t in self.tasks}

    def phase_tasks(self, phase_slug: str) -> list[Task]:
        return [t for t in self.tasks if t.phase_slug == phase_slug]

    def contract_paths(self, task: Task) -> list[str]:
        """Tests de contrato de las dependencias (transitivas) de `task`."""
        index = self.by_id()
        seen: set[str] = set()
        out: list[str] = []
        stack = list(task.depends_on)
        while stack:
            dep = stack.pop()
            if dep in seen or dep not in index:
                continue
            seen.add(dep)
            out += [p for p in index[dep].contract if p not in out]
            stack += index[dep].depends_on
        return out


def _split_list(value: str) -> list[str]:
    value = value.strip().strip("`")
    if not value:
        return []
    return [part.strip().strip("`") for part in re.split(r"[,\s]+", value) if part.strip().strip("`")]


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", norm).strip("-") or "fase"


def roadmap_path(project: Path) -> Path:
    return project / ".hefesto" / "ROADMAP.md"


def parse(path: Path) -> Roadmap:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HefestoError(f"No existe {path}.", EXIT_INPUT) from exc
    phases: list[str] = []
    tasks: list[Task] = []
    errors: list[str] = []
    current: Task | None = None
    in_comment = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        phase = PHASE_RE.match(line)
        if phase:
            phases.append(phase.group("name"))
            current = None
            continue
        match = TASK_RE.match(line)
        if match:
            if not phases:
                errors.append(f"línea {number}: tarea {match.group('id')} fuera de una fase (`## Fase …`)")
            current = Task(id=match.group("id"), title=match.group("title"), marker=match.group("marker"),
                           phase=phases[-1] if phases else "", line=number)
            tasks.append(current)
            continue
        fmatch = FIELD_RE.match(line)
        if fmatch and current is not None:
            key = fmatch.group("key")
            if key in current.fields:
                errors.append(f"línea {number}: campo `{key}` repetido en {current.id}")
            current.fields[key] = fmatch.group("value")
            continue
        if stripped.startswith("- [") and current is None and not TASK_RE.match(line):
            errors.append(f"línea {number}: tarea con formato inválido: {stripped[:60]}")
    return Roadmap(path=path, phases=phases, tasks=tasks, parse_errors=errors)


def validate(rm: Roadmap, project: Path) -> list[str]:
    errors = list(rm.parse_errors)
    seen: set[str] = set()
    for task in rm.tasks:
        where = f"{task.id} (línea {task.line})"
        if not VALID_ID.match(task.id):
            errors.append(f"{where}: ID inválido; formato TASK-NNN")
        if task.id in seen:
            errors.append(f"{where}: ID duplicado")
        seen.add(task.id)
        for key in REQUIRED:
            if not task.fields.get(key, "").strip():
                errors.append(f"{where}: falta `{key}`")
        for key in task.fields:
            if key not in KNOWN_FIELDS:
                errors.append(f"{where}: campo desconocido `{key}`")
        if task.fields.get("role") and task.role not in TASK_ROLES:
            errors.append(f"{where}: rol `{task.role}` inválido ({', '.join(TASK_ROLES)})")
        if task.size not in SIZES:
            errors.append(f"{where}: size `{task.size}` inválido (S, M, L)")
        for rel in task.files + task.contract:
            if not rel_inside(project, rel):
                errors.append(f"{where}: ruta `{rel}` fuera del proyecto o absoluta")
        for rel in task.contract:
            if not any(rel == f or rel.startswith(f.rstrip("/") + "/") for f in task.files):
                errors.append(f"{where}: el test de contrato `{rel}` debe estar dentro de `files`")
    ids = {t.id for t in rm.tasks}
    for task in rm.tasks:
        for dep in task.depends_on:
            if dep not in ids:
                errors.append(f"{task.id}: depende de `{dep}`, que no existe")
            if dep == task.id:
                errors.append(f"{task.id}: depende de sí misma")
    errors += _cycles(rm)
    return errors


def _cycles(rm: Roadmap) -> list[str]:
    graph = {t.id: [d for d in t.depends_on if d != t.id] for t in rm.tasks}
    color: dict[str, int] = {}
    errors: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        color[node] = 1
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color.get(dep) == 1:
                cycle = trail[trail.index(dep):] + [dep] if dep in trail else [node, dep]
                errors.append("ciclo de dependencias: " + " → ".join(cycle))
            elif color.get(dep) is None:
                visit(dep, trail + [dep])
        color[node] = 2

    for node in graph:
        if color.get(node) is None:
            visit(node, [node])
    return errors


def load_valid(project: Path) -> Roadmap:
    rm = parse(roadmap_path(project))
    errors = validate(rm, project)
    if errors:
        raise HefestoError("ROADMAP inválido:\n  - " + "\n  - ".join(errors), EXIT_INPUT)
    return rm


def set_marker(path: Path, task_id: str, marker: str) -> None:
    """Cambia el marcador de una tarea conservando el resto del fichero. Llamar con el lock tomado."""
    if marker not in MARKERS:
        raise HefestoError(f"Marcador inválido {marker!r}", EXIT_INPUT)
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(newline)
    found = 0
    for i, line in enumerate(lines):
        match = TASK_RE.match(line)
        if match and match.group("id") == task_id:
            lines[i] = line[:3] + marker + line[4:]
            found += 1
    if found != 1:
        raise HefestoError(f"{task_id} aparece {found} veces en el ROADMAP.", EXIT_CONFLICT)
    atomic_write_text(path, newline.join(lines))


def dependencies_done(rm: Roadmap, task: Task) -> list[str]:
    """IDs de dependencias que aún no están `[x]`."""
    index = rm.by_id()
    return [d for d in task.depends_on if d in index and index[d].marker != "x"]


def next_task(rm: Roadmap) -> tuple[Task | None, str]:
    """Primera tarea pendiente con dependencias cumplidas; si no hay, el motivo."""
    split_needed: list[str] = []
    waiting = False
    for task in rm.tasks:
        if task.marker != " ":
            continue
        if dependencies_done(rm, task):
            waiting = True
            continue
        if task.size == "L":
            split_needed.append(task.id)
            continue
        return task, ""
    if any(t.marker == "~" for t in rm.tasks):
        return None, "hay una tarea en curso"
    if any(t.marker == "!" for t in rm.tasks):
        return None, "hay tareas bloqueadas por el circuit breaker; se necesita `hefesto unblock`"
    if split_needed:
        return None, "tareas de tamaño L sin dividir: " + ", ".join(split_needed) + " (el Core debe dividirlas)"
    if waiting:
        return None, "las tareas pendientes esperan dependencias"
    if not rm.tasks:
        return None, "el ROADMAP no tiene tareas; planifica con /hefesto-plan"
    return None, "todas las tareas están completadas"
