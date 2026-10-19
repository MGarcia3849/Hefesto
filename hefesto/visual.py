"""Capturas y comprobaciones geométricas con la CLI de Playwright instalada."""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hefesto import config, roadmap, skills, util
from hefesto.util import EXIT_INPUT, EXIT_UNAVAILABLE, HefestoError

DESIGN_REFS_SOURCE = "VoltAgent/awesome-design-md"
DESIGN_REFS_COMMIT = "3b605329240ce735b510037cca322f1831fdd7f8"
DESIGN_REF_SITE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Ancestros e hijos se contienen a propósito: solo se comparan hojas visibles.
# Un contenedor desplazable no recorta contenido; overflow hidden/clip sí.
_PROBE = r"""(() => {
    const root = document.documentElement;
    const visible = el => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility === 'visible' &&
            Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
    };
    const label = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
        Array.from(el.classList).map(c => '.' + c).join('');
    const leaves = Array.from(document.body.querySelectorAll('*')).filter(el =>
        visible(el) && !Array.from(el.children).some(visible));
    const clipped = [], overlaps = [];
    for (const el of leaves) {
        const r = el.getBoundingClientRect();
        for (let p = el; p; p = p.parentElement) {
            const s = getComputedStyle(p), b = p.getBoundingClientRect();
            const x = ['hidden', 'clip'].includes(s.overflowX);
            const y = ['hidden', 'clip'].includes(s.overflowY);
            if ((x && (r.left < b.left - 1 || r.right > b.right + 1 ||
                       (p === el && p.scrollWidth > p.clientWidth + 1))) ||
                (y && (r.top < b.top - 1 || r.bottom > b.bottom + 1 ||
                       (p === el && p.scrollHeight > p.clientHeight + 1)))) {
                clipped.push(label(el)); break;
            }
        }
    }
    const boxes = leaves.map(el => ({el, r: el.getBoundingClientRect()}))
        .sort((a, b) => a.r.left - b.r.left);
    for (let i = 0; i < boxes.length; i++) {
        const a = boxes[i];
        for (let j = i + 1; j < boxes.length; j++) {
            const b = boxes[j];
            if (b.r.left >= a.r.right - 1) break;
            if (Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top) > 1 &&
                Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left) > 1 &&
                !a.el.contains(b.el) && !b.el.contains(a.el)) {
                overlaps.push([label(a.el), label(b.el)]);
            }
        }
    }
    return JSON.stringify({scrollWidth: root.scrollWidth, clientWidth: root.clientWidth,
                           clipped, overlaps});
})()"""
# Evita que los metacaracteres del JavaScript lleguen a un lanzador .cmd.
_PROBE_ARG = "function(){return eval(atob('" + base64.b64encode(_PROBE.encode()).decode("ascii") + "'))}"


@dataclass
class CaptureReport:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    skipped: str | None = None


def artifacts_dir(project: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("Identificador de tarea inválido")
    return project / ".hefesto" / "logs" / "visual" / task_id


def reviewer_images(project: Path, cfg: config.Config, task: roadmap.Task) -> list[Path]:
    """Capturas PNG disponibles para el revisor, limitadas por la configuración visual."""
    if task.kind not in PASS_KINDS:
        return []
    try:
        limit = max(0, int(cfg.get("visual", "max_images", default=4)))
    except (TypeError, ValueError):
        limit = 4
    if limit == 0:
        return []
    folder = artifacts_dir(project, task.id)
    try:
        images = sorted((path for path in folder.glob("*.png") if path.is_file()),
                        key=lambda path: path.name.casefold())
    except OSError:
        return []
    return images[:limit]


def _batch_safe(argv: list[str]) -> None:
    if os.name == "nt" and Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
        if any(any(c in a for c in '&|<>^%!"\r\n\x00') for a in argv):
            raise ValueError("Argumento inseguro para un lanzador .cmd/.bat")


def _wait_ready(url: str, timeout: int, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(url, timeout=min(0.5, remaining)):
                return True
        except urllib.error.HTTPError:
            return True  # También un 404 demuestra que el servidor ya responde.
        except (OSError, ValueError):
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return False


def _probe_problems(output: str) -> list[str]:
    data = json.loads(output)
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("se esperaba un objeto JSON")
    for key in ("scrollWidth", "clientWidth"):
        value = data.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} inválido")
    if data["clientWidth"] == 0:
        raise ValueError("clientWidth debe ser positivo")
    for key in ("clipped", "overlaps"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"{key} debe ser una lista")
    problems = []
    if data["scrollWidth"] > data["clientWidth"]:
        problems.append("scroll horizontal")
    if data["clipped"]:
        problems.append("contenido recortado: " + json.dumps(data["clipped"], ensure_ascii=False))
    if data["overlaps"]:
        problems.append("elementos solapados: " + json.dumps(data["overlaps"], ensure_ascii=False))
    return problems


def _stop_start_command(proc: subprocess.Popen) -> list[str]:
    """Cierra el árbol propio y recoge el proceso raíz con esperas acotadas."""
    errors: list[str] = []
    try:
        if os.name == "nt":
            taskkill = util.which("taskkill") or str(
                Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
            try:
                subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], shell=False,
                               capture_output=True, timeout=5, check=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, ValueError, subprocess.SubprocessError):
                proc.kill()
        else:
            # start_new_session hace que el PID sea también el PGID; no depende
            # de que el padre siga vivo cuando toque limpiar a sus descendientes.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    finally:
        if os.name != "nt":
            # El padre puede haber salido y dejar hijos que ignoran SIGTERM.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        try:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    return errors


def capture(project: Path, cfg: config.Config, task: roadmap.Task, out_dir: Path | None = None) -> CaptureReport:
    report = CaptureReport()
    if not cfg.get("visual", "enabled") or task.kind not in ("app", "web"):
        report.skipped = "Captura visual desactivada o tarea sin interfaz"
        return report
    routes = cfg.get("visual", "routes", default=["/"])
    sizes = cfg.get("visual", "viewports", default=["390x844", "1280x800"])
    for name, values in (("routes", routes), ("viewports", sizes)):
        if not values:
            report.ok = False
            report.problems.append(f"visual.{name} no puede estar vacío")
    if not report.ok:
        return report
    binary = util.which("playwright-cli")
    if not binary:
        report.skipped = "playwright-cli no está instalado; se omiten las capturas"
        print(f"Aviso: {report.skipped}", file=sys.stderr)
        return report
    folder = Path(out_dir) if out_dir is not None else artifacts_dir(project, task.id)
    folder = folder.resolve()
    base_url = cfg.get("visual", "base_url", default="")
    session = f"-s=hefesto-{task.id.lower()}"
    context = f"{routes[0]} {sizes[0]}"
    proc = None
    opened = False

    def problem(message: str) -> None:
        report.ok = False
        report.problems.append(f"{context}: {message}")

    def run(*args: str) -> str | None:
        argv = [binary, session, *args]
        try:
            _batch_safe(argv)
            result = subprocess.run(argv, cwd=project, shell=False, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=30)
            if result.returncode:
                problem(f"playwright-cli {' '.join(args[:2])} salió con {result.returncode}: "
                        f"{result.stderr.strip()}")
                return None
            return result.stdout
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            problem(f"playwright-cli {args[0]}: {exc}")
            return None

    try:
        if not base_url.startswith(("http://", "https://")):
            problem("visual.base_url debe ser una URL HTTP o HTTPS")
            return report
        folder.mkdir(parents=True, exist_ok=True)
        command = cfg.get("visual", "start_command", default=[])
        if command:
            _batch_safe(command)
            proc = subprocess.Popen(command, cwd=project, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=os.name != "nt",
                                    creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                                    if os.name == "nt" else 0)
            if not _wait_ready(base_url, cfg.get("visual", "start_timeout", default=30), proc):
                problem(f"base_url {base_url} no responde dentro de start_timeout")
                return report
        opened = True
        if run("open") is None:
            return report
        used: set[str] = set()
        for route in routes:
            slug = re.sub(r"[^A-Za-z0-9_-]", "-", route.strip("/").replace("/", "-")) or "root"
            for size in sizes:
                context = f"{route} {size}"
                width, height = size.split("x")
                target = folder / f"{slug}-{size}.png"
                if target.name in used:
                    problem("nombre de captura duplicado")
                    continue
                used.add(target.name)
                if run("resize", width, height) is None or run("goto", base_url.rstrip("/") + route) is None:
                    continue
                output = run("--raw", "eval", _PROBE_ARG)
                if output is not None:
                    try:
                        for message in _probe_problems(output):
                            problem(message)
                    except (ValueError, TypeError) as exc:
                        problem(f"sonda ilegible: {exc}")
                target.unlink(missing_ok=True)
                if run("screenshot", f"--filename={target}") is not None:
                    if target.is_file():
                        report.images.append(target)
                    else:
                        problem("screenshot no creó la captura")
    except (OSError, ValueError) as exc:
        problem(str(exc))
    finally:
        try:
            if opened:
                run("close")
        finally:
            if proc is not None:
                for error in _stop_start_command(proc):
                    problem(f"no se pudo detener start_command: {error}")
    return report


# --- Referencias de diseño (TASK-1603) ----------------------------------------------------------------------------


def _is_design_site(site: object) -> bool:
    return isinstance(site, str) and DESIGN_REF_SITE_RE.fullmatch(site) is not None and ".." not in site


def _check_design_site(site: str) -> None:
    if not _is_design_site(site):
        raise HefestoError(
            f"Nombre de sitio inválido ({site!r}): solo a-z, 0-9, punto, guion y guion bajo.", EXIT_INPUT)


@contextlib.contextmanager
def _design_refs_git(project: Path) -> Iterator[tuple[Callable[..., subprocess.CompletedProcess], str]]:
    """Clon endurecido de DESIGN_REFS_SOURCE en `[visual] design_refs_commit`, igual que `skills.fetch`."""
    cfg = config.load(project)
    commit = cfg.get("visual", "design_refs_commit", default=DESIGN_REFS_COMMIT)
    if not isinstance(commit, str) or not skills.COMMIT_RE.fullmatch(commit):
        raise HefestoError(f"visual.design_refs_commit inválido ({commit!r}): debe ser un SHA-1 de 40 hex.",
                           EXIT_INPUT)
    git_base = cfg.get("skills_sources", "git_base", default="https://github.com")
    skills._validate_git_location(git_base)
    repo_url = f"{git_base.rstrip('/')}/{DESIGN_REFS_SOURCE}.git"
    skills._validate_git_location(repo_url)
    git_bin = util.which("git")
    if not git_bin:
        raise HefestoError("git no está instalado.", EXIT_UNAVAILABLE)

    with tempfile.TemporaryDirectory(prefix="hefesto-design-refs-") as tmpdir:
        tmp_path = Path(tmpdir)
        empty_config = tmp_path / "empty_gitconfig"
        empty_config.touch()
        empty_hooks = tmp_path / "empty_hooks"
        empty_hooks.mkdir()
        env = {key: value for key, value in util.filtered_env().items() if not key.upper().startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": empty_config.as_posix(),
                    "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"})
        git_c_opts = [
            "-c", f"core.hooksPath={empty_hooks.as_posix()}",
            "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always",
            "-c", "protocol.https.allow=always",
        ]
        clone_dir = tmp_path / "repo"
        try:
            cloned = subprocess.run(
                [git_bin, *git_c_opts, "clone", "--no-checkout", "--depth", "1", "--no-tags", "--single-branch",
                 repo_url, str(clone_dir)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        except OSError as exc:
            raise HefestoError(f"Error al clonar las referencias de diseño: {exc}", EXIT_UNAVAILABLE) from exc
        if cloned.returncode:
            raise HefestoError(f"Error al clonar {repo_url} (código {cloned.returncode}): "
                               f"{cloned.stderr.strip() or cloned.stdout.strip()}", EXIT_INPUT)

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([git_bin, *git_c_opts, *args], cwd=clone_dir, capture_output=True, env=env)

        if git("fetch", "--depth", "1", "--no-tags", "origin", commit).returncode:
            if git("fetch", "--unshallow", "--no-tags", "origin").returncode:
                git("fetch", "--no-tags", "origin")
        if git("rev-parse", "--verify", f"{commit}^{{commit}}").returncode:
            raise HefestoError(f"No se encontró el commit '{commit}' en {repo_url}.", EXIT_INPUT)
        yield git, commit


def design_ref(project: Path, site: str) -> Path:
    """Copia `design-md/<site>/DESIGN.md` del commit fijado a `.hefesto/design/refs/<site>.md` (solo inspiración)."""
    _check_design_site(site)
    with _design_refs_git(project) as (git, commit):
        blob = git("cat-file", "blob", f"{commit}:design-md/{site}/DESIGN.md")
        if blob.returncode:
            raise HefestoError(f"El sitio '{site}' no está en el commit {commit} de {DESIGN_REFS_SOURCE}.",
                               EXIT_INPUT)
        data = blob.stdout
    if len(data) > skills.MAX_FILE_SIZE:
        raise HefestoError(f"La referencia '{site}' supera el tamaño máximo permitido.", EXIT_INPUT)
    target = project / ".hefesto" / "design" / "refs" / f"{site}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    skills.check_confinement(target, project)
    target.write_bytes(data)
    return target


def list_design_refs(project: Path) -> list[str]:
    """Sitios con `design-md/<site>/DESIGN.md` en el commit fijado, ordenados."""
    with _design_refs_git(project) as (git, commit):
        listing = git("ls-tree", "-r", "-z", "--name-only", commit, "--", "design-md")
        if listing.returncode:
            raise HefestoError(f"Error al listar design-md/ de {DESIGN_REFS_SOURCE}.", EXIT_INPUT)
    sites: set[str] = set()
    for name in listing.stdout.decode("utf-8", errors="replace").split("\0"):
        match = re.fullmatch(r"design-md/([^/]+)/DESIGN\.md", name)
        if match and _is_design_site(match.group(1)):
            sites.add(match.group(1))
    return sorted(sites)


# --- Detector determinista (TASK-1605) ----------------------------------------------------------------------------

SEVERITY_ORDER = ("advisory", "warning", "error")
DETECT_NOTE = "El detector no prueba que la página funcione: de eso se ocupan las capturas y el verify."
DETECT_TIMEOUT_SECONDS = 120
INTERFACE_SUFFIXES = (".html", ".css", ".scss", ".tsx", ".jsx", ".vue", ".svelte")
PROJECT_CONFIG = ".impeccable/config.json"
LOCAL_CONFIG = ".impeccable/config.local.json"
IGNORE_KEYS = ("ignoreRules", "ignoreFiles", "ignoreValues")
_CMD_METACHARS = '&|<>^%!"'


@dataclass
class DetectReport:
    ok: bool = True
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None
    note: str = DETECT_NOTE


def _interface_files(project: Path, task: roadmap.Task) -> tuple[list[str], list[str]]:
    """Ficheros de interfaz existentes de la tarea; aparte, los que llevan metacaracteres de cmd."""
    passed: list[str] = []
    unsafe: list[str] = []
    for raw in task.files:
        rel = raw.strip().replace("\\", "/")
        name = rel.rsplit("/", 1)[-1].lower()
        is_ui = name.endswith(INTERFACE_SUFFIXES) or ".component." in name
        if not rel or not is_ui or rel in passed or rel in unsafe or not (project / rel).is_file():
            continue
        if any(c in rel for c in _CMD_METACHARS):
            unsafe.append(rel)
        else:
            passed.append(rel)
    return passed, unsafe


def _rank(finding: dict[str, Any]) -> int:
    severity = finding.get("severity")
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _ignore_lists(text: str | None) -> dict[str, list[Any]] | None:
    """Excepciones del detector de un config.json; None si el JSON es ilegible."""
    if text is None:
        return {key: [] for key in IGNORE_KEYS}
    try:
        data = json.loads(text)
    except ValueError:
        return None
    detector = data.get("detector") if isinstance(data, dict) else None
    detector = detector if isinstance(detector, dict) else {}
    lists: dict[str, list[Any]] = {}
    for key in IGNORE_KEYS:
        value = detector.get(key)
        lists[key] = value if isinstance(value, list) else ([value] if value else [])
    return lists


def ignores_changed(before: str | None, after: str | None) -> bool:
    if before == after:
        return False
    old, new = _ignore_lists(before), _ignore_lists(after)
    if old is None or new is None:
        return True
    return old != new


def ignore_values_added(before: str | None, after: str | None) -> list[str]:
    """Reglas, ficheros o valores de excepción nuevos; el fichero entero si el JSON posterior es ilegible."""
    new = _ignore_lists(after)
    if new is None:
        return [PROJECT_CONFIG]
    old = _ignore_lists(before) or {key: [] for key in IGNORE_KEYS}
    return [str(value) for key in IGNORE_KEYS for value in new[key] if value not in old[key]]


def inline_ignores_added(diff_text: str) -> bool:
    return any(line.startswith("+") and not line.startswith("+++") and "impeccable-disable" in line
               for line in diff_text.splitlines())


def _local_ignore_findings(project: Path) -> list[dict[str, Any]]:
    path = project / LOCAL_CONFIG
    if not path.is_file():
        return []
    lists = _ignore_lists(path.read_text(encoding="utf-8", errors="replace"))
    if not lists or not any(lists.values()):
        return []
    return [{"antipattern": "local-ignores", "name": "local-ignores", "severity": "error", "category": "config",
             "description": "Excepciones en .impeccable/config.local.json, que git no versiona: muévelas a "
                            ".impeccable/config.json con motivo.",
             "file": LOCAL_CONFIG, "line": 1}]


def _detect_result(project: Path, task: roadmap.Task, out_dir: Path | None, ok: bool,
                   findings: list[dict[str, Any]], note: str) -> DetectReport:
    folder = Path(out_dir) if out_dir is not None else artifacts_dir(project, task.id)
    folder.mkdir(parents=True, exist_ok=True)
    util.atomic_write_json(folder / "detect.json", {"ok": ok, "findings": findings, "note": note})
    return DetectReport(ok=ok, findings=findings, note=note)


def detect(project: Path, cfg: config.Config, task: roadmap.Task, out_dir: Path | None = None) -> DetectReport:
    """`impeccable detect --json` (versión fijada) sobre los ficheros de interfaz de la tarea; solo con `[visual] detect`."""
    if not cfg.get("visual", "detect"):
        return DetectReport(skipped="Detector desactivado ([visual] detect = false).")
    fail_on = cfg.get("visual", "detect_fail_on", default="warning")
    if fail_on not in SEVERITY_ORDER:
        raise HefestoError(f"visual.detect_fail_on inválido ({fail_on!r}): usa advisory, warning o error.", EXIT_INPUT)
    pinned = str(cfg.get("visual", "impeccable_version", default="4.6.0"))
    passed, unsafe = _interface_files(project, task)
    note = DETECT_NOTE
    if unsafe:
        note += f" No se pasaron por llevar metacaracteres de cmd: {', '.join(unsafe)}."
    if not passed:
        return DetectReport(skipped="La tarea no tiene ficheros de interfaz existentes; el detector no se ejecuta.",
                            note=note)
    binary = util.which("impeccable")
    if not binary:
        return DetectReport(skipped="impeccable no está instalado: se omite el detector (no bloquea).", note=note)
    env = util.filtered_env({"DO_NOT_TRACK": "1", "IMPECCABLE_NO_TELEMETRY": "1"})
    try:
        _batch_safe([binary, "--version"])
        version = subprocess.run([binary, "--version"], cwd=project, shell=False, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return DetectReport(skipped=f"impeccable --version falló ({exc}): se omite el detector (no bloquea).",
                            note=note)
    found = re.search(r"\d+\.\d+\.\d+", version.stdout + version.stderr)
    if not found or found.group(0) != pinned:
        shown = found.group(0) if found else (version.stdout.strip() or "desconocida")
        return DetectReport(skipped=f"impeccable {shown} no es la versión fijada ({pinned}): se omite el detector "
                                    "(no bloquea).", note=note)
    argv = [binary, "detect", "--json", *passed]
    try:
        _batch_safe(argv)
        result = subprocess.run(argv, cwd=project, shell=False, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=DETECT_TIMEOUT_SECONDS, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return _detect_result(project, task, out_dir, False, [], f"{note} impeccable no pudo ejecutarse: {exc}.")
    problem = ""
    findings: list[dict[str, Any]] = []
    if result.returncode not in (0, 2):
        problem = f"impeccable detect salió con {result.returncode}: {result.stderr.strip()}"
    else:
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            findings = parsed
        else:
            problem = "salida de impeccable detect ilegible (se esperaba un array JSON)."
    findings = findings + _local_ignore_findings(project)
    blocking = [f for f in findings if f.get("severity") != "advisory" and _rank(f) >= SEVERITY_ORDER.index(fail_on)]
    if problem:
        note += f" {problem}"
    return _detect_result(project, task, out_dir, not problem and not blocking, findings, note)


# --- Flujo de impeccable: diagnóstico, comandos y pulido ---------------------------------------------------------

SHORTCUTS: dict[str, tuple[str, ...]] = {
    "impacto": ("critique", "bolder", "polish"),
    "robustez": ("audit", "harden", "optimize"),
    "tipografia": ("typeset", "layout", "polish"),
}
DIAGNOSES = ("critique", "audit")
PASS_KINDS = ("app", "web")
_NEVER_COMMANDS = frozenset({"init", "shape", "live", "generate", "craft", "overdrive", "detect", *DIAGNOSES})
_NEEDS_DIAGNOSIS = frozenset({"bolder", "overdrive"})
_COMMAND_RE = re.compile(r"impeccable\s+([a-z][a-z-]*)", re.IGNORECASE)


@dataclass
class PassReport:
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    failed: str | None = None
    report_path: Path | None = None


def _visual_modes() -> set[str]:
    from hefesto import skill_catalog

    entry = skill_catalog.load().get("impeccable")
    return set(entry.modes.get("visual", ())) if entry else set()


def parse_suggestions(text: str) -> list[str]:
    """Comandos de impeccable citados en un informe, en orden y sin repetir; solo los del rol visual."""
    allowed = _visual_modes()
    found: list[str] = []
    for match in _COMMAND_RE.finditer(text or ""):
        command = match.group(1).lower()
        if command in allowed and command not in found:
            found.append(command)
    return found


def plan_pass(diagnosis: str | None, suggestions: list[str]) -> list[str]:
    """Comandos a delegar: sin los vetados, `bolder` solo con diagnóstico, `animate` al final y `polish` último."""
    allowed = _visual_modes()
    plan: list[str] = []
    for command in suggestions:
        if command not in allowed or command in _NEVER_COMMANDS or command in plan:
            continue
        if command in _NEEDS_DIAGNOSIS and not diagnosis:
            continue
        plan.append(command)
    animate = "animate" in plan
    plan = [c for c in plan if c not in ("animate", "polish")]
    if animate:
        plan.append("animate")
    plan.append("polish")
    return plan


def shortcut_plan(shortcut: str) -> tuple[str | None, list[str]]:
    """(diagnóstico, comandos) de un atajo conocido."""
    if shortcut not in SHORTCUTS:
        raise HefestoError(f"Atajo desconocido `{shortcut}` ({', '.join(SHORTCUTS)}).", EXIT_INPUT)
    steps = list(SHORTCUTS[shortcut])
    diagnosis = steps.pop(0) if steps[0] in DIAGNOSES else None
    return diagnosis, plan_pass(diagnosis, steps)


def check_pass(project: Path, task: roadmap.Task, diagnosis: str = "critique") -> None:
    from hefesto import project as project_mod

    if diagnosis not in DIAGNOSES:
        raise HefestoError(f"Diagnóstico desconocido `{diagnosis}` ({', '.join(DIAGNOSES)}).", EXIT_INPUT)
    if task.kind not in PASS_KINDS:
        raise HefestoError(f"{task.id} es de tipo `{task.kind}`: el flujo de diseño es solo para tareas app o web.",
                           EXIT_INPUT)
    if not project_mod.design_context(project).product_ready:
        raise HefestoError("PRODUCT.md no está listo: el Core debe ejecutar `impeccable init` con el humano antes "
                           "del flujo de diseño.", EXIT_INPUT)


def _pass_instruction(command: str, task: roadmap.Task, report_path: str | None = None) -> str:
    files = ", ".join(task.files)
    diagnosis = f"Lee el diagnóstico en {report_path} y corrige los problemas que señala. " if report_path else ""
    return (f"impeccable {command} sobre los ficheros de la tarea {task.id}: {files}. "
            f"{diagnosis}Aplica solo este comando; sin maquetas con clave de API.")


def design_pass(project: Path, cfg: config.Config, task: roadmap.Task,
                delegate: Callable[[str, str], tuple[bool, str]], checkpoint: Callable[[str], Any],
                diagnosis: str = "critique", shortcut: str | None = None) -> PassReport:
    """Diagnóstico → comandos (uno por delegación, con checkpoint) → polish."""
    check_pass(project, task, diagnosis)
    commands: list[str] | None = None
    if shortcut is not None:
        diagnosis_run, commands = shortcut_plan(shortcut)
    else:
        diagnosis_run = diagnosis
    report = PassReport()
    if diagnosis_run:
        report.steps.append(diagnosis_run)
        ok, text = delegate("reviewer", _pass_instruction(diagnosis_run, task))
        if not ok:
            report.ok, report.failed = False, diagnosis_run
            return report
        folder = project / ".hefesto" / "design" / "reports"
        resolved_folder = folder.resolve()
        if not resolved_folder.is_relative_to(project.resolve()):
            raise HefestoError("La carpeta .hefesto/design/reports apunta fuera del proyecto.", EXIT_INPUT)
        report.report_path = folder / f"{task.id}-{diagnosis_run}.md"
        util.atomic_write_text(resolved_folder / report.report_path.name, text)
        if commands is None:
            commands = plan_pass(diagnosis_run, parse_suggestions(text))
    if commands is None:
        commands = plan_pass(None, [])
    for command in commands:
        report.steps.append(command)
        absolute_report = str(report.report_path.resolve()) if report.report_path else None
        ok, _ = delegate("visual", _pass_instruction(command, task, absolute_report))
        if not ok:
            report.ok, report.failed = False, command
            return report
        checkpoint(command)
    return report
