"""Aprendizaje determinista: extracción de lecciones desde evidencias, revisiones y fallos."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from hefesto import distribution, guard, memory, skills, util

_SKILL_STOPWORDS = frozenset({
    "acerca", "ademas", "ahora", "algun", "alguna", "algunas", "algunos",
    "ambas", "ambos", "antes", "aquel", "aquella", "aquellas", "aquellos",
    "aunque", "como", "contra", "cuando", "cualquier", "cualquiera", "cuales",
    "cuyas", "cuyos", "debido", "desde", "despues", "donde", "durante",
    "entonces", "entre", "estaba", "estaban", "estado", "estamos", "estan",
    "estar", "estas", "estos", "fueron", "haber", "habia", "habian", "hacer",
    "hacia", "hasta", "hecho", "intento", "intentos", "jamas", "luego",
    "mediante", "misma", "mismas", "mismo", "mismos", "mientras", "motivo",
    "muchas", "muchos", "ningun", "ninguna", "ninguno", "nunca", "otras",
    "otros", "para", "pero", "porque", "puede", "pueden", "quien", "quienes",
    "requirieron", "segun", "siempre", "siendo", "sobre", "solucion",
    "superar", "alrededor", "tambien", "tampoco", "tanta", "tantas", "tanto",
    "tantos", "tarea", "tareas", "tener", "tiene", "tienen", "todas", "todos",
    "varias", "varios", "about", "after", "again", "against", "because",
    "before", "below", "between", "could", "during", "further", "having",
    "other", "should", "their", "there", "these", "those", "through",
    "under", "until", "where", "which", "while", "would",
})


def _clean_reason_text(text: str) -> str:
    clean = guard.redact(text).lower()
    clean = re.sub(r"^revisi[óo]n negativa:\s*", "", clean)
    clean = re.sub(r"^verify sali[óo] con \d+:\s*", "", clean)
    return re.sub(r"\btask-\d+\b", " ", clean)


def _reason_words(text: str) -> set[str]:
    clean = _clean_reason_text(text)
    return {
        w
        for w in re.findall(r"[a-z0-9áéíóúüñ]+", clean)
        if len(w) >= 3 and not w.isdigit()
    }


def _normalize_reason_for_key(text: str) -> str:
    words = _reason_words(text)
    if words:
        return " ".join(sorted(words))
    return " ".join(sorted(set(_clean_reason_text(text).split())))


def _lesson_key(reasons: list[str], fallback: str = "") -> str:
    terms: set[str] = set()
    for r in reasons:
        if r and r.strip():
            terms.update(_reason_words(r))
    if not terms:
        for r in reasons:
            if r and r.strip():
                terms.update(_clean_reason_text(r).split())
    if not terms and fallback:
        terms.update(_reason_words(fallback) or set(_clean_reason_text(fallback).split()))
    norm = " ".join(sorted(terms))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _source_tag(project_name: str, task_id: str) -> str:
    """Codifica cada componente sin perder separadores, espacios ni porcentajes."""
    proj_part = quote(guard.redact(project_name), safe="")
    task_part = quote(guard.redact(task_id), safe="")
    return f"{proj_part}:{task_part}"


def _parse_sources(raw: str) -> list[str]:
    sources: list[str] = []
    for item in raw.split(","):
        part = item.strip()
        if not part:
            continue
        if ":" in part:
            proj_p, task_p = part.rsplit(":", 1)
            # Decodificar una sola vez evita acumular escapes en cada extracción.
            norm = _source_tag(unquote(proj_p), unquote(task_p))
        else:
            norm = quote(guard.redact(unquote(part)), safe="")
        if norm and norm not in sources:
            sources.append(norm)
    return sources


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _is_confined_in_hefesto(project: Path, target: Path) -> bool:
    """True si target no atraviesa enlaces/junctions y su ruta resuelta queda dentro de <project>/.hefesto."""
    try:
        proj_abs = Path(project).absolute()
        hefesto_dir = proj_abs / ".hefesto"
        target_abs = Path(target).absolute()
        if _is_link(hefesto_dir):
            return False
        rel = target_abs.relative_to(hefesto_dir)
        if ".." in rel.parts:
            return False
        current = target_abs
        while current != hefesto_dir:
            if _is_link(current):
                return False
            current = current.parent
        proj_resolved = proj_abs.resolve()
        hefesto_resolved = hefesto_dir.resolve()
        target_resolved = target_abs.resolve()
        hefesto_resolved.relative_to(proj_resolved)
        target_resolved.relative_to(hefesto_resolved)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _read_confined_jsonl(project: Path, path: Path) -> list[dict[str, Any]]:
    if not _is_confined_in_hefesto(project, path) or not path.is_file():
        return []
    return util.read_jsonl(path)


def _detect_first_stack(project: Path) -> str:
    try:
        stacks = distribution.detect_stack(project).get("stacks", [])
        if stacks:
            return str(stacks[0])
    except Exception:
        pass
    return ""


def _single_line(value: str) -> str:
    return " ".join(guard.redact(value).splitlines()).strip()


def _format_note(meta: dict[str, str], body: str) -> str:
    """Escapa escalares YAML sin añadir dependencias ni líneas de metadatos."""
    escaped: dict[str, str] = {}
    for key, value in meta.items():
        clean = _single_line(value)
        # Conserva los campos simples que los lectores de memoria usan literalmente.
        if key not in {"name", "description"} and re.fullmatch(r"[\w][\w ./%+,-]*", clean):
            escaped[key] = clean
        else:
            escaped[key] = json.dumps(clean, ensure_ascii=True)
    return memory.format_note(escaped, guard.redact(body))


def _parse_note_content(raw: str) -> tuple[dict[str, str], str]:
    """Lee también las cadenas JSON/YAML emitidas por este módulo."""
    meta, body = memory.parse_note_content(raw)
    for key, value in meta.items():
        if value.startswith('"'):
            try:
                decoded = json.loads(value)
            except ValueError:
                continue
            if isinstance(decoded, str):
                meta[key] = decoded
    return meta, body


def _normalize_key(value: str) -> str:
    raw = re.sub(r"\s+", " ", value).strip()
    if re.fullmatch(r"[0-9a-f]{12,64}", raw):
        return raw
    if guard.find_secrets(raw):
        return guard.redact(raw)
    return raw


def _parse_confidence(raw: Any, default: str | None = None) -> float | None:
    if raw is None:
        if default is None:
            return None
        raw = default
    text = str(raw).strip()
    if not text:
        if default is None:
            return None
        text = default
    try:
        val = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0.0 or val > 1.0:
        return None
    return val


def _normalize_stack(raw: str) -> str:
    return re.sub(r"\s+", " ", _single_line(raw)).strip().lower()


def _stack_slug(norm_stack: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", norm_stack)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = re.sub(r"\.{2,}", "-", slug).strip(".-_")
    if not slug or slug.split(".")[0].upper() in skills.WINDOWS_DEVICE_NAMES:
        return ""
    return slug


def _find_pending_by_key(key: str) -> tuple[Path, dict[str, str], str] | None:
    norm_key = _normalize_key(key)
    for path in memory.pending():
        try:
            raw = path.read_text(encoding="utf-8")
            meta, body = _parse_note_content(raw)
        except OSError:
            continue
        if _normalize_key(meta.get("clave", "")) == norm_key and meta.get("alcance", "proyecto") == "proyecto":
            return path, meta, body
    return None


def extract(project: Path) -> list[Path]:
    """Extrae lecciones candidatas deterministas desde evidencias, revisiones y fallos del proyecto."""
    proj = Path(project)
    source_project = proj.name or proj.resolve().name
    proj_name = re.sub(r"[\r\n]+", " ", guard.redact(source_project)).strip()
    ev_dir = proj / ".hefesto" / "evidence"
    if not _is_confined_in_hefesto(proj, ev_dir) or not ev_dir.is_dir():
        return []

    reviews = _read_confined_jsonl(proj, proj / ".hefesto" / "logs" / "reviews.jsonl")
    failures = _read_confined_jsonl(proj, proj / ".hefesto" / "logs" / "failures.jsonl")
    stack = guard.redact(_detect_first_stack(proj))
    today = util.now_iso()[:10]

    pdir = memory.pending_dir()
    memory._check_memory_path(pdir)

    results: list[Path] = []
    seen_paths: set[Path] = set()

    for ev_path in sorted(ev_dir.glob("*.json")):
        if not _is_confined_in_hefesto(proj, ev_path) or not ev_path.is_file():
            continue
        try:
            ev: dict[str, Any] = json.loads(ev_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(ev, dict):
            continue

        source_task = str(ev.get("task_id") or "")
        task_id = source_task.strip()
        if not task_id:
            continue

        try:
            attempts = int(ev.get("attempts") or 1)
        except (TypeError, ValueError):
            attempts = 1
        title = guard.redact(str(ev.get("title") or task_id).strip())

        failed_reviews = [
            r for r in reviews
            if r.get("task_id") == task_id and r.get("verdict") == "fail"
        ]
        ev_review = ev.get("review")
        if isinstance(ev_review, dict) and ev_review.get("verdict") == "fail" and not failed_reviews:
            failed_reviews = [ev_review]

        task_failures = [
            f for f in failures
            if f.get("task_id") == task_id
        ]

        if attempts <= 1 and not failed_reviews and not task_failures:
            continue

        reasons: list[str] = []
        for r in failed_reviews:
            note = guard.redact(str(r.get("notes") or "").strip())
            if note and note not in reasons:
                reasons.append(note)

        for f in task_failures:
            err = guard.redact(str(f.get("error") or "").strip())
            if not err:
                continue
            stripped = re.sub(r"^revisi[óo]n negativa:\s*", "", err, flags=re.I).strip()
            if stripped in reasons or err in reasons:
                continue
            reasons.append(err)

        key = _lesson_key(reasons, fallback=title)
        source_tag = _source_tag(source_project, source_task)

        reason_summary = "; ".join(reasons) if reasons else "Se requirieron varios intentos para superar la tarea"
        body = guard.redact(
            f"Motivo: {reason_summary}\n"
            f"Solución: {title} ({attempts} intentos)"
        )

        existing = _find_pending_by_key(key)
        if existing is not None:
            cand_path, meta, existing_body = existing
            sources = _parse_sources(meta.get("fuentes", ""))
            if source_tag not in sources:
                sources.append(source_tag)
                conf_val = min(0.9, 0.3 + 0.1 * (len(sources) - 1))
                meta["confianza"] = f"{conf_val:.1f}"
                meta["fuentes"] = ", ".join(sources)
                content = _format_note(meta, existing_body or body)
                memory._check_memory_path(cand_path)
                cand_path.write_text(content, encoding="utf-8", newline="\n")
            if cand_path not in seen_paths:
                seen_paths.add(cand_path)
                results.append(cand_path)
            continue

        meta = {
            "fecha": today,
            "proyecto": proj_name,
            "stack": stack,
            "tipo": "errores-resueltos",
            "tarea": task_id,
            "confianza": "0.3",
            "alcance": "proyecto",
            "fuentes": source_tag,
            "clave": key,
        }
        cand_path = pdir / f"{today}-{task_id.lower()}-{key}.md"
        content = _format_note(meta, body)
        memory._check_memory_path(cand_path)
        pdir.mkdir(parents=True, exist_ok=True)
        memory._check_memory_path(cand_path)
        cand_path.write_text(content, encoding="utf-8", newline="\n")
        if cand_path not in seen_paths:
            seen_paths.add(cand_path)
            results.append(cand_path)

    return results


def _skill_tokens(text: str) -> set[str]:
    clean = guard.redact(text)
    tokens: set[str] = set()
    for word in re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+", clean):
        norm = "".join(
            c for c in unicodedata.normalize("NFKD", word) if c.isascii() and c.isalpha()
        ).lower()
        if len(norm) >= 5 and norm not in _SKILL_STOPWORDS:
            tokens.add(norm)
    return tokens


def global_candidates() -> list[Path]:
    """Propone como candidatas globales las lecciones aprobadas de alcance proyecto repetidas en >= 2 proyectos."""
    pdir = memory.pending_dir()
    memory._check_memory_path(pdir)

    existing_global_keys: set[str] = set()
    by_key: dict[str, list[tuple[Path, dict[str, str], str]]] = {}

    for path in memory._approved_notes():
        try:
            raw = path.read_text(encoding="utf-8")
            meta, body = _parse_note_content(raw)
        except (OSError, UnicodeDecodeError):
            continue
        clave = _normalize_key(meta.get("clave", ""))
        if not clave:
            continue
        alcance = meta.get("alcance", "proyecto").strip().lower() or "proyecto"
        if alcance == "global":
            existing_global_keys.add(clave)
        elif alcance == "proyecto":
            conf = _parse_confidence(meta.get("confianza"), default="0.3")
            if conf is None:
                continue
            by_key.setdefault(clave, []).append((path, meta, body))

    for path in memory.pending():
        try:
            raw = path.read_text(encoding="utf-8")
            meta, _body = _parse_note_content(raw)
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get("alcance", "").strip().lower() == "global":
            clave = _normalize_key(meta.get("clave", ""))
            if clave:
                existing_global_keys.add(clave)

    today = util.now_iso()[:10]
    created: list[Path] = []

    for clave, notes in by_key.items():
        if clave in existing_global_keys:
            continue

        projects_list: list[str] = []
        projects_seen: set[str] = set()
        sources: list[str] = []
        max_conf = 0.3
        stack_val = ""
        tipo_val = "errores-resueltos"
        body_val = ""

        for _path, meta, body in notes:
            proj = _single_line(meta.get("proyecto", ""))
            if proj and proj.lower() not in projects_seen:
                projects_seen.add(proj.lower())
                projects_list.append(proj)

            raw_sources = meta.get("fuentes", "").strip()
            if raw_sources:
                for src in _parse_sources(raw_sources):
                    if src not in sources:
                        sources.append(src)
                    if ":" in src:
                        src_proj = _single_line(unquote(src.rsplit(":", 1)[0]))
                        if src_proj and src_proj.lower() not in projects_seen:
                            projects_seen.add(src_proj.lower())
                            projects_list.append(src_proj)
            elif proj:
                task_id = _single_line(meta.get("tarea", ""))
                tag = _source_tag(proj, task_id) if task_id else quote(proj, safe="")
                if tag and tag not in sources:
                    sources.append(tag)

            conf = _parse_confidence(meta.get("confianza"), default="0.3")
            if conf is not None and conf > max_conf:
                max_conf = conf

            if not stack_val and meta.get("stack", "").strip():
                stack_val = _single_line(meta.get("stack", ""))
            note_tipo = meta.get("tipo", "").strip()
            if note_tipo in memory.ALLOWED_KINDS:
                tipo_val = note_tipo
            if not body_val and body.strip():
                body_val = guard.redact(body.strip())

        if len(projects_list) < 2:
            continue

        digest = hashlib.sha256(clave.encode("utf-8")).hexdigest()[:12]
        cand_meta = {
            "fecha": today,
            "proyecto": _single_line(", ".join(projects_list)),
            "stack": stack_val,
            "tipo": tipo_val,
            "confianza": f"{min(0.9, max_conf):.1f}",
            "alcance": "global",
            "fuentes": _single_line(", ".join(sources)),
            "clave": clave,
        }
        cand_path = pdir / f"{today}-global-{digest}.md"

        content = _format_note(cand_meta, body_val or f"Lección global ({digest})")
        memory._check_memory_path(cand_path)
        pdir.mkdir(parents=True, exist_ok=True)
        memory._check_memory_path(cand_path)
        cand_path.write_text(content, encoding="utf-8", newline="\n")
        created.append(cand_path)
        existing_global_keys.add(clave)

    return created


def skill_drafts(min_repeats: int = 3, min_confidence: float = 0.7) -> list[Path]:
    """Agrupa notas aprobadas confiables por stack y término compartido y escribe borradores en skills/_proposals/."""
    if min_repeats < 1:
        min_repeats = 1
    if not math.isfinite(min_confidence):
        return []

    home = util.hefesto_home()
    skills_dir = home / "skills"
    proposals_dir = skills_dir / "_proposals"
    skills.check_confinement(skills_dir, home)
    skills.check_confinement(proposals_dir, skills_dir)

    by_stack: dict[str, list[tuple[Path, str, set[str]]]] = {}
    slug_to_stacks: dict[str, set[str]] = {}

    for path in memory._approved_notes():
        try:
            raw = path.read_text(encoding="utf-8")
            meta, body = _parse_note_content(raw)
        except (OSError, UnicodeDecodeError):
            continue

        norm_stack = _normalize_stack(meta.get("stack", ""))
        if not norm_stack or norm_stack in ("general", "-", "none"):
            continue
        stack_slug = _stack_slug(norm_stack)
        if not stack_slug:
            continue

        slug_to_stacks.setdefault(stack_slug, set()).add(norm_stack)

        conf = _parse_confidence(meta.get("confianza"))
        if conf is None or conf < min_confidence:
            continue

        tokens = _skill_tokens(body)
        if not tokens:
            continue

        by_stack.setdefault(norm_stack, []).append((path, guard.redact(body.strip()), tokens))

    created: list[Path] = []

    for norm_stack in sorted(by_stack.keys()):
        stack_slug = _stack_slug(norm_stack)
        if len(slug_to_stacks.get(stack_slug, set())) > 1:
            stack_hash = hashlib.sha256(norm_stack.encode("utf-8")).hexdigest()[:8]
            folder_prefix = f"{stack_slug}-{stack_hash}"
        else:
            folder_prefix = stack_slug

        remaining = list(by_stack[norm_stack])
        while len(remaining) >= min_repeats:
            term_groups: dict[str, list[tuple[Path, str, set[str]]]] = {}
            for item in remaining:
                for token in item[2]:
                    term_groups.setdefault(token, []).append(item)

            eligible = [
                (term, group)
                for term, group in term_groups.items()
                if len(group) >= min_repeats
            ]
            if not eligible:
                break

            eligible.sort(key=lambda kv: (-len(kv[1]), kv[0]))
            best_term, group = eligible[0]

            used_paths = {item[0] for item in group}
            remaining = [item for item in remaining if item[0] not in used_paths]

            draft_name = f"{folder_prefix}-{best_term}"
            if draft_name.casefold().startswith("hefesto-"):
                draft_name = f"stack-{draft_name}"
            skills.validate_skill_name(draft_name)

            draft_dir = proposals_dir / draft_name
            skill_path = draft_dir / "SKILL.md"
            skills.check_confinement(draft_dir, proposals_dir)
            skills.check_confinement(skill_path, proposals_dir)

            if skill_path.exists():
                continue

            bullets = "\n".join(
                f"- {_single_line(item[1])}" for item in group if item[1].strip()
            )
            body_text = guard.redact(
                f"# {draft_name}\n\n"
                f"Borrador generado desde {len(group)} lecciones aprobadas de `{norm_stack}` "
                f"sobre `{best_term}`.\n\n"
                f"## Lecciones\n\n"
                f"{bullets}\n"
            )
            meta_out = {
                "name": draft_name,
                "description": _single_line(
                    f"Patrones recurrentes de {norm_stack} sobre {best_term} ({len(group)} lecciones aprobadas)."
                ),
            }
            content = _format_note(meta_out, body_text)
            draft_dir.mkdir(parents=True, exist_ok=True)
            skills.check_confinement(draft_dir, proposals_dir)
            skills.check_confinement(skill_path, proposals_dir)
            skill_path.write_text(content, encoding="utf-8", newline="\n")
            created.append(skill_path)

    return created
