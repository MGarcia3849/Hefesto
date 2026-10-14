"""Lock portable: `mkdir` atómico + fichero `owner` (PID, host, fecha).

Un lock solo se considera huérfano si su PID ya no existe en el mismo host; nunca se borra
el lock de un proceso vivo.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from hefesto.util import EXIT_CONFLICT, IS_WINDOWS, HefestoError, now_iso

# Si el directorio existe sin `owner` durante más de esto, el creador murió entre mkdir y la escritura.
OWNERLESS_GRACE_SECONDS = 10.0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: existe pero no es nuestro
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    def __init__(self, path: Path, timeout: float = 30.0, poll: float = 0.05,
                 on_recover: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.on_recover = on_recover
        self.token = uuid.uuid4().hex
        self.held = False

    @property
    def owner_file(self) -> Path:
        return self.path / "owner"

    def read_owner(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.owner_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _is_orphan(self) -> tuple[bool, dict[str, Any]]:
        owner = self.read_owner()
        if owner is None:
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                return False, {}
            return age > OWNERLESS_GRACE_SECONDS, {"reason": "sin owner", "age_seconds": round(age, 1)}
        if owner.get("host") != socket.gethostname():
            return False, owner  # otro host: no podemos saber si vive; no se toca
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return False, owner
        return not pid_alive(pid), owner

    def _recover(self, owner: dict[str, Any]) -> None:
        stale = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(self.path, stale)  # atómico: solo un proceso gana la recuperación
        except OSError:
            return
        shutil.rmtree(stale, ignore_errors=True)
        if self.on_recover:
            self.on_recover(owner)

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                os.mkdir(self.path)
            except FileExistsError:
                orphan, owner = self._is_orphan()
                if orphan:
                    self._recover(owner)
                    continue
                if time.monotonic() >= deadline:
                    who = self.read_owner() or {}
                    raise HefestoError(
                        f"Lock ocupado ({self.path}) por PID {who.get('pid', '?')} en {who.get('host', '?')}"
                        f" desde {who.get('created', '?')}.", EXIT_CONFLICT)
                time.sleep(self.poll)
                continue
            owner_data = {"pid": os.getpid(), "host": socket.gethostname(), "created": now_iso(),
                          "token": self.token, "argv": sys.argv[1:4]}
            tmp = self.path / f".owner.{self.token}"
            tmp.write_text(json.dumps(owner_data), encoding="utf-8")
            os.replace(tmp, self.owner_file)
            self.held = True
            return

    def release(self) -> None:
        if not self.held:
            return
        owner = self.read_owner()
        if owner and owner.get("token") != self.token:
            self.held = False
            raise HefestoError(f"El lock {self.path} ya no es nuestro; no se libera.", EXIT_CONFLICT)
        try:
            self.owner_file.unlink(missing_ok=True)
            os.rmdir(self.path)
        finally:
            self.held = False

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()


def project_lock(project: Path, timeout: float = 30.0) -> Lock:
    from hefesto import log

    def recovered(owner: dict[str, Any]) -> None:
        log.event(project, "lock", "lock huérfano recuperado", owner=json.dumps(owner))

    return Lock(project / ".hefesto" / ".lock", timeout=timeout, on_recover=recovered)
