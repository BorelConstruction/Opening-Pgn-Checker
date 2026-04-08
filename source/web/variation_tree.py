from __future__ import annotations

from typing import Any

import chess.pgn

from ..core.boardtools import node_san
from ..core.repertoire import RepertoireSession


def build_variation_tree(session: RepertoireSession, root: chess.pgn.GameNode, *, end_ply: int) -> dict[str, Any]:
    """
    Build a JSON-serializable variation tree rooted at `root`.

    The tree is expressed as:
    - root: a "position" node with `children` (moves)
    - each move node contains its own `children` (moves from the resulting position)

    Each move node includes:
    - `path`: list[int] path from root through variations indices
    - `ply`, `moveNumber`, `color`, `san`, `uci`
    """

    def build_position(node: chess.pgn.GameNode, path: list[int]) -> dict[str, Any]:
        if node.ply() >= end_ply:
            children: list[dict[str, Any]] = []
        else:
            raw_children = list(session.variations(node))
            children = [
                build_move(child, [*path, idx])
                for idx, child in enumerate(raw_children)
                if child.ply() <= end_ply
            ]

        return {
            "path": path,
            "ply": node.ply(),
            "children": children,
        }

    def build_move(node: chess.pgn.GameNode, path: list[int]) -> dict[str, Any]:
        ply = node.ply()
        move_number = (ply + 1) // 2
        color = "white" if ply % 2 == 1 else "black"

        children: list[dict[str, Any]] = []
        if ply < end_ply:
            raw_children = list(session.variations(node))
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
            "children": children,
        }

    return build_position(root, [])


def node_at_path(
    session: RepertoireSession,
    root: chess.pgn.GameNode,
    path: list[int],
    *,
    end_ply: int,
) -> chess.pgn.GameNode:
    node: chess.pgn.GameNode = root
    for idx in path:
        if node.ply() >= end_ply:
            raise ValueError(f"Path exceeds end of range at ply {node.ply()}")
        children = list(session.variations(node))
        if idx < 0 or idx >= len(children):
            raise ValueError(f"Invalid path index {idx} at ply {node.ply()}")
        node = children[idx]
    return node


def path_from_root(session: RepertoireSession, root: chess.pgn.GameNode, node: chess.pgn.GameNode) -> list[int]:
    if node is root:
        return []

    path: list[int] = []
    cur: chess.pgn.GameNode = node
    while cur is not root:
        parent = getattr(cur, "parent", None)
        if parent is None:
            raise ValueError("Node is not a descendant of the current tree root")

        siblings = list(session.variations(parent))
        try:
            idx = siblings.index(cur)
        except ValueError as exc:
            raise ValueError("Node is not reachable via session.variations()") from exc

        path.append(idx)
        cur = parent

    path.reverse()
    return path

