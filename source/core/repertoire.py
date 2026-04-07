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

