"""`hefesto sync`: materializa el protocolo único y la config en los ficheros nativos de cada orquestador.

Fuente: `prompts/protocol/{plan,next,retro}.md`, `prompts/<rol>.md` y la config. Destinos:
- Claude Code: `.claude/skills/hefesto-*/SKILL.md` y `.claude/agents/hefesto-<rol>.md`.
- Codex (y Gemini, que también las lee): `.agents/skills/hefesto-*/SKILL.md`; `.codex/hooks.json`, `.codex/config.toml`.
- Gemini CLI: `.gemini/commands/hefesto/*.toml` (`/hefesto:next`) y hooks en `.gemini/settings.json`.

Cada fichero generado queda registrado con su hash en `.hefesto/generated.json`; si el usuario lo editó a
mano, se informa del conflicto en vez de sobrescribirlo.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, providers
from hefesto.util import FRAMEWORK_ROOT, atomic_write_json, atomic_write_text, read_json

PROTOCOL_DIR = FRAMEWORK_ROOT / "prompts" / "protocol"
PROMPTS_DIR = FRAMEWORK_ROOT / "prompts"
SKILLS_DIR = FRAMEWORK_ROOT / "skills"
STEPS = ("plan", "next", "retro")
WORKER_ROLES = ("runner", "forge", "visual", "reviewer")
NOTICE = "Generado por `hefesto sync`. No lo edites: los cambios a mano se detectan como conflicto."

ROLE_DESCRIPTIONS = {
    "runner": "Implementación rutinaria de una tarea de Hefesto con contexto mínimo. Úsalo para tareas S/M ya contratadas.",
    "forge": "Problemas difíciles, algoritmos y tipado en tareas de Hefesto; recoge lo que el Runner no resolvió.",
    "visual": "UI, UX, animación y SVG en tareas de Hefesto, con las skills visuales.",
    "reviewer": "Revisión en solo lectura del diff de una tarea de Hefesto: criterios, seguridad, tests y contratos.",
}
ROLE_TOOLS = {
    "runner": "Read, Edit, Write, Grep, Glob, Bash",
    "forge": "Read, Edit, Write, Grep, Glob, Bash",
    "visual": "Read, Edit, Write, Grep, Glob, Bash",
    "reviewer": "Read, Grep, Glob",
}

@dataclass
class Change:
    path: str
    action: str  # create | update | unchanged | conflict


def protocol_step(step: str) -> tuple[dict[str, str], str]:
    """Frontmatter (name, description) y cuerpo de un paso del protocolo."""
    text = (PROTOCOL_DIR / f"{step}.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        return {"name": f"hefesto-{step}", "description": step}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if value:
            meta[key.strip()] = value.strip()
    return meta, match.group(2).lstrip("\n")


def render_prompt(step: str) -> str:
    """Texto del paso para pegarlo o pasarlo en modo no interactivo (`hefesto prompt`)."""
    return protocol_step(step)[1]


def _skill(meta: dict[str, str], body: str) -> str:
    return f"---\nname: {meta['name']}\ndescription: {meta['description']}\n---\n\n<!-- {NOTICE} -->\n\n{body}"


def _toml_string(text: str) -> str:
    return '"""\n' + text.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'


def _claude_model(cfg: config.Config, role: str) -> str:
    for entry in cfg.role_chain(role):
        provider, model = providers.split_entry(entry)
        if provider == "claude":
            return model or "inherit"
    return "inherit"


def _hooks_json(pre_event: str, matcher: str) -> dict[str, object]:
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "hefesto status"}]}],
        pre_event: [{"matcher": matcher, "hooks": [{"type": "command", "command": "hefesto hook pre-tool-use"}]}],
    }}


def targets(cfg: config.Config) -> dict[str, str]:
    """Ruta relativa → contenido de cada fichero generado."""
    out: dict[str, str] = {}
    for step in STEPS:
        meta, body = protocol_step(step)
        skill = _skill(meta, body)
        out[f".claude/skills/hefesto-{step}/SKILL.md"] = skill
        out[f".agents/skills/hefesto-{step}/SKILL.md"] = skill
        out[f".gemini/commands/hefesto/{step}.toml"] = (
            f"# {NOTICE}\ndescription = {json.dumps(meta['description'], ensure_ascii=False)}\n"
            f"prompt = {_toml_string(body)}\n")
    for role in WORKER_ROLES:
        rules = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8").strip()
        out[f".claude/agents/hefesto-{role}.md"] = (
            f"---\nname: hefesto-{role}\ndescription: {ROLE_DESCRIPTIONS[role]}\n"
            f"tools: {ROLE_TOOLS[role]}\nmodel: {_claude_model(cfg, role)}\n---\n\n<!-- {NOTICE} -->\n\n{rules}\n\n"
            "Al terminar, el orquestador registra tu trabajo con `hefesto review`/`hefesto note` "
            f"indicando `--author claude:{_claude_model(cfg, role)}`.\n")
    if SKILLS_DIR.is_dir():
        for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")):
            source = skill_dir / "SKILL.md"
            if source.exists():
                text = source.read_text(encoding="utf-8")
                for root in (".claude/skills", ".agents/skills"):
                    out[f"{root}/hefesto-skill-{skill_dir.name}/SKILL.md"] = text
    out[".codex/hooks.json"] = json.dumps(_hooks_json("PreToolUse", "Bash|apply_patch|shell|exec_command"),
                                          indent=2) + "\n"
    out[".codex/config.toml"] = (f"# {NOTICE}\n# Modo más restrictivo que permite trabajar: escribir solo en el\n"
                                 "# proyecto, sin red y pidiendo aprobación para lo demás.\n"
                                 'sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n\n'
                                 "[sandbox_workspace_write]\nnetwork_access = false\n")
    out[".gemini/settings.json"] = json.dumps(_hooks_json(
        "BeforeTool", "run_shell_command|read_file|read_many_files|write_file|replace|glob|search_file_content"),
        indent=2) + "\n"
    return out


def _digest(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def manifest_path(project: Path) -> Path:
    return project / ".hefesto" / "generated.json"


def run(project: Path, check: bool = False) -> list[Change]:
    cfg = config.load(project)
    manifest = read_json(manifest_path(project), default=None) or {"schema_version": 1, "files": {}}
    recorded: dict[str, str] = manifest.setdefault("files", {})
    changes: list[Change] = []
    for rel, content in sorted(targets(cfg).items()):
        path = project / rel
        wanted = _digest(content)
        if not path.exists():
            changes.append(Change(rel, "create"))
        else:
            current = _digest(path.read_bytes())
            if current == wanted:
                changes.append(Change(rel, "unchanged"))
            elif recorded.get(rel) == current:
                changes.append(Change(rel, "update"))
            else:
                changes.append(Change(rel, "conflict"))
                continue
        if check:
            continue
        if changes[-1].action in ("create", "update"):
            atomic_write_text(path, content)
        recorded[rel] = wanted
    if not check:
        atomic_write_json(manifest_path(project), manifest)
    return changes


def summary(changes: list[Change]) -> str:
    lines = [f"  {c.action:<9} {c.path}" for c in changes if c.action != "unchanged"]
    conflicts = [c for c in changes if c.action == "conflict"]
    head = f"{len(changes)} ficheros gestionados; {len(lines)} con cambios."
    tail = ("\nConflictos: estos ficheros se editaron a mano y no se han sobrescrito. Integra tus cambios en la "
            "fuente (prompts/ o la config) o bórralos para regenerarlos.") if conflicts else ""
    return head + ("\n" + "\n".join(lines) if lines else "") + tail
