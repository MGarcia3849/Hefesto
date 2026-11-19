"""Registro local de propuestas y aprobaciones humanas para movimiento visual.

La terminal interactiva y la ausencia de variables de agente son condiciones necesarias,
no autenticación robusta. El registro local requiere una copia independiente fuera del
checkout. Los hooks y el sandbox protegen ese respaldo de los agentes conocidos;
un proceso malicioso con los permisos completos de la misma cuenta puede alterar
ambas copias o el propio programa. No se afirma aislamiento frente a esa cuenta.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from hefesto import gitops, guard, util, visual
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

EFFECTS = ("animation", "video", "motion-graphic", "3d")
KINDS = visual.PASS_KINDS
SCOPE_KEYS = ("project", "page", "kind", "effect", "proposal", "sha256")
PROPOSAL_KEYS = ("id", "project", "page", "kind", "effect", "description", "sha256", "at")
APPROVAL_KEYS = ("id", "proposal", "project", "page", "kind", "effect", "sha256", "approved_at",
                 "channel", "revoked_at", "used_for")
FINDING_KEYS = ("path", "effect", "evidence", "fingerprint")
GATE_STAGES = ("design-pass", "delegation", "checkpoint", "finish", "deploy")
AGENT_ENV = ("HEFESTO_ROLE", "CLAUDECODE", "GEMINI_CLI")
MOTION_SKILLS = ("hefesto-skill-motion-system",)
STATIC_RULE = ("Mantén el diseño estático; conserva el movimiento existente y no añadas animación, vídeo, "
               "motion graphics ni 3D sin aprobación humana específica.")


def approvals_path(project: Path) -> Path:
    return Path(project) / ".hefesto" / "approvals.json"


def proposals_dir(project: Path) -> Path:
    return Path(project) / ".hefesto" / "proposals"


def project_id(project: Path) -> str:
    return gitops.project_key(Path(project))


def proposal_hash(proposal: dict[str, Any]) -> str:
    payload = {key: proposal[key] for key in ("project", "page", "kind", "effect", "description")}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _safe_page(project: Path, page: str) -> str:
    if not isinstance(page, str) or not page or "\\" in page or "\x00" in page:
        raise HefestoError("page debe ser una ruta POSIX relativa dentro del proyecto.", EXIT_INPUT)
    rel = PurePosixPath(page)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise HefestoError("page debe ser una ruta POSIX relativa dentro del proyecto.", EXIT_INPUT)
    root = Path(project).resolve()
    target = (root / Path(*rel.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HefestoError("page queda fuera del proyecto.", EXIT_INPUT) from exc
    return rel.as_posix()


def propose(project: Path, page: str, kind: str, effect: str, description: str) -> dict[str, Any]:
    page = _safe_page(Path(project), page)
    if kind not in KINDS or effect not in EFFECTS:
        raise HefestoError("kind o effect no válido.", EXIT_INPUT)
    proposal: dict[str, Any] = {"id": "PROP-" + uuid.uuid4().hex[:12].upper(), "project": project_id(project),
                                "page": page, "kind": kind, "effect": effect,
                                "description": guard.redact(str(description)),
                                "sha256": "", "at": util.now_iso()}
    proposal["sha256"] = proposal_hash(proposal)
    util.atomic_write_json(proposals_dir(project) / f"{proposal['id']}.json", proposal)
    return proposal


def load_proposal(project: Path, proposal_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"PROP-[A-F0-9]{12}", str(proposal_id)):
        return None
    try:
        data = util.read_json(proposals_dir(project) / f"{proposal_id}.json")
        if not isinstance(data, dict) or not set(PROPOSAL_KEYS) <= data.keys():
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def human_channel() -> bool:
    return util.is_tty() and not any(key in os.environ for key in AGENT_ENV)


def _authority_path(project: Path) -> Path:
    """Respaldo independiente: nunca material de autoridad dentro del worktree.

    HEFESTO_HOME es configuración del lanzador fiable, no una credencial. Un
    proceso que controle el lanzador y toda la cuenta queda fuera de la barrera.
    """
    path = (util.hefesto_home() / "visual-approvals" / (project_id(project) + ".json")).resolve()
    roots = [Path(project).resolve()]
    worktrees = gitops.git(project, "worktree", "list", "--porcelain").stdout
    roots.extend(Path(line[9:]).resolve() for line in worktrees.splitlines() if line.startswith("worktree "))
    if any(path.is_relative_to(root) for root in roots):
        raise HefestoError("El respaldo de aprobaciones debe estar fuera de los worktrees.", EXIT_BLOCKED)
    return path


def records(project: Path) -> list[dict[str, Any]]:
    try:
        data = util.read_json(approvals_path(project), {"approvals": []})
        independent = util.read_json(_authority_path(project))
        if not isinstance(independent, dict) or data != independent:
            return []
        values = data.get("approvals") if isinstance(data, dict) else None
        if not isinstance(values, list) or any(not isinstance(x, dict) or not set(APPROVAL_KEYS) <= x.keys()
                                               for x in values):
            return []
        return values
    except (HefestoError, OSError, ValueError, TypeError, AttributeError):
        return []


def _save(project: Path, values: list[dict[str, Any]]) -> None:
    data = {"version": 2, "approvals": values}
    # Actualizar primero la autoridad: cualquier fallo intermedio invalida la copia
    # local anterior, incluidas restauraciones de permisos revocados/consumidos.
    try:
        util.atomic_write_json(_authority_path(project), data)
        util.atomic_write_json(approvals_path(project), data)
    except OSError as exc:
        raise HefestoError("No se pudo guardar el respaldo independiente de aprobaciones.", EXIT_BLOCKED) from exc


def approve(project: Path, proposal_id: str) -> dict[str, Any] | None:
    if not human_channel():
        raise HefestoError("La aprobación exige una terminal humana fiable; los agentes no pueden aprobar.", EXIT_BLOCKED)
    proposal = load_proposal(project, proposal_id)
    if proposal is None:
        raise HefestoError(f"Propuesta desconocida: {proposal_id}.", EXIT_INPUT)
    if proposal.get("project") != project_id(project) or proposal.get("sha256") != proposal_hash(proposal):
        raise HefestoError("La propuesta fue alterada o pertenece a otro proyecto; aprobación bloqueada.", EXIT_BLOCKED)
    print("Ámbito exacto de aprobación:")
    for key in ("project", "page", "kind", "effect", "id", "sha256", "description"):
        print(f"  {key}: {proposal[key]}")
    if not util.confirm("¿Apruebas esta propuesta?", default=False):
        return None
    result = {"id": "APR-" + uuid.uuid4().hex[:12].upper(), "proposal": proposal["id"],
              **{key: proposal[key] for key in ("project", "page", "kind", "effect", "sha256")},
              "approved_at": util.now_iso(), "channel": "tty", "revoked_at": None, "used_for": []}
    _save(project, records(project) + [result])
    return result


def active(project: Path) -> list[dict[str, Any]]:
    key = project_id(project)
    found = []
    for record in records(project):
        proposal = load_proposal(project, record.get("proposal", ""))
        if (record.get("project") == key and record.get("revoked_at") is None
                and record.get("channel") == "tty" and isinstance(record.get("used_for"), list)
                and all(isinstance(f, str) for f in record["used_for"]) and proposal
                and proposal.get("sha256") == record.get("sha256") == proposal_hash(proposal)
                and all(record.get(k) == proposal.get(k) for k in ("project", "page", "kind", "effect"))):
            found.append(record)
    return found


def find(project: Path, page: str, kind: str, effect: str, fingerprint: str | None = None) -> dict[str, Any] | None:
    for record in active(project):
        if (record["page"], record["kind"], record["effect"]) != (page, kind, effect):
            continue
        used = record["used_for"]
        if not used or fingerprint is not None and fingerprint in used:
            return record
    return None


def revoke(project: Path, approval_id: str) -> bool:
    values = records(project)
    for record in values:
        if record.get("id") == approval_id and record.get("revoked_at") is None:
            record["revoked_at"] = util.now_iso()
            _save(project, values)
            return True
    return False


def consume(project: Path, approval_id: str, fingerprints: list[str]) -> None:
    values = records(project)
    record = next((r for r in values if r.get("id") == approval_id), None)
    if record is None or record.get("revoked_at") is not None or record not in active(project):
        raise HefestoError("Aprobación revocada o inválida.", EXIT_BLOCKED)
    used = record["used_for"]
    if used and not set(fingerprints) <= set(used):
        raise HefestoError("La aprobación ya se usó para otro cambio.", EXIT_BLOCKED)
    record["used_for"] = list(dict.fromkeys(used + fingerprints))
    _save(project, values)


def _diff_path(value: str) -> str:
    if value.startswith('"'):
        # Git entrecomilla UTF-8 con octales, además de los escapes habituales de JSON.
        value = re.sub(r"(?:\\[0-7]{3})+", lambda m: json.dumps(bytes(
            int(octal, 8) for octal in re.findall(r"\\([0-7]{3})", m.group())).decode("utf-8"))[1:-1], value)
        value = json.loads(value)
    return value.removeprefix("b/")


_MEDIA = {".mp4": "video", ".webm": "video", ".mov": "video", ".avi": "video",
          ".glb": "3d", ".gltf": "3d", ".gif": "motion-graphic", ".webp": "motion-graphic",
          ".lottie": "motion-graphic", ".riv": "motion-graphic"}
_DOCUMENTS = {".md", ".rst", ".py", ".pyc"}


def detect_motion(diff_text: str) -> list[dict[str, Any]]:
    """Detecta adiciones; una retirada equivalente solo compensa una adición del mismo fichero."""
    files: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            match = re.search(r' ("b/(?:[^"\\]|\\.)*"|b/.*)$', line)
            path = _diff_path(match.group(1)) if match else ""
            current = {"path": path, "added": [], "removed": [], "body": [],
                       "new": False, "binary": False, "deleted": False, "blob": "", "hunk": False}
            files[str(len(files))] = current
        elif current is not None:
            if line.startswith("@@ "):
                current["hunk"] = True
            elif line.startswith("+++ ") and not current["hunk"]:
                path = line[4:].split("\t", 1)[0]
                current["path"] = _diff_path(path)
            elif line.startswith("new file mode "):
                current["new"] = True
            elif line.startswith("deleted file mode "):
                current["deleted"] = True
            elif line.startswith("index "):
                current["blob"] = line.split("..", 1)[-1].split()[0]
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                current["binary"] = True
            elif line.startswith("+") and current["hunk"]:
                current["added"].append(line[1:])
                current["body"].append((True, line[1:]))
            elif line.startswith("-") and current["hunk"]:
                current["removed"].append(line[1:])
            elif line.startswith(" ") and current["hunk"]:
                current["body"].append((False, line[1:]))

    patterns = {
        "animation": r"@(?:-webkit-)?keyframes\b|\b(?:-webkit-)?(?:animation|transition)(?:-[\w-]+)?['\"]?\s*:[^;}]+|"
                     r"\brequestAnimationFrame\b|\.animate\s*\(|\bframer-motion\b|\bAnimationController\b|"
                     r"\b(?:gsap|TweenMax|TweenLite|TimelineMax|TimelineLite|anime)\b|"
                     r"['\"](?:motion(?:/[^'\"]*)?|@motionone/[^'\"]+)['\"]",
        "video": r"<video\b|\.(?:mp4|webm|mov|avi)\b",
        "motion-graphic": r"\blottie[\w-]*\b|<animate(?:Transform|Motion)?\b|\brive\b|['\"]layers['\"]\s*:",
        "3d": r"\bthree\b|\bgetContext\s*\(\s*['\"]webgl2?['\"]|\.(?:glb|gltf)\b|\bpreserve-3d\b|"
              r"['\"]asset['\"]\s*:\s*\{\s*['\"]version['\"]",
    }
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in files.values():
        path = entry["path"]
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in _DOCUMENTS or entry["deleted"]:
            continue
        removed = Counter(" ".join(line.split()) for line in entry["removed"])
        body = entry["body"]
        media = suffix in _MEDIA and (entry["new"] or entry["added"] or entry["binary"])
        if media or entry["binary"]:
            body = [(True, f"changed media {path} {entry['blob']}")]
        text = "\n".join(line for _added, line in body)
        # Enmascarar solo declaraciones enteramente estáticas, no el resto de una
        # lista como `none, spin 1s`. Se conservan offsets para las líneas añadidas.
        candidate = re.sub(
            r"\b(?:-webkit-)?(?:animation|transition)(?:-[\w-]+)?['\"]?\s*:\s*([^;}]+)",
            lambda m: " " * len(m.group()) if all(
                part.strip().lower() in {"none", "initial", "unset", "revert", "inherit", "revert-layer"}
                for part in m.group(1).split(",")) else m.group(), text, flags=re.I)
        spans = {effect: [m.span() for m in re.finditer(pattern, candidate, re.I)]
                 for effect, pattern in patterns.items()}
        # Un recurso Lottie/glTF ya presente puede añadir fotogramas o mallas sin
        # volver a añadir su cabecera. Evaluar también el contexto del recurso.
        resource_effect = ("motion-graphic" if suffix == ".json" and re.search(r'"layers"\s*:', text)
                           and re.search(r'"(?:fr|ip|op)"\s*:', text) else
                           "3d" if re.search(r'"asset"\s*:\s*\{\s*"version"\s*:', text) else None)
        offset = 0
        for added, line in body:
            start, end = offset, offset + len(line)
            offset = end + 1
            normalized = " ".join(line.split())
            if not added:
                continue
            if removed[normalized]:
                removed[normalized] -= 1
                continue
            effects = [_MEDIA.get(suffix, "motion-graphic")] if media or entry["binary"] else [
                effect for effect, matches in spans.items() if any(a < end and b > start for a, b in matches)]
            if resource_effect and resource_effect not in effects:
                effects.append(resource_effect)
            for effect in effects:
                fingerprint = hashlib.sha256(f"{path}\0{effect}\0{normalized}".encode("utf-8")).hexdigest()
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    findings.append({"path": path, "effect": effect, "evidence": guard.redact(line.strip()),
                                     "fingerprint": fingerprint})
    return findings


def gate(project: Path, workdir: Path, base: str, kind: str, stage: str, consume: bool = False) -> list[dict[str, Any]]:
    if stage not in GATE_STAGES:
        raise HefestoError(f"Etapa de movimiento desconocida: {stage}.", EXIT_INPUT)
    try:
        if stage == "design-pass" and not workdir.exists():
            workdir = project
        if not base:
            raise ValueError("falta la base del diff")
        managed_links = gitops.managed_worktree_links(project, workdir)
        # Primero nombres: nunca abrir secretos ni seguir enlaces fuera del checkout.
        changed = gitops.git(workdir, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", base, "--")
        staged = gitops.git(workdir, "diff", "--no-ext-diff", "--no-renames", "--cached",
                            "--name-only", "-z", base, "--")
        # Incluye salidas ignoradas: .gitignore no es una barrera de publicación.
        others = gitops.git(workdir, "ls-files", "--others", "-z")
        untracked = set(filter(None, others.stdout.split("\0")))
        paths = set(filter(None, changed.stdout.split("\0"))) | set(filter(None, staged.stdout.split("\0"))) | untracked
        findings = []
        for path in sorted(paths):
            if path in untracked and any(path == rel or path.startswith(rel + "/") for rel in managed_links):
                continue
            if (PurePosixPath(path).suffix.lower() in _DOCUMENTS
                    or path.startswith(".hefesto/")
                    or guard.is_protected(path)
                    or any(part.startswith((".env", "id_")) for part in PurePosixPath(path).parts)):
                continue
            target = workdir / path
            resolved = target.resolve()
            if not resolved.is_relative_to(workdir.resolve()):
                raise ValueError(f"ruta fuera del checkout: {path}")
            if (gitops._is_link(target) or gitops._has_link_ancestor(target, workdir)
                    or guard.is_protected(resolved.relative_to(workdir.resolve()).as_posix())):
                raise ValueError(f"recurso enlazado o protegido: {path}")
            if path in untracked:
                raw = target.read_bytes()
                blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                before, after = json.dumps("a/" + path), json.dumps("b/" + path)
                header = (f"diff --git {before} {after}\nnew file mode 100644\n"
                          f"index {'0' * 40}..{blob}\n+++ {after}\n")
                if target.suffix.lower() in _MEDIA or b"\0" in raw:
                    diff = header + f"Binary files /dev/null and b/{path} differ\n"
                else:
                    diff = header + "@@ -0,0 +1 @@\n" + "\n".join(
                        "+" + line for line in raw.decode("utf-8").splitlines())
            else:
                diff = gitops.git(workdir, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                  "--no-color", "--full-index", "-U1000000", base, "--", path).stdout
            findings.extend(detect_motion(diff))
            if path not in untracked:
                staged_diff = gitops.git(workdir, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                         "--no-color", "--full-index", "-U1000000", "--cached", base, "--", path).stdout
                findings.extend(detect_motion(staged_diff))
        findings = list({f["fingerprint"]: f for f in findings}.values())
        covered: dict[str, list[str]] = {}
        for finding in findings:
            record = find(project, finding["path"], kind, finding["effect"], finding["fingerprint"])
            if record is None:
                raise HefestoError(f"Movimiento bloqueado en {finding['path']} ({finding['effect']}); "
                                   "requiere aprobación específica con `hefesto approval`.", EXIT_BLOCKED)
            covered.setdefault(record["id"], []).append(finding["fingerprint"])
        if consume:
            for approval_id, fingerprints in covered.items():
                globals()["consume"](project, approval_id, fingerprints)
        return findings
    except (HefestoError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, HefestoError) and exc.code == EXIT_BLOCKED:
            raise
        raise HefestoError("No se puede verificar el movimiento (git o registro); "
                           "revisa `hefesto approval` antes de continuar.", EXIT_BLOCKED) from exc


def skill_allowed(project: Path, task: Any, skill_name: str) -> bool:
    return skill_name.casefold() not in MOTION_SKILLS or task is not None and any(
        record["page"] in task.files and record["kind"] == task.kind for record in active(project))
