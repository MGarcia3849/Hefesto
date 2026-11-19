"""Análisis local de una referencia web pública autorizada.

No es un clonador: guarda únicamente extractos de texto saneados y produce una
comparación documental. La autorización humana y su respaldo independiente
siguen el límite honesto descrito por approval.human_channel().
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from hefesto import approval, gitops, guard, util, visual
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

SCHEMA_VERSION = 1
SIDES = ("reference", "project")
SECTIONS = ("pages", "components", "routes", "texts", "states")
INVENTORY_KEYS = ("schema_version", "id", "side", "source", *SECTIONS)
ITEM_KEYS = ("section", "key", "value", "status", "evidence", "reason")
ITEM_STATUSES = ("verificado", "no_verificable")
EVIDENCE_KEYS = ("capture", "selector", "sha256")
MATRIX_STATES = ("presente", "parcial", "ausente", "diferente", "no_aplica", "no_verificable")
ROW_KEYS = ("section", "key", "state", "reference", "project", "reference_sha256", "project_sha256")
MATRIX_KEYS = ("schema_version", "id", "reference", "project", "rows", "counts")
AUTH_KEYS = ("id", "project", "url", "host", "channel", "authorized_at", "sha256")
ACCEPT_KEYS = ("id", "project", "proposal_sha256", "matrix_sha256", "channel", "accepted_at")
ALLOWED_SCHEMES = ("http", "https")


def replica_dir(project: Path, replica_id: str) -> Path:
    if not isinstance(replica_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", replica_id):
        raise HefestoError("ID de réplica inválido.", EXIT_INPUT)
    return Path(project) / ".hefesto" / "replica" / replica_id


def resolve_host(host: str) -> list[str]:
    import socket
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except (OSError, ValueError):
        return []


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast)


def check_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise HefestoError("URL vacía o inválida.", EXIT_INPUT)
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
    except (ValueError, UnicodeError):
        raise HefestoError("URL pública HTTP/HTTPS inválida o con credenciales.", EXIT_INPUT) from None
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise HefestoError("El host no pertenece al alcance público.", EXIT_INPUT)
    addresses = [host] if _looks_ip(host) else resolve_host(host)
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise HefestoError("El host no resuelve exclusivamente a direcciones públicas.", EXIT_INPUT)
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, parts.fragment))


def _looks_ip(host: str) -> bool:
    if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", host, re.I):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def in_scope(authorized: str, url: str) -> bool:
    try:
        a, b = urlsplit(authorized), urlsplit(url)
        ap, bp = a.port or (443 if a.scheme == "https" else 80), b.port or (443 if b.scheme == "https" else 80)
        base = a.path.rstrip("/") or "/"
        return (a.scheme.lower(), (a.hostname or "").lower(), ap) == (b.scheme.lower(), (b.hostname or "").lower(), bp) and (base == "/" or b.path == base or b.path.startswith(base + "/"))
    except ValueError:
        return False


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _authority_path(project: Path, rid: str, name: str) -> Path:
    path = (util.hefesto_home() / "replica-authority" / (gitops.project_key(project) + "-" + rid + "-" + name)).resolve()
    root = Path(project).resolve()
    if path == root or root in path.parents:
        raise HefestoError("El respaldo independiente debe quedar fuera del proyecto.", EXIT_BLOCKED)
    return path


def _save_pair(project: Path, rid: str, name: str, data: dict[str, Any], local: Path) -> None:
    util.atomic_write_json(local, data)
    util.atomic_write_json(_authority_path(project, rid, name), data)


def _load_pair(project: Path, rid: str, name: str, local: Path) -> Any:
    try:
        a, b = util.read_json(local), util.read_json(_authority_path(project, rid, name))
        return a if isinstance(a, dict) and a == b else None
    except (OSError, ValueError, TypeError, HefestoError):
        return None


def authorize(project: Path, replica_id: str, url: str) -> dict[str, Any] | None:
    url = check_url(url)
    if not approval.human_channel():
        raise HefestoError("Autorizar una referencia exige canal humano interactivo.", EXIT_BLOCKED)
    if not util.confirm(f"Autorizar análisis público limitado a {url}?", default=False):
        return None
    project_key = approval.project_id(project)
    data: dict[str, Any] = {"id": "AUT-" + uuid.uuid4().hex[:12], "project": project_key, "url": url,
                             "host": urlsplit(url).hostname, "channel": "tty", "authorized_at": util.now_iso(), "sha256": ""}
    data["sha256"] = _digest({k: data[k] for k in ("project", "url", "host", "channel")})
    _save_pair(project, replica_id, "scope", data, replica_dir(project, replica_id) / "scope.json")
    return data


def load_scope(project: Path, replica_id: str) -> dict[str, Any] | None:
    data = _load_pair(project, replica_id, "scope", replica_dir(project, replica_id) / "scope.json")
    if not data or set(data) != set(AUTH_KEYS) or data.get("project") != approval.project_id(project) or data.get("channel") != "tty":
        return None
    if data.get("sha256") != _digest({k: data[k] for k in ("project", "url", "host", "channel")}):
        return None
    return data


def item_hash(section: str, key: str, value: str) -> str:
    raw = json.dumps([section, key, value], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sanitize(text: str) -> str:
    value = guard.redact(str(text))
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", value)
    value = re.sub(r"(?i)(?:authorization\s*:\s*bearer\s+|bearer\s+)[^\s,;]+", "[secret]", value)
    value = re.sub(r"(?i)(?:cookie|set-cookie)\s*:\s*[^\n;]*(?:;[^\n]*)?|document\.cookie\s*=\s*['\"][^'\"]*['\"]", "[cookie]", value)
    value = re.sub(r"(?i)([?&](?:token|key|secret|password|auth|api[_-]?key)=)[^&#\s]+", r"\1[redacted]", value)
    value = re.sub(r"\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[secret]", value)
    return " ".join(value.split())


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, dict[str, str], list[str]]] = []
        self.stack: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: v or "" for k, v in attrs}
        idx = len(self.entries)
        self.entries.append((tag.lower(), attrs_map, []))
        if tag.lower() not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append(idx)

    def handle_endtag(self, tag: str) -> None:
        for pos in range(len(self.stack) - 1, -1, -1):
            if self.entries[self.stack[pos]][0] == tag.lower():
                del self.stack[pos:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack and not any(self.entries[idx][0] in ("script", "style") for idx in self.stack):
            for idx in self.stack:
                self.entries[idx][2].append(data)


def _extract(html: str, route: str, base: str | None = None) -> dict[tuple[str, str], str]:
    parser = _Elements()
    parser.feed(html)
    rows: dict[tuple[str, str], str] = {}
    title = ""
    for tag, attrs, chunks in parser.entries:
        text = sanitize(" ".join(chunks))
        if tag == "title": title = text
    rows[("pages", route)] = title
    counters: Counter[str] = Counter()
    for tag, attrs, chunks in parser.entries:
        text = sanitize(" ".join(chunks))
        if "data-component" in attrs: rows[("components", f"{route}#{sanitize(attrs['data-component'])}")] = text
        if "data-state" in attrs: rows[("states", f"{route}#{sanitize(attrs['data-state'])}")] = text
        if tag in ("h1", "h2", "h3", "button"):
            counters[tag] += 1
            rows[("texts", f"{route}#{tag}[{counters[tag]}]")] = text
        if tag == "a" and attrs.get("href"):
            href = attrs["href"]
            if base:
                target = urljoin(base, href)
                try:
                    target = check_url(target)
                except HefestoError:
                    continue
                if not in_scope(base, target): continue
                parsed = urlsplit(target)
                dest = urlunsplit(("", "", parsed.path or "/", "", ""))
            else:
                parsed = urlsplit(href)
                dest = parsed.path or ("/" if href.startswith("/") else "")
                if parsed.scheme or parsed.netloc or dest.startswith("../"): continue
            if dest: rows[("routes", dest)] = text
    return rows


def _inventory(rid: str, side: str, source: str, pages: list[tuple[str, str | None]], folder: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "id": rid, "side": side, "source": source}
    for section in SECTIONS: result[section] = []
    for route, html in pages:
        if html is None:
            result["pages"].append({"section": "pages", "key": route, "value": None, "status": "no_verificable", "evidence": None, "reason": "La página no pudo cargarse."})
            continue
        # For pages, retain the page record first; other records follow in deterministic order.
        values = _extract(html, route)
        for (section, key), value in sorted(values.items(), key=lambda x: (SECTIONS.index(x[0][0]), x[0][1])):
            if not value: value = ""
            digest = item_hash(section, key, value)
            selector = key.split("#", 1)[1] if "#" in key else "title"
            capture_rel = f"captures/{digest}.txt"
            capture_text = sanitize(f"{selector}: {value}") + "\n"
            util.atomic_write_text(folder / capture_rel, capture_text)
            item = {"section": section, "key": key, "value": value, "status": "verificado",
                    "evidence": {"capture": capture_rel, "selector": selector, "sha256": digest}, "reason": ""}
            result[section].append(item)
    for section in SECTIONS:
        result[section].sort(key=lambda x: x["key"])
    return result


def _safe_capture(project: Path, capture: str) -> bool:
    p = PurePosixPath(capture)
    return not p.is_absolute() and ".." not in p.parts and capture.startswith("captures/") and capture.endswith(".txt")


def validate_inventory(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(INVENTORY_KEYS) or data.get("schema_version") != SCHEMA_VERSION or data.get("side") not in SIDES or not isinstance(data.get("id"), str):
        raise HefestoError("Inventario inválido o versión desconocida.", EXIT_INPUT)
    for section in SECTIONS:
        if not isinstance(data[section], list): raise HefestoError("Sección de inventario inválida.", EXIT_INPUT)
        for item in data[section]:
            if not isinstance(item, dict) or tuple(item.keys()) != ITEM_KEYS or item.get("section") != section or item.get("status") not in ITEM_STATUSES:
                raise HefestoError("Elemento de inventario inválido.", EXIT_INPUT)
            if item["status"] == "no_verificable":
                if item.get("value") is not None or item.get("evidence") is not None or not item.get("reason"):
                    raise HefestoError("Elemento no verificable con datos inventados.", EXIT_INPUT)
            else:
                ev = item.get("evidence")
                if not isinstance(item.get("value"), str) or not isinstance(ev, dict) or tuple(ev.keys()) != EVIDENCE_KEYS or not _safe_capture(Path(), ev.get("capture", "")) or not ev.get("selector") or ev.get("sha256") != item_hash(section, item["key"], item["value"]):
                    raise HefestoError("Evidencia de inventario inválida.", EXIT_INPUT)
    return data


def _save_inventory(project: Path, rid: str, data: dict[str, Any], filename: str) -> dict[str, Any]:
    util.atomic_write_json(replica_dir(project, rid) / filename, data)
    return data


def capture_reference(project: Path, replica_id: str, routes: tuple[str, ...] = ()) -> dict[str, Any]:
    folder = replica_dir(project, replica_id)
    scope = load_scope(project, replica_id)
    if not scope: raise HefestoError("Referencia sin alcance autorizado.", EXIT_BLOCKED)
    urls = [scope["url"]]
    for route in routes:
        target = check_url(urljoin(scope["url"], route))
        if not in_scope(scope["url"], target): raise HefestoError("Ruta fuera del alcance autorizado.", EXIT_INPUT)
        urls.append(target)
    binary = shutil.which("playwright-cli")
    if not binary: raise HefestoError("No se encontró playwright-cli en PATH.", EXIT_INPUT)
    session = f"hefesto-replica-{replica_id}"
    pages: list[tuple[str, str | None]] = []
    opened = False
    try:
        def call(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([binary, f"-s={session}", *args], cwd=project, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        opened = call("open").returncode == 0
        if not opened: raise HefestoError("playwright-cli no pudo abrir una sesión.", EXIT_INPUT)
        for target in urls:
            goto = call("goto", target)
            if goto.returncode:
                pages.append((urlsplit(target).path or "/", None)); continue
            probe = "JSON.stringify({url: location.href, html: document.documentElement.outerHTML})"
            result = call("--raw", "eval", probe)
            try:
                payload = json.loads(result.stdout)
                try:
                    final = check_url(payload["url"])
                except HefestoError as exc:
                    raise HefestoError(f"Una redirección salió del alcance autorizado: {exc}", EXIT_BLOCKED) from exc
                if not in_scope(scope["url"], final): raise HefestoError("Una redirección salió del alcance autorizado.", EXIT_BLOCKED)
                pages.append((urlsplit(final).path or "/", str(payload["html"])))
            except HefestoError: raise
            except (ValueError, KeyError, TypeError):
                pages.append((urlsplit(target).path or "/", None))
    finally:
        if opened:
            try: subprocess.run([binary, f"-s={session}", "close"], cwd=project, shell=False, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError): pass
    folder.mkdir(parents=True, exist_ok=True)
    inventory = _inventory(replica_id, "reference", scope["url"], pages, folder)
    return _save_inventory(project, replica_id, inventory, "reference.json")


def _relative_page(project: Path, raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise HefestoError("Ruta de página inválida.", EXIT_INPUT)
    posix = PurePosixPath(raw)
    root = Path(project).resolve()
    candidate = (root / Path(*posix.parts)).resolve()
    if posix.is_absolute() or any(part in (".", "..") for part in posix.parts) or not candidate.is_relative_to(root):
        raise HefestoError("La página debe estar dentro del proyecto.", EXIT_INPUT)
    return posix.as_posix()


def inventory_project(project: Path, replica_id: str, pages: dict[str, str]) -> dict[str, Any]:
    folder = replica_dir(project, replica_id); folder.mkdir(parents=True, exist_ok=True)
    loaded: list[tuple[str, str | None]] = []
    for route, raw in pages.items():
        rel = _relative_page(project, raw)
        path = Path(project) / Path(*PurePosixPath(rel).parts)
        try: html = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError): html = None
        loaded.append((route, html))
    data = _inventory(replica_id, "project", "project", loaded, folder)
    return _save_inventory(project, replica_id, data, "project.json")


def compare(reference: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    a, b = validate_inventory(reference), validate_inventory(project)
    if a["side"] != "reference" or b["side"] != "project" or a["id"] != b["id"]: raise HefestoError("Inventarios incompatibles.", EXIT_INPUT)
    aa = {(i["section"], i["key"]): i for s in SECTIONS for i in a[s]}; bb = {(i["section"], i["key"]): i for s in SECTIONS for i in b[s]}
    counts = {state: 0 for state in MATRIX_STATES}; rows = []
    for section, key in sorted(aa.keys() | bb.keys(), key=lambda x: (SECTIONS.index(x[0]), x[1])):
        x, y = aa.get((section, key)), bb.get((section, key))
        xv, yv = (x or {}).get("value"), (y or {}).get("value")
        if (x and x["status"] == "no_verificable") or (y and y["status"] == "no_verificable"): state = "no_verificable"
        elif x is None: state = "no_aplica"
        elif y is None: state = "ausente"
        else:
            nx, ny = " ".join(xv.lower().split()), " ".join(yv.lower().split())
            state = "presente" if nx == ny else "parcial" if nx in ny or ny in nx else "diferente"
        counts[state] += 1
        rows.append({"section": section, "key": key, "state": state, "reference": xv, "project": yv,
                     "reference_sha256": (x or {}).get("evidence", {}).get("sha256") if x and x.get("evidence") else None,
                     "project_sha256": (y or {}).get("evidence", {}).get("sha256") if y and y.get("evidence") else None})
    return {"schema_version": SCHEMA_VERSION, "id": a["id"], "reference": a["source"], "project": b["source"], "rows": rows, "counts": counts}


def matrix_json(matrix: dict[str, Any]) -> str:
    return json.dumps(matrix, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def propose(project: Path, replica_id: str) -> dict[str, Path]:
    folder = replica_dir(project, replica_id)
    try:
        ref = validate_inventory(util.read_json(folder / "reference.json")); own = validate_inventory(util.read_json(folder / "project.json"))
        matrix = compare(ref, own)
    except (TypeError, AttributeError) as exc:
        raise HefestoError("Faltan inventarios válidos para proponer.", EXIT_INPUT) from exc
    matrix_text = matrix_json(matrix)
    lines = ["# Propuesta de análisis", "", f"Referencia autorizada: {sanitize(matrix['reference'])}", "", "## Diferencias", ""]
    for row in matrix["rows"]:
        if row["state"] in ("ausente", "parcial", "diferente"):
            lines.append(f"- **{row['state']}** · {row['section']} · `{sanitize(row['key'])}` (referencia: {sanitize(row['reference'] or '')}; proyecto: {sanitize(row['project'] or '')})")
    lines += ["", "Esta propuesta es documental, queda **sin aplicar** y no escribe código.", "Para aceptar el análisis: `hefesto replica accept`.", ""]
    proposal_text = "\n".join(lines)
    matrix_path, proposal_path = folder / "matrix.json", folder / "proposal.md"
    util.atomic_write_text(matrix_path, matrix_text); util.atomic_write_text(proposal_path, proposal_text)
    return {"proposal": proposal_path, "matrix": matrix_path}


def accept(project: Path, replica_id: str) -> dict[str, Any] | None:
    if not approval.human_channel(): raise HefestoError("Aceptar exige canal humano interactivo.", EXIT_BLOCKED)
    folder = replica_dir(project, replica_id); proposal = folder / "proposal.md"; matrix = folder / "matrix.json"
    try: ph, mh = _document_hash(proposal), _document_hash(matrix)
    except OSError as exc: raise HefestoError("Falta la propuesta o la matriz.", EXIT_INPUT) from exc
    if not util.confirm(f"Aceptar análisis {replica_id}: propuesta {ph}, matriz {mh}?", default=False): return None
    data = {"id": "ACC-" + uuid.uuid4().hex[:12], "project": approval.project_id(project), "proposal_sha256": ph,
            "matrix_sha256": mh, "channel": "tty", "accepted_at": util.now_iso()}
    _save_pair(project, replica_id, "acceptance", data, folder / "acceptance.json")
    return data


def accepted(project: Path, replica_id: str) -> bool:
    folder = replica_dir(project, replica_id)
    data = _load_pair(project, replica_id, "acceptance", folder / "acceptance.json")
    if not data or set(data) != set(ACCEPT_KEYS) or data.get("project") != approval.project_id(project): return False
    try:
        proposal_hash = _document_hash(folder / "proposal.md")
        matrix_hash = _document_hash(folder / "matrix.json")
        return data["proposal_sha256"] == proposal_hash and data["matrix_sha256"] == matrix_hash
    except OSError: return False


def _document_hash(path: Path) -> str:
    # Los finales CRLF y LF representan el mismo documento tras una edición local.
    text = path.read_text(encoding="utf-8")
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()
