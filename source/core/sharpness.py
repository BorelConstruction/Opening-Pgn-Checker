from __future__ import annotations

import argparse
import datetime
from dataclasses import dataclass, field
import os
import re
from typing import Optional

import chess
import chess.polyglot
from chess.pgn import GameNode as Node

from .boardtools import fen, BoardLike, to_board
from .options import RepertoireOptions
from .runner import PgnSession


DEFAULT_CAPTURE_WINDOW_FULL_MOVES = 2


@dataclass
class SharpnessOptions(RepertoireOptions):
    output_pgn: str = field(
        default="",
        metadata={"label": "Output PGN Filename", "ui_hint": "save_file"},
    )
    full_moves: int = field(
        default=DEFAULT_CAPTURE_WINDOW_FULL_MOVES,
        metadata={"label": "Capture Window (Full Moves)", "min": 1, "max": 20},
    )


def default_sharpness_cache_path(options: SharpnessOptions) -> str:
    input_stem = os.path.splitext(os.path.basename(options.input_pgn))[0]
    return os.path.join("cache", "sharpness", f"{input_stem}.json")


def default_sharpness_output_path(input_pgn: str) -> str:
    output_dir = "output pgns"
    input_stem = os.path.splitext(os.path.basename(input_pgn))[0]
    timestamp = datetime.datetime.now().strftime("%d-%m_%H-%M-%S")
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, f"{input_stem} -- sharpness -- {timestamp}.pgn")


def sharpness_comment_pattern(full_moves: int) -> re.Pattern[str]:
    return re.compile(rf"Sharpness:\s*\d+")


def set_sharpness_comment(node: Node, value: int, *, full_moves: int) -> None:
    metric = f"Sharpness: {value}"
    existing_comment = node.comment or ""
    cleaned_comment = sharpness_comment_pattern(full_moves).sub("", existing_comment).strip()
    node.comment = f"{cleaned_comment} {metric}".strip()


def sharpness(
    position: BoardLike,
    *,
    full_moves: int = DEFAULT_CAPTURE_WINDOW_FULL_MOVES,
    cache: Optional[dict[tuple[int, int], int]] = None,
) -> int:
    memo = {} if cache is None else cache
    board = to_board(position)
    return _sharpness_from_board(board, remaining_plies=2 * full_moves, cache=memo)


def _sharpness_from_board(
    board: chess.Board,
    *,
    remaining_plies: int,
    cache: dict[tuple[int, int], int],
) -> int:
    if remaining_plies <= 0 or board.is_game_over():
        return 0

    if remaining_plies == 1:
        return sum(1 for _ in board.generate_legal_captures())

    cache_key = (chess.polyglot.zobrist_hash(board), remaining_plies)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    total = 0
    for move in board.legal_moves:
        total += int(board.is_capture(move))
        board.push(move)
        total += _sharpness_from_board(
            board,
            remaining_plies=remaining_plies - 1,
            cache=cache,
        )
        board.pop()

    cache[cache_key] = total
    return total


def annotate_capture_sharpness(
    session: PgnSession,
    root: Node,
    *,
    full_moves: int = DEFAULT_CAPTURE_WINDOW_FULL_MOVES,
) -> int:
    nodes_annotated = 0
    cache: dict[tuple[int, int], int] = {}

    def visit(node: Node) -> None:
        nonlocal nodes_annotated
        value = sharpness(node, full_moves=full_moves, cache=cache)
        set_sharpness_comment(node, value, full_moves=full_moves)
        nodes_annotated += 1

    session.report_message(f"Annotating sharpness over the next {full_moves} full moves...")
    session.progress.reset()
    session.traverse(root, visit=visit, get_children=lambda node: node.variations)
    return nodes_annotated


def save_game_to_pgn(game: Node, output_pgn: str) -> None:
    output_dir = os.path.dirname(output_pgn)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_pgn, "w", encoding="utf-8") as output_file:
        print(game, file=output_file, end="\n\n")


def annotate_pgn_sharpness(options: SharpnessOptions) -> tuple[str, int]:
    output_pgn = options.output_pgn or default_sharpness_output_path(options.input_pgn)

    with PgnSession(
        options,
        default_cache_path=lambda: default_sharpness_cache_path(options),
    ) as session:
        annotated_nodes = annotate_capture_sharpness(
            session,
            session.game,
            full_moves=options.full_moves,
        )
        save_game_to_pgn(session.game, output_pgn)
        session.save_cache()

    return output_pgn, annotated_nodes


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Annotate each PGN node with a simple sharpness metric."
    )
    parser.add_argument("input_pgn", help="Path to the PGN file to annotate.")
    parser.add_argument(
        "-o",
        "--output",
        dest="output_pgn",
        help="Where to write the annotated PGN. Defaults to output pgns/<name> -- sharpness -- <timestamp>.pgn",
    )
    parser.add_argument(
        "--full-moves",
        type=int,
        default=DEFAULT_CAPTURE_WINDOW_FULL_MOVES,
        help="How many full moves ahead to inspect when counting captures.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    cli_args = build_arg_parser().parse_args(argv)
    options = SharpnessOptions(
        input_pgn=cli_args.input_pgn,
        output_pgn=cli_args.output_pgn or "",
        full_moves=cli_args.full_moves,
    )
    output_pgn, annotated_nodes = annotate_pgn_sharpness(options)
    print(f"Annotated {annotated_nodes} nodes -> {output_pgn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
