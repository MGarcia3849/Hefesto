"""Descubrimiento y selección determinista (sin LLM) de skills para el paquete de tarea."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hefesto import config, roadmap, skills
from hefesto.util import FRAMEWORK_ROOT, hefesto_home

PROTOCOL_SKILLS: frozenset[str] = frozenset({
    "hefesto-next",
    "hefesto-plan",
    "hefesto-retro",
})

MAX_SKILL_BYTES = 256 * 1024

STOPWORDS: frozenset[str] = frozenset({
    # Español
    "para", "como", "este", "esta", "estos", "estas", "esos", "esas", "aquel", "aquella",
    "pero", "sino", "porque", "cuando", "donde", "quien", "cual", "cuales", "todo", "toda",
    "todos", "todas", "otro", "otra", "otros", "otras", "mismo", "misma", "mismos", "mismas",
    "cada", "algun", "alguna", "algunos", "algunas", "ningun", "ninguna", "entre", "hacia",
    "hasta", "desde", "sobre", "tras", "bajo", "ante", "contra", "segun", "durante", "mediante",
    "solo", "solamente", "tambien", "ademas", "aunque", "mientras", "pues", "luego", "entonces",
    "antes", "despues", "ahora", "aqui", "alli", "algo", "nada", "muy", "mas", "menos", "poco",
    "mucho", "mucha", "muchos", "muchas", "tanto", "tanta", "tantos", "tantas", "haber", "hacer",
    "tener", "estar", "debe", "deben", "puede", "pueden", "sera", "seran", "sido", "sean",
    "tiene", "tienen", "del", "las", "los", "una", "uno", "unas", "unos", "con", "sin", "por",
    "que", "sus",
    # Inglés
    "with", "that", "from", "this", "these", "those", "have", "will", "would", "could", "should",
    "into", "onto", "upon", "over", "under", "between", "among", "through", "during", "before",
    "after", "above", "below", "about", "against", "within", "without", "along", "across",
    "behind", "beyond", "plus", "except", "some", "such", "than", "then", "them", "they", "their",
    "theirs", "there", "where", "when", "what", "which", "while", "whom", "whose", "only", "also",
    "very", "more", "most", "much", "many", "each", "every", "other", "another", "same", "been",
    "were", "does", "make", "made",
})

ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "visual": ("visual", "ui", "ux", "frontend", "animacion", "diseno"),
    "reviewer": ("review", "revision"),
    "core": ("test", "contrato"),
}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    source: str
    path: Path
    description: str
    tags: str
    body: str


@dataclass(frozen=True)
class SkillChoice:
    name: str
    source: str
    path: Path
    description: str
    score: int
    reasons: list[str] = field(default_factory=list)
    body: str = field(default="", compare=False)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalize(text))


def default_roots(project: Path, cfg: config.Config) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = [
        ("proyecto", project / ".claude" / "skills"),
        ("proyecto", project / ".agents" / "skills"),
        ("promovida", hefesto_home() / "skills" / "promoted"),
        ("framework", FRAMEWORK_ROOT / "skills"),
    ]
    user_roots = cfg.get("skills_selection", "user_roots", default=[]) or []
    if isinstance(user_roots, list):
        for raw in user_roots:
            if isinstance(raw, str) and raw.strip():
                roots.append(("usuario", Path(os.path.expanduser(raw.strip()))))
    return roots


def discover(roots: list[tuple[str, Path]]) -> list[SkillInfo]:
    seen: set[str] = set()
    infos: list[SkillInfo] = []
    for source, root_dir in roots:
        root_path = Path(root_dir)
        try:
            if not root_path.is_dir():
                continue
            resolved_root = root_path.resolve()
            entries = sorted(root_path.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for sub in entries:
            try:
                if not sub.is_dir():
                    continue
                if not sub.resolve().is_relative_to(resolved_root):
                    continue
                skill_md = sub / "SKILL.md"
                if not skill_md.is_file():
                    continue
                if not skill_md.resolve().is_relative_to(resolved_root):
                    continue
                if skill_md.stat().st_size > MAX_SKILL_BYTES:
                    continue
                raw = skill_md.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, body = skills._parse_frontmatter(raw)
            name = (meta.get("name") or "").strip() or sub.name
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            infos.append(
                SkillInfo(
                    name=name,
                    source=source,
                    path=skill_md,
                    description=(meta.get("description") or "").strip(),
                    tags=(meta.get("tags") or "").strip(),
                    body=body.strip(),
                )
            )
    return infos


def _file_keywords(rel_path: str) -> list[str]:
    low = rel_path.strip().lower()
    out: list[str] = []
    if low.endswith((".component.ts", ".component.html", ".component.css", ".component.scss")):
        out.append("angular")
    if low.endswith(".ts"):
        out.append("typescript")
    elif low.endswith((".tsx", ".jsx")):
        out.append("react")
    elif low.endswith((".css", ".scss")):
        out.append("css")
    elif low.endswith(".html"):
        out.append("html")
    elif low.endswith(".py"):
        out.append("python")
    elif low.endswith(".dart"):
        out.append("flutter")
    return out


def keywords(role: str, task: roadmap.Task) -> list[str]:
    seen: set[str] = set()
    words: list[str] = []

    def add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            words.append(token)

    for text in (getattr(task, "title", "") or "", getattr(task, "accept", "") or ""):
        for tok in _tokenize(text):
            if len(tok) >= 4 and tok not in STOPWORDS:
                add(tok)

    for fpath in getattr(task, "files", []) or []:
        for tok in _file_keywords(fpath):
            add(tok)

    role_key = (role or getattr(task, "role", "") or "").strip().lower()
    for tok in ROLE_KEYWORDS.get(role_key, ()):
        add(tok)

    return words


def _pinned_names(task: Any) -> list[str]:
    if task is None:
        return []
    raw = getattr(task, "skills", None)
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    fields = getattr(task, "fields", None)
    if isinstance(fields, dict) and "skills" in fields:
        return roadmap._split_list(str(fields["skills"]))
    return []


def _score_skill(info: SkillInfo, words: list[str]) -> tuple[int, list[str]]:
    name_tokens = set(_tokenize(info.name))
    tag_tokens = set(_tokenize(info.tags))
    desc_tokens = set(_tokenize(info.description))
    score = 0
    reasons: list[str] = []
    for kw in words:
        if kw in name_tokens:
            score += 3
            reasons.append(f"nombre: {kw} (+3)")
        elif kw in tag_tokens:
            score += 2
            reasons.append(f"tags: {kw} (+2)")
        elif kw in desc_tokens:
            score += 1
            reasons.append(f"descripción: {kw} (+1)")
    return score, reasons


def select(
    project: Path,
    cfg: config.Config,
    role: str,
    task: roadmap.Task,
    roots: list[tuple[str, Path]] | None = None,
) -> list[SkillChoice]:
    actual_roots = default_roots(project, cfg) if roots is None else roots
    installed = discover(actual_roots)
    by_name = {info.name.casefold(): info for info in installed}

    enabled = bool(cfg.get("skills_selection", "enabled", default=True))
    max_skills = int(cfg.get("skills_selection", "max_skills", default=3))
    min_score = int(cfg.get("skills_selection", "min_score", default=2))
    raw_exclude = cfg.get("skills_selection", "exclude", default=[]) or []
    excluded = set(PROTOCOL_SKILLS)
    if isinstance(raw_exclude, list):
        for item in raw_exclude:
            if isinstance(item, str) and item.strip():
                excluded.add(item.strip().casefold())

    words = keywords(role, task) if task is not None else []

    pinned_choices: list[SkillChoice] = []
    pinned_seen: set[str] = set()
    for pinned_name in _pinned_names(task):
        key = pinned_name.casefold()
        if key in pinned_seen or key in excluded:
            continue
        info = by_name.get(key)
        if info is None:
            continue
        pinned_seen.add(key)
        score, match_reasons = _score_skill(info, words)
        pinned_choices.append(
            SkillChoice(
                name=info.name,
                source=info.source,
                path=info.path,
                description=info.description,
                score=score,
                reasons=["fijada en el ROADMAP", *match_reasons],
                body=info.body,
            )
        )

    if not enabled:
        return pinned_choices

    scored: list[SkillChoice] = []
    for info in installed:
        key = info.name.casefold()
        if key in pinned_seen or key in excluded:
            continue
        score, reasons = _score_skill(info, words)
        if score > 0 and score >= min_score:
            scored.append(
                SkillChoice(
                    name=info.name,
                    source=info.source,
                    path=info.path,
                    description=info.description,
                    score=score,
                    reasons=reasons,
                    body=info.body,
                )
            )

    scored.sort(key=lambda c: (-c.score, c.name))
    # Las fijadas en el ROADMAP entran a propósito aunque superen max_skills (decisión del Core).
    remaining = max(0, max_skills - len(pinned_choices))
    return pinned_choices + scored[:remaining]
