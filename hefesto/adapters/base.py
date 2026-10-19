"""Interfaz común de adaptadores: reciben rol, prompt, task_id y run_id; devuelven un resultado validado.

Una salida que no cumpla el esquema es un fallo (`invalid_output`), nunca un éxito.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hefesto import activity, config, lock, log
from hefesto.guard import redact
from hefesto.util import filtered_env

# Comandos que un orquestador headless puede ejecutar sin pedir aprobación.
ORCHESTRATOR_SHELL = ("hefesto", "graphify", "git status", "git diff", "git log")

ERROR_TYPES = ("missing_binary", "auth", "quota", "rate_limit", "timeout", "invalid_output", "denied_by_policy")
READ_ONLY_ROLES = ("reviewer", "challenge")
SUMMARY_CHARS = 1500

BASE_PATTERNS = {
    "auth": re.compile(r"ineligibletiererror|unsupported_client|no longer supported", re.I),
    "rate_limit": re.compile(r"\b503\b|unavailable|service is currently unavailable", re.I),
}


@dataclass
class AdapterResult:
    success: bool
    provider: str
    model: str
    summary: str
    error_type: str | None
    usage: dict[str, Any] | str
    log_path: str
    reset_hint: str = ""
    duration: float = 0.0
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("reset_hint")
        data.pop("text")
        return data


def validate_result(data: Any) -> list[str]:
    """Esquema mínimo del resultado de un adaptador."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["no es un objeto"]
    types = {"success": bool, "provider": str, "model": str, "summary": str, "log_path": str}
    for key, typ in types.items():
        if not isinstance(data.get(key), typ):
            errors.append(f"`{key}` debe ser {typ.__name__}")
    if data.get("error_type") is not None and data.get("error_type") not in ERROR_TYPES:
        errors.append(f"error_type desconocido {data.get('error_type')!r}")
    if data.get("success") and data.get("error_type"):
        errors.append("success con error_type")
    usage = data.get("usage")
    if not (usage == "unknown" or isinstance(usage, dict)):
        errors.append("usage debe ser un objeto o `unknown`")
    return errors


@dataclass
class Call:
    role: str
    prompt: str
    task_id: str | None
    run_id: str
    model: str
    workdir: Path
    project: Path
    packet_path: Path | None = None
    timeout: int = 600
    orchestrator: bool = False
    max_turns: int | None = None
    model_id: str = ""
    env: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.role in READ_ONLY_ROLES and not self.orchestrator

    def child_env(self) -> dict[str, str]:
        """Entorno del proceso hijo: sin claves de API y con el rol de Hefesto identificado."""
        extra: dict[str, str] = {"HEFESTO_ROLE": "orchestrator" if self.orchestrator else self.role}
        if self.task_id:
            extra["HEFESTO_TASK_ID"] = self.task_id
        return filtered_env({**extra, **self.env}, strip_credentials=False)


class Adapter:
    """Adaptador de CLI. Las subclases definen `command`, `parse` y los patrones de error."""

    provider = ""
    patterns: dict[str, re.Pattern[str]] = {}

    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg

    # -- a implementar por cada CLI --------------------------------------------------
    def binary(self) -> str | None:
        from hefesto.providers import binary_for

        return binary_for(self.cfg, self.provider)

    def command(self, binary: str, call: Call) -> list[str]:
        raise NotImplementedError

    def stdin_for(self, call: Call) -> str:
        return call.prompt

    def parse(self, stdout: str, stderr: str, returncode: int, call: Call) -> AdapterResult:
        raise NotImplementedError
    # -- comunes -----------------------------------------------------------------------
    def classify(self, text: str) -> str | None:
        for error_type in ("auth", "quota", "rate_limit"):
            pattern = self.patterns.get(error_type)
            if pattern is not None and pattern.search(text):
                return error_type
        for error_type in ("auth", "quota", "rate_limit"):
            base_pattern = BASE_PATTERNS.get(error_type)
            if base_pattern is not None and base_pattern.search(text):
                return error_type
        return None

    def result(self, call: Call, success: bool, summary: str, error_type: str | None,
               usage: dict[str, Any] | str, log_path: str, reset_hint: str = "") -> AdapterResult:
        text = redact(summary.strip())
        return AdapterResult(success=success, provider=self.provider, model=call.model or "default",
                             summary=text[:SUMMARY_CHARS], error_type=error_type, usage=usage,
                             log_path=log_path, reset_hint=reset_hint, text=text)

    def run(self, call: Call) -> AdapterResult:
        rel_log = f"runs/{call.run_id}-{self.provider}.log"
        binary = self.binary()
        if not binary:
            path = log.write_text(call.project, rel_log, f"{self.provider}: binario no encontrado")
            return self.result(call, False, f"{self.provider} no está instalado", "missing_binary", "unknown",
                               _rel(call.project, path))
        argv = self.command(binary, call)
        log_file = activity.resolve_log_path(call.project, f".hefesto/logs/{rel_log}")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_rel = _rel(call.project, log_file)
        call.extra["log_path"] = log_rel
        header = f"$ {' '.join(argv)}\n"
        # Abrir y probar el log antes de crear un proceso que haya que limpiar.
        with open(log_file, "w+", encoding="utf-8", newline="\n") as fh:
            fh.write(redact(header))
            fh.flush()
            start = time.monotonic()
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=call.workdir, text=True, encoding="utf-8", errors="replace", env=call.child_env(),
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
            except OSError as exc:
                fh.write(redact(str(exc)))
                return self.result(call, False, str(exc), "missing_binary", "unknown", log_rel)

            events: queue.Queue[tuple[str, str | BaseException]] = queue.Queue()
            chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
            threads: list[threading.Thread] = []
            code: int | None = None
            timed_out = False

            def read_stream(name: str, stream: Any) -> None:
                try:
                    for line in iter(stream.readline, ""):
                        events.put((name, line))
                except Exception as exc:
                    events.put((name, exc))
                finally:
                    stream.close()

            def write_stdin() -> None:
                try:
                    proc.stdin.write(self.stdin_for(call))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass  # El hijo puede terminar sin consumir el prompt.
                except Exception as exc:
                    events.put(("stdin", exc))
                finally:
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass

            def drain(count: int, live: bool = True) -> None:
                for _ in range(count):
                    try:
                        name, value = events.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(value, BaseException):
                        if live:
                            raise value
                        continue
                    chunks[name].append(value)
                    if live:
                        fh.write(redact(value))
                if live:
                    fh.flush()

            try:
                activity.register(
                    call.project, call.run_id, "orchestrator" if call.orchestrator else "worker",
                    call.role, self.provider,
                    call.model_id or f"{self.provider}:{call.model or 'default'}",
                    call.task_id, log_rel, proc.pid,
                )
                for target, args in ((read_stream, ("stdout", proc.stdout)),
                                     (read_stream, ("stderr", proc.stderr)), (write_stdin, ())):
                    thread = threading.Thread(target=target, args=args, daemon=True)
                    thread.start()
                    threads.append(thread)
                deadline = start + call.timeout
                while True:
                    drain(1000)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        code = proc.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                try:
                    # También cubre errores de registro, de E/S y KeyboardInterrupt.
                    if code is None:
                        _terminate_tree(proc)
                    join_deadline = time.monotonic() + 2
                    for thread in threads:
                        thread.join(timeout=max(0, join_deadline - time.monotonic()))
                    if not any(thread.is_alive() for thread in threads):
                        for stream in (proc.stdin, proc.stdout, proc.stderr):
                            if stream is not None:
                                stream.close()
                    # Los lectores solo encolan datos: nunca escriben después del cierre.
                    drain(events.qsize(), live=False)
                    duration = round(time.monotonic() - start, 3)
                    stdout, stderr = "".join(chunks["stdout"]), "".join(chunks["stderr"])
                    status = f"timeout {call.timeout}s" if timed_out else f"exit {proc.returncode}, {duration}s"
                    # Redactar el texto completo captura también asignaciones multilínea,
                    # incluso al agotar el plazo o interrumpir la ejecución.
                    fh.seek(0)
                    fh.write(redact(f"{header}[{status}]\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"))
                    fh.truncate()
                    fh.flush()
                finally:
                    if proc.poll() is not None:
                        activity.finish(call.project, call.run_id)

        if timed_out:
            result = self.result(call, False, f"timeout tras {call.timeout}s", "timeout", "unknown", log_rel)
        else:
            result = self.parse(stdout, stderr, code, call)
        result.duration = duration
        if validate_result(result.to_dict()):
            return self.result(call, False, "salida del adaptador fuera de esquema", "invalid_output", "unknown",
                               result.log_path)
        return result

    def failure_from_text(self, call: Call, text: str, code: int) -> AdapterResult:
        error_type = self.classify(text)
        summary = text.strip().splitlines()[-1] if text.strip() else f"salida vacía (exit {code})"
        return self.result(call, False, summary, error_type or ("invalid_output" if code == 0 else None),
                           "unknown", call.extra.get("log_path", ""), reset_hint=text)


def _terminate_tree(proc: subprocess.Popen[str]) -> None:
    """Termina la sesión POSIX o el árbol Windows antes de retirar su registro."""
    if os.name == "nt":
        # PATH puede contener solo las CLIs permitidas; taskkill pertenece al sistema.
        taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
        try:
            result = subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)],
                                    capture_output=True, timeout=5, check=False)
            failed = result.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            failed = True
        if failed and proc.poll() is None:
            # taskkill puede carecer de acceso a WMI en un entorno restringido.
            # Toolhelp permite enumerar descendientes y terminar solo los nuestros.
            for pid in reversed(_windows_process_tree(proc.pid)):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    if lock.pid_alive(pid):
                        raise
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=5)


def _windows_process_tree(root_pid: int) -> list[int]:
    """Snapshot de parentesco, sin leer comandos ni el entorno de otros procesos."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    for name in ("Process32FirstW", "Process32NextW"):
        fn = getattr(kernel, name)
        fn.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        fn.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    tree = [root_pid]
    seen = {root_pid}
    for pid in tree:
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                tree.append(child)
    return tree


def _rel(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return str(path)


def loads_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def last_json_object(text: str) -> Any:
    """El último objeto JSON válido de la salida (algunas CLIs imprimen avisos antes)."""
    whole = loads_or_none(text.strip())
    if whole is not None:
        return whole
    for line in reversed(text.strip().splitlines()):
        parsed = loads_or_none(line.strip())
        if isinstance(parsed, dict):
            return parsed
    start = text.find("{")
    return loads_or_none(text[start:]) if start >= 0 else None
