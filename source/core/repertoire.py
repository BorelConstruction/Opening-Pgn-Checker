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
    A `PgnSession` configured for repertoire-like features.

    - Sets `options.side` from `play_white`
    - Converts `start_move`/`end_move` into `start_ply`/`end_ply`
    - Uses the same default cache naming as the PGN checker
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
        self.options.start_ply = ply_from_move_number(self.options.start_move)
        self.options.end_ply = ply_from_move_number(self.options.end_move)

