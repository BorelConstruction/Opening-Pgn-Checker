from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .board.contracts import Arrow, Circle
from .board.session import BoardSession
from .spaced_repetition import SpacedRepetitionController


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(ws)


class BoardHub:
    """
    Glue between the Python-level board API (BoardSession) and web clients.

    When you call 'hub.set_from_node(...)' from Python, the browser updates
    immediately via websocket broadcast (when the FastAPI app is running).
    """

    def __init__(self, *, board: BoardSession, manager: ConnectionManager) -> None:
        self._board = board
        self._manager = manager
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_event_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def get_state(self) -> dict[str, Any]:
        return self._board.get_state().to_dict()

    def set_fen(self, fen: str, **kwargs: Any) -> dict[str, Any]:
        state = self._board.set_fen(fen, **kwargs)
        self._broadcast_state(state.to_dict())
        return state.to_dict()

    def apply_uci(self, uci: str) -> dict[str, Any]:
        state = self._board.apply_uci(uci)
        self._broadcast_state(state.to_dict())
        return state.to_dict()

    def set_from_node(self, node: Any, **kwargs: Any) -> dict[str, Any]:
        state = self._board.set_from_node(node, **kwargs)
        self._broadcast_state(state.to_dict())
        return state.to_dict()

    def _broadcast_state(self, state: dict[str, Any]) -> None:
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast({"type": "state", "state": state}),
            self._loop,
        )


_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

app = FastAPI(title="PgnChecker Web Board")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def _log_move(uci: str, _state: Any) -> None:
    print(f"[webboard] move={uci}")


manager = ConnectionManager()
board = BoardSession(on_move=_log_move)
hub = BoardHub(board=board, manager=manager)
sr_controller = SpacedRepetitionController(hub)


@app.on_event("startup")
async def _startup() -> None:
    hub.bind_event_loop()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/favicon.ico")
def favicon() -> JSONResponse:
    return JSONResponse({}, status_code=204)


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return hub.get_state()


@app.post("/api/set")
def api_set(payload: dict[str, Any]) -> JSONResponse:
    fen = payload.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        return JSONResponse({"error": "fen is required"}, status_code=400)

    orientation = payload.get("orientation") or "white"
    arrows = _parse_arrows(payload.get("arrows"))
    circles = _parse_circles(payload.get("circles"))
    message = payload.get("message") or "Position set"

    try:
        state = hub.set_fen(
            fen,
            arrows=arrows,
            circles=circles,
            orientation=orientation,
            message=message,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(state)


@app.post("/api/move")
def api_move(payload: dict[str, Any]) -> JSONResponse:
    uci = payload.get("uci")
    if not isinstance(uci, str) or not uci.strip():
        return JSONResponse({"error": "uci is required"}, status_code=400)

    try:
        state = hub.apply_uci(uci.strip())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(state)


@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    await manager.connect(ws)
    await ws.send_json({"type": "state", "state": hub.get_state()})
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")

            if msg_type == "move":
                uci = msg.get("uci")
                if not isinstance(uci, str):
                    await ws.send_json({"type": "error", "message": "uci must be a string"})
                    continue
                try:
                    if sr_controller.active:
                        sr_controller.handle_guess(uci.strip())
                    else:
                        hub.apply_uci(uci.strip())
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    continue

            elif msg_type == "sr_new":
                try:
                    sr_controller.new_random()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})

            elif msg_type == "sr_continue":
                try:
                    sr_controller.continue_line()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})

            elif msg_type == "set":
                fen = msg.get("fen")
                if not isinstance(fen, str) or not fen.strip():
                    await ws.send_json({"type": "error", "message": "fen is required"})
                    continue
                try:
                    hub.set_fen(
                        fen.strip(),
                        arrows=_parse_arrows(msg.get("arrows")),
                        circles=_parse_circles(msg.get("circles")),
                        orientation=msg.get("orientation") or "white",
                        message=msg.get("message") or "Position set",
                    )
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    continue

            else:
                await ws.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        manager.disconnect(ws)


def _parse_arrows(value: Any) -> list[Arrow]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError("arrows must be a list")
    out: list[Arrow] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        orig = item.get("orig")
        dest = item.get("dest")
        color = item.get("color") or "green"
        if isinstance(orig, str) and isinstance(dest, str):
            out.append(Arrow(orig=orig, dest=dest, color=str(color)))
    return out


def _parse_circles(value: Any) -> list[Circle]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError("circles must be a list")
    out: list[Circle] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        square = item.get("square")
        color = item.get("color") or "green"
        if isinstance(square, str):
            out.append(Circle(square=square, color=str(color)))
    return out
