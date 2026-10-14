"""Localización del proyecto e `init` idempotente."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hefesto import __version__, config, util
from hefesto.util import EXIT_INPUT, FRAMEWORK_ROOT, HefestoError, atomic_write_json

TEMPLATES = FRAMEWORK_ROOT / "templates" / "project"

GITIGNORE_ENTRIES = (
    ".hefesto/state.json",
    ".hefesto/.lock/",
    ".hefesto/trust.json",
    ".hefesto/logs/",
    ".hefesto/STOP",
    ".hefesto/worktree/",
    ".hefesto/worktrees/",
    ".hefesto/progress.jsonl",
    "graphify-out/",
)

BANNER = r"""
  _   _       __           _
 | | | | ___ / _| ___  ___| |_ ___
 | |_| |/ _ \ |_ / _ \/ __| __/ _ \
 |  _  |  __/  _|  __/\__ \ || (_) |
 |_| |_|\___|_|  \___||___/\__\___/   v{version}
"""


def find_project(start: Path | None = None) -> Path:
    """Sube desde `start` hasta encontrar un directorio con `.hefesto/`."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".hefesto").is_dir():
            return candidate
    raise HefestoError("No es un proyecto Hefesto (no hay `.hefesto/`). Ejecuta `hefesto init`.", EXIT_INPUT)


def hefesto_dir(project: Path) -> Path:
    return project / ".hefesto"


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")


def copy_templates(project: Path) -> list[str]:
    """Copia las plantillas sin sobrescribir nunca un fichero existente."""
    created: list[str] = []
    for src in sorted(TEMPLATES.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(TEMPLATES)
        dest = project / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        created.append(rel.as_posix())
    return created


def update_gitignore(project: Path) -> list[str]:
    path = project / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]
    if missing:
        lines = list(existing)
        if lines and lines[-1].strip():
            lines.append("")
        if "# Hefesto" not in present:
            lines.append("# Hefesto")
        lines.extend(missing)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return missing


def detect_main_branch(project: Path) -> str:
    result = _git(project, "symbolic-ref", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode == 0 and branch and not branch.startswith("hefesto/"):
        return branch
    return "main"


def ask_providers(provided: str | None) -> tuple[list[str], list[str]]:
    """Devuelve (aprobados, rechazados) entre Gemini y Codex."""
    candidates = ["gemini", "codex"]
    if provided is not None:
        chosen = [p.strip() for p in provided.split(",") if p.strip()]
        unknown = [p for p in chosen if p not in config.EXTERNAL_PROVIDERS]
        if unknown:
            raise HefestoError(f"Proveedores desconocidos: {', '.join(unknown)}", EXIT_INPUT)
        if "local" in chosen:
            raise HefestoError("El modelo local solo se activa con `hefesto local enable`.", EXIT_INPUT)
        return chosen, [p for p in candidates if p not in chosen]
    if not util.is_tty():
        print("Sin terminal interactiva: no se autoriza ningún proveedor externo. Usa `--providers gemini,codex`.")
        return [], candidates
    approved, rejected = [], []
    for name in candidates:
        if util.confirm(f"¿Autorizar que el código de este proyecto se envíe a {name.capitalize()}?", default=True):
            approved.append(name)
        else:
            rejected.append(name)
    return approved, rejected


def warn_rejected(cfg: config.Config, rejected: list[str]) -> list[str]:
    msgs: list[str] = []
    if not rejected:
        return msgs
    approved = set(cfg.approved_providers())
    for role in config.ROLES:
        chain = [e.split(":", 1)[0] for e in cfg.role_chain(role)]
        usable = [p for p in chain if p in approved and cfg.get("adapters", p, "enabled")]
        if usable and set(usable) == {"claude"} and any(p in rejected for p in chain):
            msgs.append(f"  - rol {role}: solo quedará Claude.")
    if msgs:
        msgs.insert(0, "Aviso: has rechazado " + ", ".join(rejected) + ". Estos roles cargarán sobre tu cuenta de Claude:")
        msgs.append("  Más llamadas a Claude significa agotar antes su cuota. Puedes autorizar luego con `hefesto providers add`.")
    return msgs


def init(target: Path, providers: str | None) -> list[str]:
    """Inicializa (o completa) un proyecto. Idempotente: nunca sobrescribe."""
    from hefesto import state as state_mod

    target.mkdir(parents=True, exist_ok=True)
    project = target.resolve()
    report: list[str] = [BANNER.format(version=__version__)]
    if not (project / ".git").exists():
        result = _git(project, "init")
        if result.returncode != 0:
            raise HefestoError(f"git init falló: {result.stderr.strip()}", EXIT_INPUT)
        report.append("git init")
    created = copy_templates(project)
    report += [f"creado {c}" for c in created]
    for sub in ("logs", "evidence"):
        (hefesto_dir(project) / sub).mkdir(parents=True, exist_ok=True)
    settings = config.init_project_settings(project)
    if "main_branch" not in settings:
        settings["main_branch"] = detect_main_branch(project)
        atomic_write_json(config.project_settings_path(project), settings)
    state_path = hefesto_dir(project) / "state.json"
    if not state_path.exists():
        state_mod.write_initial(project)
        report.append("creado .hefesto/state.json")
    added = update_gitignore(project)
    if added:
        report.append(".gitignore: " + ", ".join(added))
    already = settings.get("approved_external_providers", [])
    if providers is not None or not already:
        approved, rejected = ask_providers(providers)
        merged = sorted(set(already) | set(approved)) if providers is None else sorted(set(approved) | {p for p in already if p == "local"})
        settings = config.read_settings(config.project_settings_path(project))
        settings["approved_external_providers"] = merged
        config.write_settings(config.project_settings_path(project), settings)
        report.append("proveedores autorizados: " + (", ".join(merged) if merged else "ninguno (solo Claude)"))
        cfg = config.load(project)
        report += warn_rejected(cfg, rejected)
    from hefesto import sync

    changes = sync.run(project)
    report.append("sync: " + sync.summary(changes))
    return report
