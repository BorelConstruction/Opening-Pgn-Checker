from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .board.contracts import Arrow, Circle
from .board.session import BoardSession
from .spaced_repetition import AppController, PromptLineId


def _format_exception_detail() -> str:
    """Format current exception with full traceback."""
    return traceback.format_exc()


def _prompt_line_id_from_payload(payload: Any) -> PromptLineId:
    if not isinstance(payload, dict):
        raise TypeError("promptId must be an object")

    start_fen = payload.get("startFen")
    moves = payload.get("moves")
    if not isinstance(start_fen, str):
        raise TypeError("promptId.startFen must be a string")
    if not isinstance(moves, list) or not all(isinstance(move, str) for move in moves):
        raise TypeError("promptId.moves must be a list of strings")
    return PromptLineId(start_fen, tuple(moves))


def _search_move_notation_from_payload(payload: Any) -> str | None:
    if payload is None:
        return None
    if not isinstance(payload, str) or not payload.strip():
        raise TypeError("notation must be a non-empty string")
    return payload.strip()


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

    def broadcast(self, message: dict[str, Any]) -> None:
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._manager.broadcast(message), self._loop)

    def _broadcast_state(self, state: dict[str, Any]) -> None:
        self.broadcast({"type": "state", "state": state})


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
sr_controller = AppController(hub)


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
    await ws.send_json({"type": "sr_state", "sr": sr_controller.ui_state()})
    try:
        while True:
            msg = await ws.receive_json()
            # if not isinstance(msg, dict):
            #     continue

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
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
                    continue

            elif msg_type == "sr_new":
                try:
                    sr_controller.start_next_prompt()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
            

            elif msg_type == "sr_give_up":
                try:
                    sr_controller.give_up()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_finish_prompt":
                try:
                    sr_controller.finish_prompt()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_finish_prompt_new":
                try:
                    sr_controller.finish_prompt_and_start_new()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_blacklist_prompt":
                try:
                    sr_controller.blacklist_current_move()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_blacklist_line_prompt":
                try:
                    sr_controller.blacklist_current_line()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_skip_move":
                try:
                    sr_controller.skip_current_move()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_accept_alternative":
                try:
                    sr_controller.accept_pending_alternative()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_hint":
                try:
                    sr_controller.provide_hint()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_study_from_here":
                start_range = msg.get("start_range")
                if isinstance(start_range, bool) or not isinstance(start_range, int):
                    await ws.send_json({"type": "error", "message": "start_range must be an integer"})
                    continue
                try:
                    sr_controller.study_from_here(start_range)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_search_move":
                try:
                    search_notation = _search_move_notation_from_payload(msg.get("notation"))
                except Exception:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
                    continue
                try:
                    if search_notation is None:
                        sr_controller.search_nodes_by_move()
                    else:
                        sr_controller.search_nodes_by_move_notation(search_notation)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_marked_moves":
                try:
                    sr_controller.show_marked_moves()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_history":
                try:
                    await ws.send_json({"type": "sr_history", "history": sr_controller.history_payload()})
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_bookmarks":
                try:
                    await ws.send_json({"type": "sr_bookmarks", "bookmarks": sr_controller.bookmarks_payload()})
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_bookmark_current_prompt":
                try:
                    sr_controller.bookmark_current_prompt()
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_bookmark_set":
                try:
                    prompt_id = _prompt_line_id_from_payload(msg.get("promptId"))
                except Exception:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
                    continue

                bookmarked = msg.get("bookmarked")
                if not isinstance(bookmarked, bool):
                    await ws.send_json({"type": "error", "message": "bookmarked must be a boolean"})
                    continue

                view = msg.get("view", "history")
                if view not in ("history", "bookmarks"):
                    await ws.send_json({"type": "error", "message": "view must be history or bookmarks"})
                    continue

                try:
                    sr_controller.set_prompt_bookmark(prompt_id, bookmarked)
                    if view == "bookmarks":
                        await ws.send_json({"type": "sr_bookmarks", "bookmarks": sr_controller.bookmarks_payload()})
                    else:
                        await ws.send_json({"type": "sr_history", "history": sr_controller.history_payload()})
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_progress":
                try:
                    await ws.send_json({"type": "sr_progress", "progress": sr_controller.progress_payload()})
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_review_show_alternatives":
                enabled = msg.get("enabled")
                if not isinstance(enabled, bool):
                    await ws.send_json({"type": "error", "message": "enabled must be a boolean"})
                    continue
                try:
                    sr_controller.set_review_show_alternatives(enabled)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_unmark_move":
                mark = msg.get("mark")
                position_fen = msg.get("fen")
                uci = msg.get("uci")
                if mark not in ("blacklist", "skip"):
                    await ws.send_json({"type": "error", "message": "mark must be blacklist or skip"})
                    continue
                if not isinstance(position_fen, str) or not position_fen.strip():
                    await ws.send_json({"type": "error", "message": "fen must be a non-empty string"})
                    continue
                if not isinstance(uci, str) or not uci.strip():
                    await ws.send_json({"type": "error", "message": "uci must be a non-empty string"})
                    continue
                try:
                    sr_controller.unmark_move(mark, position_fen.strip(), uci.strip())
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_history_goto":
                path = msg.get("path")
                position_fen = msg.get("fen")
                san = msg.get("san")
                if path is not None and (not isinstance(path, list) or not all(isinstance(i, int) for i in path)):
                    await ws.send_json({"type": "error", "message": "path must be a list of integers"})
                    continue
                if position_fen is not None and not isinstance(position_fen, str):
                    await ws.send_json({"type": "error", "message": "fen must be a string"})
                    continue
                if not isinstance(san, str):
                    await ws.send_json({"type": "error", "message": "san must be a string"})
                    continue
                if path is None and position_fen is None:
                    await ws.send_json({"type": "error", "message": "history target must provide a path or a fen"})
                    continue
                try:
                    sr_controller.goto_history_move(path, position_fen, san)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_history_study":
                try:
                    prompt_id = _prompt_line_id_from_payload(msg.get("promptId"))
                except Exception:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
                    continue

                spec_id = msg.get("specId")
                if spec_id is not None and not isinstance(spec_id, str):
                    await ws.send_json({"type": "error", "message": "specId must be a string"})
                    continue
                try:
                    sr_controller.study_history_prompt(prompt_id, spec_id)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

            elif msg_type == "sr_goto":
                path = msg.get("path")
                if not isinstance(path, list) or not all(isinstance(i, int) for i in path):
                    await ws.send_json({"type": "error", "message": "path must be a list of integers"})
                    continue
                try:
                    sr_controller.goto_review_path(path)
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})

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
                    await ws.send_json({"type": "error", "message": _format_exception_detail()})
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
