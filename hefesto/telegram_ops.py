"""Operaciones de Telegram: consentimiento acotado, persistencia y ejecución única."""

from __future__ import annotations

import hashlib
import hmac
import json
import contextlib
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from hefesto import config, delivery, gitops, guard, roadmap, state, trust, util, workspaces
from hefesto.lock import Lock
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

ACTIONS = ("plan", "run", "trust", "finish", "push", "deploy")
PIN_ACTIONS = ("push", "deploy")
STATUSES = ("pending", "running", "done", "failed", "rejected", "expired")
DEFAULT_TTL = 300
OP_KEYS = ("id", "action", "project", "instruction", "actor", "chat", "thread", "scope", "scope_sha256",
           "created_at", "expires_at", "status", "result")
RESULT_KEYS = ("ok", "branch", "files", "diff", "tests", "summary")
PIN_ITERATIONS = 600_000


def ops_path() -> Path:
    return util.state_home() / "telegram-ops.json"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def needs_pin(op: dict) -> bool:
    if op['action'] != 'assist':
        return op['action'] in PIN_ACTIONS
    from hefesto import hermes_bridge, telegram_intent

    text = telegram_intent.normalize(op['instruction'])
    if re.search(r'\b(contest\w*|respond\w*|envi\w*|mand\w*|smtp)\b', text):
        return True
    # Cualquier ruta externa o ambigua exige PIN, aunque también se mencione Oficina.
    root = hermes_bridge.office_dir()
    paths = re.findall(r'(?:[a-zA-Z]:[\\/]|/|\.\.[\\/]|~[\\/])[^\s\"<>|]+', op['instruction'])
    for value in paths:
        path = Path(value.rstrip('.,;:)'))
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            return True
    if re.search(r'\b(fuera|extern\w*|escritorio|desktop|documentos|documents|sistema|home)\b', text):
        return True
    # Sin destino explícito no se presupone que una modificación quede dentro de Oficina.
    return 'oficina' not in text


def set_pin(pin: str) -> None:
    util.require_tty("hefesto telegram pin")
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or len(pin) < 4:
        raise HefestoError("El PIN debe contener al menos cuatro dígitos.", EXIT_INPUT)
    salt = secrets.token_hex(32)
    hashed = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS).hex()
    util.atomic_write_json(util.hefesto_home() / "telegram-pin.json",
                           {"salt": salt, "iterations": PIN_ITERATIONS, "sha256": hashed})


def check_pin(pin: str | None) -> bool:
    if not isinstance(pin, str):
        return False
    try:
        data = json.loads((util.hefesto_home() / "telegram-pin.json").read_text(encoding="utf-8"))
        iterations = data["iterations"]
        if type(iterations) is not int or not 100_000 <= iterations <= 2_000_000:
            return False
        hashed = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(data["salt"]), iterations).hex()
        return hmac.compare_digest(hashed, data["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _phase(project: Path) -> tuple[str, dict]:
    from hefesto import acceptance
    data = state.load(project)
    if not data.get("phases"):
        return "", {"branch": data.get("branch") or gitops.current_branch(project)}
    return acceptance._phase_info(project, data, roadmap.load_valid(project), None)


def scope(action: str, project: workspaces.Project, instruction: str = "") -> dict:
    """Vincula el alcance también a la ubicación registrada del proyecto."""
    if action == 'assist':
        return {'instruction': instruction}
    exact = _action_scope(action, project, instruction)
    return {**exact, "project_path": str(project.path.resolve())} if exact else {}


def _action_scope(action: str, project: workspaces.Project, instruction: str = "") -> dict:
    root = project.path
    if action in ("plan", "run"):
        return {"instruction": instruction}
    if action == "trust":
        return {"verifies": [{"task": t.id, "verify": t.verify, "sha256": trust.verify_hash(t.verify)}
                             for t in roadmap.load_valid(root).tasks
                             if t.verify and trust.status(root, t) != "trusted"]}
    if action in ("finish", "push"):
        slug, info = _phase(root)
        if action == "finish":
            return {"phase": slug, "branch": info["branch"], "main": gitops.main_branch(root, config.load(root))}
        return {"phase": slug, "branch": info["branch"], "remote": "origin"}
    if action == "deploy":
        target = delivery.load_target(root)
        return {"target": target.name, "fingerprint": delivery.target_fingerprint(target)} if target else {}
    raise HefestoError("Operación desconocida.", EXIT_INPUT)


class Store:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else ops_path()
        self.clock = clock

    def _lock(self) -> Lock:
        return Lock(self.path.with_suffix(".lock"))

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data["operations"], dict) or not isinstance(data["approved"], dict):
                raise ValueError
            return data
        except FileNotFoundError:
            return {"operations": {}, "approved": {}}
        except (OSError, ValueError, KeyError, TypeError):
            raise HefestoError("Registro de operaciones ilegible.", EXIT_BLOCKED) from None

    @staticmethod
    def _get(data: dict, op_id: str) -> dict:
        if op_id not in data["operations"]:
            raise HefestoError("Operación desconocida.", EXIT_BLOCKED)
        return data["operations"][op_id]

    def get(self, op_id: str) -> dict:
        return self._get(self._read(), op_id)

    def list(self) -> list[dict]:
        return list(self._read()["operations"].values())

    scope = staticmethod(scope)

    def create_assist(self, instruction: str, actor: int, chat: int, thread: int | None = None) -> dict:
        if not isinstance(instruction, str) or not instruction.strip():
            raise HefestoError('Falta la instrucción de assist.', EXIT_INPUT)
        exact = {'instruction': instruction}
        now = self.clock()
        with self._lock():
            data = self._read()
            op_id = secrets.token_hex(6)
            while op_id in data['operations']:
                op_id = secrets.token_hex(6)
            op = dict(zip(OP_KEYS, (op_id, 'assist', None, instruction, actor, chat, thread, exact,
                                   _digest(exact), now, now + DEFAULT_TTL, 'pending', None)))
            data['operations'][op_id] = op
            util.atomic_write_json(self.path, data)
        return op

    def create_clone(self, url: str, actor: int, chat: int, thread: int | None = None,
                     instruction: str = '', editor: str | None = None) -> dict:
        if editor is not None and editor not in workspaces.EDITORS:
            raise HefestoError('Editor desconocido.', EXIT_INPUT)
        project = workspaces.clone_target(url)
        exact = {'url': url, 'destination': str(project.path), 'instruction': instruction, 'editor': editor}
        now = self.clock()
        with self._lock():
            data = self._read()
            op_id = secrets.token_hex(6)
            while op_id in data['operations']:
                op_id = secrets.token_hex(6)
            op = dict(zip(OP_KEYS, (op_id, 'clone', project.id, instruction, actor, chat, thread, exact,
                                   _digest(exact), now, now + DEFAULT_TTL, 'pending', None)))
            data['operations'][op_id] = op
            util.atomic_write_json(self.path, data)
        return op

    def create(self, action: str, project: workspaces.Project, actor: int, chat: int,
               thread: int | None = None, instruction: str = "") -> dict:
        project = workspaces.resolve(project.id)
        if "write" not in project.capabilities:
            raise HefestoError("Proyecto sin permiso de escritura.", EXIT_BLOCKED)
        exact = scope(action, project, instruction)
        now = self.clock()
        with self._lock():
            data = self._read()
            op_id = secrets.token_hex(6)
            while op_id in data["operations"]:
                op_id = secrets.token_hex(6)
            op = dict(zip(OP_KEYS, (op_id, action, project.id, instruction, actor, chat, thread, exact,
                                   _digest(exact), now, now + DEFAULT_TTL, "pending", None)))
            data["operations"][op_id] = op
            util.atomic_write_json(self.path, data)
        return op

    @staticmethod
    def describe(op: dict) -> str:
        text = (f"{op['id']} — {op['action']} — {op['project']}\n"
                f"Alcance: {json.dumps(op['scope'], ensure_ascii=False)}\nCaduca: {op['expires_at']}\n"
                f"/approve {op['id']}" + (" PIN" if needs_pin(op) else ""))
        if any(word in op["instruction"].lower() for word in ("anim", "movimiento", "vídeo", "video", "motion", "3d")):
            text += "\nEl movimiento requiere permiso expreso; esta operación no lo autoriza."
        return guard.redact(text)

    def _validate(self, op: dict) -> workspaces.Project | None:
        if op['action'] == 'assist':
            if (self.clock() >= op['expires_at'] or op['project'] is not None
                    or op['scope'] != {'instruction': op['instruction']}
                    or _digest(op['scope']) != op['scope_sha256']):
                raise HefestoError('El alcance de assist ha cambiado o ha caducado.', EXIT_BLOCKED)
            return None
        if op['action'] == 'clone':
            project = workspaces.clone_target(op['scope']['url'])
            if (self.clock() >= op['expires_at'] or _digest(op['scope']) != op['scope_sha256']
                    or op['scope']['destination'] != str(project.path) or op['project'] != project.id
                    or op['instruction'] != op['scope']['instruction']
                    or op['scope']['editor'] not in (*workspaces.EDITORS, None)):
                raise HefestoError('El alcance de clonado ha cambiado o ha caducado.', EXIT_BLOCKED)
            return project
        project = workspaces.resolve(op["project"])
        if "write" not in project.capabilities or workspaces.inside(project, ".hefesto/STOP").exists():
            raise HefestoError("STOP o proyecto sin permiso de escritura.", EXIT_BLOCKED)
        if self.clock() >= op["expires_at"]:
            raise HefestoError("Operación caducada.", EXIT_BLOCKED)
        if (_digest(op["scope"]) != op["scope_sha256"] or
                _digest(scope(op["action"], project, op["instruction"])) != op["scope_sha256"]):
            raise HefestoError("El alcance ha cambiado; crea otra operación.", EXIT_BLOCKED)
        return project

    def approve(self, op_id: str, actor: int, chat: int, thread: int | None = None,
                pin: str | None = None) -> dict:
        with self._lock():
            data = self._read()
            op = self._get(data, op_id)
            if ((actor, chat, thread) != (op["actor"], op["chat"], op["thread"]) or
                    op["status"] != "pending" or op_id in data["approved"]):
                raise HefestoError("Aprobación no permitida.", EXIT_BLOCKED)
            if self.clock() >= op["expires_at"]:
                op["status"] = "expired"
                util.atomic_write_json(self.path, data)
                raise HefestoError("Operación caducada.", EXIT_BLOCKED)
            self._validate(op)
            if needs_pin(op) and not check_pin(pin):
                raise HefestoError("Se necesita un PIN válido.", EXIT_BLOCKED)
            data["approved"][op_id] = _digest(op)
            util.atomic_write_json(self.path, data)
            return op

    def reject(self, op_id: str, actor: int) -> dict:
        with self._lock():
            data = self._read()
            op = self._get(data, op_id)
            if actor != op["actor"] or op["status"] != "pending":
                raise HefestoError("No se puede rechazar esta operación.", EXIT_BLOCKED)
            op["status"] = "rejected"
            data["approved"].pop(op_id, None)
            util.atomic_write_json(self.path, data)
            return op

    def execute(self, op: dict, executor: Callable[[dict], dict]) -> dict:
        with self._lock():
            data = self._read()
            saved = self._get(data, op["id"])
            if (saved != op or saved["status"] != "pending" or
                    data["approved"].get(op["id"]) != _digest(saved)):
                raise HefestoError("Operación sin aprobación o ya ejecutada.", EXIT_BLOCKED)
            saved["status"] = "running"
            data["approved"].pop(op["id"])
            util.atomic_write_json(self.path, data)
        try:
            project = self._validate(saved)
            with workspaces.write_slot(project) if project is not None else contextlib.nullcontext():
                self._validate(saved)
                if saved['action'] == 'assist':
                    result = self._execute_assist(dict(saved))
                else:
                    result = self._execute_clone(dict(saved)) if saved['action'] == 'clone' else executor(dict(saved))
                if not isinstance(result, dict) or not set(RESULT_KEYS) <= result.keys():
                    raise HefestoError("Resultado del ejecutor inválido.", EXIT_BLOCKED)
        except Exception as exc:
            result = dict(zip(RESULT_KEYS, (False, "", [], "", "", guard.redact(str(exc)))))
        with self._lock():
            data = self._read()
            saved = self._get(data, op["id"])
            saved.update(status="done" if result["ok"] is True else "failed", result=result)
            util.atomic_write_json(self.path, data)
        return saved

    def _execute_assist(self, op: dict) -> dict:
        from hefesto import hermes_bridge

        if op['status'] != 'running' or self.get(op['id']) != op:
            raise HefestoError('Assist sin aprobación en ejecución.', EXIT_BLOCKED)
        self._validate(op)
        result = hermes_bridge.ask(op['instruction'], mode='accion', approved_operation=op['id'])
        return dict(zip(RESULT_KEYS, (result.ok, '', [str(p) for p in result.files], '',
                                     'No ejecutadas (asistencia).', guard.redact(result.text))))

    def _execute_clone(self, op: dict) -> dict:
        if op['status'] != 'running' or self.get(op['id']) != op:
            raise HefestoError('Clonado sin aprobación en ejecución.', EXIT_BLOCKED)
        project = self._validate(op)
        # Reserva exclusiva: ni siquiera un directorio vacío existente se reutiliza.
        project.path.mkdir(exist_ok=False)
        try:
            proc = subprocess.run(['git', 'clone', '--', op['scope']['url'], str(project.path)],
                                  cwd=project.path.parent, shell=False, capture_output=True,
                                  text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            raise HefestoError('No se pudo clonar: git o la red no están disponibles.', EXIT_INPUT) from None
        if proc.returncode != 0:
            raise HefestoError('git clone falló; el proyecto no se ha registrado. Revisa la red y el destino.', EXIT_INPUT)
        project = workspaces._register_clone(project, op, self)
        workspaces.bind(op['chat'], op['thread'], project.id)
        summary = f'Clonado y registrado: {project.id}.'
        if op['scope']['editor']:
            summary += '\n' + workspaces.open_in_editor(project, op['scope']['editor'])[1]
        if op['instruction']:
            followup = self.create('plan', project, op['actor'], op['chat'], op['thread'], op['instruction'])
            summary += '\nEncargo pendiente de aprobación:\n' + self.describe(followup)
        return dict(zip(RESULT_KEYS, (True, '', [], '', 'No ejecutadas (clonado).', summary)))

    def recover(self) -> list[str]:
        with self._lock():
            data = self._read()
            recovered = []
            for op in data["operations"].values():
                if op["status"] == "running":
                    op.update(status="failed", result=dict(zip(RESULT_KEYS, (False, "", [], "", "", "interrumpida"))))
                    recovered.append(op["id"])
            util.atomic_write_json(self.path, data)
            return recovered


def verify_running(project_root: Path, op_id: str, action: str) -> dict:
    """Prueba de aprobación para las barreras de finish y deploy.

    Un parámetro no basta: la operación debe existir en el almacén, estar en curso (aprobada y no consumida por
    otra ejecución), ser de esta acción y de este proyecto, y su alcance no puede haber cambiado.
    """
    op = Store().get(op_id)
    project = workspaces.resolve(op["project"])
    if (op["status"] != "running" or op["action"] != action
            or project.path.resolve() != Path(project_root).resolve()
            or _digest(op["scope"]) != op["scope_sha256"]
            or _digest(scope(action, project, op["instruction"])) != op["scope_sha256"]):
        raise HefestoError("No hay una operación de Telegram aprobada y en curso para esta acción.", EXIT_BLOCKED)
    return op


def default_executor(op: dict) -> dict:
    from hefesto import acceptance

    project = workspaces.resolve(op["project"])
    root = project.path
    action = op["action"]
    if op["status"] != "running" or _digest(op["scope"]) != op["scope_sha256"]:
        raise HefestoError("Operación sin ejecutar aprobada.", EXIT_BLOCKED)
    summary, tests, ok = "", "No ejecutadas (operación administrativa).", True
    slug, info = _phase(root)
    branch = info["branch"]
    workdir = Path(info.get("workdir") or root)
    if action != "trust" and not branch.startswith("hefesto/"):
        raise HefestoError("La rama debe ser hefesto/*.", EXIT_BLOCKED)
    # Finish puede retirar la rama y su worktree; captura el diff antes de integrarla.
    base = gitops.out(workdir, "rev-parse", "HEAD")
    diff_base = (gitops.main_branch(root, config.load(root)) if action in ("finish", "push", "deploy") else base)
    files = gitops.out(workdir, "diff", "--name-only", diff_base).splitlines()
    diff = gitops.out(workdir, "diff", "--stat", diff_base)
    util.append_jsonl(root / ".hefesto/logs/telegram.jsonl",
                      {"at": util.now_iso(), "operation": op["id"], "action": action, "channel": "telegram"})
    if action == "trust":
        expected = {entry["task"]: entry["sha256"] for entry in op["scope"]["verifies"]}
        approved = trust.approve_tasks(root, expected, channel="telegram")
        ok = set(approved) == set(expected)
        summary = "Verifies aprobados: " + ", ".join(approved)
    elif action in ("plan", "run"):
        if not gitops.current_branch(workdir).startswith("hefesto/"):
            raise HefestoError("La rama de trabajo debe ser hefesto/*.", EXIT_BLOCKED)
        args = (["run", "core", "--", op["instruction"]] if action == "plan" else
                ["loop", "--max-tasks", "1", "--max-turns", "6"])
        proc = subprocess.run([sys.executable, "-m", "hefesto", "-C", str(root), *args], cwd=workdir,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=3600, shell=False)
        ok = proc.returncode == 0
        summary = guard.redact((proc.stdout + proc.stderr)[-6000:])
        tests = summary if action == "run" else "No ejecutadas (planificación)."
        current = state.load(root)
        branch = current.get("branch") or branch
        workdir = Path(current.get("workdir") or workdir)
        files = gitops.out(workdir, "diff", "--name-only", base).splitlines()
        diff = gitops.out(workdir, "diff", "--stat", base)
    elif action in ("finish", "push"):
        code, summary = acceptance.finish(root, slug or None, pr=action == "push", yes=True,
                                          approved_operation=op["id"])
        ok = code == 0
        tests = summary
    elif action == "deploy":
        result = delivery.deploy(root, approved_operation=op["id"])
        ok = result.ok
        summary = result.status
        tests = f"Gates y despliegue: exit {result.exit_code}"
    else:
        raise HefestoError("Operación desconocida.", EXIT_INPUT)
    return dict(zip(RESULT_KEYS, (ok, branch, files, diff, tests, summary)))
