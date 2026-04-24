from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Union

import chess
from chess.pgn import GameNode as Node

from .boardtools import fen

BoardLike = Union[Node, chess.Board, str]

CASTLING_RIGHTS = (
    chess.BB_H1,
    chess.BB_A1,
    chess.BB_H8,
    chess.BB_A8,
)


@dataclass(frozen=True)
class PositionSimilarity:
    distance: float
    similarity: float
    piece_distance: float
    castling_distance: float
    en_passant_distance: float
    turn_distance: float
    piece_count_a: int
    piece_count_b: int


def compare_positions(a: BoardLike, b: BoardLike) -> PositionSimilarity:
    board_a = _to_board(a)
    board_b = _to_board(b)

    piece_distance = _piece_distance(board_a, board_b)
    castling_distance = _castling_distance(board_a, board_b)
    en_passant_distance = _en_passant_distance(board_a, board_b)
    turn_distance = 0.0 if board_a.turn == board_b.turn else 1.0

    distance = piece_distance + castling_distance + en_passant_distance + turn_distance
    similarity = 1.0 / (distance + 1.0)

    return PositionSimilarity(
        distance=distance,
        similarity=similarity,
        piece_distance=piece_distance,
        castling_distance=castling_distance,
        en_passant_distance=en_passant_distance,
        turn_distance=turn_distance,
        piece_count_a=len(board_a.piece_map()),
        piece_count_b=len(board_b.piece_map()),
    )


def _to_board(position: BoardLike) -> chess.Board:
    if isinstance(position, Node):
        return position.board()
    if isinstance(position, chess.Board):
        return position.copy(stack=False)
    if isinstance(position, str):
        normalized_fen = fen(position)
        if len(normalized_fen.split()) == 4:
            normalized_fen = f"{normalized_fen} 0 1"
        return chess.Board(normalized_fen)
    raise TypeError(f"Unsupported position type: {type(position)!r}")


def _piece_distance(a: chess.Board, b: chess.Board) -> float:
    raw_square_difference = 0
    for square in chess.SQUARES:
        piece_a = a.piece_at(square)
        piece_b = b.piece_at(square)
        if piece_a == piece_b:
            continue
        if piece_a is None or piece_b is None:
            raw_square_difference += 1
            continue
        raw_square_difference += 2

    average_piece_count = (len(a.piece_map()) + len(b.piece_map())) / 2.0
    scale = max(1.0, average_piece_count)
    return (raw_square_difference / 2.0) / scale


def _castling_distance(a: chess.Board, b: chess.Board) -> float:
    different_rights = 0
    for rook_square in CASTLING_RIGHTS:
        if (a.castling_rights & rook_square) == (b.castling_rights & rook_square):
            continue
        different_rights += 1
    return different_rights / len(CASTLING_RIGHTS)


def _en_passant_distance(a: chess.Board, b: chess.Board) -> float:
    return 0.0 if a.ep_square == b.ep_square else 0.5
