"""Escaneo local, pasivo y reproducible de señales de seguridad."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hefesto import approval, config, gitops, guard, util
from hefesto.util import EXIT_INPUT, HefestoError

SCHEMA_VERSION = 1
SEVERITIES = ("critical", "high", "medium", "low", "info")
BLOCKING_SEVERITIES = ("critical", "high")
STATUSES = ("candidato", "confirmado", "descartado", "aceptado")
FINDING_KEYS = ("id", "rule", "severity", "status", "in_scope", "title", "evidence", "reason")
EVIDENCE_KEYS = ("path", "line", "rule", "excerpt")
NOT_VERIFIABLE = "no verificable"
REPORT_KEYS = ("schema_version", "profile", "scope", "controls", "adapters", "findings", "counts", "legal")
LEGAL_STATUS = "NO_VERIFICADO"
BASELINE_RULES = ("secret-in-code", "firestore-open-rule", "firestore-access")
RULES = (*BASELINE_RULES, "dangerous-eval", "debug-enabled", "personal-data")
CONTROLS = ("env-ignored", "security-policy", "lockfile", "firestore-rules", "firestore-rules-tested")
CONTROL_KEYS = ("id", "status", "evidence")
CONTROL_STATUSES = ("presente", "ausente", "no_verificable")
EVALUATED_ADAPTERS = ("gitleaks",)
ADAPTER_STATUSES = ("ejecutado", "ausente", "no_evaluado", "error")
PROFILE_KEYS = ("name", "description", "rules", "adapters", "synthetic_only", "collections", "frameworks")
RULE_CASES = ("anon-read", "anon-write", "other-user-read")
REQUEST_KEYS = ("case", "collection", "document", "auth", "method", "data")
FIRESTORE_RULES = "firestore.rules"
GATE_STAGES = ("done", "finish", "deploy")
EXCEPTION_KEYS = ("id", "finding", "project", "person", "reason", "expires", "channel", "created_at", "sha256")
MAX_EXCEPTION_DAYS = 90
MAX_FILE_BYTES = 1024 * 1024


def report_path(project: Path) -> Path:
    return Path(project) / ".hefesto" / "security" / "report.json"


def exceptions_path(project: Path) -> Path:
    return Path(project) / ".hefesto" / "security" / "exceptions.json"


def redact(text: str) -> str:
    value = guard.redact(str(text))
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED]", value)
    value = re.sub(r"\b(?:[0-9]{8}[A-Z]|[XYZ][0-9]{7}[A-Z])\b", "[REDACTED]", value, flags=re.I)
    value = re.sub(r"\+34[\s.-]?(?:\d[\s.-]?){9}", "[REDACTED]", value)
    return value


def finding_id(rule: str, path: str, excerpt: str, occurrence: int = 1) -> str:
    payload = json.dumps([rule, path, excerpt, occurrence], ensure_ascii=False, separators=(",", ":"))
    return "SEC-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def profiles(project: Path) -> dict[str, dict[str, Any]]:
    cfg = _security_config(project)
    configured = cfg.get("security", "profiles", default={})
    defaults = {
        "default": {"name": "default", "description": "Reglas generales de seguridad", "rules": ["dangerous-eval", "debug-enabled"], "adapters": [], "synthetic_only": False, "collections": ["documentos"], "frameworks": []},
        "centuria-policial": {"name": "centuria-policial", "description": "Perfil policial con datos sintéticos", "rules": ["dangerous-eval", "debug-enabled", "personal-data"], "adapters": [], "synthetic_only": True, "collections": ["agentes", "avisos", "partes"], "frameworks": ["ENS", "RGPD", "LOPDGDD"]},
    }
    for name, values in configured.items() if isinstance(configured, dict) else ():
        if name not in defaults or not isinstance(values, dict):
            continue
        defaults[name].update(values)
    return defaults


def load_profile(project: Path, name: str | None = None) -> dict[str, Any]:
    cfg = _security_config(project)
    selected = name or cfg.get("security", "profile", default="default")
    available = profiles(project)
    if selected not in available:
        raise HefestoError(f"Perfil de seguridad desconocido: {selected}", EXIT_INPUT)
    profile = dict(available[selected])
    profile["rules"] = list(dict.fromkeys([*BASELINE_RULES, *profile.get("rules", [])]))
    profile["adapters"] = list(profile.get("adapters", []))
    return profile


def _security_config(project: Path) -> config.Config:
    # El escaneo también se ejecuta con destinos argv de delivery en settings.
    from hefesto.delivery import _delivery_config

    return _delivery_config(project)


def _synthetic_text(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_synthetic_text(k) and _synthetic_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_synthetic_text(v) for v in value)
    if not isinstance(value, str):
        return True
    if "+34" in value or _valid_identity(value):
        return False
    for email in re.findall(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", value):
        domain = email.rsplit("@", 1)[1].lower()
        if not (domain in {"example.com", "example.net", "example.org", "invalid", "test"}
                or domain.endswith((".example", ".invalid", ".test"))):
            return False
    return True


def _valid_identity(value: str) -> bool:
    letters = "TRWAGMYFPDXBNJZSQVHLCKE"
    dni = re.search(r"\b(\d{8})([A-Z])\b", value, re.I)
    if dni and letters[int(dni.group(1)) % 23] == dni.group(2).upper():
        return True
    nie = re.search(r"\b([XYZ])(\d{7})([A-Z])\b", value, re.I)
    if nie:
        number = str({"X": 0, "Y": 1, "Z": 2}[nie.group(1).upper()]) + nie.group(2)
        return letters[int(number) % 23] == nie.group(3).upper()
    return False


def synthetic_fixtures(profile: dict[str, Any]) -> dict[str, Any]:
    users = [{"uid": "sintetico-1", "email": "agente1@centuria.invalid", "name": "Agente Uno"},
             {"uid": "sintetico-2", "email": "agente2@centuria.invalid", "name": "Agente Dos"}]
    docs = {collection: [{"id": f"registro-{i}", "owner": users[(i - 1) % 2]["uid"]} for i in (1, 2)]
            for collection in profile.get("collections", [])}
    return {"synthetic": True, "users": users, "documents": docs}


def is_synthetic(fixtures: Any) -> bool:
    return isinstance(fixtures, dict) and fixtures.get("synthetic") is True and _synthetic_text(fixtures)


def _git(project: Path, workdir: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=workdir, text=True, encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise HefestoError("No se pudo consultar el alcance de seguridad.", util.EXIT_BLOCKED) from exc
    if result.returncode:
        raise HefestoError("No se pudo consultar el diff para el escaneo de seguridad.", util.EXIT_BLOCKED)
    return result.stdout


def _changed_paths(project: Path, workdir: Path, base: str) -> list[str]:
    raw = _git(project, workdir, "diff", "--name-only", "-z", base, "--")
    paths = {p.replace("\\", "/") for p in raw.split("\0") if p and not p.startswith((".git/", ".hefesto/"))}
    untracked = _git(project, workdir, "ls-files", "--others", "--exclude-standard", "-z")
    paths.update(p.replace("\\", "/") for p in untracked.split("\0") if p and not p.startswith((".git/", ".hefesto/")))
    return sorted(paths)


def _evidence(path: str, line: int, rule: str, text: str) -> dict[str, Any]:
    excerpt = " ".join(text.split())
    excerpt = redact(excerpt)[:160]
    return {"path": path, "line": line, "rule": rule, "excerpt": excerpt}


def _finding(rule: str, severity: str, status: str, path: str | None, line: int | None,
             excerpt: str | None, title: str, reason: str = "") -> dict[str, Any]:
    evidence: Any = _evidence(path, line, rule, excerpt) if path is not None and line is not None and excerpt is not None else NOT_VERIFIABLE
    identity_path = path or ""
    identity_excerpt = evidence["excerpt"] if isinstance(evidence, dict) else ""
    return {"id": finding_id(rule, identity_path, identity_excerpt), "rule": rule, "severity": severity,
            "status": status, "in_scope": True, "title": title, "evidence": evidence,
            "reason": redact(reason) if reason else ("Evidencia no disponible." if evidence == NOT_VERIFIABLE else "")}


def _walk_files(root: Path):
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".hefesto"} and not (Path(current) / d).is_symlink())
        for filename in sorted(files):
            path = Path(current) / filename
            if path.is_symlink():
                continue
            yield path


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError):
        return None


def _rules_file(root: Path) -> str | None:
    path = root / FIRESTORE_RULES
    return _read_text(path) if path.is_file() else None


def _rule_line(root: Path, collection: str) -> int:
    text = _rules_file(root) or ""
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(rf"\bmatch\s+/{re.escape(collection)}(?:/|\s|\{{)", line):
            return number
    return 1


def rules_test(project: Path, emulator: Callable[[str, dict[str, Any]], bool] | None, fixtures: Any = None,
               profile: dict[str, Any] | None = None, workdir: Path | None = None) -> list[dict[str, Any]]:
    root = Path(workdir or project)
    selected = profile or load_profile(project)
    if fixtures is None:
        fixtures = synthetic_fixtures(selected)
    if selected.get("synthetic_only") and not is_synthetic(fixtures):
        raise HefestoError("El perfil requiere datos sintéticos.", EXIT_INPUT)
    rules = _rules_file(root)
    if rules is None or emulator is None:
        return []
    findings = []
    for collection, documents in fixtures.get("documents", {}).items():
        if not documents:
            continue
        documents = documents[:1]
        for case in RULE_CASES:
            for document in documents:
                owner = document["owner"]
                other = next((u["uid"] for u in fixtures["users"] if u["uid"] != owner), "usuario-ajeno")
                auth = None if case.startswith("anon-") else other
                request = {"case": case, "collection": collection, "document": document["id"], "auth": auth,
                           "method": "create" if case == "anon-write" else "get", "data": document}
                try:
                    allowed = emulator(rules, request)
                    status = "confirmado" if allowed else "descartado"
                except Exception:
                    status = "candidato"
                severity = "critical" if case == "anon-write" else "high"
                excerpt = f"{case} {collection}"
                if status == "candidato":
                    finding = _finding("firestore-access", severity, status, None, None, None,
                                       "Acceso Firestore no verificable", "El emulador no pudo verificar el acceso.")
                else:
                    finding = _finding("firestore-access", severity, status, FIRESTORE_RULES,
                                       _rule_line(root, collection), excerpt, "Acceso Firestore permitido")
                findings.append(finding)
    return findings


def controls(project: Path, workdir: Path | None = None, emulator: Callable | None = None) -> list[dict[str, Any]]:
    root = Path(workdir or project)
    ignore_path = root / ".gitignore"
    ignore = _read_text(ignore_path) or ""
    env_ignored = any(line.strip() in {".env", ".env*", ".env.*"} for line in ignore.splitlines())
    policy = next((p for p in ("SECURITY.md", ".github/SECURITY.md") if (root / p).is_file()), None)
    manifests = ("package.json", "pyproject.toml", "requirements.txt", "Pipfile", "Cargo.toml", "go.mod", "pubspec.yaml")
    lockfiles = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "uv.lock", "pubspec.lock")
    has_manifest = any((root / p).is_file() for p in manifests)
    lock = next((p for p in lockfiles if (root / p).is_file()), None)
    rules_exist = (root / FIRESTORE_RULES).is_file()
    checks = [("env-ignored", "presente" if env_ignored else "ausente", ".gitignore" if env_ignored else None),
              ("security-policy", "presente" if policy else "ausente", policy),
              ("lockfile", "presente" if lock else "ausente" if has_manifest else "no_verificable", lock),
              ("firestore-rules", "presente" if rules_exist else "ausente", FIRESTORE_RULES if rules_exist else None),
              ("firestore-rules-tested", "presente" if rules_exist and emulator else "no_verificable", FIRESTORE_RULES if rules_exist and emulator else None)]
    return [{"id": key, "status": status, "evidence": evidence} for key, status, evidence in checks]


def _secret_match(line: str) -> bool:
    return bool(guard.find_secrets(line))


def scan(project: Path, profile: str | None = None, workdir: Path | None = None, base: str | None = None,
         emulator: Callable[[str, dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    project, root = Path(project), Path(workdir or project)
    selected = load_profile(project, profile)
    changed = _changed_paths(project, root, base) if base else None
    in_scope_paths = set(changed) if changed is not None else None
    found: list[dict[str, Any]] = []
    occurrences: dict[tuple[str, str, str], int] = {}
    for file in _walk_files(root):
        rel = file.relative_to(root).as_posix()
        text = _read_text(file)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            checks = []
            if _secret_match(line):
                checks.append(("secret-in-code", "critical", "confirmado", "Posible secreto en el código"))
            if file.suffix == ".rules" and re.search(r"\ballow\s+[\w, ]+\s*(?::\s*if\s+true)?\s*;", line):
                checks.append(("firestore-open-rule", "critical", "confirmado", "Regla Firestore abierta"))
            if "dangerous-eval" in selected["rules"] and file.suffix.lower() in {".py", ".js", ".ts"} and re.search(r"\beval\s*\(", line):
                checks.append(("dangerous-eval", "medium", "candidato", "Uso de eval"))
            if "debug-enabled" in selected["rules"] and file.suffix.lower() not in {".md", ".rst", ".txt"} and re.search(r"\bDEBUG\s*=\s*True\b|\bdebug\s*:\s*true\b", line, re.I):
                checks.append(("debug-enabled", "low", "candidato", "Depuración habilitada"))
            if "personal-data" in selected["rules"] and (_valid_identity(line) or
                    (re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", line) and not _synthetic_text(line))):
                checks.append(("personal-data", "high", "candidato", "Posible dato personal"))
            for rule, severity, status, title in checks:
                excerpt = " ".join(line.split())
                key = (rule, rel, redact(excerpt))
                occurrences[key] = occurrences.get(key, 0) + 1
                finding = _finding(rule, severity, status, rel, number, excerpt, title)
                finding["id"] = finding_id(rule, rel, finding["evidence"]["excerpt"], occurrences[key])
                finding["in_scope"] = in_scope_paths is None or rel in in_scope_paths
                found.append(finding)
    found.extend(rules_test(project, emulator, profile=selected, workdir=root))
    for item in found:
        item["in_scope"] = in_scope_paths is None or (item["evidence"].get("path") in in_scope_paths if isinstance(item["evidence"], dict) else False)
    adapters = _run_adapters(root, selected, found)
    order = {value: index for index, value in enumerate(SEVERITIES)}
    found.sort(key=lambda f: (order[f["severity"]], f["evidence"].get("path", "") if isinstance(f["evidence"], dict) else "",
                              f["evidence"].get("line", 0) if isinstance(f["evidence"], dict) else 0, f["rule"], f["id"]))
    counts = {severity: sum(f["severity"] == severity for f in found) for severity in SEVERITIES}
    frameworks = selected.get("frameworks", [])
    legal = {"frameworks": list(frameworks), "status": LEGAL_STATUS,
             "note": "No sustituye una revisión especializada."} if frameworks else None
    return {"schema_version": SCHEMA_VERSION, "profile": selected["name"],
            "scope": {"base": base, "paths": changed} if base else {"base": None, "paths": None},
            "controls": controls(project, root, emulator), "adapters": adapters, "findings": found,
            "counts": counts, "legal": legal}


def _run_adapters(root: Path, profile: dict[str, Any], findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    results = []
    for name in sorted(set(profile.get("adapters", []))):
        if name not in EVALUATED_ADAPTERS:
            results.append({"name": name, "status": "no_evaluado"})
            continue
        binary = shutil.which(name)
        if not binary:
            results.append({"name": name, "status": "ausente"})
            continue
        try:
            proc = subprocess.run([binary, "detect", "--no-git", "--source", str(root), "--report-format", "json", "--report-path", "-"],
                                  shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            parsed = json.loads(proc.stdout)
            if proc.returncode not in (0, 1) or not isinstance(parsed, list):
                raise ValueError("salida inválida")
            results.append({"name": name, "status": "ejecutado"})
            for item in parsed:
                rel = str(item.get("File", "")).replace("\\", "/")
                if Path(rel).is_absolute() or ".." in Path(rel).parts:
                    continue
                line = int(item.get("StartLine", 1))
                excerpt = str(item.get("Match", ""))
                findings.append(_finding(f"gitleaks:{item.get('RuleID', 'unknown')}", "high", "candidato", rel,
                                         line, excerpt, "Posible secreto detectado por gitleaks"))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError, json.JSONDecodeError):
            results.append({"name": name, "status": "error"})
    return results


def validate_report(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(REPORT_KEYS) or data.get("schema_version") != SCHEMA_VERSION:
        raise HefestoError("Informe de seguridad inválido o versión desconocida.", EXIT_INPUT)
    if not isinstance(data["scope"], dict) or set(data["scope"]) != {"base", "paths"}:
        raise HefestoError("Alcance del informe inválido.", EXIT_INPUT)
    if not isinstance(data["counts"], dict) or set(data["counts"]) != set(SEVERITIES):
        raise HefestoError("Recuentos del informe inválidos.", EXIT_INPUT)
    if not isinstance(data["findings"], list):
        raise HefestoError("Hallazgos del informe inválidos.", EXIT_INPUT)
    for item in data["findings"]:
        if (not isinstance(item, dict) or set(item) != set(FINDING_KEYS)
                or item["severity"] not in SEVERITIES or item["status"] not in STATUSES):
            raise HefestoError("Hallazgo inválido.", EXIT_INPUT)
        evidence = item["evidence"]
        if evidence != NOT_VERIFIABLE and (not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_KEYS)):
            raise HefestoError("Evidencia inválida.", EXIT_INPUT)
    return data


def report_json(report: dict[str, Any]) -> str:
    validate_report(report)
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_text(report: dict[str, Any]) -> str:
    validate_report(report)
    lines = [f"Escaneo de seguridad — perfil {report['profile']}", "Recuento: " + ", ".join(f"{s}={report['counts'][s]}" for s in SEVERITIES)]
    for item in report["findings"]:
        lines.append(f"{item['id']} {item['severity']} — {item['title']}")
    if report["legal"]:
        lines.append(f"{LEGAL_STATUS}: requiere revisión especializada.")
    return "\n".join(lines) + "\n"


def blocking(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in validate_report(report)["findings"] if f["in_scope"] and f["status"] == "confirmado" and f["severity"] in BLOCKING_SEVERITIES]


def today() -> date:
    return datetime.now(timezone.utc).date()


def _exception_hash(record: dict[str, Any]) -> str:
    payload = {key: record[key] for key in EXCEPTION_KEYS if key != "sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _exception_authority_path(project: Path) -> Path:
    """El respaldo del lanzador fiable debe quedar fuera de todos los worktrees."""
    path = (util.hefesto_home() / "security-exceptions" / (approval.project_id(project) + ".json")).resolve()
    roots = [Path(project).resolve()]
    worktrees = _git(project, project, "worktree", "list", "--porcelain")
    roots.extend(Path(line[9:]).resolve() for line in worktrees.splitlines() if line.startswith("worktree "))
    if any(path.is_relative_to(root) for root in roots):
        raise HefestoError("El respaldo de excepciones debe estar fuera de los worktrees.", util.EXIT_BLOCKED)
    return path


def exception_records(project: Path) -> list[dict[str, Any]]:
    try:
        data = util.read_json(exceptions_path(project))
        independent = util.read_json(_exception_authority_path(project))
        if not isinstance(data, dict) or data != independent:
            return []
        values = data.get("exceptions")
        if not isinstance(values, list):
            return []
        key = approval.project_id(project)
        for record in values:
            if (not isinstance(record, dict) or set(record) != set(EXCEPTION_KEYS)
                    or not all(isinstance(value, str) for value in record.values())
                    or record["project"] != key or record["channel"] != "tty"
                    or not re.fullmatch(r"SEC-[0-9a-f]{16}", record["finding"])
                    or not record["person"].strip() or not record["reason"].strip()
                    or record["sha256"] != _exception_hash(record)):
                return []
            expiry = date.fromisoformat(record["expires"])
            created = datetime.fromisoformat(record["created_at"]).astimezone(timezone.utc).date()
            if record["expires"] != expiry.isoformat() or not 0 <= (expiry - created).days <= MAX_EXCEPTION_DAYS:
                return []
        return values
    except (HefestoError, OSError, ValueError, TypeError, AttributeError, KeyError):
        return []


def exceptions(project: Path) -> list[dict[str, Any]]:
    return [record for record in exception_records(project) if today() <= date.fromisoformat(record["expires"])]


def add_exception(project: Path, finding_id: str, person: str, reason: str, expires: str) -> dict[str, Any] | None:
    if not approval.human_channel():
        raise HefestoError("La excepción de seguridad requiere una persona desde una terminal humana.", util.EXIT_BLOCKED)
    if (not isinstance(finding_id, str) or not re.fullmatch(r"SEC-[0-9a-f]{16}", finding_id)
            or not isinstance(person, str) or not person.strip()
            or not isinstance(reason, str) or not reason.strip()):
        raise HefestoError("La excepción requiere hallazgo SEC válido, persona y motivo.", EXIT_INPUT)
    try:
        expiry = date.fromisoformat(expires)
        valid = expires == expiry.isoformat() and 0 <= (expiry - today()).days <= MAX_EXCEPTION_DAYS
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise HefestoError("Caducidad inválida: usa AAAA-MM-DD, desde hoy y hasta 90 días.", EXIT_INPUT)
    person, reason = redact(person.strip()), redact(reason.strip())
    print(f"Excepción de seguridad: {finding_id}\nPersona: {person}\nMotivo: {reason}\nCaducidad: {expires}")
    if not util.confirm("¿Aceptar este riesgo hasta la caducidad indicada?", default=False):
        return None
    result = {"id": "EXC-" + uuid.uuid4().hex[:16], "finding": finding_id,
              "project": approval.project_id(project), "person": person, "reason": reason,
              "expires": expires, "channel": "tty", "created_at": util.now_iso(), "sha256": ""}
    result["sha256"] = _exception_hash(result)
    data = {"exceptions": exception_records(project) + [result]}
    # Primero el respaldo: un fallo intermedio invalida el registro local.
    util.atomic_write_json(_exception_authority_path(project), data)
    util.atomic_write_json(exceptions_path(project), data)
    return result


def gate(project: Path, workdir: Path, base: str, stage: str,
         emulator: Callable[[str, dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    if stage not in GATE_STAGES:
        raise HefestoError("Etapa del gate de seguridad desconocida.", EXIT_INPUT)
    report = scan(project, workdir=workdir, base=base, emulator=emulator)
    accepted = {record["finding"]: record for record in exceptions(project)}
    for finding in report["findings"]:
        if finding["id"] in accepted:
            record = accepted[finding["id"]]
            finding["status"] = "aceptado"
            finding["reason"] = f"Excepción humana {record['id']} vigente hasta {record['expires']}."
    blocked = blocking(report)
    if blocked:
        details = []
        for finding in blocked:
            evidence = finding["evidence"]
            location = f"{evidence['path']}:{evidence['line']}" if isinstance(evidence, dict) else NOT_VERIFIABLE
            details.append(f"{finding['id']} {finding['severity']} {location} ({finding['title']})")
        raise HefestoError("Gate de seguridad bloqueado: " + redact("; ".join(details))
                           + ". Una persona puede pedir excepción con `hefesto security except ID "
                           "--person PERSONA --reason MOTIVO --expires AAAA-MM-DD`.", util.EXIT_BLOCKED)
    return report


def warning_text(report: dict[str, Any]) -> str:
    lines = [f"Aviso de seguridad: {finding['id']} {finding['severity']} ({finding['status']})."
             for finding in report["findings"]]
    if report["legal"]:
        lines.append(f"{LEGAL_STATUS}: requiere revisión especializada.")
    return "\n".join(lines)


