"""Detección de modo y stack, distribución y adopción de proyectos."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from hefesto import __version__, config, gitops, project, roadmap, sync, util, workflow
from hefesto.util import EXIT_BLOCKED, EXIT_CONFLICT, EXIT_INPUT, EXIT_OK, HefestoError


def validate_safe_project_paths(target: Path | str) -> None:
    """Verifica que .hefesto y .gitignore no sean enlaces simbólicos y que .hefesto no resuelva fuera."""
    root = Path(target)
    try:
        resolved_root = root.resolve()
    except OSError:
        resolved_root = root

    hefesto_candidates = [root / ".hefesto"]
    gitignore_candidates = [root / ".gitignore"]
    if resolved_root != root:
        hefesto_candidates.append(resolved_root / ".hefesto")
        gitignore_candidates.append(resolved_root / ".gitignore")

    for h in hefesto_candidates:
        is_link = h.is_symlink() or os.path.islink(h) or (hasattr(os.path, "isjunction") and os.path.isjunction(h))
        if is_link:
            raise HefestoError("`.hefesto` no puede ser un enlace simbólico.", EXIT_BLOCKED)
        if h.exists():
            resolved_h = h.resolve()
            try:
                rel = resolved_h.relative_to(resolved_root)
                if rel == Path(".") or str(rel).startswith(".."):
                    raise HefestoError("`.hefesto` resuelve fuera del proyecto.", EXIT_BLOCKED)
            except ValueError:
                raise HefestoError("`.hefesto` resuelve fuera del proyecto.", EXIT_BLOCKED)

    for g in gitignore_candidates:
        is_link = g.is_symlink() or os.path.islink(g) or (hasattr(os.path, "isjunction") and os.path.isjunction(g))
        if is_link:
            raise HefestoError("`.gitignore` no puede ser un enlace simbólico.", EXIT_BLOCKED)
        if g.exists():
            resolved_g = g.resolve()
            try:
                rel = resolved_g.relative_to(resolved_root)
                if str(rel).startswith(".."):
                    raise HefestoError("`.gitignore` resuelve fuera del proyecto.", EXIT_BLOCKED)
            except ValueError:
                raise HefestoError("`.gitignore` resuelve fuera del proyecto.", EXIT_BLOCKED)


def detect_mode(path: Path) -> str:
    """Detecta si el proyecto es `create`, `continue` o `improve`.

    - `continue` si existe `path/.hefesto/`.
    - `create` si no hay ficheros de código (se ignoran `.git/`, `README*`, `LICENSE*`,
      `.gitignore`, `.gitattributes` y ficheros ocultos).
    - `improve` en cualquier otro caso.
    """
    root = Path(path)
    if (root / ".hefesto").exists():
        return "continue"
    if not root.exists():
        return "create"
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        name = item.name.upper()
        if name.startswith("README") or name.startswith("LICENSE") or name.startswith("LICENCE"):
            continue
        return "improve"
    return "create"


def detect_stack(path: Path) -> dict[str, Any]:
    """Detecta los stacks del proyecto y sus comandos de test, lint y build.

    Señales:
    - `pubspec.yaml` → flutter (`flutter test`, `flutter analyze`, `flutter build`)
    - `package.json` → node (`npm test`/`npm run lint`/`npm run build`, solo si el script existe)
    - `pyproject.toml`, `setup.py` o `requirements.txt` → python (`python -m pytest`)
    - `Cargo.toml` → rust (`cargo test`, `cargo clippy`, `cargo build`)
    - `go.mod` → go (`go test ./...`, `go vet ./...`, `go build ./...`)
    """
    root = Path(path)
    stacks: list[str] = []
    test_cmd: str | None = None
    lint_cmd: str | None = None
    build_cmd: str | None = None

    # flutter
    if (root / "pubspec.yaml").exists():
        stacks.append("flutter")
        test_cmd = test_cmd or "flutter test"
        lint_cmd = lint_cmd or "flutter analyze"
        build_cmd = build_cmd or "flutter build"

    # node
    pkg_path = root / "package.json"
    if pkg_path.exists():
        stacks.append("node")
        scripts: dict[str, Any] = {}
        try:
            raw = pkg_path.read_bytes().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
                scripts = data["scripts"]
        except Exception:
            scripts = {}
        if isinstance(scripts, dict):
            if "test" in scripts and test_cmd is None:
                test_cmd = "npm test"
            if "lint" in scripts and lint_cmd is None:
                lint_cmd = "npm run lint"
            if "build" in scripts and build_cmd is None:
                build_cmd = "npm run build"

    # python
    if any((root / marker).exists() for marker in ("pyproject.toml", "setup.py", "requirements.txt")):
        stacks.append("python")
        test_cmd = test_cmd or "python -m pytest"

    # rust
    if (root / "Cargo.toml").exists():
        stacks.append("rust")
        test_cmd = test_cmd or "cargo test"
        lint_cmd = lint_cmd or "cargo clippy"
        build_cmd = build_cmd or "cargo build"

    # go
    if (root / "go.mod").exists():
        stacks.append("go")
        test_cmd = test_cmd or "go test ./..."
        lint_cmd = lint_cmd or "go vet ./..."
        build_cmd = build_cmd or "go build ./..."

    return {
        "stacks": stacks,
        "test": test_cmd,
        "lint": lint_cmd,
        "build": build_cmd,
    }


def extract_readme_headings(path: Path) -> list[str]:
    """Extrae encabezados de nivel 1 y 2 (# y ##) de un fichero README."""
    try:
        content = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    headings: list[str] = []
    lines = content.splitlines()
    in_code_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            headings.append(stripped)
        elif stripped.startswith("## ") and not stripped.startswith("### "):
            headings.append(stripped)
        elif i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if stripped and not stripped.startswith("#"):
                if next_line and set(next_line) == {"="} and len(next_line) >= 2:
                    headings.append(f"# {stripped}")
                elif next_line and set(next_line) == {"-"} and len(next_line) >= 2:
                    headings.append(f"## {stripped}")
    return headings


def find_existing_docs(root: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Encuentra ficheros de documentación existentes y extrae encabezados de READMEs.

    Busca:
    - README* en la raíz
    - docs/**/*.md
    - CHANGELOG* en la raíz
    - ADR en docs/adr
    """
    if not root.exists():
        return [], {}

    doc_paths: set[Path] = set()

    # README* y CHANGELOG* en la raíz
    try:
        for item in root.iterdir():
            if not item.is_file():
                continue
            name_upper = item.name.upper()
            if name_upper.startswith("README") or name_upper.startswith("CHANGELOG"):
                doc_paths.add(item)
    except OSError:
        pass

    # docs/**/*.md y docs/adr
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        try:
            for item in docs_dir.rglob("*.md"):
                if item.is_file():
                    doc_paths.add(item)
        except OSError:
            pass
        adr_dir = docs_dir / "adr"
        if adr_dir.is_dir():
            try:
                for item in adr_dir.rglob("*"):
                    if item.is_file():
                        doc_paths.add(item)
            except OSError:
                pass

    rel_docs = sorted(p.relative_to(root).as_posix() for p in doc_paths)

    readme_headings: dict[str, list[str]] = {}
    for rel in rel_docs:
        p = root / rel
        if p.name.upper().startswith("README"):
            headings = extract_readme_headings(p)
            if headings:
                readme_headings[rel] = headings

    return rel_docs, readme_headings


def find_existing_code_path(root: Path) -> str:
    """Busca una ruta existente dentro del proyecto para el campo `files` del ROADMAP."""
    if not root.exists():
        return "src/"
    for candidate in ("src", "lib", "app", "pkg"):
        if (root / candidate).exists():
            return f"{candidate}/"
    try:
        items = sorted(root.iterdir())
    except OSError:
        return "src/"
    for item in items:
        rel = item.name
        if rel.startswith(".") or rel in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "README.md", "LICENSE", "LICENSE.md"):
            continue
        return f"{rel}/" if item.is_dir() else rel
    for item in items:
        if not item.name.startswith("."):
            return f"{item.name}/" if item.is_dir() else item.name
    return "src/"


def adopt(target: Path, providers: str | None = None) -> list[str]:
    """Inicializa Hefesto en un proyecto existente sin modificar su código."""
    validate_safe_project_paths(target)
    project_dir = Path(target).resolve()

    # Pre-validación y detección antes de escribir nada
    stack = detect_stack(project_dir)
    rel_docs, readme_headings = find_existing_docs(project_dir)

    rm_path = project_dir / ".hefesto" / "ROADMAP.md"
    pre_existing_rm_error: str | None = None
    pre_existing_tasks = False
    if rm_path.exists():
        try:
            rm_obj = roadmap.parse(rm_path)
            rm_errs = roadmap.validate(rm_obj, project_dir)
            if rm_errs:
                pre_existing_rm_error = "; ".join(rm_errs)
            else:
                pre_existing_tasks = len(rm_obj.tasks) > 0
        except Exception as exc:
            pre_existing_rm_error = str(exc)

    report = list(project.init(target, providers=providers))

    stacks_desc = ", ".join(stack["stacks"]) if stack["stacks"] else "ninguno"
    doc_lines = [f"- {d}" for d in rel_docs] if rel_docs else ["- ninguno"]
    decisions_lines: list[str] = []
    if readme_headings:
        for r_name, headings in readme_headings.items():
            decisions_lines.append(f"- {r_name}:")
            for h in headings:
                decisions_lines.append(f"  - {h}")
    else:
        decisions_lines.append("- ningunas")

    decision_body = (
        f"Stacks detectados: {stacks_desc}\n\n"
        f"- stacks: {stacks_desc}\n"
        f"- test: {stack['test'] or 'ninguno'}\n"
        f"- lint: {stack['lint'] or 'ninguno'}\n"
        f"- build: {stack['build'] or 'ninguno'}\n\n"
        f"Documentación existente:\n"
        + "\n".join(doc_lines)
        + "\n\n"
        + "Decisiones visibles a revisar por el Core:\n"
        + "\n".join(decisions_lines)
        + "\n"
    )
    workflow.append_decision(project_dir, "Stack detectado en la adopción", decision_body)
    report.append("registrada decisión de stack detectado en .hefesto/DECISIONS.md")

    if pre_existing_rm_error is not None:
        report.append(f"aviso: no se pudo leer o validar .hefesto/ROADMAP.md ({pre_existing_rm_error}); se conserva sin cambios")
    elif pre_existing_tasks:
        report.append(".hefesto/ROADMAP.md ya contiene tareas; no se genera la fase de adopción")
    else:
        rm_valid = False
        rm_empty = False
        parse_err: str | None = None
        if rm_path.exists():
            try:
                rm = roadmap.parse(rm_path)
                errors = roadmap.validate(rm, project_dir)
                if errors:
                    parse_err = "; ".join(errors)
                else:
                    rm_valid = True
                    rm_empty = len(rm.tasks) == 0
            except Exception as exc:
                parse_err = str(exc)

        if rm_valid and rm_empty:
            verify_cmd = stack["test"] if stack.get("test") else "true"
            code_path = find_existing_code_path(project_dir)
            roadmap_content = (
                "# ROADMAP\n\n"
                "## Fase 1 — Adopción\n"
                "- [ ] TASK-001: Adopción del proyecto\n"
                f"  - verify: `{verify_cmd}`\n"
                f"  - files: {code_path}\n"
                "  - role: runner\n"
                "  - size: S\n"
            )
            util.atomic_write_text(rm_path, roadmap_content)
            report.append("creada tarea TASK-001 en .hefesto/ROADMAP.md")
        elif not rm_valid:
            err_msg = parse_err or "fichero no encontrado"
            report.append(f"aviso: no se pudo leer o validar .hefesto/ROADMAP.md ({err_msg}); se conserva sin cambios")
        else:
            report.append(".hefesto/ROADMAP.md ya contiene tareas; no se genera la fase de adopción")

    return report


def bootstrap(target: Path | str, providers: str | None = None, yes: bool = False) -> int:
    """Detecta el modo de trabajo (crear, continuar, mejorar) y arranca."""
    validate_safe_project_paths(target)
    project_dir = Path(target).resolve()
    mode = detect_mode(project_dir)
    mode_names = {"create": "crear", "continue": "continuar", "improve": "mejorar"}
    mode_sp = mode_names.get(mode, mode)
    rec_cmds = {
        "create": "hefesto init",
        "continue": "hefesto resume",
        "improve": "hefesto adopt",
    }
    rec_cmd = rec_cmds.get(mode, "hefesto status")
    print(f"Modo detectado: {mode_sp}. Comando recomendado: {rec_cmd}")
    if not yes:
        if not util.is_tty():
            return EXIT_OK
        if not util.confirm(f"¿Desea proceder con `{rec_cmd}`?"):
            return EXIT_OK
    effective_providers = ("gemini,codex" if providers is None else providers) if yes else providers
    if mode == "create":
        for line in project.init(project_dir, providers=effective_providers):
            print(line)
        print("Siguiente paso sugerido: /hefesto-plan")
        return EXIT_OK
    if mode == "continue":
        print("Para continuar con el proyecto ejecuta: hefesto resume")
        return EXIT_OK
    if mode == "improve":
        for line in adopt(project_dir, providers=effective_providers):
            print(line)
        return EXIT_OK
    return EXIT_OK


def available_tags(root: Path) -> list[str]:
    """Tags `vX.Y.Z[-sufijo]` del repositorio de Hefesto en `root`, tras `git fetch --tags origin`."""
    root_dir = Path(root)
    gitops.git(root_dir, "fetch", "-q", "--tags", "origin", check=False)
    raw = gitops.git(root_dir, "tag", check=False).stdout
    tags = [line.strip() for line in raw.splitlines() if line.strip()]

    def parse_semver(tag: str) -> tuple[int, int, int, int, str] | None:
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", tag)
        if not m:
            return None
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        suffix = m.group(4)
        suffix_flag = 1 if suffix is None else 0
        return (major, minor, patch, suffix_flag, suffix or "")

    valid_tags: list[tuple[tuple[int, int, int, int, str], str]] = []
    for tag in tags:
        parsed = parse_semver(tag)
        if parsed is not None:
            valid_tags.append((parsed, tag))

    valid_tags.sort(key=lambda item: item[0])
    return [tag for _, tag in valid_tags]


def self_update(root: Path, to: str | None = None, project: Path | None = None) -> str:
    """Actualiza la copia instalada de Hefesto en `root` a un tag de versión."""
    util.require_tty("self-update")

    root_dir = Path(root)
    status = gitops.git(root_dir, "status", "--porcelain", check=False).stdout.strip()
    if status:
        raise HefestoError("La copia de Hefesto tiene cambios sin commitear.", EXIT_CONFLICT)

    tags = available_tags(root_dir)
    if to:
        if to not in tags:
            raise HefestoError(f"La versión `{to}` no existe entre los tags disponibles.", EXIT_INPUT)
        target_tag = to
    else:
        if not tags:
            raise HefestoError("No hay versiones disponibles para actualizar.", EXIT_INPUT)
        target_tag = tags[-1]

    show_res = gitops.git(root_dir, "show", f"{target_tag}:CHANGELOG.md", check=False)
    if show_res.returncode == 0 and show_res.stdout.strip():
        print(show_res.stdout)

    if not util.confirm(f"¿Actualizar Hefesto a {target_tag}?"):
        return f"Actualización a {target_tag} cancelada."

    gitops.git(root_dir, "checkout", "-q", target_tag)

    home = util.hefesto_home()
    home.mkdir(parents=True, exist_ok=True)
    util.atomic_write_text(home / "VERSION", f"{target_tag}\n")

    if project is not None:
        sync.run(Path(project))

    return f"Hefesto actualizado a {target_tag}."


# Pasos de migración registrados entre versiones (vacía por ahora).
# Cada paso es una tupla: (versión_destino, función que recibe (project: Path, settings: dict[str, Any]))
MIGRATION_STEPS: list[tuple[str, Callable[[Path, dict[str, Any]], None]]] = []


def _parse_version(v: str | None) -> tuple[int, int, int]:
    """Parsea una versión semver base a tupla (mayor, menor, parche), ignorando sufijos y 'v'."""
    if not v:
        return (0, 0, 0)
    base = str(v).strip().lstrip("v").split("-", 1)[0]
    parts: list[int] = []
    for part in base.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def needs_migration(settings: dict[str, Any] | None) -> bool:
    """True si falta hefesto_version o si su versión base (sin sufijo tras el guion) difiere de la de __version__."""
    if not settings or not isinstance(settings, dict):
        return True
    version = settings.get("hefesto_version")
    if not version:
        return True
    base_project = str(version).split("-", 1)[0].strip()
    base_current = __version__.split("-", 1)[0].strip()
    return base_project != base_current


def migrate(project: Path) -> str:
    """Aplica migraciones y copia de seguridad si la versión del proyecto difiere de la actual."""
    project_dir = Path(project)
    settings_path = config.project_settings_path(project_dir)
    settings = config.read_settings(settings_path)

    if not needs_migration(settings):
        return "El proyecto ya está al día con la versión actual de Hefesto."

    current_version = settings.get("hefesto_version")

    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = project_dir / ".hefesto" / "backups" / timestamp
    if backup_dir.exists():
        timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = project_dir / ".hefesto" / "backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    files_to_backup = ("settings.json", "ROADMAP.md", "DECISIONS.md", "LEARNINGS.md")
    for name in files_to_backup:
        src = project_dir / ".hefesto" / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)

    proj_key = _parse_version(current_version)
    for target_version, step_fn in MIGRATION_STEPS:
        if _parse_version(target_version) > proj_key:
            step_fn(project_dir, settings)

    settings["hefesto_version"] = __version__
    config.write_settings(settings_path, settings)

    sync.run(project_dir)

    v_from = current_version or "desconocida"
    return f"Proyecto migrado de {v_from} a {__version__}."
