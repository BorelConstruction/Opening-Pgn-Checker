from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Optional

import chess

from .contracts import Arrow, BoardState, Circle, MoveCallback, WebBoard
from .pgn_annotations import parse_comment


class BoardSession(WebBoard):
    def __init__(self, *, on_move: Optional[MoveCallback] = None):
        self._lock = threading.RLock()
        self._board = chess.Board()
        self._state = self._compute_state(
            orientation="white",
            arrows=[],
            circles=[],
            last_move=None,
            message="Ready",
            allow_moves=True,
        )
        self._on_move = on_move

    def get_state(self) -> BoardState:
        with self._lock:
            return replace(self._state)

    def set_fen(
        self,
        fen: str,
        *,
        arrows: Optional[list[Arrow]] = None,
        circles: Optional[list[Circle]] = None,
        orientation: str = "white",
        message: str = "",
        allow_moves: bool = True,
    ) -> BoardState:
        with self._lock:
            if fen.strip().lower() == "startpos":
                self._board.reset()
            else:
                self._board.set_fen(fen)
            self._state = self._compute_state(
                orientation=orientation,
                arrows=arrows or [],
                circles=circles or [],
                last_move=None,
                message=message,
                allow_moves=allow_moves,
            )
            return replace(self._state)

    def set_from_node(
        self,
        node: Any,
        *,
        orientation: str = "white",
        message: str = "",
        allow_moves: bool = True,
    ) -> BoardState:
        """
        Accepts a python-chess 'chess.pgn.GameNode'.
        """
        with self._lock:
            board = node.board()
            self._board = board
            ann = parse_comment(getattr(node, "comment", "") or "")
            self._state = self._compute_state(
                orientation=orientation,
                arrows=ann.arrows,
                circles=ann.circles,
                last_move=_node_last_move(node),
                message=message or "Loaded from PGN node",
                allow_moves=allow_moves,
            )
            return replace(self._state)

    def apply_uci(self, uci: str) -> BoardState:
        with self._lock:
            move = chess.Move.from_uci(uci)
            if move not in self._board.legal_moves:
                raise ValueError(f"Illegal move: {uci}")

            orig = chess.square_name(move.from_square)
            dest = chess.square_name(move.to_square)
            self._board.push(move)

            self._state = self._compute_state(
                orientation=self._state.orientation,
                arrows=self._state.arrows,
                circles=self._state.circles,
                last_move=[orig, dest],
                message=f"Applied {uci}",
                allow_moves=True,
            )

            if self._on_move:
                self._on_move(uci, replace(self._state))

            return replace(self._state)

    def _compute_state(
        self,
        *,
        orientation: str,
        arrows: list[Arrow],
        circles: list[Circle],
        last_move: Optional[list[str]],
        message: str,
        allow_moves: bool,
    ) -> BoardState:
        return BoardState(
            fen=self._board.fen(),
            turn="white" if self._board.turn == chess.WHITE else "black",
            orientation=orientation,
            dests=_legal_dests(self._board) if allow_moves else {},
            last_move=last_move,
            arrows=arrows,
            circles=circles,
            message=message,
        )


def _legal_dests(board: chess.Board) -> dict[str, list[str]]:
    dests: dict[str, set[str]] = {}
    for move in board.legal_moves:
        orig = chess.square_name(move.from_square)
        dest = chess.square_name(move.to_square)
        dests.setdefault(orig, set()).add(dest)
    return {k: sorted(v) for k, v in dests.items()}


def _node_last_move(node: Any) -> Optional[list[str]]:
    move = getattr(node, "move", None)
    parent = getattr(node, "parent", None)
    if move is None or parent is None:
        return None
    try:
        board = parent.board()
    except Exception:
        return None
    return [chess.square_name(move.from_square), chess.square_name(move.to_square)]
