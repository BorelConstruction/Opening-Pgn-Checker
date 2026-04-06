from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class WebServerHandle:
    host: str
    port: int
    thread: threading.Thread

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


_handle: Optional[WebServerHandle] = None


def ensure_web_server(*, host: str = "127.0.0.1", port: int = 8000) -> WebServerHandle:
    """
    Starts `source.web.app:app` via uvicorn in a background thread (once).
    """
    global _handle

    if _handle and _handle.thread.is_alive():
        return _handle

    if _is_port_open(host, port):
        raise RuntimeError(f"Port {port} is already in use on {host}")

    try:
        import uvicorn  # type: ignore
    except Exception as exc:  # ImportError + packaging edge cases
        raise RuntimeError("uvicorn is required for the web board. Install requirements-web.txt") from exc

    from .app import app

    config = uvicorn.Config(app=app, host=host, port=port, reload=False, log_level="info")
    server = uvicorn.Server(config=config)

    t = threading.Thread(target=server.run, name="webboard-uvicorn", daemon=True)
    t.start()

    _wait_for_port(host, port, timeout_s=5.0)
    _handle = WebServerHandle(host=host, port=port, thread=t)
    return _handle


def _is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, *, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_port_open(host, port):
            return
        time.sleep(0.05)
    raise RuntimeError(f"Failed to start web server on {host}:{port}")
