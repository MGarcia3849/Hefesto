"""Informe local de predespliegue. Sus indicios no certifican cumplimiento."""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from hefesto import security
from hefesto.util import EXIT_INPUT, HefestoError

SCHEMA_VERSION = 1
CONTROLS = ("costs", "accessibility", "data-access", "communications", "abuse")
STATUSES = ("confirmed", "suspected", "not_tested", "not_applicable", "passed")
STATUS_FROM_SECURITY = {"confirmado": "confirmed", "candidato": "suspected",
                       "descartado": "discarded", "aceptado": "accepted"}
VERDICTS = ("PASS", "REVIEW_REQUIRED", "BLOCKED")
PROFILES = ("web-general", "public-sector-sensitive", "auto")
FINDING_KEYS = ("id", "control", "severity", "status", "title", "evidence", "recommendation")
REPORT_KEYS = ("schema_version", "profile", "stack", "controls", "findings", "verdict", "notes")
MANUAL_CHECKS = "Checklist manual de accesibilidad: teclado, foco visible, nombres accesibles, etiquetas, contraste y zoom."
_SKIP = {".git", ".hefesto", "node_modules", "tests", "__pycache__", ".venv", "venv"}
_PAYMENT = re.compile(r"openai|anthropic|@google/generative-ai|stripe|twilio|@sendgrid|google.?maps|mapbox|firebase.?functions", re.I)
_POLL = re.compile(r"setInterval\s*\([^\n]*?,\s*(\d+)|poll(?:ing)?[^\n]{0,80}?(\d+)\s*ms", re.I)
_NETWORK = re.compile(r"\b(fetch|requests\.(?:get|post|put|delete)|axios\.|httpx\.|urllib\.|\$\.ajax|\.send\s*\()", re.I)
_RETRY = re.compile(r"\b(?:retry|retries|attempts?)\b", re.I)


def _finding(fid: str, control: str, severity: str, status: str, title: str,
             path: str, line: int, excerpt: str, recommendation: str) -> dict[str, Any]:
    return {"id": fid, "control": control, "severity": severity, "status": status,
            "title": title, "evidence": {"path": path, "line": line,
            "excerpt": security.redact(excerpt)[:160]}, "recommendation": recommendation}


def _files(project: Path):
    for current, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink() or path.stat().st_size > security.MAX_FILE_BYTES:
                continue
            try:
                raw = path.read_bytes()
                if b"\0" in raw:
                    continue
                yield path.relative_to(project).as_posix(), raw.decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                continue


class _A11yParser(HTMLParser):
    """Comprobaciones estáticas mínimas; no sustituyen a axe-core ni a la revisión manual."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[tuple[int, str]] = []
        self.labelled: set[str] = set()
        self.inputs: list[tuple[int, dict[str, str]]] = []
        self.button: tuple[int, dict[str, str], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "html" and not a.get("lang", "").strip():
            self.problems.append((line, "La página no declara el idioma (lang)."))
        elif tag == "img" and "alt" not in a and a.get("role") != "presentation":
            self.problems.append((line, "Imagen sin texto alternativo (alt)."))
        elif tag == "label" and a.get("for"):
            self.labelled.add(a["for"])
        elif tag in ("input", "select", "textarea") and a.get("type", "") not in ("hidden", "submit", "button", "reset"):
            self.inputs.append((line, a))
        elif tag == "button":
            self.button = (line, a, [])

    def handle_data(self, data: str) -> None:
        if self.button is not None:
            self.button[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self.button is not None:
            line, a, text = self.button
            if not "".join(text).strip() and not (a.get("aria-label") or a.get("title")):
                self.problems.append((line, "Botón sin nombre accesible."))
            self.button = None

    def finish(self) -> list[tuple[int, str]]:
        for line, a in self.inputs:
            if not (a.get("aria-label") or a.get("aria-labelledby") or a.get("title") or a.get("id") in self.labelled):
                self.problems.append((line, "Campo de formulario sin etiqueta."))
        return sorted(self.problems)


def _static_a11y(files: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, lines in files:
        if not path.lower().endswith((".html", ".htm")):
            continue
        parser = _A11yParser()
        try:
            parser.feed("\n".join(lines))
            parser.close()
        except Exception:  # HTML imposible de analizar: lo dirá la revisión manual
            continue
        for number, problem in parser.finish():
            out.append(_finding(f"a11y-static-{path}-{number}", "accessibility", "medium", "suspected", problem,
                                path, number, lines[number - 1] if 0 < number <= len(lines) else "",
                                "Corregir y comprobar con teclado y tecnología asistiva."))
    return out


def run(project: Path, profile: str = "auto", emulator: Callable | None = None,
        a11y_runner: Callable | None = None) -> dict[str, Any]:
    project = Path(project)
    if profile not in PROFILES:
        raise HefestoError("Perfil predespliegue desconocido.", EXIT_INPUT)
    files = list(_files(project))
    rules_exist = (project / security.FIRESTORE_RULES).is_file()
    data_detected = rules_exist or any(re.search(r"firestore|firebase|supabase|prisma|sequelize|mongoose|sqlalchemy|database|\.sql$", p, re.I) for p, _ in files)
    base_report = security.scan(project, emulator=emulator)
    findings: list[dict[str, Any]] = []
    accepted_ids = {item["finding"] for item in security.exceptions(project)}
    for item in base_report["findings"]:
        translated = STATUS_FROM_SECURITY[item["status"]]
        if translated in {"discarded", "accepted"} or item["id"] in accepted_ids:
            continue
        ev = item["evidence"]
        path = ev.get("path", "") if isinstance(ev, dict) else ""
        line = ev.get("line", 0) if isinstance(ev, dict) else 0
        excerpt = ev.get("excerpt", "") if isinstance(ev, dict) else ""
        # Reglas abiertas, accesos y credenciales expuestas comprometen los datos; el resto queda en abuso.
        to_data = item["rule"].startswith("firestore-") or item["rule"] == "secret-in-code"
        findings.append(_finding(item["id"], "data-access" if to_data else "abuse",
                                 item["severity"], translated, item["title"], path, line, excerpt,
                                 "Revisar el hallazgo con el equipo responsable."))

    static: dict[str, list[dict[str, Any]]] = {key: [] for key in ("costs", "abuse", "communications")}
    for path, lines in files:
        lower = path.lower()
        marketing = "sendgrid" in "\n".join(lines).lower() or "newsletter" in lower or "campaign" in lower
        for number, line in enumerate(lines, 1):
            if _PAYMENT.search(line):
                target = "communications" if re.search(r"sendgrid|twilio", line, re.I) else "costs"
                static[target].append(_finding(f"static-{target}-{path}-{number}", target, "medium", "suspected",
                    "Integración comercial o de pago detectada", path, number, line,
                    "Revisar costes, consentimiento y límites de uso."))
            poll = _POLL.search(line)
            if poll and int(next(v for v in poll.groups() if v)) < 5000:
                static["costs"].append(_finding(f"poll-{path}-{number}", "costs", "medium", "suspected",
                    "Polling frecuente detectado", path, number, line, "Aumentar intervalo y aplicar backoff."))
            if re.search(r"while\s*(?:True|true)\s*:", line) and any(_NETWORK.search(v) for v in lines[number-1:number+15]):
                static["abuse"].append(_finding(f"loop-{path}-{number}", "abuse", "high", "suspected",
                    "Bucle de red potencialmente ilimitado", path, number, " ".join(lines[number-1:number+4]),
                    "Añadir límite de intentos, espera y salida condicionada."))
            elif _RETRY.search(line) and not re.search(r"(?:max[_ ]?retries|attempts\s*[<>=]|break|return)", line, re.I):
                if _NETWORK.search(" ".join(lines[max(0, number-3):number+2])):
                    static["abuse"].append(_finding(f"retry-{path}-{number}", "abuse", "high", "suspected",
                        "Reintento de red sin límite visible", path, number, line, "Limitar reintentos y añadir backoff."))
    findings.extend(f for group in static.values() for f in group)

    controls: dict[str, dict[str, str]] = {}
    cost_findings = static["costs"]
    controls["costs"] = {"id": "costs", "status": "suspected" if cost_findings else "passed",
                          "reason": "Integraciones o polling requieren revisión." if cost_findings else "Sin patrones de coste detectados."}
    accessibility = []
    runner_failed = False
    if a11y_runner:
        try:
            for i, result in enumerate(a11y_runner(project), 1):
                if isinstance(result, dict):
                    rule = str(result.get("rule", "a11y"))
                    accessibility.append(_finding(f"a11y-{i}", "accessibility", "medium", "suspected",
                        "Hallazgo de accesibilidad: " + rule, str(result.get("target", "")), i,
                        json.dumps(result, ensure_ascii=False), "Corregir y verificar manualmente."))
        except Exception:  # un analizador que falla no puede contar como superado
            runner_failed = True
    accessibility += _static_a11y(files)
    findings.extend(accessibility)
    ran = bool(a11y_runner) and not runner_failed
    controls["accessibility"] = {"id": "accessibility", "status": "suspected" if accessibility else "passed" if ran else "not_tested",
        "reason": "Runner automático ejecutado." if ran else
                  "El analizador automático falló." if runner_failed else "No se ejecutó axe-core con Playwright."}
    data_findings = [f for f in findings if f["control"] == "data-access"]
    if any(f["status"] == "confirmed" and f["severity"] in {"critical", "high"} for f in data_findings):
        data_status, reason = "confirmed", "Hay una regla abierta, un acceso no autorizado o credenciales expuestas confirmadas."
    elif not data_detected:
        data_status, reason = "not_applicable", "No se detecta una capa de datos ni firestore.rules."
    elif not rules_exist:
        data_status, reason = "not_tested", "Hay indicios de capa de datos, pero no reglas Firestore para probar."
    elif emulator is None:
        data_status, reason = "not_tested", "Hay reglas Firestore, pero no se proporcionó emulador."
    else:
        data_status, reason = "passed", "El emulador denegó los casos de acceso no autorizado."
    controls["data-access"] = {"id": "data-access", "status": data_status, "reason": reason}
    comms = static["communications"]
    controls["communications"] = {"id": "communications", "status": "suspected" if comms else "not_applicable",
        "reason": "Integración de envío detectada; revisar consentimiento y baja." if comms else "No se detectan librerías ni envíos de marketing."}
    abuse = static["abuse"]
    controls["abuse"] = {"id": "abuse", "status": "suspected" if abuse else "passed",
        "reason": "Patrones de reintento o bucle requieren revisión." if abuse else "Sin patrones de abuso detectados."}
    notes = [MANUAL_CHECKS]
    has_block = any(f["status"] == "confirmed" and f["severity"] in {"critical", "high"} for f in findings)
    unresolved = any(c["status"] in {"suspected", "not_tested"} for c in controls.values()) or any(f["status"] == "suspected" for f in findings)
    verdict = "BLOCKED" if has_block else "REVIEW_REQUIRED" if unresolved else "PASS"
    stack = sorted({Path(path).suffix.lstrip(".") for path, _ in files if Path(path).suffix})
    return {"schema_version": SCHEMA_VERSION, "profile": profile, "stack": stack,
            "controls": [controls[c] for c in CONTROLS], "findings": [
                {key: finding[key] for key in FINDING_KEYS} for finding in findings],
            "verdict": verdict, "notes": notes}


def write_report(report: dict[str, Any], path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Informe predespliegue", "", f"**Veredicto: {report['verdict']}**", "",
             "Este informe aporta indicios y no certifica cumplimiento.", "", "## Controles", ""]
    lines += [f"- **{c['id']}**: {c['status']} — {security.redact(c['reason'])}" for c in report["controls"]]
    if report["findings"]:
        lines += ["", "## Hallazgos", ""]
        lines += [f"- {f['severity']} / {f['status']}: {security.redact(f['title'])} ({f['evidence']})" for f in report["findings"]]
    lines += ["", *report["notes"]]
    return "\n".join(lines) + "\n"


def gate(project: Path, cwd: Path) -> dict[str, Any] | None:
    """Puerta de `deploy` tras el gate de seguridad, según `security.predeploy` (warn | block | off)."""
    from hefesto import config
    from hefesto.util import EXIT_BLOCKED

    mode = str(config.load(project).get("security", "predeploy", default="warn"))
    if mode == "off":
        return None
    report = run(Path(cwd))
    verdict = report.get("verdict")
    print(f"Predespliegue: {verdict} (modo {mode}). Indicios, no certificación de cumplimiento.")
    if mode == "block" and verdict == "BLOCKED":
        raise HefestoError("Predespliegue BLOCKED: revisa `hefesto security predeploy` antes de desplegar.",
                           EXIT_BLOCKED)
    return report
