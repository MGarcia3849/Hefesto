"""Servidor local de Bot API para pruebas E2E, sin token ni acceso a Internet."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.request import Request, urlopen


class FakeTelegram:
    """Implementa getUpdates/sendMessage sobre HTTP en localhost y guarda el tráfico."""

    def __init__(self) -> None:
        self.updates: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
                method = self.path.rsplit("/", 1)[-1]
                length = int(self.headers.get("Content-Length", "0"))
                params = json.loads(self.rfile.read(length) or b"{}")
                with owner._lock:
                    owner.calls.append((method, params))
                    if method == "getUpdates":
                        offset = params.get("offset")
                        result = [row for row in owner.updates
                                  if offset is None or row["update_id"] >= offset]
                    elif method == "sendMessage":
                        owner.sent.append(params)
                        result = {"message_id": len(owner.sent)}
                    else:
                        self.send_error(404)
                        return
                body = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> FakeTelegram:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def push(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.updates.append(update)

    def transport(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in ("getUpdates", "sendMessage"):
            raise ValueError("método no implementado por FakeTelegram")
        request = Request(f"{self.url}/botFAKE/{method}", data=json.dumps(params).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
        if result.get("ok") is not True:
            raise RuntimeError("FakeTelegram rechazó la solicitud")
        return result

