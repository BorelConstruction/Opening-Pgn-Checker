from __future__ import annotations

from typing import Any, Callable

from chess.pgn import GameNode as Node

from ..core.boardtools import node_san, fen
from .board.pgn_annotations import comment_text


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
            "comment": _node_comment(node),
            "children": children,
        }

    return build_position(root, [])


def _node_comment(node: Node) -> str:
    return comment_text(
        getattr(node, "starting_comment", "") or "",
        getattr(node, "comment", "") or "",
    )


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

