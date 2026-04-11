

from collections import namedtuple
from collections.abc import Callable

import chess
from chess.pgn import GameNode as Node


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
                progress = None):
    if tp is None:
        tp = TraversalPolicy()
    start_ply, end_ply, get_children = tp

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
            reasons_to_stop, tp, progress))

    if post:
        if start_ply <= node.ply() <= end_ply:
            if progress:
                progress.step()
        return post(node, child_results, v_res)
    return v_res