from __future__ import annotations

import os
from typing import Callable, Optional

import chess

from .boardtools import ply_from_move_number
from .options import RepertoireOptions
from .runner import PgnSession


def default_repertoire_cache_path(options: RepertoireOptions) -> str:
    base = "cache"
    name = "cache"
    if options.input_pgn:
        name = os.path.splitext(os.path.basename(options.input_pgn))[0]
    return os.path.join(base, f"{name}.json")

    # more rubust -- does not depend on the file naming
    with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
        game = chess.pgn.read_game(pgnFile)
    if game is None:
        raise ValueError(f"Failed to parse PGN: {self.options.input_pgn}")

    moves_uci: list[str] = []
    node: Node = game
    PLY_LIMIT = 20  # first 10 moves
    while node.variations and node.ply() < PLY_LIMIT:
        node = node.variations[0]
        moves_uci.append(node.move.uci())

    pgn_basename = os.path.basename(self.options.input_pgn)

    # Prefer an existing cache for a shorter prefix, so extending the PGN mainline
    # doesn't force a brand-new cache file.
    for prefix_len in range(min(PLY_LIMIT, len(moves_uci)), 8, -1):
        signature = f"{pgn_basename}|{' '.join(moves_uci[:prefix_len])}"
        candidate = cache_filename_from_string("pgn_checker", signature)
        if os.path.exists(candidate):
            return candidate

    signature = f"{pgn_basename}|{' '.join(moves_uci)}"
    return cache_filename_from_string("pgn_checker", signature)


class RepertoireSession(PgnSession):
    """
    A 'PgnSession' configured for repertoire-like features.

    Mainly, caching is determined by the line, not a single position.
    """

    def __init__(
        self,
        options: RepertoireOptions,
        progress_cb=None,
        report_cb=None,
        *,
        default_cache_path: Optional[Callable[[], str]] = None,
    ):
        options.side = chess.WHITE if options.play_white else chess.BLACK
        super().__init__(
            options,
            progress_cb=progress_cb,
            report_cb=report_cb,
            default_cache_path=default_cache_path or (lambda: default_repertoire_cache_path(options)),
        )
        self._convert_moves_to_plies()

    def _convert_moves_to_plies(self):
        o = self.options
        o.start_ply = ply_from_move_number(o.start_move)
        o.end_ply = ply_from_move_number(o.end_move)

