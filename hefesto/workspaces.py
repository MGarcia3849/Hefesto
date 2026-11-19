"""Registro local de proyectos y asociaciones de chats/temas, fuera del repositorio."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from hefesto import state, util
from hefesto.lock import Lock
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError

CAPABILITIES = ("read", "write")
EDITORS = ("vscode", "studio")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path
    capabilities: tuple[str, ...] = CAPABILITIES


def registry_path() -> Path:
    return util.hefesto_home() / "projects.toml"


def bindings_path() -> Path:
    return util.state_home() / "telegram-bindings.json"


def _validate_id(project_id: str) -> None:
    if not isinstance(project_id, str) or _ID.fullmatch(project_id) is None:
        raise HefestoError("ID de proyecto inválido.", EXIT_INPUT)


def _directory(path: Path) -> Path:
    try:
        # Comprobar antes de resolve: también se rechazan ancestros enlazados.
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise HefestoError("La ruta del proyecto debe ser un directorio existente sin enlaces.", EXIT_INPUT) from None


def list_projects() -> list[Project]:
    try:
        data = tomllib.loads(registry_path().read_text(encoding="utf-8"))
        rows = data.get("project", [])
        if not isinstance(rows, list):
            raise ValueError
        projects = []
        for row in rows:
            _validate_id(row["id"])
            caps = row["capabilities"]
            if (not isinstance(row["name"], str) or not isinstance(row["path"], str)
                    or not Path(row["path"]).is_absolute() or not isinstance(caps, list)
                    or any(cap not in CAPABILITIES for cap in caps)
                    or any(p.id == row["id"] for p in projects)):
                raise ValueError
            projects.append(Project(row["id"], row["name"], Path(row["path"]), tuple(caps)))
        return projects
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        raise HefestoError("No se puede leer el registro de proyectos.", EXIT_INPUT) from None


def _save_projects(projects: list[Project], settings: dict[str, str] | None = None) -> None:
    def quote(value: str) -> str:
        # JSON comparte los escapes de cadenas básicas TOML; DEL requiere escape adicional.
        return json.dumps(value, ensure_ascii=False).replace(chr(127), "\\u007f")

    blocks = ["\n".join(("[[project]]", f"id = {quote(p.id)}", f"name = {quote(p.name)}",
                         f"path = {quote(str(p.path))}",
                         "capabilities = [" + ", ".join(quote(c) for c in p.capabilities) + "]"))
              for p in projects]
    settings = _settings() if settings is None else settings
    header = '\n'.join(f'{key} = {quote(value)}' for key, value in settings.items())
    util.atomic_write_text(registry_path(), header + "\n\n" + "\n\n".join(blocks) + "\n")


def add_project(project_id: str, path: Path | str, name: str | None = None,
                capabilities: tuple[str, ...] = CAPABILITIES) -> Project:
    util.require_tty("hefesto projects add")
    _validate_id(project_id)
    if any(cap not in CAPABILITIES for cap in capabilities) or (name is not None and not isinstance(name, str)):
        raise HefestoError("Nombre o capacidades inválidos.", EXIT_INPUT)
    project = Project(project_id, name if name is not None else project_id, _directory(Path(path)),
                      tuple(dict.fromkeys(capabilities)))
    with Lock(registry_path().with_suffix(".lock")):
        projects = list_projects()
        if any(p.id == project_id for p in projects):
            raise HefestoError("El ID de proyecto ya está registrado.", EXIT_INPUT)
        _save_projects([*projects, project])
    return project


def remove_project(project_id: str) -> None:
    util.require_tty("hefesto projects remove")
    _validate_id(project_id)
    with Lock(registry_path().with_suffix(".lock")):
        projects = list_projects()
        if not any(p.id == project_id for p in projects):
            raise HefestoError("Proyecto desconocido.", EXIT_INPUT)
        _save_projects([p for p in projects if p.id != project_id])


def resolve(project_id: str) -> Project:
    _validate_id(project_id)
    for project in list_projects():
        if project.id == project_id:
            return Project(project.id, project.name, _directory(project.path), project.capabilities)
    raise HefestoError("Proyecto desconocido.", EXIT_INPUT)


def inside(project: Project, rel: str) -> Path:
    try:
        relative = Path(rel)
        windows = PureWindowsPath(rel)
        if (relative.is_absolute() or windows.drive or windows.root
                or ".." in relative.parts or ".." in windows.parts):
            raise ValueError
        root = _directory(project.path)
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError
        return target
    except (OSError, ValueError, RuntimeError, HefestoError):
        # No incluir rutas, argumentos ni nombres suministrados en la auditoría.
        util.append_jsonl(util.state_home() / "telegram-audit.jsonl",
                          {"at": util.now_iso(), "command": "inside", "result": "denied"})
        raise HefestoError("Ruta fuera del proyecto o no permitida.", EXIT_BLOCKED) from None


def _bindings() -> dict[str, str]:
    try:
        data = json.loads(bindings_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
            raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise HefestoError("No se pueden leer las asociaciones de Telegram.", EXIT_INPUT) from None


def _key(chat_id: int, thread_id: int | None) -> str:
    if type(chat_id) is not int or (thread_id is not None and (type(thread_id) is not int or thread_id <= 0)):
        raise HefestoError("Chat o tema inválido.", EXIT_INPUT)
    return f"{chat_id}:{thread_id if thread_id is not None else 'general'}"


def bind(chat_id: int, thread_id: int | None, project_id: str) -> None:
    project = resolve(project_id)
    key = _key(chat_id, thread_id)
    with Lock(bindings_path().with_suffix(".lock")):
        data = _bindings()
        data[key] = project.id
        util.atomic_write_json(bindings_path(), data)


def unbind(chat_id: int, thread_id: int | None) -> None:
    key = _key(chat_id, thread_id)
    with Lock(bindings_path().with_suffix(".lock")):
        data = _bindings()
        data.pop(key, None)
        util.atomic_write_json(bindings_path(), data)


def select(chat_id: int, project_id: str) -> None:
    bind(chat_id, None, project_id)


def active(chat_id: int, thread_id: int | None) -> Project | None:
    project_id = _bindings().get(_key(chat_id, thread_id))
    return resolve(project_id) if project_id is not None else None


def close(chat_id: int, thread_id: int | None) -> None:
    unbind(chat_id, thread_id)


def write_slot(project: Project) -> Lock:
    _validate_id(project.id)
    return Lock(util.state_home() / "telegram-locks" / project.id, timeout=0)


def project_status(project: Project) -> str:
    root = _directory(project.path)

    def git(*args: str) -> str:
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=10, shell=False)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    branch = git("branch", "--show-current") or "sin rama"
    changes = len(git("status", "--porcelain").splitlines())
    task = "sin estado Hefesto"
    if (root / ".hefesto").is_dir():
        try:
            data = state.load(root)
            task = f"{data['status']} {data.get('current_task_id') or ''}".strip()
        except (HefestoError, OSError):
            task = "estado Hefesto no disponible"
    return f"{project.id} — {project.name}\nRama: {branch}\nCambios sin confirmar: {changes}\nHefesto: {task}"


def has_desktop() -> bool:
    ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))
    wsl = bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
               or "microsoft" in platform.release().lower())
    if wsl:
        return bool(os.environ.get("WSL_INTERACTIVE") and os.environ.get("DISPLAY"))
    if sys.platform == "win32":
        return not ssh
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                or (sys.platform == "darwin" and not ssh))


def open_in_vscode(project: Project) -> tuple[bool, str]:
    return open_in_editor(project, "vscode")


def _settings() -> dict[str, str]:
    try:
        data = tomllib.loads(registry_path().read_text(encoding="utf-8"))
        result = {k: data[k] for k in ('root', 'studio', 'vscode', 'github_owner') if k in data}
        if any(not isinstance(v, str) for v in result.values()):
            raise ValueError
        return result
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise HefestoError("Configuración de proyectos ilegible.", EXIT_INPUT) from None


def _set_setting(key: str, value: str) -> None:
    with Lock(registry_path().with_suffix('.lock')):
        projects = list_projects()
        settings = _settings()
        settings[key] = value
        # Mantiene las tablas de proyectos y las claves de configuración de este registro.
        _save_projects(projects, settings)


def set_projects_root(path: Path | str) -> None:
    util.require_tty('hefesto projects root')
    _set_setting('root', str(_directory(Path(path))))


def projects_root() -> Path:
    value = _settings().get('root')
    if not value or not Path(value).is_absolute():
        raise HefestoError('Configura la raíz desde terminal: hefesto projects root RUTA.', EXIT_INPUT)
    return _directory(Path(value))


def set_github_owner(owner: str) -> None:
    """Configuración local por terminal, persistida en projects.toml."""
    util.require_tty('configurar propietario GitHub')
    if re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', owner) is None:
        raise HefestoError('Propietario GitHub inválido.', EXIT_INPUT)
    _set_setting('github_owner', owner)


def github_owner() -> str | None:
    """Propietario de GitHub configurado con `hefesto projects owner`, o None."""
    return _settings().get('github_owner') or None


def github_repos(runner=None) -> list[dict]:
    owner = _settings().get('github_owner')
    if not owner:
        return []
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', owner) is None:
        raise HefestoError('Propietario GitHub inválido.', EXIT_INPUT)
    try:
        result = (runner or subprocess.run)(['gh', 'repo', 'list', owner, '--json', 'name,url', '--limit', '200'],
                                            shell=False, capture_output=True, text=True, timeout=30)
        rows = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('name'), str)
                                              or not isinstance(r.get('url'), str) for r in rows):
            raise ValueError
        for row in rows:
            github_url(row['url'])
        return rows
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise HefestoError('No se pudieron consultar los repositorios de GitHub.', EXIT_INPUT) from None


def github_url(url: str) -> tuple[str, str]:
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/'
                         r'([A-Za-z0-9][A-Za-z0-9._-]{0,99})', url)
    if not match:
        raise HefestoError('Solo se acepta https://github.com/propietario/repo(.git), sin credenciales.', EXIT_INPUT)
    owner, name = match.groups()
    name = name.removesuffix('.git')
    if not name or name.endswith(('.', ' ')) or name.lower().split('.')[0] in (
            'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))):
        raise HefestoError('Nombre de repositorio no permitido.', EXIT_INPUT)
    return owner, name


def clone_target(url: str) -> Project:
    _, name = github_url(url)
    root = projects_root()
    destination = root / name
    project_id = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:32].rstrip('-')
    _validate_id(project_id)
    if destination.exists() or destination.is_symlink() or any(p.id == project_id for p in list_projects()):
        raise HefestoError('El destino o ID de proyecto ya existe; no se sobrescribe.', EXIT_INPUT)
    return Project(project_id, name, destination)


def _register_clone(project: Project, operation: dict, store) -> Project:
    """Solo una operación clone aprobada, en ejecución y con alcance intacto registra sin TTY."""
    from hefesto import telegram_ops
    saved = store.get(operation['id'])
    if (saved != operation or saved['status'] != 'running' or saved['action'] != 'clone'
            or telegram_ops._digest(saved['scope']) != saved['scope_sha256']
            or str(project.path) != saved['scope']['destination'] or project.id != saved['project']):
        raise HefestoError('No hay operación de clonado aprobada en ejecución.', EXIT_BLOCKED)
    registered = Project(project.id, project.name, _directory(project.path))
    if registered.path.parent != projects_root():
        raise HefestoError('La raíz de proyectos ha cambiado.', EXIT_BLOCKED)
    with Lock(registry_path().with_suffix('.lock')):
        projects = list_projects()
        if any(p.id == registered.id for p in projects):
            raise HefestoError('ID ya registrado.', EXIT_INPUT)
        _save_projects([*projects, registered])
    return registered


def find_editor(editor: str) -> str | None:
    if editor not in EDITORS:
        raise HefestoError('Editor desconocido. Usa vscode o studio.', EXIT_INPUT)
    configured = _settings().get(editor)
    if configured:
        return str(Path(configured)) if Path(configured).is_file() else None
    for command in (('code',) if editor == 'vscode' else ('studio64.exe', 'studio64', 'studio')):
        found = shutil.which(command)
        if found:
            return found
    if editor == 'studio':
        for base, suffix in (('ProgramFiles', 'Android/Android Studio/bin/studio64.exe'),
                             ('LOCALAPPDATA', 'Programs/Android Studio/bin/studio64.exe')):
            if os.environ.get(base):
                path = Path(os.environ[base]) / suffix
                if path.is_file():
                    return str(path)
    return None


def open_in_editor(project: Project, editor: str) -> tuple[bool, str]:
    root = _directory(project.path)
    code = find_editor(editor)
    label = 'Android Studio' if editor == 'studio' else 'VS Code'
    if not has_desktop() or not code:
        return False, f'{label} no se ha abierto: hace falta un escritorio y el lanzador instalado o configurado.'
    try:
        subprocess.Popen([code, str(root)], shell=False)
    except OSError:
        return False, f'No se pudo abrir {label}.'
    return True, f'Proyecto abierto en {label}.'
