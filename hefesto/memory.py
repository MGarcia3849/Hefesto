"""Pack memory: memoria permanente entre proyectos (preferencias, stacks, errores, decisiones).

Guarda notas en Markdown en `~/hefesto/memory/` (o `<raíz>/LEARNINGS.md` sin el pack).
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from hefesto import config, distribution, guard, packs, util
from hefesto.util import (
    EXIT_BLOCKED,
    EXIT_INPUT,
    HefestoError,
    hefesto_home,
)

ALLOWED_SCOPES = ("project", "global")
ALLOWED_KINDS = ("preferencias", "stacks", "errores-resueltos", "decisiones")


def root() -> Path:
    """Raíz global de Hefesto (`util.hefesto_home()`)."""
    return hefesto_home()


def memory_dir() -> Path:
    """Directorio almacén de la memoria global."""
    return root() / "memory"


def pending_dir() -> Path:
    """Directorio de notas candidatas pendientes de revisión."""
    return memory_dir() / "_pendientes"


def is_memory_enabled(project: Path | None = None) -> bool:
    """True si el pack memory está habilitado en el proyecto (o globalmente si no hay proyecto)."""
    if project is not None:
        return packs.is_pack_enabled(project, "memory")
    try:
        settings = config.read_settings(config.global_settings_path())
        return bool(settings.get("packs", {}).get("memory", {}).get("enabled", False))
    except Exception:
        return False


def _is_inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _check_path(base: Path, target: Path) -> None:
    """Valida antes de acceder: ruta léxica, enlaces y confinamiento resuelto."""
    base, target = base.absolute(), target.absolute()
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise HefestoError("Ruta fuera del almacén de memoria.", EXIT_BLOCKED) from None
    if ".." in relative.parts:
        raise HefestoError("Ruta fuera del almacén de memoria.", EXIT_BLOCKED)
    current = target
    while current != base:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise HefestoError("Enlace simbólico no permitido en el almacén de memoria.", EXIT_BLOCKED)
        current = current.parent
    if not _is_inside(base, target):
        raise HefestoError("Ruta fuera del almacén de memoria.", EXIT_BLOCKED)


def _check_memory_path(path: Path) -> None:
    _check_path(root(), memory_dir())
    _check_path(memory_dir(), path)


def _safe_to_read(path: Path) -> bool:
    try:
        _check_memory_path(path)
    except HefestoError as exc:
        print(f"Aviso: se ignora una ruta de memoria: {exc}", file=sys.stderr)
        return False
    return True


def _approved_notes() -> list[Path]:
    """Recorre el almacén sin entrar en enlaces ni en candidatas pendientes."""
    store = memory_dir()
    _check_memory_path(store)
    notes: list[Path] = []
    for directory, dirs, files in os.walk(store, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(d for d in dirs if _safe_to_read(parent / d)
                         and parent / d != pending_dir())
        for name in sorted(files):
            path = parent / name
            if path.suffix == ".md" and _safe_to_read(path) and path.is_file():
                notes.append(path)
    return sorted(notes)


def _check_secrets(text: str) -> None:
    secrets = guard.find_secrets(text)
    if secrets:
        raise HefestoError(
            f"Secreto detectado en la nota ({', '.join(secrets)}); no se guarda nada.", EXIT_BLOCKED,
        )


def _check_kind(kind: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise HefestoError("Categoría no válida. Opciones: " + ", ".join(ALLOWED_KINDS), EXIT_INPUT)


def _stack_slug(stack: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", stack.lower())
    slug = re.sub(r"\.{2,}", "-", slug).strip(".-")
    if not slug or re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", slug.split(".")[0]):
        return "general"
    return slug


def parse_note_content(raw: str) -> tuple[dict[str, str], str]:
    """Extrae metadatos de cabecera y el cuerpo de una nota Markdown."""
    meta: dict[str, str] = {}
    lines = raw.splitlines()
    body_lines: list[str] = []
    in_frontmatter = False
    frontmatter_ended = False

    if lines and lines[0].strip() == "---":
        in_frontmatter = True
        lines = lines[1:]

    for line in lines:
        if in_frontmatter and not frontmatter_ended:
            if line.strip() == "---":
                frontmatter_ended = True
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
            continue
        body_lines.append(line)

    return meta, "\n".join(body_lines).strip()


def format_note(meta: dict[str, str], body: str) -> str:
    """Genera el contenido Markdown de una nota con frontmatter."""
    header_lines = [f"{k}: {v}" for k, v in meta.items()]
    header = "\n".join(header_lines)
    return f"---\n{header}\n---\n\n{body.strip()}\n"


def remember(
    project: Path | None,
    text: str,
    scope: str = "project",
    kind: str = "errores-resueltos",
    stack: str | None = None,
) -> Path:
    """Guarda una nota técnica o preferencia en el ámbito local o global."""
    proj_name = project.resolve().name if project is not None else ""
    _check_secrets("\n".join((text, kind, stack or "", proj_name)))

    clean_text = text.strip()
    if not clean_text:
        raise HefestoError("La nota está vacía.", EXIT_INPUT)

    if scope not in ALLOWED_SCOPES:
        raise HefestoError(
            f"Scope no válido: `{scope}`. Opciones permitidas: {', '.join(ALLOWED_SCOPES)}.",
            EXIT_INPUT,
        )

    _check_kind(kind)

    if scope == "project":
        if project is None:
            raise HefestoError("`scope='project'` requiere un proyecto.", EXIT_INPUT)
        learnings_path = project / ".hefesto" / "LEARNINGS.md"
        dot_hefesto = project / ".hefesto"
        _check_path(dot_hefesto, learnings_path)
        learnings_path.parent.mkdir(parents=True, exist_ok=True)
        today = util.now_iso()[:10]
        line = f"- {today}: {clean_text}\n"
        _check_path(dot_hefesto, learnings_path)
        with open(learnings_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return learnings_path

    pdir = pending_dir()
    today = util.now_iso()[:10]
    if not stack and project is not None:
        try:
            detected = distribution.detect_stack(project).get("stacks", [])
            stack = detected[0] if detected else ""
        except Exception:
            stack = ""
    _check_secrets(stack or "")
    stack = _stack_slug(stack or "")

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", clean_text[:20].strip()).strip("-").lower() or "nota"
    digest = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()[:8]
    filename = f"{today}-{slug}-{digest}.md"
    candidate_path = pdir / filename

    meta = {
        "fecha": today,
        "proyecto": proj_name,
        "stack": stack,
        "tipo": kind,
    }
    content = format_note(meta, clean_text)
    _check_secrets(content)
    _check_memory_path(candidate_path)
    pdir.mkdir(parents=True, exist_ok=True)
    _check_memory_path(candidate_path)
    candidate_path.write_text(content, encoding="utf-8", newline="\n")
    return candidate_path


def pending() -> list[Path]:
    """Lista las rutas de notas candidatas pendientes en `_pendientes/`."""
    pdir = pending_dir()
    _check_memory_path(pdir)
    if not pdir.is_dir():
        return []
    dated: list[tuple[str, str, Path]] = []
    for path in pdir.glob("*.md"):
        try:
            if _safe_to_read(path) and path.is_file():
                date_val = ""
                try:
                    meta, _body = parse_note_content(path.read_text(encoding="utf-8"))
                    date_val = meta.get("fecha", "")
                except (OSError, UnicodeDecodeError) as exc:
                    print(f"Aviso: no se pudo leer la candidata {path.name}: {exc}", file=sys.stderr)
                dated.append((date_val, path.name, path))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Aviso: no se pudo leer la candidata {path.name}: {exc}", file=sys.stderr)
            dated.append(("", path.name, path))
    return [path for _date, _name, path in sorted(dated)]


def _read_candidate(candidate: Path) -> tuple[dict[str, str], str] | None:
    if not _safe_to_read(candidate):
        return None
    try:
        _check_path(pending_dir(), candidate)
        raw = candidate.read_text(encoding="utf-8")
        _check_secrets(raw)
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "---":
            raise HefestoError("Cabecera rota: falta delimitador inicial de frontmatter.", EXIT_INPUT)
        found_closing = False
        for line in lines[1:]:
            if line.strip() == "---":
                found_closing = True
                break
        if not found_closing:
            raise HefestoError("Cabecera rota: frontmatter sin cerrar.", EXIT_INPUT)
        meta, body = parse_note_content(raw)
        if "tipo" not in meta:
            raise HefestoError("Cabecera rota: falta el campo 'tipo'.", EXIT_INPUT)
        _check_kind(meta["tipo"])
        return meta, body
    except (HefestoError, OSError, UnicodeDecodeError) as exc:
        print(f"Aviso: candidata rechazada; permanece en _pendientes. {exc}", file=sys.stderr)
        return None


def _approve_candidate(candidate: Path, project: Path | None, edited_body: str | None = None) -> bool:
    # Releer tras la interacción: el fichero pudo cambiar desde que se mostró.
    note = _read_candidate(candidate)
    if note is None:
        return False
    meta, body = note
    if edited_body is not None:
        body = edited_body.strip()
    try:
        _check_secrets(format_note(meta, body))
    except HefestoError as exc:
        print(f"Aviso: candidata rechazada; permanece en _pendientes. {exc}", file=sys.stderr)
        return False
    fecha = meta.get("fecha") or util.now_iso()[:10]
    proj_name = meta.get("proyecto") or ""
    stack = _stack_slug(meta.get("stack", ""))
    tipo = meta["tipo"]

    if is_memory_enabled(project):
        if tipo == "stacks":
            target_dir = memory_dir() / "stacks" / (stack or "general")
        else:
            target_dir = memory_dir() / tipo
        dest = target_dir / candidate.name
        _check_memory_path(dest)
        target_dir.mkdir(parents=True, exist_ok=True)
        approved_meta = {
            "fecha": fecha,
            "proyecto": proj_name,
            "stack": stack,
            "tipo": tipo,
        }
        for extra_key in ("tarea", "confianza", "alcance", "fuentes", "clave"):
            if extra_key in meta:
                approved_meta[extra_key] = meta[extra_key]
        content = format_note(approved_meta, body)
        _check_memory_path(dest)
        dest.write_text(content, encoding="utf-8", newline="\n")
    else:
        learnings_path = root() / "LEARNINGS.md"
        _check_path(root(), learnings_path)
        learnings_path.parent.mkdir(parents=True, exist_ok=True)
        _check_path(root(), learnings_path)
        with open(learnings_path, "a", encoding="utf-8") as fh:
            fh.write(f"- {fecha}: {body}\n")
    _check_memory_path(candidate)
    _check_path(pending_dir(), candidate)
    candidate.unlink()
    return True


def review(project: Path | None = None, yes: bool = False) -> str:
    """Revisa las notas candidatas pendientes de la memoria global."""
    if yes:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    else:
        util.require_tty("hefesto memory review")

    notes = pending()
    if not notes:
        return "No hay notas pendientes de revisión."

    approved = 0
    edited = 0
    discarded = 0
    skipped = 0

    for cand in notes:
        note = _read_candidate(cand)
        if note is None:
            skipped += 1
            continue
        meta, body = note

        if yes:
            if _approve_candidate(cand, project):
                approved += 1
            else:
                skipped += 1
            continue

        print(f"\n--- Candidata: {cand.name} ---")
        print(f"Fecha: {meta.get('fecha', '')}  Proyecto: {meta.get('proyecto', '')}  Stack: {meta.get('stack', '')}  Tipo: {meta.get('tipo', '')}")
        print(f"Texto: {body}\n")
        ans = input("¿Aprobar (a), editar (e), descartar (d) o saltar (s)? ").strip().lower()

        if ans in ("a", "aprobar"):
            if _approve_candidate(cand, project):
                approved += 1
            else:
                skipped += 1
        elif ans in ("e", "editar"):
            new_text = input("Nuevo texto: ")
            if not new_text.strip():
                skipped += 1
                continue
            if _approve_candidate(cand, project, new_text):
                edited += 1
            else:
                skipped += 1
        elif ans in ("d", "descartar"):
            _check_memory_path(cand)
            _check_path(pending_dir(), cand)
            if _read_candidate(cand) is None:
                skipped += 1
                continue
            cand.unlink()
            discarded += 1
        else:
            skipped += 1

    return f"Revisión completada: {approved} aprobadas, {edited} editadas, {discarded} descartadas, {skipped} saltadas."


def search(term: str, limit: int = 10) -> list[dict[str, Any]]:
    """Busca sin distinguir mayúsculas en `memory/**/*.md` (excepto `_pendientes`) y en `<raíz>/LEARNINGS.md`."""
    if not term.strip():
        return []

    lower_term = term.lower()
    results: list[dict[str, Any]] = []

    mem = memory_dir()
    try:
        _check_memory_path(mem)
        if mem.is_dir():
            for f in _approved_notes():
                if not _safe_to_read(f):
                    continue
                try:
                    raw = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                count = raw.lower().count(lower_term)
                if count > 0:
                    _meta, body = parse_note_content(raw)
                    lines = [ln.strip() for ln in body.splitlines() if lower_term in ln.lower()]
                    if not lines:
                        lines = [ln.strip() for ln in raw.splitlines() if lower_term in ln.lower()]
                    snippet = guard.redact(lines[0] if lines else (body or raw))[:160]
                    results.append({"path": str(f), "snippet": snippet, "matches": count})
    except (HefestoError, OSError) as exc:
        print(f"Aviso: se ignora el almacén de memoria: {exc}", file=sys.stderr)

    learnings_path = root() / "LEARNINGS.md"
    try:
        _check_path(root(), learnings_path)
        if learnings_path.is_file():
            try:
                raw = learnings_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            count = raw.lower().count(lower_term)
            if count > 0:
                lines = [ln.strip() for ln in raw.splitlines() if lower_term in ln.lower()]
                snippet = guard.redact(lines[0] if lines else raw)[:160]
                results.append({"path": str(learnings_path), "snippet": snippet, "matches": count})
    except (HefestoError, OSError) as exc:
        print(f"Aviso: se ignora el LEARNINGS global: {exc}", file=sys.stderr)

    results.sort(key=lambda r: r["matches"], reverse=True)
    return results[:limit]


def relevant(
    project: Path | None,
    task: Any = None,
    max_notes: int | None = None,
    max_chars: int | None = None,
) -> list[str]:
    """Devuelve las notas globales más relevantes para la tarea y stack del proyecto."""
    try:
        try:
            cfg = config.load(project)
        except Exception:
            cfg = config.Config(data={})

        if max_notes is None:
            max_notes = int(cfg.get("memory", "max_notes", default=5))
        if max_chars is None:
            max_chars = int(cfg.get("memory", "max_chars", default=2000))
        if max_notes <= 0 or max_chars <= 0:
            return []

        title = ""
        files: list[str] = []
        if task is not None:
            title = getattr(task, "title", None) or (task.get("title") if isinstance(task, dict) else "") or ""
            files = list(getattr(task, "files", None) or (task.get("files") if isinstance(task, dict) else []) or [])

        terms: set[str] = set()
        if title:
            for w in re.findall(r"[\w-]+", title):
                if len(w) >= 4:
                    terms.add(w.lower())

        for f in files:
            for part in re.split(r"[/\\.]+", f):
                if len(part) >= 3:
                    terms.add(part.lower())

        if project is not None and project.exists():
            try:
                for st in distribution.detect_stack(project).get("stacks", []):
                    if st and len(st) >= 2:
                        terms.add(st.lower())
            except Exception:
                pass

        if not terms:
            return []

        candidates: list[tuple[int, str]] = []
        seen: set[str] = set()

        mem = memory_dir()
        try:
            _check_memory_path(mem)
            if mem.is_dir():
                for f in _approved_notes():
                    if not _safe_to_read(f):
                        continue
                    try:
                        raw = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    _meta, body = parse_note_content(raw)
                    searchable = (f.relative_to(mem).as_posix() + "\n" + body).lower()
                    score = sum(searchable.count(t) for t in terms)
                    content = body or raw.strip()
                    if score > 0 and content not in seen:
                        seen.add(content)
                        candidates.append((score, content))
        except (HefestoError, OSError) as exc:
            print(f"Aviso: se ignora el almacén de memoria: {exc}", file=sys.stderr)

        learnings_path = root() / "LEARNINGS.md"
        try:
            _check_path(root(), learnings_path)
            if learnings_path.is_file():
                try:
                    raw = learnings_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = ""
                blocks = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("- ")]
                for b in blocks:
                    score = sum(b.lower().count(t) for t in terms)
                    content = b.lstrip("- ").strip()
                    if score > 0 and content not in seen:
                        seen.add(content)
                        candidates.append((score, content))
        except (HefestoError, OSError) as exc:
            print(f"Aviso: se ignora el LEARNINGS global: {exc}", file=sys.stderr)

        candidates.sort(key=lambda c: c[0], reverse=True)

        selected: list[str] = []
        total_chars = 0
        for score, content in candidates:
            if len(selected) >= max_notes:
                break
            redacted = guard.redact(content)
            if total_chars + len(redacted) > max_chars:
                if not selected:
                    selected.append(redacted[:max_chars])
                break
            selected.append(redacted)
            total_chars += len(redacted)

        return selected
    except Exception as exc:
        print(f"Aviso: fallo al obtener notas de memoria: {exc}", file=sys.stderr)
        return []


def prune(project: Path | None = None, yes: bool = False) -> str:
    """Limpia notas duplicadas y obsoletas de la memoria global."""
    if yes:
        if "HEFESTO_ROLE" in os.environ:
            raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    else:
        util.require_tty("hefesto memory prune")

    store = memory_dir()
    _check_memory_path(store)
    if not store.is_dir():
        return "No hay notas en la memoria para limpiar."

    try:
        cfg = config.load(project)
    except Exception:
        cfg = config.Config(data={})

    prune_days = int(cfg.get("memory", "prune_days", default=365))

    proposals: list[tuple[Path, str]] = []
    seen_texts: dict[str, Path] = {}
    now = datetime.datetime.now(datetime.timezone.utc)

    for f in _approved_notes():
        if not _safe_to_read(f):
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Aviso: no se pudo leer la nota {f.name}; se conserva. {exc}", file=sys.stderr)
            continue
        meta, body = parse_note_content(raw)
        norm_text = " ".join(body.split()).lower()
        if norm_text:
            if norm_text in seen_texts:
                orig = seen_texts[norm_text]
                proposals.append((f, f"duplicado de {orig.name}"))
                continue
            seen_texts[norm_text] = f

        date_str = meta.get("fecha")
        is_old = False
        if date_str:
            try:
                d = datetime.date.fromisoformat(date_str)
                if (now.date() - d).days > prune_days:
                    is_old = True
            except Exception:
                pass
        if not is_old and not date_str:
            try:
                mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc)
                if (now - mtime).days > prune_days:
                    is_old = True
            except OSError:
                pass
        if is_old:
            proposals.append((f, f"más antigua que {prune_days} días"))

    if not proposals:
        return "No hay notas obsoletas ni duplicadas en la memoria."

    deleted = 0
    for f, reason in proposals:
        _check_memory_path(f)
        if f.is_file() and (yes or util.confirm(f"¿Borrar {f.name} ({reason})?", default=False)):
            _check_memory_path(f)
            try:
                f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"Aviso: no se pudo leer la nota {f.name}; se conserva. {exc}", file=sys.stderr)
                continue
            f.unlink()
            deleted += 1

    return f"Limpieza completada: {deleted} notas eliminadas."
