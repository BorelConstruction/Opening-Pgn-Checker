

from collections import namedtuple
from collections.abc import Callable

import chess
from chess.pgn import GameNode as Node
from chess import WHITE

TraversalPolicy = namedtuple("TraversalPolicy", ["start_ply", "end_ply", "check_alternatives", "get_children"], 
                             defaults=(0, 1000, False, lambda n: n.variations))

def traverse(node: Node,
                visit: Callable = None,
                post: Callable = None,
                reasons_to_stop: Callable = None,
                tp: TraversalPolicy = TraversalPolicy(),
                side: chess.Color = WHITE,
                progress = None):
    start_ply, end_ply, check_alternatives, get_children = tp

    child_results = []

    v_res = None
    if visit and start_ply <= node.ply() <= end_ply:
        v_res = visit(node)
        if progress:
            progress.step()
            # node.comment += f"Step {s}"

    if reasons_to_stop:
        if reasons_to_stop(node, v_res):
            return child_results

    if node.ply() == end_ply:
        return child_results

    vars = node.variations
    if node.turn()==side and not check_alternatives:
        vars = vars[:1]

    for n in vars:
        child_results += traverse(n, visit, post,
            reasons_to_stop, tp, side, progress)
        pass

    if post:
        if start_ply <= node.ply() <= end_ply:
            if progress:
                progress.step()
        return post(node, child_results)
    return child_results