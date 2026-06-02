from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import chess
import chess.pgn

from ..core.repertoire import RepertoireSession


@dataclass(frozen=True)
class ExportedPgn:
    fen: str
    pgn: str
    initial_ply: int


def export_pgn_subtree(
    session: RepertoireSession,
    root: chess.pgn.GameNode,
    *,
    end_ply: int,
    include_comments: bool = True,
    prefer_mainline_path: list[int] | None = None,
    get_children: Callable[[chess.pgn.GameNode], list[chess.pgn.GameNode]] | None = None,
) -> ExportedPgn:
    """
    Export the PGN subtree rooted at 'root' as a standalone PGN string.

    The returned PGN starts from 'root.board()' (returned as 'fen'), and includes
    all variations selected by 'get_children' or 'session.variations(...)'.
    """

    start_board = root.board()
    start_fen = start_board.fen()

    game = chess.pgn.Game()
    game.setup(start_board)

    work_board = start_board.copy(stack=False)

    preferred_ply = 0
    children_for = get_children or session.variations

    def copy_children(
        src: chess.pgn.GameNode,
        dst: chess.pgn.GameNode,
        board: chess.Board,
        *,
        depth: int,
        on_preferred_line: bool,
    ) -> None:
        nonlocal preferred_ply

        if src.ply() >= end_ply:
            return

        children: list[tuple[int, chess.pgn.GameNode]] = [
            (idx, child)
            for idx, child in enumerate(children_for(src))
            if child.ply() <= end_ply
        ]
        preferred_idx: int | None = None
        if on_preferred_line and prefer_mainline_path is not None and depth < len(prefer_mainline_path):
            preferred_idx = prefer_mainline_path[depth]
            children.sort(key=lambda item: 0 if item[0] == preferred_idx else 1)

        for idx, child in children:

            move = child.move

            if include_comments:
                dst_child = dst.add_variation(
                    move,
                    comment=getattr(child, "comment", "") or "",
                    starting_comment=getattr(child, "starting_comment", "") or "",
                    nags=sorted(getattr(child, "nags", set()) or ()),
                )
            else:
                dst_child = dst.add_variation(move)

            next_on_preferred = bool(on_preferred_line and preferred_idx is not None and idx == preferred_idx)
            if next_on_preferred and depth + 1 > preferred_ply:
                preferred_ply = depth + 1

            board.push(move)
            copy_children(
                child,
                dst_child,
                board,
                depth=depth + 1,
                on_preferred_line=next_on_preferred,
            )
            board.pop()

    copy_children(root, game, work_board, depth=0, on_preferred_line=True)

    exporter = chess.pgn.StringExporter(headers=False, variations=True, comments=include_comments)
    pgn_text = game.accept(exporter).strip()
    return ExportedPgn(
        fen=start_fen,
        pgn=pgn_text,
        initial_ply=preferred_ply,
    )
