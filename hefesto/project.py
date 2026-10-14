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

LOCAL_EXCLUDES = (
    ".hefesto/",
    ".claude/settings.local.json",
    ".claude/skills/hefesto-*/",
    ".claude/agents/hefesto-*.md",
    ".agents/skills/hefesto-*/",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".gemini/settings.json",
    ".gemini/commands/hefesto/",
)

LOCAL_ROOT_SKIPPED = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".claude/settings.json",
}

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
        if candidate.name in ("worktree", "worktrees") and candidate.parent.name == ".hefesto":
            dot_hefesto = candidate.parent
            if (dot_hefesto / "state.json").exists() or (dot_hefesto / "settings.json").exists():
                return dot_hefesto.parent
    for candidate in (here, *here.parents):
        if (candidate / ".hefesto").is_dir():
            return candidate
    raise HefestoError("No es un proyecto Hefesto (no hay `.hefesto/`). Ejecuta `hefesto init`.", EXIT_INPUT)


def hefesto_dir(project: Path) -> Path:
    return project / ".hefesto"


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")


def copy_templates(project: Path, shared: bool = False) -> list[str]:
    """Copia las plantillas sin sobrescribir nunca un fichero existente."""
    created: list[str] = []
    if shared:
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
    else:
        for src in sorted(TEMPLATES.rglob("*")):
            if src.is_dir():
                continue
            rel = src.relative_to(TEMPLATES).as_posix()
            if rel in LOCAL_ROOT_SKIPPED:
                continue
            dest = project / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            created.append(rel)
        proto_src = TEMPLATES / "AGENTS.md"
        proto_dest = project / ".hefesto" / "AGENTS.md"
        if proto_src.exists() and not proto_dest.exists():
            proto_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(proto_src, proto_dest)
            created.append(".hefesto/AGENTS.md")
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


def git_exclude_path(project: Path) -> Path:
    result = _git(project, "rev-parse", "--git-path", "info/exclude")
    if result.returncode == 0 and result.stdout.strip():
        raw = Path(result.stdout.strip())
        return raw if raw.is_absolute() else (project / raw).resolve()
    return project / ".git" / "info" / "exclude"


def update_git_exclude(project: Path) -> list[str]:
    path = git_exclude_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in LOCAL_EXCLUDES if entry not in present]
    if missing or not path.exists():
        new_lines = list(existing) + missing
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")
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


def init(target: Path, providers: str | None = None, shared: bool = False) -> list[str]:
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
    settings_path = config.project_settings_path(project)
    existing_settings = config.read_settings(settings_path) if settings_path.exists() else {}
    if shared:
        mode = "shared"
    elif "install_mode" in existing_settings:
        mode = existing_settings["install_mode"]
    else:
        mode = "local"
    created = copy_templates(project, shared=(mode == "shared"))
    report += [f"creado {c}" for c in created]
    for sub in ("logs", "evidence"):
        (hefesto_dir(project) / sub).mkdir(parents=True, exist_ok=True)
    settings = config.init_project_settings(project)
    settings["install_mode"] = mode
    if "main_branch" not in settings:
        settings["main_branch"] = detect_main_branch(project)
    config.write_settings(settings_path, settings)
    state_path = hefesto_dir(project) / "state.json"
    if not state_path.exists():
        state_mod.write_initial(project)
        report.append("creado .hefesto/state.json")
    if mode == "shared":
        added = update_gitignore(project)
        if added:
            report.append(".gitignore: " + ", ".join(added))
    else:
        added = update_git_exclude(project)
        if added:
            report.append(".git/info/exclude: " + ", ".join(added))
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
