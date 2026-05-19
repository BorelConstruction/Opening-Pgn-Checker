from __future__ import annotations

from typing import Any, Callable

import chess.pgn

from chess.pgn import GameNode as Node

from ..core.boardtools import node_san, fen
from .board.pgn_annotations import comment_text


_NAG_DISPLAY = {
    chess.pgn.NAG_GOOD_MOVE: "!",
    chess.pgn.NAG_MISTAKE: "?",
    chess.pgn.NAG_BRILLIANT_MOVE: "‼",
    chess.pgn.NAG_BLUNDER: "⁇",
    chess.pgn.NAG_SPECULATIVE_MOVE: "⁉",
    chess.pgn.NAG_DUBIOUS_MOVE: "⁈",
    # The PGN/NAG tables do not give dedicated Unicode symbols for every entry.
    # For the common symbol-less cases used in this repertoire, use the nearest
    # standard display instead of falling back to English labels.
    chess.pgn.NAG_FORCED_MOVE: "□",
    chess.pgn.NAG_SINGULAR_MOVE: "□",
    chess.pgn.NAG_WORST_MOVE: "⁇",
    chess.pgn.NAG_DRAWISH_POSITION: "=",
    chess.pgn.NAG_QUIET_POSITION: "=",
    chess.pgn.NAG_ACTIVE_POSITION: "=",
    chess.pgn.NAG_UNCLEAR_POSITION: "∞",
    chess.pgn.NAG_WHITE_SLIGHT_ADVANTAGE: "⩲",
    chess.pgn.NAG_BLACK_SLIGHT_ADVANTAGE: "⩱",
    chess.pgn.NAG_WHITE_MODERATE_ADVANTAGE: "±",
    chess.pgn.NAG_BLACK_MODERATE_ADVANTAGE: "∓",
    chess.pgn.NAG_WHITE_DECISIVE_ADVANTAGE: "+−",
    chess.pgn.NAG_BLACK_DECISIVE_ADVANTAGE: "−+",
    chess.pgn.NAG_WHITE_ZUGZWANG: "⨀",
    chess.pgn.NAG_BLACK_ZUGZWANG: "⨀",
    chess.pgn.NAG_WHITE_MODERATE_COUNTERPLAY: "⇆",
    chess.pgn.NAG_BLACK_MODERATE_COUNTERPLAY: "⇆",
    chess.pgn.NAG_WHITE_DECISIVE_COUNTERPLAY: "⇆",
    chess.pgn.NAG_BLACK_DECISIVE_COUNTERPLAY: "⇆",
    chess.pgn.NAG_WHITE_MODERATE_TIME_PRESSURE: "⨁",
    chess.pgn.NAG_BLACK_MODERATE_TIME_PRESSURE: "⨁",
    chess.pgn.NAG_WHITE_SEVERE_TIME_PRESSURE: "⨁",
    chess.pgn.NAG_BLACK_SEVERE_TIME_PRESSURE: "⨁",
    chess.pgn.NAG_NOVELTY: "N",
    26: "○",
    27: "○",
    32: "⟳",
    33: "⟳",
    36: "↑",
    37: "↑",
    38: "↑",
    39: "↑",
    40: "→",
    41: "→",
    44: "⯹",
    45: "⯹",
    46: "⯹",
    47: "⯹",
    140: "∆",
    141: "∇",
    142: "⌓",
    143: "≤",
}


def build_variation_tree(get_children: Callable[[Node], list[Node]], root: Node, *, end_ply: int) -> dict[str, Any]:
    """
    Build a JSON-serializable variation tree rooted at 'root'.

    The tree is expressed as:
    - root: a "position" node
    - each move node contains its own children (moves from the resulting position)

    Each move node includes:
    - 'path': list[int] path from root through variations indices
    - 'ply', 'moveNumber', 'color', 'san', 'uci'
    """

    def build_position(node: Node, path: list[int]) -> dict[str, Any]:
        raw_children = get_children(node)
        children = [
            build_move(child, [*path, idx])
            for idx, child in enumerate(raw_children)
        ]

        return {
            "path": path,
            "ply": node.ply(),
            "comment": _node_comment(node),
            "children": children,
        }

    def build_move(node: Node, path: list[int]) -> dict[str, Any]:
        ply = node.ply()
        move_number = (ply + 1) // 2
        color = "white" if ply % 2 == 1 else "black"

        children: list[dict[str, Any]] = []
        if ply < end_ply:
            raw_children = get_children(node)
            children = [
                build_move(child, [*path, idx])
                for idx, child in enumerate(raw_children)
                if child.ply() <= end_ply
            ]

        return {
            "path": path,
            "ply": ply,
            "moveNumber": move_number,
            "color": color,
            "san": node_san(node),
            "uci": node.move.uci() if node.move is not None else "",
            "nags": _node_nags(node),
            "comment": _node_comment(node),
            "children": children,
        }

    return build_position(root, [])


def _node_comment(node: Node) -> str:
    return comment_text(
        getattr(node, "starting_comment", "") or "",
        getattr(node, "comment", "") or "",
    )


def _node_nags(node: Node) -> list[str]:
    return [_format_nag(nag) for nag in sorted(getattr(node, "nags", set()) or ())]


def _format_nag(nag: int) -> str:
    return _NAG_DISPLAY.get(nag, f"${nag}")


def node_at_path(
    root: Node,
    path: list[int],
    get_children: Callable[[Node], list[Node]]
) -> Node:
    node: Node = root
    for idx in path:
        children = get_children(node)
        if idx < 0 or idx >= len(children):
            raise ValueError(f"Invalid path index {idx} at ply {node.ply()}")
        node = children[idx]
    return node


def path_from_root(root: Node, node: Node, get_children: Callable[[Node], list[Node]]) -> list[int]:
    if node is root:
        return []

    path: list[int] = []
    cur: Node = node
    while fen(cur) != fen(root): # TODO
        parent = getattr(cur, "parent", None)
        if parent is None:
            raise ValueError("Node is not a descendant of the current tree root")

        siblings = get_children(parent)

        idx = siblings.index(cur)

        path.append(idx)
        cur = parent

    path.reverse()
    return path

