from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import chess
import chess.pgn

from ..core.repertoire import RepertoireSession


@dataclass(frozen=True)
class ExportedPgn:
    fen: str
    pgn: str
    initial_ply: int
    skipped_illegal_moves: int
    first_illegal_move: Optional[str] = None


def export_pgn_subtree(
    session: RepertoireSession,
    root: chess.pgn.GameNode,
    *,
    end_ply: int,
    include_comments: bool = True,
    prefer_mainline_path: list[int] | None = None,
) -> ExportedPgn:
    """
    Export the PGN subtree rooted at `root` as a standalone PGN string.

    The returned PGN starts from `root.board()` (returned as `fen`), and includes
    all variations selected by `session.variations(...)` up to `end_ply`.
    """

    start_board = root.board()
    start_fen = start_board.fen()

    game = chess.pgn.Game()
    game.setup(start_board)

    work_board = start_board.copy(stack=False)

    skipped_illegal_moves = 0
    first_illegal_move: Optional[str] = None
    preferred_ply = 0

    def copy_children(
        src: chess.pgn.GameNode,
        dst: chess.pgn.GameNode,
        board: chess.Board,
        *,
        depth: int,
        on_preferred_line: bool,
    ) -> None:
        nonlocal skipped_illegal_moves, first_illegal_move, preferred_ply

        if src.ply() >= end_ply:
            return

        children: list[tuple[int, chess.pgn.GameNode]] = list(enumerate(session.variations(src)))
        preferred_idx: int | None = None
        if on_preferred_line and prefer_mainline_path is not None and depth < len(prefer_mainline_path):
            preferred_idx = prefer_mainline_path[depth]
            children.sort(key=lambda item: 0 if item[0] == preferred_idx else 1)

        for idx, child in children:
            if child.ply() > end_ply:
                continue

            move = child.move
            if move is None:
                skipped_illegal_moves += 1
                if first_illegal_move is None:
                    first_illegal_move = f"missing move at ply {child.ply()}"
                continue

            if not board.is_legal(move):
                skipped_illegal_moves += 1
                if first_illegal_move is None:
                    first_illegal_move = f"illegal {move.uci()} in {board.fen()}"
                continue

            dst_child = dst.add_variation(move)
            if include_comments:
                dst_child.comment = getattr(child, "comment", "") or ""

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
        skipped_illegal_moves=skipped_illegal_moves,
        first_illegal_move=first_illegal_move,
    )
