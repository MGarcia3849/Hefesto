"""Búsqueda y gestión de skills locales y del registro público."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hefesto import config, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB por fichero
MAX_TOTAL_SIZE = 20 * 1024 * 1024  # 20 MB en total
MAX_FILES = 500  # 500 ficheros dentro de la carpeta de la skill
MAX_FILES_COUNT = MAX_FILES

PORTABLE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9 _.@+-]+$")
WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def is_portable_component(comp: str) -> bool:
    if not comp or not PORTABLE_COMPONENT_RE.fullmatch(comp):
        return False
    if comp.endswith(".") or comp.endswith(" "):
        return False
    stem = comp.split(".")[0].upper()
    if stem in WINDOWS_DEVICE_NAMES:
        return False
    return True


def is_portable_rel_path(rel_str: str) -> bool:
    if not rel_str or rel_str.startswith("/") or rel_str.startswith("\\"):
        return False
    parts = rel_str.split("/")
    return all(is_portable_component(part) for part in parts)


_is_portable_rel_path = is_portable_rel_path
_is_portable_component = is_portable_component


def normalize_rel_path(rel_str: str) -> str:
    """Normalización Unicode NFC y casefold de cada componente de una ruta relativa POSIX."""
    parts = rel_str.split("/")
    norm_parts = [unicodedata.normalize("NFC", p).casefold() for p in parts]
    return "/".join(norm_parts)


def sanitize_text(text: str) -> str:
    """Sustituye caracteres de control (salvo \n y \t) por su escape visible."""
    if not text:
        return ""
    result: list[str] = []
    for c in text:
        if c in ("\n", "\t"):
            result.append(c)
        elif c == "\r":
            result.append(r"\r")
        elif c == "\b":
            result.append(r"\b")
        elif c == "\x1b":
            result.append(r"\x1b")
        elif c == "\a":
            result.append(r"\a")
        elif c == "\f":
            result.append(r"\f")
        elif c == "\v":
            result.append(r"\v")
        elif c == "\0":
            result.append(r"\0")
        else:
            cat = unicodedata.category(c)
            if cat in ("Cc", "Cf") or ord(c) < 32 or (127 <= ord(c) <= 159):
                if ord(c) <= 0xFF:
                    result.append(f"\\x{ord(c):02x}")
                else:
                    result.append(f"\\u{ord(c):04x}")
            else:
                result.append(c)
    return "".join(result)


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))")
SPEC_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

SCRIPT_EXTENSIONS = {".sh", ".bash", ".ps1", ".bat", ".cmd", ".py", ".js", ".ts", ".exe"}

NETWORK_SHELL_PATTERNS = [
    re.compile(r"\bcurl\b", re.IGNORECASE),
    re.compile(r"\bwget\b", re.IGNORECASE),
    re.compile(r"\bInvoke-WebRequest\b", re.IGNORECASE),
    re.compile(r"\bnpx\b", re.IGNORECASE),
    re.compile(r"\bpip\s+install\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+install\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-zA-Z]*rf\b", re.IGNORECASE),
    re.compile(r"\bbash\s+-c\b", re.IGNORECASE),
]

OVERRIDE_PATTERNS = [
    re.compile(r"ignore\s+previous", re.IGNORECASE),
    re.compile(r"ignora\s+las\s+instrucciones", re.IGNORECASE),
    re.compile(r"sin\s+pedir\s+confirmaci[oó]n", re.IGNORECASE),
    re.compile(r"without\s+asking", re.IGNORECASE),
    re.compile(r"dangerously-skip-permissions", re.IGNORECASE),
    re.compile(r"bypass\s+permissions?", re.IGNORECASE),
    re.compile(r"\byolo\b", re.IGNORECASE),
]

SENSITIVE_PATTERNS = [
    re.compile(r"\.env\b", re.IGNORECASE),
    re.compile(r"\bid_rsa\b", re.IGNORECASE),
    re.compile(r"\.ssh\b", re.IGNORECASE),
    re.compile(r"\bapi_key\b", re.IGNORECASE),
    re.compile(r"\bprocess\.env\b", re.IGNORECASE),
    re.compile(r"\bos\.environ\b", re.IGNORECASE),
]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, text
    for line in lines[1:end_idx]:
        key, sep, val = line.partition(":")
        if sep:
            meta[key.strip().lower()] = val.strip()
    body = "".join(lines[end_idx + 1:])
    return meta, body


def search_local(term: str, roots: list[Path]) -> list[dict[str, Any]]:
    """Busca en los SKILL.md de cada subcarpeta de roots por nombre, tags, descripción y contenido."""
    term_clean = term.strip().lower()
    if not term_clean:
        return []
    results: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for sub in entries:
            if not sub.is_dir():
                continue
            skill_file = sub / "SKILL.md"
            if not skill_file.is_file():
                continue
            resolved = skill_file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)

            field: str | None = None
            name_val = meta.get("name", "")
            # Orden de preferencia: name, tags, description, content
            if term_clean in sub.name.lower() or term_clean in name_val.lower():
                field = "name"
            elif term_clean in meta.get("tags", "").lower():
                field = "tags"
            elif term_clean in meta.get("description", "").lower():
                field = "description"
            elif term_clean in body.lower():
                field = "content"

            if field is not None:
                display_name = name_val or sub.name
                results.append({
                    "name": display_name,
                    "path": skill_file,
                    "field": field,
                })

    field_order = {"name": 0, "tags": 1, "description": 2, "content": 3}
    results.sort(key=lambda r: (field_order.get(r["field"], 99), r["name"].lower()))
    return results


def _parse_count(value: str) -> int | str:
    s = value.strip()
    if not s or s.lower() == "unknown":
        return "unknown"
    upper = s.upper()
    try:
        if upper.endswith("K"):
            return int(round(float(upper[:-1]) * 1_000))
        if upper.endswith("M"):
            return int(round(float(upper[:-1]) * 1_000_000))
        return int(float(s))
    except (ValueError, OverflowError):
        return "unknown"


def _fetch_stars(owner: str, repo: str) -> int | str:
    if not util.which("gh"):
        return "unknown"
    try:
        res = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".stargazers_count"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env=util.filtered_env(),
        )
        if res.returncode == 0:
            return int(res.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _compute_trust(owner: str, installs: int | str, stars: int | str, trusted_owners: set[str]) -> str:
    if owner in trusted_owners:
        return "alta"
    installs_low = isinstance(installs, int) and installs < 100
    stars_low = isinstance(stars, int) and stars < 100
    if installs_low or stars_low:
        return "baja"
    return "media"


def _validate_find_input(value: str, label: str) -> None:
    """Lista blanca: npx.cmd interpreta metacaracteres incluso con shell=False."""
    if not isinstance(value, str) or not value.strip():
        raise HefestoError(f"{label} no puede estar vacío.", EXIT_INPUT)
    if len(value) > 100:
        raise HefestoError(f"{label} no puede superar los 100 caracteres.", EXIT_INPUT)
    if value.lstrip(" ").startswith("-"):
        raise HefestoError(f"{label} no puede empezar por guion.", EXIT_INPUT)
    if any(not (c.isalpha() or c.isdecimal() or c in " -_./@") for c in value):
        raise HefestoError(
            f"{label} solo admite letras, dígitos, espacios, guion, guion bajo, punto, barra y arroba.",
            EXIT_INPUT,
        )


def find(term: str, owner: str | None = None) -> list[dict[str, Any]]:
    """Busca skills en skills.sh usando `npx -y skills@<versión> find TÉRMINO`."""
    _validate_find_input(term, "El término de búsqueda")
    if owner is not None:
        _validate_find_input(owner, "El filtro owner")

    npx_bin = util.which("npx")
    if not npx_bin:
        raise HefestoError(
            "`npx` no encontrado: hace falta Node.js para buscar skills en el registro público.",
            EXIT_UNAVAILABLE,
        )

    cfg = config.load(validate_layers=False)
    version = cfg.get("skills_sources", "npm_version", default="1.7.2")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise HefestoError(
            f"skills_sources.npm_version inválida ({version!r}): debe ser exactamente X.Y.Z (solo dígitos y puntos).",
            EXIT_INPUT,
        )
    raw_trusted = cfg.get("skills_sources", "trusted_owners", default=["anthropics", "vercel-labs", "microsoft"])
    trusted_owners = set(raw_trusted) if isinstance(raw_trusted, list) else set()

    cmd = [npx_bin, "-y", f"skills@{version}", "find", term]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=util.filtered_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise HefestoError("Tiempo de espera agotado (120 s) buscando skills con npx.", EXIT_UNAVAILABLE) from exc
    except OSError as exc:
        raise HefestoError(f"Error al ejecutar npx: {exc}", EXIT_UNAVAILABLE) from exc

    if result.returncode != 0:
        raise HefestoError(
            f"Error al buscar skills con npx (código {result.returncode}): {result.stderr.strip() or result.stdout.strip()}",
            EXIT_UNAVAILABLE,
        )

    clean_output = ANSI_RE.sub("", result.stdout)
    lines = clean_output.splitlines()
    all_results: list[dict[str, Any]] = []

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        match = re.search(
            r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+)\s+([0-9.]+[kKmM]?|unknown)\s+installs?",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue

        full_name = match.group(1)
        installs_str = match.group(2)
        owner_repo, skill = full_name.split("@", 1)
        owner_part, repo_part = owner_repo.split("/", 1)
        installs = _parse_count(installs_str)

        url = "unknown"
        if i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line.startswith(("└", "https://", "http://")):
                url_match = re.search(r"https?://\S+", next_line)
                if url_match:
                    url = url_match.group(0)

        stars = _fetch_stars(owner_part, repo_part)
        trust = _compute_trust(owner_part, installs, stars, trusted_owners)

        all_results.append({
            "name": full_name,
            "owner": owner_part,
            "repo": repo_part,
            "skill": skill,
            "installs": installs,
            "stars": stars,
            "url": url,
            "trust": trust,
        })

    # Guardar en caché
    cache_path = util.hefesto_home() / "skills" / "find-cache.json"
    cache_data = {r["name"]: r for r in all_results}
    util.atomic_write_json(cache_path, cache_data)

    if owner is not None:
        return [r for r in all_results if r["owner"] == owner]
    return all_results


def check_confinement(target: Path, base: Path) -> None:
    """Verifica que target ni sus ancestros hasta base sean symlinks o junctions,
    y que la ruta resuelta quede dentro de base resuelta. Si no, HefestoError 2."""
    resolved_base = base.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_base)
    except ValueError:
        raise HefestoError(
            f"Confinamiento violado: '{target}' resuelve fuera de '{base}'.",
            EXIT_BLOCKED,
        )

    cur = target
    while cur is not None:
        if cur.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(cur)):
            raise HefestoError(
                f"Confinamiento violado: '{cur}' es un enlace simbólico o junction.",
                EXIT_BLOCKED,
            )
        if cur == base:
            break
        try:
            if cur.resolve() == resolved_base:
                break
        except OSError:
            pass
        if str(cur).lower() == str(base).lower():
            break
        if cur == cur.parent:
            break
        cur = cur.parent


def _validate_safe_rel_path(rel_str: str) -> None:
    if not rel_str or rel_str.startswith("/") or rel_str.startswith("\\"):
        raise HefestoError(f"Ruta de fichero no permitida: {rel_str!r}", EXIT_INPUT)
    if any(ord(c) < 32 or ord(c) == 127 for c in rel_str):
        raise HefestoError(f"Ruta de fichero contiene caracteres de control: {rel_str!r}", EXIT_INPUT)
    parts = rel_str.replace("\\", "/").split("/")
    if any(part in ("..", ".") for part in parts):
        raise HefestoError(f"Ruta de fichero con componentes inválidos: {rel_str!r}", EXIT_INPUT)
    p = Path(rel_str)
    if p.is_absolute() or p.drive:
        raise HefestoError(f"Ruta de fichero absoluta no permitida: {rel_str!r}", EXIT_INPUT)


def _text_decodings(raw_bytes: bytes) -> list[str]:
    """Todas las interpretaciones plausibles; un endian válido no excluye el otro."""
    if not raw_bytes:
        return [""]
    encodings = ["utf-8-sig"]
    if raw_bytes.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ["utf-32"]
    elif raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    elif raw_bytes.count(b"\x00") / len(raw_bytes) > 0.15:
        encodings += ["utf-16-le", "utf-16-be"]
        if len(raw_bytes) % 4 == 0:
            encodings += ["utf-32-le", "utf-32-be"]
    texts: list[str] = []
    for encoding in encodings:
        try:
            text = raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
        if raw_bytes.count(b"\x00") / len(raw_bytes) > 0.15 and encoding == "utf-8-sig":
            continue
        if text not in texts:
            texts.append(text)
    if not texts and raw_bytes.count(b"\x00") / len(raw_bytes) <= 0.15:
        clean = raw_bytes.decode("utf-8", errors="replace")
        if clean not in texts:
            texts.append(clean)
    return texts


def decode_text(raw_bytes: bytes) -> str | None:
    """Elige una representación legible para mostrar texto, nunca para descartar riesgos."""
    texts = _text_decodings(raw_bytes)
    return max(texts, key=lambda t: sum(c.isascii() and c.isprintable() for c in t), default=None)


def validate_skill_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in (".", ".."):
        raise HefestoError(f"Nombre de skill inválido: {name!r}.", EXIT_INPUT)
    if name.casefold().startswith("hefesto-"):
        raise HefestoError(f"Nombre de skill reservado: {name!r}.", EXIT_INPUT)


def skill_from_spec(spec: str) -> str:
    match = SPEC_RE.fullmatch(spec) if isinstance(spec, str) else None
    if not match or any(part in (".", "..") for part in match.groups()):
        raise HefestoError(f"Especificación de skill inválida: {spec!r}.", EXIT_INPUT)
    skill = match.group(3)
    validate_skill_name(skill)
    return skill


def skill_identity(raw: bytes, skill: str) -> tuple[bytes, list[dict[str, str]]]:
    """Valida el frontmatter y materializa el nombre del spec, conservando el cuerpo."""
    validate_skill_name(skill)
    risks: list[dict[str, str]] = []
    for text in _text_decodings(raw):
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            continue
        for number, line in enumerate(lines[1:end], 2):
            key, sep, name = line.partition(":")
            if not sep or key.strip().casefold() != "name":
                continue
            name = name.strip()
            validate_skill_name(name)
            if name.casefold() != skill.casefold():
                risks.append({"file": "SKILL.md", "kind": "override",
                              "detail": sanitize_text(f"línea {number}: frontmatter name: {name} distinto del spec: {skill}")})
    text = decode_text(raw)
    if text is None:
        raise HefestoError("SKILL.md no contiene texto decodificable.", EXIT_INPUT)
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if lines and lines[0].strip() == "---" and end is not None:
        header = [line for line in lines[1:end] if line.partition(":")[0].strip().casefold() != "name"]
        names = [line.partition(":")[2].strip() for line in lines[1:end]
                 if line.partition(":")[0].strip().casefold() == "name"]
        if names == [skill]:
            return raw, risks
        text = "---\n" + f"name: {skill}\n" + "".join(header) + "".join(lines[end:])
    else:
        text = f"---\nname: {skill}\n---\n" + text
    return text.encode("utf-8"), risks


def _validate_git_location(location: str) -> None:
    """Solo HTTPS o rutas absolutas locales que Git no interprete como un helper."""
    if not isinstance(location, str) or not location or any(ord(c) < 32 or ord(c) == 127 for c in location):
        raise HefestoError("skills_sources.git_base inválida.", EXIT_INPUT)
    if location.startswith("https://"):
        try:
            url = urlsplit(location)
            valid = (url.hostname and url.netloc and not url.username and not url.password
                     and not url.query and not url.fragment and "\\" not in location
                     and not any(c.isspace() for c in location))
            _ = url.port
        except ValueError:
            valid = False
    else:
        path = Path(location)
        valid = path.is_absolute() and "::" not in location and "://" not in location
        # Un ':' en una ruta POSIX puede activar la sintaxis scp de Git.
        valid = valid and ":" not in location[len(path.drive):]
    if not valid:
        raise HefestoError("git_base y repo_url solo admiten https:// o una ruta local absoluta sin '::'.", EXIT_INPUT)


def classify_binary(raw_bytes: bytes) -> tuple[str, str]:
    """Clasifica un fichero no decodificable o con NUL como script (ejecutable) o binary."""
    if raw_bytes.startswith(b"\x7fELF"):
        return "script", "ejecutable ELF binario"
    if raw_bytes.startswith(b"MZ"):
        return "script", "ejecutable PE binario (MZ)"
    macho_magics = (
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    )
    if len(raw_bytes) >= 4 and raw_bytes[:4] in macho_magics:
        return "script", "ejecutable Mach-O binario"
    return "binary", f"fichero binario ({len(raw_bytes)} bytes)"


def is_binary_for_display(raw_bytes: bytes) -> bool:
    """True solo si empieza por firma de ejecutable o supera el 30% de bytes no imprimibles."""
    if not raw_bytes:
        return False
    if raw_bytes.startswith(b"\x7fELF") or raw_bytes.startswith(b"MZ"):
        return True
    macho_magics = (
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    )
    if len(raw_bytes) >= 4 and raw_bytes[:4] in macho_magics:
        return True

    for enc in ("utf-8", "utf-16", "utf-32", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        try:
            decoded = raw_bytes.decode(enc)
            if decoded:
                printable_chars = sum(
                    1 for c in decoded
                    if c in ("\t", "\n", "\r") or (c.isprintable() and c != "\x00")
                )
                non_printable_chars = len(decoded) - printable_chars
                if (non_printable_chars / len(decoded)) <= 0.30:
                    return False
        except (UnicodeDecodeError, ValueError):
            pass

    printable_bytes = sum(1 for b in raw_bytes if b in (9, 10, 13) or (32 <= b <= 126))
    non_printable_bytes = len(raw_bytes) - printable_bytes
    return (non_printable_bytes / len(raw_bytes)) > 0.30


def _analyze_file_risks(rel_str: str, blob_bytes: bytes, mode: str) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    if mode == "100755":
        risks.append({"file": rel_str, "kind": "script", "detail": "modo ejecutable 100755"})

    ext = Path(rel_str).suffix.lower()
    if ext in SCRIPT_EXTENSIONS:
        risks.append({"file": rel_str, "kind": "script", "detail": f"extensión {ext}"})
    if is_binary_for_display(blob_bytes):
        kind, detail = classify_binary(blob_bytes)
        risks.append({"file": rel_str, "kind": kind, "detail": sanitize_text(detail)})

    texts: list[str] = []
    for t in _text_decodings(blob_bytes):
        if t not in texts:
            texts.append(t)
    raw_utf8 = blob_bytes.decode("utf-8", errors="replace")
    if raw_utf8 not in texts:
        texts.append(raw_utf8)
    clean_utf8 = ANSI_RE.sub("", blob_bytes.decode("utf-8", errors="replace").replace("\x00", ""))
    if clean_utf8 not in texts:
        texts.append(clean_utf8)

    for text in texts:
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if lines and lines[0].strip(" \t\n").startswith("#!"):
            shebang_risk = {"file": rel_str, "kind": "script", "detail": f"shebang {sanitize_text(lines[0].strip(' \t\n'))}"}
            if not any(r["file"] == rel_str and r["kind"] == "script" and "shebang" in r.get("detail", "") for r in risks):
                risks.append(shebang_risk)
        for line_idx, line in enumerate(lines, 1):
            clean_line = ANSI_RE.sub("", line)
            clean_line = "".join(
                c if (c == "\t" or (ord(c) >= 32 and unicodedata.category(c) not in ("Cc", "Cf"))) else " "
                for c in clean_line
            )
            for kind, patterns in (("network_shell", NETWORK_SHELL_PATTERNS),
                                   ("override", OVERRIDE_PATTERNS), ("sensitive", SENSITIVE_PATTERNS)):
                if any(pattern.search(clean_line) for pattern in patterns):
                    line_detail = sanitize_text(line.strip(" \t\n"))
                    if not any(r["file"] == rel_str and r["kind"] == kind and r.get("detail", "").startswith(f"línea {line_idx}:") for r in risks):
                        risks.append({"file": rel_str, "kind": kind, "detail": f"línea {line_idx}: {line_detail}"})
    return risks


def fetch(spec: str) -> Path:
    """Descarga una skill desde su repositorio git a la carpeta de propuestas."""
    match = SPEC_RE.fullmatch(spec)
    if not match:
        raise HefestoError(
            f"Especificación de skill inválida ({spec!r}): debe tener el formato owner/repo@skill.",
            EXIT_INPUT,
        )
    owner, repo, skill = match.groups()
    if any(part in (".", "..") for part in (owner, repo, skill)):
        raise HefestoError(
            f"Especificación de skill inválida ({spec!r}): ninguna parte puede ser '.' o '..'.",
            EXIT_INPUT,
        )
    if skill.casefold().startswith("hefesto-"):
        raise HefestoError(
            f"Nombre de skill reservado: '{skill}' no puede empezar por 'hefesto-'.",
            EXIT_INPUT,
        )

    cfg = config.load(validate_layers=False)
    git_base = cfg.get("skills_sources", "git_base", default="https://github.com")
    _validate_git_location(git_base)
    repo_url = f"{git_base.rstrip('/')}/{owner}/{repo}.git"
    _validate_git_location(repo_url)

    git_bin = util.which("git")
    if not git_bin:
        raise HefestoError("git no está instalado.", EXIT_UNAVAILABLE)

    with tempfile.TemporaryDirectory(prefix="hefesto-fetch-") as tmpdir:
        tmp_path = Path(tmpdir)
        empty_config = tmp_path / "empty_gitconfig"
        empty_config.touch()
        empty_hooks = tmp_path / "empty_hooks"
        empty_hooks.mkdir(exist_ok=True)

        git_env = {key: value for key, value in util.filtered_env().items()
                   if not key.upper().startswith("GIT_")}
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        git_env["GIT_CONFIG_GLOBAL"] = empty_config.as_posix()
        git_env["GIT_LFS_SKIP_SMUDGE"] = "1"
        git_env["GIT_TERMINAL_PROMPT"] = "0"

        clone_dir = tmp_path / "repo"
        clone_cmd = [
            git_bin,
            "-c", f"core.hooksPath={empty_hooks.as_posix()}",
            "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always",
            "-c", "protocol.https.allow=always",
            "clone",
            "--no-checkout",
            "--depth", "1",
            "--no-tags",
            "--single-branch",
            repo_url,
            str(clone_dir),
        ]
        try:
            res = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=git_env,
            )
        except OSError as exc:
            raise HefestoError(f"Error al clonar el repositorio de skills: {exc}", EXIT_UNAVAILABLE) from exc
        if res.returncode != 0:
            raise HefestoError(
                f"Error al clonar {repo_url} (código {res.returncode}): {res.stderr.strip() or result.stdout.strip() if 'result' in locals() else res.stderr.strip() or res.stdout.strip()}",
                EXIT_INPUT,
            )

        rev_res = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            cwd=clone_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=git_env,
        )
        if rev_res.returncode != 0:
            raise HefestoError("Error al obtener el commit del clon de la skill.", EXIT_INPUT)
        commit = rev_res.stdout.strip()

        tree_res = subprocess.run(
            [git_bin, "ls-tree", "-r", "-z", "HEAD"],
            cwd=clone_dir,
            capture_output=True,
            env=git_env,
        )
        if tree_res.returncode != 0:
            raise HefestoError("Error al listar el árbol del repositorio de la skill.", EXIT_INPUT)

        tree_entries: list[tuple[str, str, str, str]] = []
        for chunk in tree_res.stdout.split(b"\x00"):
            if not chunk:
                continue
            meta_part, sep, path_bytes = chunk.partition(b"\t")
            if not sep:
                continue
            parts = meta_part.split()
            if len(parts) < 3:
                continue
            mode_s = parts[0].decode("ascii", errors="replace")
            type_s = parts[1].decode("ascii", errors="replace")
            sha_s = parts[2].decode("ascii", errors="replace")
            path_s = path_bytes.decode("utf-8", errors="replace")
            tree_entries.append((mode_s, type_s, sha_s, path_s))

        candidate_dirs: list[str] = []
        for mode_s, type_s, sha_s, path_s in tree_entries:
            norm_p = normalize_rel_path(path_s)
            base_p = posixpath.basename(path_s)
            if norm_p == "skill.md" or norm_p.endswith("/skill.md") or base_p.casefold().rstrip(". ") == "skill.md":
                dir_path = "" if "/" not in path_s else posixpath.dirname(path_s)
                dir_name = posixpath.basename(dir_path) if dir_path else ""
                if dir_name == skill:
                    candidate_dirs.append(dir_path)
                    continue

                cat_res = subprocess.run(
                    [git_bin, "cat-file", "blob", sha_s],
                    cwd=clone_dir,
                    capture_output=True,
                    env=git_env,
                )
                if cat_res.returncode == 0:
                    text_content = decode_text(cat_res.stdout)
                    if text_content is not None:
                        fm, _ = _parse_frontmatter(text_content)
                        if fm.get("name") == skill:
                            candidate_dirs.append(dir_path)

        if not candidate_dirs:
            raise HefestoError(f"No se encontró la carpeta de la skill '{skill}' en {repo_url}.", EXIT_INPUT)

        candidate_dirs.sort(key=lambda d: (0 if posixpath.basename(d) == skill else 1, len(d.split("/")) if d else 0))
        skill_dir = candidate_dirs[0]
        rel_path_in_repo = skill_dir if skill_dir else "."

        prefix = f"{skill_dir}/" if skill_dir else ""
        matching_entries: list[tuple[str, str, str, str]] = []
        for mode_s, type_s, sha_s, path_s in tree_entries:
            if prefix:
                if path_s.startswith(prefix):
                    matching_entries.append((mode_s, type_s, sha_s, path_s[len(prefix):]))
            else:
                matching_entries.append((mode_s, type_s, sha_s, path_s))

        max_files = MAX_FILES if MAX_FILES != 500 else MAX_FILES_COUNT
        if len(matching_entries) > max_files:
            raise HefestoError(
                f"La skill supera el límite máximo de ficheros ({max_files}).",
                EXIT_BLOCKED,
            )

        proposals_parent = util.hefesto_home() / "skills" / "_proposals"
        proposals_parent.mkdir(parents=True, exist_ok=True)
        proposal_dir = proposals_parent / f"{owner}-{skill}"
        check_confinement(proposal_dir, util.hefesto_home())
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{proposal_dir.name}-", dir=proposals_parent))
        check_confinement(temp_dir, util.hefesto_home())
        skill_md_written = False
        found_non_portable_skill_md = False
        try:
            risks: list[dict[str, str]] = []
            seen_normalized: dict[str, str] = {}
            total_size = 0

            for mode_s, type_s, sha_s, rel_in_skill in matching_entries:
                norm_rel = normalize_rel_path(rel_in_skill)
                base_name = posixpath.basename(rel_in_skill)
                is_skill_md_candidate = (
                    norm_rel == "skill.md"
                    or base_name.casefold().rstrip(". ") == "skill.md"
                )

                if not is_portable_rel_path(rel_in_skill):
                    if is_skill_md_candidate:
                        found_non_portable_skill_md = True
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "override",
                        "detail": "nombre de fichero no portable",
                    })
                    continue

                _validate_safe_rel_path(rel_in_skill)

                norm_base = posixpath.basename(norm_rel)

                if norm_base in ("source.json", "overlay.md"):
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "override",
                        "detail": "nombre reservado por Hefesto",
                    })
                    continue

                if norm_rel in seen_normalized:
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "override",
                        "detail": f"colisión de ruta con {seen_normalized[norm_rel]}",
                    })
                    continue
                seen_normalized[norm_rel] = rel_in_skill

                if mode_s == "120000":
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "symlink",
                        "detail": "enlace simbólico no seguido",
                    })
                    continue
                if mode_s == "160000":
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "symlink",
                        "detail": "submódulo",
                    })
                    continue

                size_res = subprocess.run(
                    [git_bin, "cat-file", "-s", sha_s],
                    cwd=clone_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=git_env,
                )
                if size_res.returncode != 0:
                    raise HefestoError(f"Error al consultar el tamaño de {rel_in_skill}.", EXIT_INPUT)
                try:
                    blob_size = int(size_res.stdout.strip())
                except ValueError:
                    raise HefestoError(f"Tamaño inválido para {rel_in_skill}.", EXIT_INPUT)

                if blob_size > MAX_FILE_SIZE:
                    if norm_rel == "skill.md":
                        raise HefestoError(
                            f"SKILL.md supera el tamaño máximo permitido ({MAX_FILE_SIZE} bytes).",
                            EXIT_INPUT,
                        )
                    risks.append({
                        "file": rel_in_skill,
                        "kind": "binary",
                        "detail": "demasiado grande",
                    })
                    continue

                if total_size + blob_size > MAX_TOTAL_SIZE:
                    raise HefestoError(
                        f"La skill supera el tamaño total máximo permitido ({MAX_TOTAL_SIZE} bytes).",
                        EXIT_BLOCKED,
                    )
                total_size += blob_size

                cat_res = subprocess.run(
                    [git_bin, "cat-file", "blob", sha_s],
                    cwd=clone_dir,
                    capture_output=True,
                    env=git_env,
                )
                if cat_res.returncode != 0:
                    raise HefestoError(f"Error al leer contenido de {rel_in_skill}.", EXIT_INPUT)
                blob_bytes = cat_res.stdout
                if norm_rel == "skill.md":
                    _, identity_risks = skill_identity(blob_bytes, skill)
                    risks.extend(identity_risks)
                    dest_file = temp_dir / "SKILL.md"
                    skill_md_written = True
                else:
                    dest_file = temp_dir / Path(rel_in_skill)

                check_confinement(dest_file, util.hefesto_home())
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(blob_bytes)

                file_risks = _analyze_file_risks(rel_in_skill, blob_bytes, mode_s)
                risks.extend(file_risks)

            if not skill_md_written or not (temp_dir / "SKILL.md").is_file():
                if found_non_portable_skill_md:
                    raise HefestoError(
                        f"SKILL.md no es un fichero portable en la skill '{skill}'.",
                        EXIT_INPUT,
                    )
                raise HefestoError(
                    f"Falta SKILL.md en la skill '{skill}'.",
                    EXIT_INPUT,
                )

            files_hashes: dict[str, str] = {}
            for p in sorted(temp_dir.rglob("*")):
                if p.is_file() and p.name != "SOURCE.json":
                    rel = p.relative_to(temp_dir).as_posix()
                    files_hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

            for r in risks:
                if "detail" in r:
                    r["detail"] = sanitize_text(str(r["detail"]))

            cache_path = util.hefesto_home() / "skills" / "find-cache.json"
            cache = util.read_json(cache_path, default={}) if cache_path.is_file() else {}
            cached_entry = cache.get(spec, {}) if isinstance(cache, dict) else {}
            trust = {
                "installs": cached_entry.get("installs", "unknown"),
                "stars": cached_entry.get("stars", "unknown"),
                "score": cached_entry.get("trust", "unknown"),
            }

            source_data = {
                "spec": spec,
                "repo_url": repo_url,
                "path": rel_path_in_repo,
                "commit": commit,
                "fetched_at": util.now_iso(),
                "files": files_hashes,
                "trust": trust,
                "risks": risks,
            }
            source_file = temp_dir / "SOURCE.json"
            check_confinement(source_file, util.hefesto_home())
            util.atomic_write_json(source_file, source_data)

            if proposal_dir.exists():
                backup_parent = Path(tempfile.mkdtemp(prefix=f".{proposal_dir.name}-backup-", dir=proposals_parent))
                backup_dir = backup_parent / "prev"
                os.replace(proposal_dir, backup_dir)
                try:
                    os.replace(temp_dir, proposal_dir)
                except BaseException:
                    if backup_dir.exists():
                        os.replace(backup_dir, proposal_dir)
                    raise
                finally:
                    shutil.rmtree(backup_parent, ignore_errors=True)
            else:
                os.replace(temp_dir, proposal_dir)

        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

        return proposal_dir


def promote(name: str, yes: bool = False, pack: str | None = None, overlay: str | None = None) -> Path:
    """Mueve una propuesta de skill a util.hefesto_home()/skills/promoted/<skill>/."""
    if not name or not NAME_RE.fullmatch(name) or name in (".", "..") or "/" in name or "\\" in name:
        raise HefestoError(f"Nombre de propuesta inválido: {name!r}.", EXIT_INPUT)
    if name.casefold().startswith("hefesto-"):
        raise HefestoError(f"Nombre de propuesta reservado: '{name}' no puede empezar por 'hefesto-'.", EXIT_INPUT)

    proposal_dir = util.hefesto_home() / "skills" / "_proposals" / name
    check_confinement(proposal_dir, util.hefesto_home())
    if not proposal_dir.is_dir():
        raise HefestoError(f"No existe la propuesta '{name}'.", EXIT_INPUT)

    source_file = proposal_dir / "SOURCE.json"
    check_confinement(source_file, util.hefesto_home())
    if not source_file.is_file():
        raise HefestoError(f"La propuesta '{name}' no contiene SOURCE.json.", EXIT_INPUT)

    source_data = util.read_json(source_file, default={})
    if not isinstance(source_data, dict):
        raise HefestoError(f"SOURCE.json corrupto en la propuesta '{name}'.", EXIT_INPUT)

    skill_name = skill_from_spec(source_data.get("spec", ""))
    skill_file = proposal_dir / "SKILL.md"
    check_confinement(skill_file, util.hefesto_home())
    if not skill_file.is_file():
        raise HefestoError("La propuesta no contiene SKILL.md.", EXIT_INPUT)
    effective_skill, identity_risks = skill_identity(skill_file.read_bytes(), skill_name)

    # Imprime el contenido completo: binarios como [binario, N bytes] y texto decodificado
    for p in sorted(proposal_dir.rglob("*")):
        if p.is_file() and p.name != "SOURCE.json":
            rel = p.relative_to(proposal_dir).as_posix()
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            text = decode_text(raw)
            print(f"== {rel} ==")
            if is_binary_for_display(raw):
                print(f"[binario, {len(raw)} bytes]")
            else:
                print(sanitize_text(text or ""))

    # Imprime el informe de riesgos
    risks = list(source_data.get("risks", []))
    risks.extend(r for r in identity_risks if r not in risks)
    print("== Informe de riesgos ==")
    if risks:
        for r in risks:
            kind_s = sanitize_text(str(r.get('kind', '')))
            file_s = sanitize_text(str(r.get('file', '')))
            detail_s = sanitize_text(str(r.get('detail', '')))
            print(f"- [{kind_s}] {file_s}: {detail_s}")
    else:
        print("Sin riesgos detectados.")

    if not yes:
        util.require_tty("hefesto promote")
        if not util.confirm(f"¿Promover la propuesta '{name}' a promovidas?", default=False):
            return proposal_dir

    promoted_dir = util.hefesto_home() / "skills" / "promoted" / skill_name
    check_confinement(promoted_dir, util.hefesto_home())

    overlay_content: bytes | None = None
    if promoted_dir.is_dir():
        overlay_file = promoted_dir / "OVERLAY.md"
        if overlay_file.is_file():
            overlay_content = overlay_file.read_bytes()
        shutil.rmtree(promoted_dir)

    promoted_dir.parent.mkdir(parents=True, exist_ok=True)
    check_confinement(promoted_dir.parent, util.hefesto_home())
    shutil.move(str(proposal_dir), str(promoted_dir))
    # SOURCE conserva los hashes y el commit del origen; el nombre activo siempre procede del spec.
    (promoted_dir / "SKILL.md").write_bytes(effective_skill)

    if overlay is not None:
        overlay_text = overlay if overlay.endswith("\n") else f"{overlay}\n"
        (promoted_dir / "OVERLAY.md").write_text(overlay_text, encoding="utf-8")
    elif overlay_content is not None:
        (promoted_dir / "OVERLAY.md").write_bytes(overlay_content)

    if pack is not None:
        src_file = promoted_dir / "SOURCE.json"
        src_data = util.read_json(src_file, default={})
        if isinstance(src_data, dict):
            src_data["pack"] = pack
            util.atomic_write_json(src_file, src_data)

    return promoted_dir


def update(skill: str) -> Path:
    """Vuelve a descargar la skill promovida desde su SOURCE.json (mismo spec) a propuestas."""
    if not skill or not NAME_RE.fullmatch(skill) or skill in (".", ".."):
        raise HefestoError(f"Nombre de skill inválido: {skill!r}.", EXIT_INPUT)
    if skill.casefold().startswith("hefesto-"):
        raise HefestoError(f"Nombre de skill reservado: '{skill}' no puede empezar por 'hefesto-'.", EXIT_INPUT)

    promoted_dir = util.hefesto_home() / "skills" / "promoted" / skill
    check_confinement(promoted_dir, util.hefesto_home())
    if not promoted_dir.is_dir():
        raise HefestoError(f"No existe la skill promovida '{skill}'.", EXIT_INPUT)

    source_file = promoted_dir / "SOURCE.json"
    check_confinement(source_file, util.hefesto_home())
    if not source_file.is_file():
        raise HefestoError(f"La skill promovida '{skill}' no contiene SOURCE.json.", EXIT_INPUT)

    source_data = util.read_json(source_file, default={})
    if not isinstance(source_data, dict) or not source_data.get("spec"):
        raise HefestoError(f"SOURCE.json de '{skill}' no contiene 'spec'.", EXIT_INPUT)

    return fetch(source_data["spec"])
