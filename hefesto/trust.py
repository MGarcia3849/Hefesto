"""Confianza de los comandos `verify`: hash SHA-256 aprobado por un humano, por tarea."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
from typing import Any

from hefesto import roadmap, util
from hefesto.util import EXIT_BLOCKED, HefestoError, atomic_write_json, now_iso, read_json

FORBIDDEN_PREFIXES = ("git", "rm", "rmdir", "curl", "wget", "eval", "sh", "bash", "npx")

ALLOWED_REDIRECT_TOKENS = {"2>&1", ">/dev/null", "2>/dev/null"}
# Deliberately narrow: unknown syntax/options require human approval. Even quoted
# shell expansions are rejected, so tokenization cannot conceal executable text.
FORBIDDEN_SHELL_CHARS = frozenset("\\(){}[]*?~$`#")
OPERAND = re.compile(r"[A-Za-z0-9_./:@+,^ -]+")
SCRIPT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*")

# Each entry separates switches from options taking exactly one value. No prefix
# matching, short-option bundling, or arbitrary tool configuration is permitted.
PYTHON_OPTIONS = {
    "unittest": ({"-q", "-v", "-f", "-b"}, {"-k"}),
    "pytest": ({"-q", "-v", "-x"}, {"-k", "--maxfail"}),
    "py_compile": (set(), set()),
    "compileall": ({"-q", "-f"}, set()),
    "mypy": ({"--strict", "--ignore-missing-imports", "--quiet", "-v"}, set()),
}
GO_OPTIONS = ({"-v", "-race", "-short", "-cover"}, {"-run", "-count", "-timeout", "-tags"})
CARGO_OPTIONS = ({"--release", "--all-targets", "--workspace"}, {"-p"})
GREP_OPTIONS = ({"-q", "-i", "-v", "-n", "-F", "-E", "-c", "-w", "-x"}, set())


def trust_path(project: Path) -> Path:
    return project / ".hefesto" / "trust.json"


def verify_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def load(project: Path) -> dict[str, Any]:
    data = read_json(trust_path(project), default=None) or {}
    data.setdefault("schema_version", 1)
    data.setdefault("verify", {})
    return data


def status(project: Path, task: roadmap.Task) -> str:
    """`trusted`, `new` o `changed`."""
    entry = load(project)["verify"].get(task.id)
    if not entry:
        return "new"
    return "trusted" if entry.get("sha256") == verify_hash(task.verify) else "changed"


def _split_pipeline(command: str) -> list[list[str]] | None:
    """Parse only quoted words, exact redirects and the four supported separators."""
    subcommands: list[list[str]] = []
    stages: list[str] = []
    piece: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote:
            # Redirects/operators inside quotes are data, never syntax exceptions.
            if c in "<>&!":
                return None
            if c == quote:
                quote = None
            piece.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            piece.append(c)
            i += 1
            continue
        # Only a complete unquoted token can be a redirect. In particular, a
        # quoted redirect must not disappear from a tool's argument list.
        redirect = next((r for r in ALLOWED_REDIRECT_TOKENS if command.startswith(r, i)
                         and (i == 0 or command[i - 1] in " ;|&")
                         and (i + len(r) == len(command) or command[i + len(r)] in " ;|&")), None)
        if redirect:
            piece.append(" ")
            i += len(redirect)
            continue
        separator = command[i:i + 2] if command[i:i + 2] in ("&&", "||") else c
        if separator in ("&&", "||", ";", "|"):
            stage = "".join(piece).strip()
            if not stage:
                return None
            stages.append(stage)
            piece = []
            if separator != "|":
                subcommands.append(stages)
                stages = []
            i += len(separator)
            continue
        if c in "<>&":
            return None
        if c == "!" and ("".join(piece).strip() or i + 1 == len(command) or command[i + 1] != " "):
            return None
        piece.append(c)
        i += 1
    last = "".join(piece).strip()
    if quote is not None or not last:
        return None
    stages.append(last)
    subcommands.append(stages)
    return subcommands


def _operand(value: str) -> bool:
    """Literal paths, names and simple search expressions; never options/assignments."""
    return bool(value and not value.startswith("-") and OPERAND.fullmatch(value))


def _option_value(option: str, value: str) -> bool:
    if not _operand(value):
        return False
    if option in ("--maxfail", "-count", "-n", "-c"):
        return re.fullmatch(r"[+]?[0-9]+", value) is not None
    if option == "-timeout":
        return re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?(?:ns|us|ms|s|m|h))+|0", value) is not None
    return option in ("-k", "-run") or " " not in value


def _arguments(args: list[str], flags: set[str], valued: set[str]) -> bool:
    i = 0
    while i < len(args):
        arg = args[i]
        option, equal, value = arg.partition("=")
        if option in valued:
            if not equal:
                i += 1
                if i == len(args):
                    return False
                value = args[i]
            if not _option_value(option, value):
                return False
        elif arg not in flags and not _operand(arg):
            return False
        i += 1
    return True


def _read_filter(tokens: list[str], piped: bool) -> bool:
    tool, args = tokens[0], tokens[1:]
    if tool == "grep":
        # Outside a pipe, require the exact quiet-search contract.
        if not piped:
            return len(args) == 3 and args[0] == "-q" and all(_operand(a) for a in args[1:])
        return _arguments(args, *GREP_OPTIONS)
    if piped and tool in ("head", "tail"):
        return _arguments(args, {"-q", "-v"}, {"-n", "-c"})
    if piped and tool == "wc":
        return _arguments(args, {"-l", "-w", "-c", "-m", "-L"}, set())
    return False


def _check_tool_safety(tokens: list[str]) -> bool:
    tool, args = tokens[0], tokens[1:]
    if tool in ("python", "python3"):
        # Interpreter flags or arbitrary modules/scripts are not auto-approved.
        if len(args) < 2 or args[0] != "-m":
            return False
        tool, args = args[1], args[2:]
        if tool not in PYTHON_OPTIONS and tool != "ruff":
            return False
    elif tool in PYTHON_OPTIONS and tool != "pytest":
        return False
    if tool in PYTHON_OPTIONS:
        return _arguments(args, *PYTHON_OPTIONS[tool])
    if tool == "ruff":
        return bool(args and args[0] == "check"
                    and _arguments(args[1:], {"--quiet"}, {"--select", "--ignore"}))
    if tool == "npm":
        return args == ["test"] or (len(args) == 2 and args[0] == "run"
                                    and SCRIPT_NAME.fullmatch(args[1]) is not None)
    if tool in ("flutter", "dart"):
        subcommands = ("test", "analyze", "build") if tool == "flutter" else ("test", "analyze")
        return bool(args and args[0] in subcommands and _arguments(args[1:], set(), set()))
    if tool == "cargo":
        return bool(args and args[0] in ("test", "clippy", "build", "check")
                    and _arguments(args[1:], *CARGO_OPTIONS))
    if tool == "go":
        return bool(args and args[0] in ("test", "vet", "build") and _arguments(args[1:], *GO_OPTIONS))
    if tool == "test":
        return len(args) == 2 and args[0] in ("-f", "-d", "-e") and _operand(args[1])
    if tool == "true":
        return not args
    return _read_filter(tokens, piped=False)


def _extra_safe(tokens: list[str], prefixes: list[str]) -> bool:
    # An extension explicitly authorizes a command prefix, not shell syntax or
    # arbitrary options appended to it. Known tools still use their own grammar.
    if tokens[0] in FORBIDDEN_PREFIXES:
        return False
    known = {"pytest", "ruff", "npm", "flutter", "dart", "cargo", "go", "test", "true",
             "grep", "head", "tail", "wc"}
    if tokens[0] in known:
        return False
    if tokens[0] in ("python", "python3") and (len(tokens) < 2 or not _operand(tokens[1])):
        return False
    for prefix in prefixes:
        try:
            expected = shlex.split(prefix, posix=True)
        except ValueError:
            continue
        if expected and tokens[:len(expected)] == expected:
            if _arguments(tokens[len(expected):], set(), set()):
                return True
    return False


def is_safe_verify(command: str, extra_prefixes: list[str] | None = None) -> bool:
    """Approve only the explicit verification grammar; uncertainty requires a human."""
    if not isinstance(command, str) or not command.strip():
        return False
    if any(c in FORBIDDEN_SHELL_CHARS or ord(c) < 32 or ord(c) == 127 for c in command):
        return False
    subcommands = _split_pipeline(command)
    if subcommands is None:
        return False
    for stages in subcommands:
        for index, raw in enumerate(stages):
            # Negation is syntax only at the beginning, never a quoted word.
            if raw.startswith("! "):
                raw = raw[2:].lstrip()
            if "!" in raw:
                return False
            try:
                tokens = shlex.split(raw, posix=True)
            except ValueError:
                return False
            if not tokens:
                return False
            if index:
                if not _read_filter(tokens, piped=True):
                    return False
            elif not (_check_tool_safety(tokens) or _extra_safe(tokens, extra_prefixes or [])):
                return False
    return True


def approve_interactive(project: Path, rm: roadmap.Roadmap, yes: bool = False) -> tuple[int, list[str]]:
    """Muestra todos los `verify` y guarda los hashes si el humano confirma. Exige TTY salvo con `yes=True`."""
    if yes:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    else:
        util.require_tty("hefesto trust")
    data = load(project)
    lines: list[str] = []
    pending: list[roadmap.Task] = []
    for task in rm.tasks:
        if not task.verify:
            continue
        state = status(project, task)
        lines.append(f"  [{state:>7}] {task.id}: {task.verify}")
        if state != "trusted":
            pending.append(task)
    print("Comandos `verify` del ROADMAP (se ejecutarán en tu máquina con bash):")
    print("\n".join(lines) if lines else "  (ninguno)")
    if not pending:
        return 0, ["Todos los verify ya estaban aprobados."]
    if not yes and not util.confirm(f"¿Apruebas {len(pending)} comando(s) nuevo(s) o modificado(s)?", default=False):
        return 0, ["No se ha aprobado nada."]
    for task in pending:
        data["verify"][task.id] = {"sha256": verify_hash(task.verify), "approved_at": now_iso()}
    atomic_write_json(trust_path(project), data)
    return len(pending), [f"Aprobados: {', '.join(t.id for t in pending)}"]
