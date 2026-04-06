from __future__ import annotations

from typing import Any

from .pgn_annotations import parse_comment



def payload_from_node(node: Any, *, orientation: str = "white", message: str = "Loaded from PGN node") -> dict:
    """
    Converts a python-chess `chess.pgn.GameNode` into a JSON payload compatible with:
    - POST /api/set
    - websocket message {"type":"set", ...}
    """
    board = node.board()
    ann = parse_comment(getattr(node, "comment", "") or "")
    return {
        "fen": board.fen(),
        "orientation": orientation,
        "arrows": [a.__dict__ for a in ann.arrows],
        "circles": [c.__dict__ for c in ann.circles],
        "message": message,
    }

