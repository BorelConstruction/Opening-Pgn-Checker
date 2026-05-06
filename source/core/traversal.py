

from collections import defaultdict, namedtuple
from collections.abc import Callable

import chess
from chess.pgn import GameNode as Node

from source.core.boardtools import fen

# from source.core.runner import Progress


def default_children(node):
    return node.variations

def mainline_children(sides: tuple[chess.Color]) -> Callable[[Node], list[Node]]:
    def get_children(node):
        if node.turn() in sides:
            return node.variations[:1]
        return node.variations
    return get_children

def propagator_post(node, child_results, v_res):
    for i in child_results:
        if i:
            return i

TraversalPolicy = namedtuple("TraversalPolicy", ["start_ply", "end_ply", "get_children"], 
                             defaults=(0, 1000, default_children))

def traverse(node: Node,
                visit: Callable = None,
                post: Callable = None,
                reasons_to_stop: Callable = None,
                tp: TraversalPolicy = None,
                progress: 'Progress' = None,
                _seen=None):
    if tp is None:
        tp = TraversalPolicy()
    start_ply, end_ply, get_children = tp

    if _seen is None:
        _seen = set()

    # avoid infinite loops in case of cycles in the graph (e.g. due to transposition teleporting)
    key = id(node)
    if key in _seen:
        return
    _seen.add(key)

    child_results = []

    v_res = None
    if visit and start_ply <= node.ply() <= end_ply:
        v_res = visit(node)
        if progress:
            progress.step()
            # node.comment += f"Step {s}"

    if reasons_to_stop:
        if reasons_to_stop(node, v_res):
            return v_res

    if node.ply() == end_ply:
        return v_res

    variations = get_children(node)

    for n in variations:
        child_results.append(traverse(n, visit, post,
            reasons_to_stop, tp, progress, _seen))

    if post:
        if start_ply <= node.ply() <= end_ply:
            if progress:
                progress.step()
        return post(node, child_results, v_res)
    return v_res

def iter_nodes(node: Node, tp: TraversalPolicy = None):
    if tp is None:
        tp = TraversalPolicy()
    start_ply, end_ply, get_children = tp

    if start_ply <= node.ply() <= end_ply:
        yield node

    if node.ply() == end_ply:
        return

    variations = get_children(node)

    for n in variations:
        yield from iter_nodes(n, tp)