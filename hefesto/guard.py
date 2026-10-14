"""Barreras de seguridad deterministas: rutas protegidas, secretos y comandos peligrosos.

Se aplican desde la CLI para cualquier orquestador (los hooks de Claude solo protegen a Claude).
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

DEFAULT_PROTECTED = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "*.keystore", "*.jks", "credentials.json", "service-account*.json",
)

# Patrones de secretos por contenido. Prefieren falsos positivos a dejar pasar un secreto.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clave privada", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("token de GitHub", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("clave de Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("clave de OpenAI", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}")),
    ("clave de Google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("token de Slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("token de Stripe", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("cadena de conexión con credenciales",
     re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s:/@'\"]{1,64}:[^\s@/'\"]{3,128}@[^\s/'\"]+", re.I)),
    ("asignación de secreto",
     re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd)\b"
                r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
)

DANGEROUS_COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("git push", re.compile(r"\bgit\b[^;&|\n]*\bpush\b")),
    ("git reset --hard", re.compile(r"\bgit\b[^;&|\n]*\breset\b[^;&|\n]*--hard\b")),
    ("git checkout -- .", re.compile(r"\bgit\b[^;&|\n]*\bcheckout\b[^;&|\n]*--\s+\.(?:\s|$)")),
    ("git restore .", re.compile(r"\bgit\b[^;&|\n]*\brestore\b[^;&|\n]*(?:\s)\.(?:\s|$)")),
    ("git clean", re.compile(r"\bgit\b[^;&|\n]*\bclean\b[^;&|\n]*-[a-zA-Z]*f")),
    ("rm -rf", re.compile(r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r|--recursive\s+--force|--force\s+--recursive)\b")),
    ("curl | sh", re.compile(r"\b(?:curl|wget)\b[^;&|\n]*\|\s*(?:(?:/usr)?/bin/)?(?:ba)?sh\b")),
    ("| sh", re.compile(r"\|\s*(?:(?:/usr)?/bin/)?(?:ba)?sh\b")),
    ("Remove-Item -Recurse -Force", re.compile(r"(?i)\bRemove-Item\b[^;\n]*-Recurse[^;\n]*-Force|\bRemove-Item\b[^;\n]*-Force[^;\n]*-Recurse")),
    ("--dangerously-skip-permissions", re.compile(r"--dangerously-skip-permissions|--yolo\b|danger-full-access")),
)


def _norm(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_protected(path: str, patterns: Iterable[str] = DEFAULT_PROTECTED) -> bool:
    """True si el nombre o cualquier componente de la ruta coincide con un patrón protegido."""
    norm = _norm(path)
    parts = PurePosixPath(norm).parts
    for pattern in patterns:
        if fnmatch.fnmatch(norm, pattern):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def find_secrets(text: str) -> list[str]:
    """Devuelve los tipos de secreto encontrados (sin el valor)."""
    found: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


def redact(text: str) -> str:
    out = text
    for _label, pattern in SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def dangerous_command(command: str) -> str | None:
    for label, pattern in DANGEROUS_COMMANDS:
        if pattern.search(command):
            return label
    return None


def branch_switch_target(command: str) -> str | None:
    """Si el comando cambia de rama, devuelve la rama destino; None si no cambia de rama."""
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        if len(tokens) < 3 or tokens[0] != "git" or tokens[1] not in ("checkout", "switch"):
            continue
        args = tokens[2:]
        if "--" in args:
            continue  # checkout de ficheros, no de rama
        positional: list[str] = []
        skip = False
        for i, arg in enumerate(args):
            if skip:
                skip = False
                continue
            if arg in ("-b", "-B", "-c", "-C", "--orphan"):
                if i + 1 < len(args):
                    return args[i + 1]
                return ""
            if arg.startswith("-"):
                continue
            positional.append(arg)
        if positional:
            return positional[0]
    return None


def branch_allowed(branch: str) -> bool:
    return branch.startswith("hefesto/")


PATH_KEYS = ("file_path", "path", "notebook_path", "absolute_path")
CONTROL_FILES = (".hefesto/state.json", ".hefesto/trust.json", ".hefesto/settings.json")

# Nombres de herramientas de Gemini CLI y Codex → equivalente de Claude Code.
TOOL_ALIASES = {
    "run_shell_command": "Bash", "shell": "Bash", "exec_command": "Bash",
    "read_file": "Read", "read_many_files": "Read", "write_file": "Write", "replace": "Edit",
    "glob": "Glob", "search_file_content": "Grep", "grep": "Grep",
    "apply_patch": "ApplyPatch",
}
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "ApplyPatch")
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


REDIRECTION_STRIP = re.compile(
    r"2>&1|2>& 1|1>&2|>&2|(?:&>>?|[0-2]?>>?)\s*(?:/dev/null|NUL\b)",
    re.I,
)

CONTROL_PUNCTUATION = set(";&|<>()")


def is_plain_hefesto_invocation(command: str) -> bool:
    """Devuelve True si `command` es una invocación simple de `hefesto` sin operadores ni sustituciones."""
    if "#" in command or any(ch in command for ch in ("$", "`", "\\", "\n", "\r")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.commenters = ""
        lexer.whitespace_split = True
        tokens = list(lexer)
    except Exception:
        return False
    if not tokens or tokens[0] != "hefesto":
        return False
    if any(tok and all(c in CONTROL_PUNCTUATION for c in tok) for tok in tokens):
        return False
    return True


def _check_bash(command: str, patterns: tuple[str, ...]) -> str | None:
    if is_plain_hefesto_invocation(command):
        return None
    label = dangerous_command(command)
    if label:
        return f"Hefesto bloquea `{label}`: usa la CLI `hefesto` (finish/discard) o pide al usuario que lo haga."
    target = branch_switch_target(command)
    if target is not None and not branch_allowed(target):
        return f"Hefesto bloquea el cambio a la rama `{target}`: solo se trabaja en ramas `hefesto/*`."
    for word in re.split(r"[\s'\"=<>|;&()]+", command):
        if word and not word.startswith("-") and is_protected(word, patterns):
            return f"Hefesto bloquea el acceso a una ruta protegida (`{word}`)."
    command_for_control = REDIRECTION_STRIP.sub(" ", command)
    for control in CONTROL_FILES:
        for seg in re.split(r"&&|\|\||;|\|", command_for_control):
            if control in seg.replace("\\", "/") and re.search(r">|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b", seg):
                return f"`{control}` solo lo modifica la CLI `hefesto`."
    return None


def _check_path(tool: str, value: str, patterns: tuple[str, ...]) -> str | None:
    if is_protected(value, patterns):
        return f"Hefesto bloquea el acceso a una ruta protegida (`{value}`)."
    if tool in WRITE_TOOLS:
        norm = value.replace("\\", "/")
        if any(norm.endswith(control) for control in CONTROL_FILES):
            return f"`{value}` solo lo modifica la CLI `hefesto`."
    return None


def _check_pack_policies(project: Path | None, cmd_str: str, fail_closed: bool = False) -> str | None:
    cat: dict[str, dict[str, Any]] | None = None
    try:
        from hefesto import packs
        cat = packs.load_catalog()
    except Exception as exc:
        print(f"hefesto hook: aviso: no se pudo leer el catálogo de packs: {exc}", file=sys.stderr)

    settings_readable = True
    settings: dict[str, object] = {}
    if project is not None:
        try:
            from hefesto import config
            settings_path = config.project_settings_path(project)
            if not settings_path.is_file():
                raise FileNotFoundError(f"No existe {settings_path}")
            settings = config.read_settings(settings_path)
        except Exception as exc:
            settings_readable = False
            print(f"hefesto hook: aviso: no se pudo leer settings.json: {exc}", file=sys.stderr)
    else:
        settings_readable = False

    packs_data = settings.get("packs", {}) if isinstance(settings, dict) else {}
    active_packs = {
        name for name, pdata in packs_data.items()
        if isinstance(pdata, dict) and pdata.get("enabled")
    }

    # (b) si el catálogo no se puede leer o es inválido:
    # si settings.json es legible y algún pack está activo (enabled true), bloquear el comando con código 2
    # y un mensaje claro (no se puede verificar la política); si no hay ningún pack activo, dejar pasar.
    if cat is None:
        if settings_readable and active_packs:
            return "Hefesto bloquea este comando: no se puede verificar la política de packs porque el catálogo no se puede leer o es inválido."
        if settings_readable and not active_packs:
            return None
        if fail_closed or not settings_readable:
            return "Hefesto bloquea este comando: no se puede verificar la política de packs."
        return None

    # (a) si settings.json del proyecto no se puede leer: bloquear lo que encaje con cualquier policy_block válida
    # del catálogo (como ahora)
    if not settings_readable or fail_closed:
        for pack_name, entry in cat.items():
            pb = entry.get("policy_block")
            if pb and isinstance(pb, list):
                for pat in pb:
                    if not isinstance(pat, str):
                        continue
                    try:
                        compiled = re.compile(pat)
                        if compiled.search(cmd_str):
                            return f"Hefesto bloquea este comando según la política del pack '{pack_name}'."
                    except re.error as exc:
                        print(
                            f"hefesto hook: aviso: expresión de policy_block inválida en pack '{pack_name}' ({pat!r}): {exc}",
                            file=sys.stderr,
                        )
        return None

    has_invalid_regex = False
    invalid_regex_packs: set[str] = set()
    compiled_by_pack: list[tuple[str, list[re.Pattern[str]]]] = []

    for name, entry in cat.items():
        pb = entry.get("policy_block")
        if pb and isinstance(pb, list):
            pack_patterns: list[re.Pattern[str]] = []
            for pat in pb:
                if not isinstance(pat, str):
                    has_invalid_regex = True
                    invalid_regex_packs.add(name)
                    continue
                try:
                    compiled = re.compile(pat)
                    pack_patterns.append(compiled)
                except re.error as exc:
                    has_invalid_regex = True
                    invalid_regex_packs.add(name)
                    print(
                        f"hefesto hook: aviso: expresión de policy_block inválida en pack '{name}' ({pat!r}): {exc}",
                        file=sys.stderr,
                    )
            if pack_patterns:
                compiled_by_pack.append((name, pack_patterns))

    # (c) una regex inválida de un pack activo bloquea.
    for broken_pack in invalid_regex_packs:
        if broken_pack in active_packs:
            return f"Hefesto bloquea este comando: no se puede verificar la política del pack '{broken_pack}' (expresión regular inválida)."

    for pack_name, patterns in compiled_by_pack:
        if pack_name in active_packs:
            for pat in patterns:
                if pat.search(cmd_str):
                    return f"Hefesto bloquea este comando según la política del pack '{pack_name}'."

    if has_invalid_regex:
        for pack_name, patterns in compiled_by_pack:
            for pat in patterns:
                if pat.search(cmd_str):
                    return f"Hefesto bloquea este comando según la política del pack '{pack_name}'."

    return None


def check_tool_use(
    payload: dict[str, object],
    protected: Iterable[str] = DEFAULT_PROTECTED,
    project: Path | None = None,
    fail_closed: bool = False,
) -> str | None:
    """Decisión de los hooks previos a una herramienta (Claude, Codex, Gemini): motivo del bloqueo o None."""
    raw_tool = str(payload.get("tool_name") or "")
    tool = TOOL_ALIASES.get(raw_tool, raw_tool)
    raw_input = payload.get("tool_input")
    tool_input: dict[str, object] = raw_input if isinstance(raw_input, dict) else {}
    patterns = tuple(protected)
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        cmd_str = str(command or "")
        reason = _check_bash(cmd_str, patterns)
        if reason:
            return reason
        proj = project
        if proj is None and isinstance(payload, dict) and payload.get("cwd"):
            try:
                from hefesto import project as proj_mod
                proj = proj_mod.find_project(Path(str(payload["cwd"])))
            except Exception:
                proj = None
        if proj is not None or fail_closed:
            reason = _check_pack_policies(proj, cmd_str, fail_closed=fail_closed)
            if reason:
                return reason
        return None
    if tool == "ApplyPatch":
        patch = str(tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or "")
        for match in PATCH_FILE.finditer(patch):
            reason = _check_path(tool, (match.group(1) or match.group(2)).strip(), patterns)
            if reason:
                return reason
        return None
    values: list[str] = []
    for key in PATH_KEYS + ("paths",):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            values.append(value)
        elif isinstance(value, list):
            values += [v for v in value if isinstance(v, str)]
    for value in values:
        reason = _check_path(tool, value, patterns)
        if reason:
            return reason
    pattern = tool_input.get("pattern") if tool == "Glob" else None
    if isinstance(pattern, str) and is_protected(pattern, patterns):
        return f"Hefesto bloquea la búsqueda de rutas protegidas (`{pattern}`)."
    return None
