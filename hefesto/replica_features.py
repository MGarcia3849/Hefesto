"""Inventario funcional observable, sin conservar DOM, código ni valores de campos."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from collections import deque
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import shlex
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from hefesto import approval, config, replica, roadmap, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

SCHEMA_VERSION = 1
FEATURE_KINDS = ("section", "form", "action", "search", "filter", "flow", "account", "state")
FEATURE_KEYS = ("kind", "key", "label", "details", "route", "evidence")
SIGNAL_KEYS = ("technologies", "requests", "patterns")
FEATURE_STATES = ("tenemos", "falta", "parcial", "mejor_nuestra", "no_aplica", "no_verificable")
ROW_KEYS = ("kind", "key", "state", "reference", "project", "note")
IMPROVEMENT_KEYS = ("key", "title", "why", "effort", "risk")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_VALIDATIONS = ("pattern", "min", "max", "minlength", "maxlength")
_ACCEPTANCE_KEYS = ("id", "project", "proposal_sha256", "matrix_sha256", "channel", "accepted_at")


def _clean(value: str) -> str:
    return replica.sanitize(value)[:300]


def _normalized(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.lower())
    return "-".join(re.findall(r"[a-z0-9]+", "".join(c for c in plain if not unicodedata.combining(c))))


class _DOM(HTMLParser):
    """Transient tree; text in scripts, styles and form values is never collected."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict[str, Any]] = []
        self.stack: list[int] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: v or "" for k, v in attrs}
        hidden = any(self.nodes[i]["hidden"] for i in self.stack)
        hidden = hidden or tag in ("script", "style", "template", "noscript") or "hidden" in attributes
        hidden = hidden or attributes.get("aria-hidden") == "true" or attributes.get("type", "").lower() == "hidden"
        hidden = hidden or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attributes.get("style") or "", re.I))
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs if k not in ("value", "srcdoc")},
                "text": [], "parents": tuple(self.stack), "hidden": hidden, "line": self.getpos()[0]}
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag: str) -> None:
        for pos in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[pos]]["tag"] == tag:
                del self.stack[pos:]
                break

    def handle_data(self, data: str) -> None:
        if any(self.nodes[i]["hidden"] or self.nodes[i]["tag"] in ("textarea", "option") for i in self.stack):
            return
        for index in self.stack:
            self.nodes[index]["text"].append(data)


def _route(url: str) -> str:
    return _clean(urlsplit(url).path or "/")


def extract_features(html: str, route: str) -> list[dict[str, Any]]:
    dom = _DOM(html)
    features: list[dict[str, Any]] = []
    labels = {n["attrs"].get("for"): _clean(" ".join(n["text"])) for n in dom.nodes
              if n["tag"] == "label" and not n["hidden"] and n["attrs"].get("for")}

    def label(node: dict[str, Any], default: str = "") -> str:
        a = node["attrs"]
        return _clean(a.get("aria-label") or labels.get(a.get("id")) or a.get("placeholder")
                      or " ".join(node["text"]) or a.get("name") or default)

    def add(kind: str, text: str, details: dict[str, Any], tag: str) -> None:
        features.append(dict(zip(FEATURE_KEYS, (kind, kind + ":" + _normalized(text), text, details,
                                               _route(route), {"selector": tag, "line": node["line"]}))))

    for index, node in enumerate(dom.nodes):
        if node["hidden"]:
            continue
        tag, a = node["tag"], node["attrs"]
        text = label(node)
        typ = a.get("type", "text").lower()
        if tag == "form":
            fields = []
            for child in dom.nodes:
                if index not in child["parents"] or child["hidden"] or child["tag"] not in ("input", "textarea", "select"):
                    continue
                ca = child["attrs"]
                ct = ca.get("type", "text").lower() if child["tag"] == "input" else child["tag"]
                if ct in ("submit", "reset", "button", "image"):
                    continue
                field = {"name": _clean(ca.get("name") or ca.get("formcontrolname") or ca.get("id", "")),
                         "type": _clean(ct), "required": "required" in ca or ca.get("aria-required") == "true"}
                field.update({key: _clean(ca[key]) for key in _VALIDATIONS if key in ca})
                fields.append(field)
            form_label = _clean(a.get("aria-label") or a.get("name") or " ".join(f["name"] for f in fields) or "Formulario")
            add("form", form_label, {"fields": fields, "method": _clean(a.get("method", "get").upper())}, tag)
        if tag in ("h1", "h2", "h3"):
            add("section", text, {}, tag)
        if tag == "button" or (tag == "input" and typ in ("submit", "button", "reset")) or a.get("role") == "button":
            add("action", text, {"named": bool(text)}, tag)
        if tag == "input" and (typ == "search" or a.get("role") == "searchbox"):
            add("search", text or "Buscar", {"name": _clean(a.get("name") or a.get("formcontrolname", ""))}, tag)
        if tag == "select" or (tag == "input" and typ in ("checkbox", "radio")):
            add("filter", text or "Filtro", {"type": typ if tag == "input" else tag}, tag)
        if (tag == "input" and typ == "password") or (tag == "a" and re.search(r"entrar|iniciar sesi[oó]n|registr|log[ -]?in|sign[ -]?(?:in|up)", text, re.I)):
            add("account", text or "Acceso", {}, tag)
        if "data-step" in a or a.get("aria-current") == "step":
            add("flow", text or "Paso", {"progress": a.get("aria-current") == "step"}, tag)
        if "data-state" in a or "aria-busy" in a:
            add("state", _clean(a.get("data-state") or "Carga"), {}, tag)
        elif tag in ("p", "div", "span") and re.search(r"no hay|sin resultados|vac[ií]o|error|no results|empty", text, re.I):
            if not any(n["tag"] in ("p", "div", "span") and index in n["parents"] for n in dom.nodes):
                add("state", text, {}, tag)
    return features


def _template_source(source: str) -> str:
    """Convert literal JSX/bound attributes without evaluating expressions or moving lines."""
    def attribute(match: re.Match[str]) -> str:
        name, value = match.group(1), match.group(2).strip()
        if value in ("false", "null", "undefined"):
            return "\n" * match.group(0).count("\n")
        if value == "true":
            rendered = name
        elif re.fullmatch(r"(['\"])[\s\S]*\1|\d+", value):
            literal = value[1:-1] if value[0] in "'\"" else value
            rendered = name + '="' + escape(literal, quote=True) + '"'
        else:
            rendered = ""
        return rendered.replace("\n", " ") + "\n" * match.group(0).count("\n")

    names = r"name|id|type|placeholder|aria-label|required|min|max|minlength|maxlength|pattern|formcontrolname"
    source = re.sub(rf"\b({names})\s*=\s*\{{([^{{}}]*)\}}", attribute, source, flags=re.I)

    def bound_attribute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = match.group(4).strip()
        if value == "true":
            rendered = name
        elif value in ("false", "null", "undefined"):
            rendered = ""
        elif re.fullmatch(r"\d+|(['\"])[\s\S]*\1", value):
            literal = value[1:-1] if value[0] in "'\"" else value
            rendered = name + '="' + escape(literal, quote=True) + '"'
        else:
            rendered = ""
        return rendered.replace("\n", " ") + "\n" * match.group(0).count("\n")

    return re.sub(rf"(?:\[({names})\]|(?:v-bind:|:)({names}))\s*=\s*(['\"])(.*?)\3",
                  bound_attribute, source, flags=re.I | re.S)


def _call_body(source: str, start: int) -> str:
    """Read a balanced Dart constructor, respecting strings and nested calls."""
    depth = 1
    quote = ""
    escaped = False
    for end in range(start, len(source)):
        char = source[end]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return source[start:end]
    return source[start:]


def _code_features(source: str, suffix: str, path: str) -> list[dict[str, Any]]:
    template = _template_source(source)
    if suffix == ".vue":
        template = re.sub(r"</?template\b[^>]*>", lambda m: "\n" * m.group(0).count("\n"), template)
    features = extract_features(template, "/") if suffix != ".dart" else []
    for feature in features:
        feature["evidence"]["path"] = path

    def add(kind: str, label: str, details: dict[str, Any], offset: int, route: str = "/") -> None:
        label = _clean(label)
        features.append(dict(zip(FEATURE_KEYS, (kind, kind + ":" + _normalized(label), label, details,
                                               route, {"path": path, "line": source.count("\n", 0, offset) + 1}))))

    # Angular/Vue object routes, React Router props and Flutter's named route map.
    routes = r"\bpath\s*[:=]\s*\{?\s*['\"]([^'\"\r\n]*)['\"]"
    if suffix == ".dart":
        routes = r"['\"](/[^'\"\r\n]*)['\"]\s*:"
    for match in re.finditer(routes, source):
        route = "/" + match.group(1).lstrip("/")
        add("section", route, {"route": route}, match.start(), route)
    if suffix == ".dart":
        fields = []
        field_offset = 0
        for match in re.finditer(r"\bTextFormField\s*\(", source):
            body = _call_body(source, match.end())
            name = re.search(r"\b(?:labelText|hintText|name)\s*:\s*['\"]([^'\"]*)['\"]", body)
            fields.append({"name": _clean(name.group(1)) if name else f"campo-{len(fields) + 1}",
                           "type": "password" if re.search(r"obscureText\s*:\s*true", body) else "text",
                           "required": bool(re.search(r"\bvalidator\s*:", body))})
            if len(fields) == 1:
                field_offset = match.start()
        if fields:
            add("form", " ".join(f["name"] for f in fields), {"fields": fields, "method": "GET"}, field_offset)
        for match in re.finditer(r"\b(?:ElevatedButton|TextButton)\s*\(", source):
            body = _call_body(source, match.end())
            label = re.search(r"\bText\s*\(\s*['\"]([^'\"]*)['\"]", body)
            add("action", label.group(1) if label else "", {"named": bool(label)}, match.start())
    return features


def _service_requests(source: str, path: str) -> list[dict[str, Any]]:
    pattern = (r"\b(?:(fetch)\s*\(|(?:axios|http|httpClient|client)\s*\.\s*"
               r"(get|post|put|patch|delete|head|options)\s*(?:<[^>]*>)?\s*\()\s*['\"]([^'\"\r\n]+)['\"]")
    requests = []
    for match in re.finditer(pattern, source, re.I):
        method = (match.group(2) or "GET").upper()
        if match.group(1):
            options = _call_body(source, source.index("(", match.start()) + 1)
            explicit = re.search(r"\bmethod\s*:\s*['\"](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]", options, re.I)
            if explicit:
                method = explicit.group(1).upper()
        try:
            route = _route(match.group(3))
        except ValueError:
            continue
        requests.append({"method": method, "path": route, "type": "fetch" if match.group(1) else "xhr",
                         "evidence": {"path": path, "line": source.count("\n", 0, match.start()) + 1}})
    return requests


def study_project(project: Path, replica_id: str) -> dict[str, Any]:
    """Inventory source files as data; never import, run or follow linked project code."""
    folder = replica.replica_dir(project, replica_id)
    project = Path(project)
    ignored = {".git", "node_modules", "dist", "build", ".hefesto", "__pycache__", ".dart_tool", ".next"}
    suffixes = {".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx", ".js", ".ts", ".dart"}
    features: list[dict[str, Any]] = []
    signals: dict[str, Any] = {"technologies": [], "requests": [], "patterns": []}
    technologies: set[str] = set()
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.lower() not in ignored and not (Path(root) / d).is_symlink()
                         and not os.path.islink(Path(root) / d)
                         and not getattr(Path(root) / d, "is_junction", lambda: False)())
        for filename in sorted(files):
            path = Path(root) / filename
            suffix = path.suffix.lower()
            if (suffix not in suffixes or filename.lower().startswith((".env", "id_")) or path.is_symlink()):
                continue
            try:
                raw = path.read_bytes()
                if b"\x00" in raw:
                    continue
                source = raw.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            relative = path.relative_to(project).as_posix()
            features.extend(_code_features(source, suffix, relative))
            signals["requests"].extend(_service_requests(source, relative))
            if suffix in (".jsx", ".tsx"):
                technologies.add("React")
            if suffix == ".vue":
                technologies.add("Vue")
            if suffix == ".svelte":
                technologies.add("Svelte")
            if suffix == ".dart":
                technologies.add("Flutter")
            if "@angular/" in source or "formControlName" in source or "HttpClient" in source:
                technologies.add("Angular")
    signals["technologies"] = sorted(technologies)
    signals["patterns"] = sorted({f["kind"] for f in features if f["kind"] not in ("section", "action")})
    result = {"schema_version": SCHEMA_VERSION, "id": replica_id, "side": "project",
              "pages": sorted({f["route"] for f in features}), "features": features, "signals": signals,
              "notes": ["Inventario estático: las señales del código no prueban su ejecución."]}
    util.atomic_write_json(folder / "functional-project.json", result)
    return result


def _field_map(feature: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_normalized(str(f.get("name", ""))): f for f in feature.get("details", {}).get("fields", [])
            if f.get("name")}


def _form_state(reference: dict[str, Any], project: dict[str, Any]) -> tuple[str, str]:
    expected, actual = _field_map(reference), _field_map(project)
    if not expected:
        return "no_verificable", "La referencia no describe los campos del formulario."
    if expected.keys() - actual.keys():
        return "parcial", "Faltan campos de la referencia."
    improved = False
    for name, field in expected.items():
        own = actual[name]
        if field.get("required") and not own.get("required"):
            return "parcial", "Faltan campos obligatorios o validaciones."
        improved |= bool(own.get("required") and not field.get("required"))
        ref_type, own_type = field.get("type", "text"), own.get("type", "text")
        if ref_type != own_type:
            if ref_type == "text" and own_type in ("email", "url", "number", "tel"):
                improved = True
            else:
                return "parcial", "Difiere el tipo de un campo de la referencia."
        for key in _VALIDATIONS:
            ref_value, own_value = field.get(key), own.get(key)
            if ref_value is not None and ref_value != own_value:
                try:
                    stronger = (float(own_value) >= float(ref_value) if key in ("min", "minlength")
                                else float(own_value) <= float(ref_value)) if key != "pattern" else False
                except (TypeError, ValueError):
                    stronger = False
                if not stronger:
                    return "parcial", "Falta o difiere una validación de la referencia."
                improved = True
            elif ref_value is None and own_value is not None:
                improved = True
    if improved:
        return "mejor_nuestra", "Cubre los campos de la referencia y añade validaciones."
    return "tenemos", "Cubre los campos y validaciones de la referencia."


def feature_matrix(reference: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Match individual features, preserving both original features and their evidence."""
    def identity(feature: dict[str, Any]) -> tuple[str, str]:
        return str(feature.get("kind", "")), _normalized(str(feature.get("key") or feature.get("label", "")))

    def order(feature: dict[str, Any]) -> tuple[str, str, str]:
        return (*identity(feature), json.dumps(feature, sort_keys=True, ensure_ascii=False))

    own = sorted(project.get("features") or [], key=order)
    refs = sorted(reference.get("features") or [], key=order)
    used: set[int] = set()
    matches: dict[int, int] = {}
    # Reserve exact matches first so a fuzzy form match cannot take another form's counterpart.
    for i, ref in enumerate(refs):
        exact = next((j for j, candidate in enumerate(own) if j not in used and identity(ref) == identity(candidate)), None)
        if exact is not None:
            matches[i] = exact
            used.add(exact)
    for i, ref in enumerate(refs):
        if i not in matches and ref.get("kind") == "search":
            name = _normalized(str(ref.get("details", {}).get("name", "")))
            candidates = [j for j, f in enumerate(own) if j not in used and f.get("kind") == "search"
                          and name and name == _normalized(str(f.get("details", {}).get("name", "")))]
            if candidates:
                matches[i] = candidates[0]
                used.add(candidates[0])
        if i in matches or ref.get("kind") != "form":
            continue
        names = _field_map(ref).keys()
        candidates = [(len(names & _field_map(f).keys()) / len(names | _field_map(f).keys()), j)
                      for j, f in enumerate(own) if j not in used and f.get("kind") == "form"
                      and names & _field_map(f).keys()]
        if candidates:
            _, chosen = min(candidates, key=lambda item: (-item[0], item[1]))
            matches[i] = chosen
            used.add(chosen)
    rows = []

    def row(ref: dict[str, Any] | None, candidate: dict[str, Any] | None, state: str, note: str) -> None:
        feature = ref if ref is not None else candidate
        assert feature is not None
        rows.append(dict(zip(ROW_KEYS, (feature.get("kind", "section"), feature.get("key", ""), state,
                                        ref, candidate, note))))

    for i, ref in enumerate(refs):
        candidate = own[matches[i]] if i in matches else None
        if (not ref.get("key") or not ref.get("evidence") or ref.get("status") == "no_verificable"
                or (ref.get("kind") == "form" and not _field_map(ref))):
            state, note = "no_verificable", "La referencia no aporta datos suficientes para comprobarla."
        elif candidate is None:
            state, note = "falta", "Solo se observa en la referencia."
        elif ref.get("kind") == "form":
            state, note = _form_state(ref, candidate)
        else:
            state, note = "tenemos", "Funcionalidad presente en ambos inventarios."
        row(ref, candidate, state, note)
    for j, candidate in enumerate(own):
        if j not in used:
            row(None, candidate, "no_aplica", "Funcionalidad propia sin equivalente en la referencia.")
    if not refs:
        row({"kind": "section", "key": "section:referencia", "evidence": {}}, None,
            "no_verificable", "La referencia no contiene funcionalidades observables.")
    rows.sort(key=lambda r: (r["kind"], r["key"], json.dumps(r, sort_keys=True, ensure_ascii=False)))
    return {"schema_version": SCHEMA_VERSION, "id": reference.get("id", project.get("id")), "rows": rows,
            "counts": {state: sum(r["state"] == state for r in rows) for state in FEATURE_STATES}}


def improvements(reference: dict[str, Any]) -> list[dict[str, str]]:
    """Suggest concrete improvements for weaknesses visible in the reference DOM."""
    found: dict[str, dict[str, str]] = {}

    def add(key: str, title: str, why: str, effort: str = "M", risk: str = "bajo") -> None:
        found.setdefault(key, dict(zip(IMPROVEMENT_KEYS, (key, title, why, effort, risk))))

    for feature in reference.get("features", []):
        kind = feature.get("kind")
        details = feature.get("details") or {}
        if kind == "form":
            fields = details.get("fields") or []
            for field in fields:
                typ = str(field.get("type", "text")).lower()
                name = _normalized(str(field.get("name", "campo"))) or "campo"
                expected_type = ("email" if re.search(r"email|correo|mail", name) else
                                 "tel" if re.search(r"telefono|tel|movil|celular", name) else
                                 "date" if re.search(r"fecha|date", name) else None)
                if expected_type and typ != expected_type:
                    add(f"validation:{name}", f"Añadir validación a {name}",
                        f"El campo {name} usa type={typ}; conviene usar type={expected_type} y validar su formato.",
                        "S", "bajo")
                elif typ in ("email", "tel", "date") and not field.get("required") and not any(
                        k in field for k in _VALIDATIONS):
                    add(f"validation:{name}", f"Añadir validación a {name}",
                        f"El campo {name} es {typ} y no muestra obligatoriedad ni validación visible.", "S", "bajo")
                elif field.get("required") and not any(k in field for k in _VALIDATIONS):
                    add(f"validation:{name}", f"Añadir validación a {name}",
                        f"El campo obligatorio {name} no muestra reglas de validación visibles.", "S", "bajo")
            if len(fields) > 8:
                add(f"form-length:{_normalized(str(feature.get('key', 'form')))}", "Dividir el formulario largo",
                    f"El formulario tiene {len(fields)} campos; agruparlos por pasos facilita completarlo.", "M", "medio")
        elif kind == "action" and not details.get("named"):
            add(f"accessible-name:{_normalized(str(feature.get('key', 'button')))}", "Añadir nombre accesible al botón",
                "El botón no tiene un nombre accesible observable.", "S", "bajo")
        elif kind == "flow" and not details.get("progress"):
            add(f"flow-progress:{_normalized(str(feature.get('key', 'flow')))}", "Añadir indicador de progreso",
                "El flujo presenta pasos sin indicar visualmente el progreso actual.", "M", "medio")
    visible_states = [f for f in reference.get("features", []) if f.get("kind") == "state"]
    if not visible_states or not any(re.search(r"vac[ií]o|empty|sin resultados|no hay", str(f.get("label", "")), re.I)
                                     for f in visible_states):
        add("state:empty", "Mostrar estado vacío", "No se observa un estado vacío o sin resultados.", "S", "bajo")
    if not visible_states or not any(re.search(r"error|fall", str(f.get("label", "")), re.I) for f in visible_states):
        add("state:error", "Mostrar estado de error", "No se observa un estado de error recuperable.", "S", "medio")
    if not visible_states or not any(re.search(r"carg|load|busy", str(f.get("label", "")), re.I) for f in visible_states):
        add("state:loading", "Mostrar estado de carga", "No se observa un estado de carga durante las operaciones.", "S", "bajo")
    return [found[key] for key in sorted(found)]


def _document_hash(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _acceptance_paths(project: Path, replica_id: str) -> tuple[Path, Path]:
    local = replica.replica_dir(project, replica_id) / "functional-acceptance.json"
    authority = replica._authority_path(project, replica_id, "functional-acceptance")
    return local, authority


def accept(project: Path, replica_id: str) -> bool:
    if not approval.human_channel():
        return False
    folder = replica.replica_dir(project, replica_id)
    proposal, matrix = folder / "functional-proposal.md", folder / "functional-matrix.json"
    try:
        ph, mh = _document_hash(proposal), _document_hash(matrix)
    except (OSError, UnicodeError):
        raise HefestoError("Falta la propuesta funcional o su matriz.", EXIT_INPUT)
    if not util.confirm(f"Aceptar propuesta funcional {replica_id} ({ph}, {mh})?", default=False):
        return False
    data = {"id": "FAC-" + uuid.uuid4().hex[:12], "project": approval.project_id(project),
            "proposal_sha256": ph, "matrix_sha256": mh, "channel": "tty", "accepted_at": util.now_iso()}
    local, authority = _acceptance_paths(project, replica_id)
    util.atomic_write_json(local, data)
    util.atomic_write_json(authority, data)
    return True


def accepted(project: Path, replica_id: str) -> bool:
    try:
        local, authority = _acceptance_paths(project, replica_id)
        data, backup = util.read_json(local), util.read_json(authority)
        if not isinstance(data, dict) or data != backup or set(data) != set(_ACCEPTANCE_KEYS):
            return False
        if data.get("project") != approval.project_id(project) or data.get("channel") != "tty":
            return False
        folder = replica.replica_dir(project, replica_id)
        return (data.get("proposal_sha256") == _document_hash(folder / "functional-proposal.md")
                and data.get("matrix_sha256") == _document_hash(folder / "functional-matrix.json"))
    except (OSError, ValueError, TypeError, HefestoError, UnicodeError):
        return False


def _load_functional(project: Path, replica_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    folder = replica.replica_dir(project, replica_id)
    try:
        reference = util.read_json(folder / "functional-reference.json")
        inventory = util.read_json(folder / "functional-project.json")
    except (OSError, ValueError, TypeError) as exc:
        raise HefestoError("Faltan los estudios funcionales; ejecuta replica study e inventory.", EXIT_INPUT) from exc
    if not isinstance(reference, dict) or not isinstance(reference.get("features"), list):
        raise HefestoError("Estudio funcional de referencia inválido.", EXIT_INPUT)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("features"), list):
        raise HefestoError("Inventario funcional del proyecto inválido.", EXIT_INPUT)
    return reference, inventory


def propose(project: Path, replica_id: str) -> dict[str, Path]:
    folder = replica.replica_dir(project, replica_id)
    reference, inventory = _load_functional(project, replica_id)
    matrix = feature_matrix(reference, inventory)
    matrix_path, proposal_path = folder / "functional-matrix.json", folder / "functional-proposal.md"
    util.atomic_write_json(matrix_path, matrix)
    groups = (("Lo que falta", {"falta"}), ("Lo que tenemos parcial", {"parcial"}),
              ("Lo que ya tenemos mejor", {"mejor_nuestra"}))
    lines = ["# Propuesta funcional", "", "Análisis documental. No aplica código ni altera la aplicación.", ""]
    for heading, states in groups:
        lines.extend((f"## {heading}", ""))
        rows = [r for r in matrix["rows"] if r["state"] in states]
        if not rows:
            lines.extend(("- Ningún punto observado.", ""))
        for row in rows:
            candidate = row.get("project") or {}
            evidence = candidate.get("evidence", {}) if isinstance(candidate, dict) else {}
            lines.append(f"- **{row['state']}** `{row['key']}`: {row['note']} (esfuerzo M, riesgo medio; evidencia: {evidence.get('path') or evidence.get('line', 'referencia')}).")
        lines.append("")
    lines.extend(("## Mejoras sobre la referencia", ""))
    items = improvements(reference)
    if not items:
        lines.extend(("- No se detectaron debilidades observables.", ""))
    for item in items:
        lines.append(f"- `{item['key']}` — {item['title']}: {item['why']} (esfuerzo {item['effort']}, riesgo {item['risk']}).")
    lines.extend(("", "Esta propuesta es documental y no aplica código.", ""))
    util.atomic_write_text(proposal_path, "\n".join(lines))
    return {"proposal": proposal_path, "matrix": matrix_path}


def plan(project: Path, replica_id: str, gaps: list[str] | None = None) -> list[str]:
    if not accepted(project, replica_id):
        raise HefestoError("Se requiere una propuesta aceptada y sin cambios.", EXIT_BLOCKED)
    folder = replica.replica_dir(project, replica_id)
    try:
        matrix = util.read_json(folder / "functional-matrix.json")
    except (OSError, ValueError, TypeError) as exc:
        raise HefestoError("Falta una matriz funcional válida.", EXIT_INPUT) from exc
    eligible = [row for row in matrix.get("rows", []) if row.get("state") in ("falta", "parcial")]
    known = {row.get("key") for row in matrix.get("rows", [])}
    if gaps is not None:
        unknown = [gap for gap in gaps if gap not in known]
        if unknown:
            raise HefestoError("Hueco desconocido: " + ", ".join(unknown), EXIT_INPUT)
        eligible = [row for row in eligible if row.get("key") in gaps]
    path = roadmap.roadmap_path(project)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HefestoError("No existe el ROADMAP del proyecto.", EXIT_INPUT) from exc
    rm = roadmap.parse_text(source, path)
    errors = roadmap.validate(rm, project)
    if errors:
        raise HefestoError("ROADMAP inválido:\n  - " + "\n  - ".join(errors), EXIT_INPUT)
    next_task = max((int(re.search(r"\d+", t.id).group()) for t in rm.tasks), default=0) + 1
    phase_nums = [int(m.group(1)) for name in rm.phases if (m := re.match(r"Fase\s+(\d+)", name, re.I))]
    phase = max(phase_nums, default=0) + 1
    lines = [source.rstrip(), "", f"## Fase {phase} — Replica {replica_id}", ""]
    ids: list[str] = []
    for row in eligible:
        task_id = f"TASK-{next_task:03d}"
        next_task += 1
        ids.append(task_id)
        key = str(row["key"])
        project_feature = row.get("project") or {}
        evidence = project_feature.get("evidence", {}) if isinstance(project_feature, dict) else {}
        files = evidence.get("path") if isinstance(evidence, dict) else None
        if not files or not util.rel_inside(project, files):
            files = "src/"
        safe_key = shlex.quote(key)
        lines.extend((f"- [ ] {task_id}: Cerrar hueco {key}",
                      f"  - verify: `hefesto replica check {replica_id} --gap {safe_key}`",
                      f"  - files: {files}", "  - role: runner", "  - size: M", "  - parallel: false",
                      f"  - accept: Completar la funcionalidad observada: {row.get('note', '')}", ""))
    result = "\n".join(lines).rstrip() + "\n"
    candidate = roadmap.parse_text(result, path)
    errors = roadmap.validate(candidate, project)
    if errors:
        raise HefestoError("El ROADMAP generado no es válido:\n  - " + "\n  - ".join(errors), EXIT_INPUT)
    util.atomic_write_text(path, result)
    return ids


def check(project: Path, replica_id: str, gaps: list[str]) -> tuple[int, str]:
    if not gaps:
        return EXIT_INPUT, "Indica al menos un --gap."
    reference, inventory = _load_functional(project, replica_id)
    inventory = study_project(project, replica_id)
    matrix = feature_matrix(reference, inventory)
    rows = {row["key"]: row for row in matrix["rows"]}
    unknown = [gap for gap in gaps if gap not in rows]
    if unknown:
        return EXIT_INPUT, "Hueco desconocido: " + ", ".join(unknown)
    open_gaps = [gap for gap in gaps if rows[gap]["state"] not in ("tenemos", "mejor_nuestra")]
    if open_gaps:
        return 1, "Pendientes: " + "; ".join(f"{key} ({rows[key]['state']}: {rows[key]['note']})" for key in open_gaps)
    return 0, "Huecos cerrados: " + ", ".join(gaps)


def how_it_works(html: str, headers: dict | None = None, requests: list[dict] | None = None) -> dict[str, Any]:
    technologies: set[str] = set()
    for node in _DOM(html).nodes:
        tag, a = node["tag"], node["attrs"]
        if tag == "meta" and a.get("name", "").lower() == "generator":
            technologies.add(_clean(a.get("content", "")))
        if "ng-version" in a:
            technologies.add("Angular")
        if "data-reactroot" in a:
            technologies.add("React")
        if a.get("id") == "__NEXT_DATA__" or (tag == "script" and "/_next/" in a.get("src", "")):
            technologies.add("Next.js")
        if tag == "script":
            src = a.get("src", "").lower()
            for marker, name in (("react", "React"), ("vue", "Vue"), ("angular", "Angular"), ("jquery", "jQuery"), ("svelte", "Svelte")):
                if marker in src:
                    technologies.add(name)
    for name, value in (headers or {}).items():
        if name.lower() in ("server", "x-powered-by"):
            technologies.add(_clean(str(value)))
    observed = []
    for request in requests or []:
        try:
            path = _route(str(request.get("url", "")))
        except ValueError:
            continue
        method = str(request.get("method", "GET")).upper()
        resource = str(request.get("type", "other")).lower()
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            method = "OTHER"
        if resource not in ("xhr", "fetch", "document", "script", "stylesheet", "image", "font", "media", "websocket", "other"):
            resource = "other"
        item = {"method": method, "path": path, "type": resource}
        if item not in observed:
            observed.append(item)
    patterns = sorted({f["kind"] for f in extract_features(html, "/") if f["kind"] in ("form", "search", "filter", "flow", "account", "state")})
    return dict(zip(SIGNAL_KEYS, (sorted(technologies - {""}), observed, patterns)))


def _navigation_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))


def study_reference(project: Path, replica_id: str, browser: Any, max_pages: int | None = None) -> dict[str, Any]:
    try:
        scope = replica.load_scope(project, replica_id)
        if not scope:
            raise HefestoError("Referencia sin alcance autorizado.", EXIT_BLOCKED)
        limit = config.load(project).get("replica", "max_pages", default=10) if max_pages is None else max_pages
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise HefestoError("max_pages debe ser un entero positivo.", EXIT_INPUT)
        base = replica.check_url(scope["url"])
        queue = deque([_navigation_url(base)])
        seen: set[str] = set()
        pages: list[str] = []
        features = []
        signals: dict[str, Any] = {"technologies": [], "requests": [], "patterns": []}
        browser.open()
        visits = 0
        while queue and visits < limit:
            target = queue.popleft()
            if target in seen:
                continue
            target = replica.check_url(target)
            if not replica.in_scope(base, target):
                continue
            seen.add(target)
            visits += 1
            final, html = browser.goto(target)
            try:
                final = replica.check_url(final)
            except HefestoError as exc:
                raise HefestoError("Redirección fuera del alcance autorizado.", EXIT_BLOCKED) from exc
            if not replica.in_scope(base, final):
                raise HefestoError("Redirección fuera del alcance autorizado.", EXIT_BLOCKED)
            seen.add(_navigation_url(final))
            route = _route(final)
            pages.append(route)
            features.extend(extract_features(html, route))
            public = how_it_works(html, browser.headers(), browser.requests())
            for key in SIGNAL_KEYS:
                for item in public[key]:
                    if item not in signals[key]:
                        signals[key].append(item)
            dom = _DOM(html)
            actions = set()
            for node in dom.nodes:
                if node["tag"] == "form" and node["attrs"].get("action"):
                    try:
                        actions.add(_navigation_url(urljoin(final, node["attrs"]["action"])))
                    except ValueError:
                        continue
            for node in dom.nodes:
                if node["tag"] != "a" or node["hidden"] or not node["attrs"].get("href") or "download" in node["attrs"]:
                    continue
                try:
                    link = _navigation_url(urljoin(final, node["attrs"]["href"]))
                    if not replica.in_scope(base, link) or link in actions or link in seen:
                        continue
                    link = replica.check_url(link)
                except (HefestoError, ValueError):
                    continue
                if link not in queue:
                    queue.append(link)
        result = {"schema_version": SCHEMA_VERSION, "id": replica_id, "side": "reference", "pages": pages,
                  "features": features, "signals": signals, "notes": [_clean(str(n)) for n in getattr(browser, "notes", [])]}
        util.atomic_write_json(replica.replica_dir(project, replica_id) / "functional-reference.json", result)
        return result
    finally:
        browser.close()


class PlaywrightBrowser:
    """Read-only DOM probes over an isolated playwright-cli session."""

    def __init__(self, project: Path, replica_id: str) -> None:
        replica.replica_dir(project, replica_id)
        self.project = project
        self.binary = shutil.which("playwright-cli")
        self.session = "hefesto-functional-" + uuid.uuid4().hex[:12]
        self.notes = ["Red y cabeceras no verificables: este adaptador de playwright-cli no expone peticiones observadas ni cabeceras."]

    def _call(self, *args: str) -> str:
        if not self.binary:
            raise HefestoError("No se encontró playwright-cli en PATH.", EXIT_INPUT)
        argv = [self.binary, f"-s={self.session}", *args]
        # Windows batch launchers interpret metacharacters even with shell=False.
        if os.name == "nt" and Path(self.binary).suffix.lower() in (".cmd", ".bat"):
            if any(any(c in arg for c in '&|<>^%!"\r\n\x00') for arg in argv):
                raise HefestoError("Argumento inseguro para un lanzador de playwright-cli.", EXIT_INPUT)
        try:
            result = subprocess.run(argv, cwd=self.project, shell=False,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HefestoError("No se pudo ejecutar playwright-cli.", EXIT_INPUT) from exc
        if result.returncode:
            raise HefestoError("playwright-cli no pudo completar la lectura.", EXIT_INPUT)
        return result.stdout

    def open(self) -> None:
        self._call("open")

    def goto(self, url: str) -> tuple[str, str]:
        self._call("goto", url)
        probe = "JSON.stringify({url: location.href, html: document.documentElement.outerHTML})"
        encoded = base64.b64encode(probe.encode("utf-8")).decode("ascii")
        raw = self._call("--raw", "eval", "function(){return eval(atob('" + encoded + "'))}")
        try:
            payload = json.loads(raw)
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload["url"], str) or not isinstance(payload["html"], str):
                raise ValueError
            return payload["url"], payload["html"]
        except (ValueError, TypeError, KeyError) as exc:
            raise HefestoError("Respuesta DOM inválida de playwright-cli.", EXIT_INPUT) from exc

    def requests(self) -> list[dict]:
        return []

    def headers(self) -> dict:
        return {}

    def close(self) -> None:
        try:
            self._call("close")
        except HefestoError:
            pass
