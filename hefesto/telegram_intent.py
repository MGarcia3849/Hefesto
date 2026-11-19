"""Intérprete determinista: los mensajes son datos, nunca comandos ejecutables."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

_REQUEST = re.compile(r'^\s*(abre|abrir|ábreme|abreme|arregla|cambia|haz)\s+(?:el\s+)?proyecto\s+(.+?)\s*$', re.I)
_GENERAL = re.compile(r'\b(redact\w*|escrib\w*|informe|word|excel|correo|email|mail|contest\w*|'
                      r'respond\w*|busc\w*|resum\w*|calendario|archivos?|carpetas?|descarg\w*)\b')
_EFFECT = re.compile(r'\b(envi\w*|mand\w*|borr\w*|elimin\w*|muev\w*|mov\w*|instal\w*|'
                     r'ejecut\w*|public\w*|compr\w*|contest\w*|respond\w*)\b')
_PROJECT = re.compile(r'\b(proyecto|repositorio|hefesto|roadmap|commit|git|codigo|bug|fallo)\b|'
                      r'/(?:deploy|run|finish|push)\b|^(?:corrige|arregla)\b')


def is_project_request(text: str) -> bool:
    return isinstance(text, str) and _REQUEST.match(text) is not None


def normalize(text: str) -> str:
    text = ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn')
    return re.sub(r'[\s_-]+', ' ', text).strip()


@dataclass(frozen=True)
class Intent:
    kind: str = 'unknown'
    project_id: str | None = None
    repo_url: str | None = None
    editor: str | None = None
    instruction: str = ''
    options: list[str] = field(default_factory=list)


def parse(text: str, projects: list, repos: list[dict] | None = None) -> Intent:
    if not isinstance(text, str):
        return Intent()
    match = _REQUEST.match(text)
    if not match:
        normalized = normalize(text)
        named = [p for p in projects if any(re.search(r'(?<!\w)' + re.escape(normalize(n)) + r'(?!\w)',
                                                      normalized) for n in (p.id, p.name) if normalize(n))]
        if len(named) > 1:
            return Intent('ambiguous', options=[p.id for p in named])
        if named:
            return Intent('work', project_id=named[0].id, instruction=text.strip())
        if _PROJECT.search(normalized):
            return Intent()
        if _GENERAL.search(normalized) or _EFFECT.search(normalized):
            return Intent('general_action' if _EFFECT.search(normalized) else 'general',
                          instruction=text.strip())
        return Intent()
    verb, rest = match.groups()
    opening = normalize(verb) in ('abre', 'abrir', 'abreme')
    parts = re.split(r'\s+y\s+(?=(?:haz|arr[eé]gla\w*|cambia\w*|que)\b)', rest, maxsplit=1, flags=re.I)
    name = parts[0]
    instruction = parts[1].strip() if len(parts) == 2 else ('' if opening else text.strip())
    editor = None
    editor_match = re.search(r'\s+en\s+(android\s+studio|studio|visual\s+studio\s+code|vs\s*code|vsc)\s*$',
                             name, re.I)
    if editor_match:
        editor = 'studio' if normalize(editor_match[1]) in ('android studio', 'studio') else 'vscode'
        name = name[:editor_match.start()]
    wanted = normalize(name)
    if not wanted:
        return Intent()

    def candidates(rows: list, local: bool) -> list:
        def names(row: object) -> list[str]:
            return [normalize(row.id), normalize(row.name)] if local else [normalize(row['name'])]
        exact = [row for row in rows if wanted in names(row)]
        if exact:
            return exact
        return [row for row in rows if any(n.startswith(wanted + ' ') or
                difflib.SequenceMatcher(None, wanted, n).ratio() >= .86 for n in names(row))]

    for rows, local in ((projects, True), (repos or [], False)):
        found = candidates(rows, local)
        if len(found) > 1:
            return Intent('ambiguous', options=[p.id if local else p['name'] for p in found])
        if found:
            row = found[0]
            kind = ('open_and_work' if opening and instruction else 'open' if opening else 'work') if local else (
                'clone_and_work' if instruction else 'clone')
            return Intent(kind, row.id if local else None, None if local else row['url'],
                          editor or ('vscode' if opening else None), instruction)
    return Intent()
