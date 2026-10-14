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
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hefesto import config, providers, skills
from hefesto.util import FRAMEWORK_ROOT, HefestoError, atomic_write_json, atomic_write_text, hefesto_home, read_json

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
        "SessionStart": [{"hooks": [{"type": "command", "command": "hefesto status --protocol"}]}],
        pre_event: [{"matcher": matcher, "hooks": [{"type": "command", "command": "hefesto hook pre-tool-use"}]}],
    }}


def targets(cfg: config.Config) -> dict[str, str | bytes]:
    """Ruta relativa → contenido de cada fichero generado."""
    out: dict[str, str | bytes] = {}
    if cfg.settings.get("install_mode") == "local":
        claude_tpl = read_json(FRAMEWORK_ROOT / "templates" / "project" / ".claude" / "settings.json", default={})
        deny = list(claude_tpl.get("permissions", {}).get("deny", [])) if isinstance(claude_tpl, dict) else []
        allow = [
            "Bash(hefesto:*)",
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
        ]
        ask = [
            "Bash(hefesto -C:*)",
            "Bash(hefesto --project:*)",
            "Bash(hefesto trust:*)",
            "Bash(hefesto unblock:*)",
            "Bash(hefesto finish:*)",
            "Bash(hefesto local:*)",
            "Bash(hefesto providers add:*)",
            "Bash(hefesto providers remove:*)",
            "Bash(hefesto providers reset:*)",
            "Bash(hefesto init:*)",
            "Bash(hefesto adopt:*)",
        ]
        local_settings = {
            "permissions": {
                "allow": allow,
                "ask": ask,
                "deny": deny,
            },
            "statusLine": {"type": "command", "command": "hefesto statusline"},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "hefesto status --protocol"}]}],
                "PreToolUse": [{
                    "matcher": "Bash|Read|Edit|Write|MultiEdit|Grep|Glob|NotebookEdit",
                    "hooks": [{"type": "command", "command": "hefesto hook pre-tool-use"}],
                }],
            },
        }
        out[".claude/settings.local.json"] = json.dumps(local_settings, indent=2) + "\n"
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
    promoted_root = hefesto_home() / "skills" / "promoted"
    used_names = {path.split("/")[2].casefold() for path in out if path.startswith(".agents/skills/")}
    if promoted_root.is_dir():
        for skill_dir in sorted(p for p in promoted_root.iterdir() if p.is_dir()):
            try:
                skills.check_confinement(skill_dir, hefesto_home())
                skills.validate_skill_name(skill_dir.name)
                source_file = skill_dir / "SOURCE.json"
                skills.check_confinement(source_file, hefesto_home())
                source = read_json(source_file, default={})
                pack_name = source.get("pack") if isinstance(source, dict) else None
                if pack_name:
                    enabled = bool(cfg.settings.get("packs", {}).get(pack_name, {}).get("enabled", False))
                    if not enabled:
                        continue
                skill_name = skills.skill_from_spec(source.get("spec", "") if isinstance(source, dict) else "")
                skill_file = skill_dir / "SKILL.md"
                skills.check_confinement(skill_file, hefesto_home())
                effective_skill, _ = skills.skill_identity(skill_file.read_bytes(), skill_name)
            except (HefestoError, OSError, ValueError) as exc:
                print(f"hefesto sync: aviso: skill promovida '{skill_dir.name}' se omite: {exc}", file=sys.stderr)
                continue
            if skill_name.casefold() in used_names:
                print(f"hefesto sync: aviso: skill promovida '{skill_name}' duplicada, se omite.", file=sys.stderr)
                continue
            used_names.add(skill_name.casefold())
            if (skill_dir / "SKILL.md").is_file():
                for p in sorted(skill_dir.rglob("*")):
                    skills.check_confinement(p, hefesto_home())
                    if p.is_file() and p.name.casefold() != "source.json":
                        rel = p.relative_to(skill_dir).as_posix()
                        try:
                            raw = effective_skill if rel == "SKILL.md" else p.read_bytes()
                        except OSError:
                            continue
                        for root in (".claude/skills", ".agents/skills"):
                            out[f"{root}/{skill_name}/{rel}"] = raw
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


def _digest(content: str | bytes) -> str:
    if isinstance(content, bytes):
        return hashlib.sha256(content).hexdigest()
    raw = content.encode("utf-8")
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def manifest_path(project: Path) -> Path:
    return project / ".hefesto" / "generated.json"


def run(project: Path, check: bool = False) -> list[Change]:
    from hefesto.skills import check_confinement

    cfg = config.load(project)
    manifest = read_json(manifest_path(project), default=None) or {"schema_version": 1, "files": {}}
    recorded: dict[str, str] = manifest.setdefault("files", {})
    changes: list[Change] = []
    current_targets = targets(cfg)

    for rel, content in sorted(current_targets.items()):
        path = project / rel
        check_confinement(path, project)
        wanted = _digest(content)
        if not path.exists():
            changes.append(Change(rel, "create"))
        else:
            raw_current = path.read_bytes()
            if isinstance(content, bytes):
                current = hashlib.sha256(raw_current).hexdigest()
            else:
                current = hashlib.sha256(raw_current.replace(b"\r\n", b"\n")).hexdigest()
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
            if isinstance(content, bytes):
                _atomic_write_bytes(path, content)
            else:
                atomic_write_text(path, content)
        recorded[rel] = wanted

    # Ficheros gestionados que ya no corresponden a ningún destino actual
    for rel in sorted(list(recorded.keys())):
        if rel in current_targets:
            continue
        path = project / rel
        check_confinement(path, project)
        if not path.exists():
            if not check:
                del recorded[rel]
            continue
        raw_current = path.read_bytes()
        h_bin = hashlib.sha256(raw_current).hexdigest()
        h_txt = hashlib.sha256(raw_current.replace(b"\r\n", b"\n")).hexdigest()
        if recorded.get(rel) in (h_bin, h_txt):
            changes.append(Change(rel, "delete"))
            if not check:
                try:
                    path.unlink()
                except OSError:
                    pass
                del recorded[rel]
                cleanable_roots = (
                    project / ".claude" / "skills",
                    project / ".agents" / "skills",
                    project / ".claude" / "agents",
                )
                parent = path.parent
                while any(parent != root and parent.is_relative_to(root) for root in cleanable_roots):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        else:
            changes.append(Change(rel, "conflict"))

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
