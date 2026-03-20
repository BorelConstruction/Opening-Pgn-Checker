"""
chess_similarity.py
-------------------
Position similarity metrics for chess opening repertoire tools.

Two positions that differ only by a pair of pawn pushes should score
close to 0 distance so the opening file generator can suggest the same
reply for both. Positions that differ by a developed piece or a moved
rook score meaningfully higher.

Requirements: python-chess  (pip install chess)
"""

import chess
import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Piece weights (centipawn-free, tuned for *structural* discrimination)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[chess.PieceType, float] = {
    chess.PAWN:   1.0,   # pawn differences are cheap — they're the noise
    chess.KNIGHT: 3.0,
    chess.BISHOP: 3.0,
    chess.ROOK:   5.0,
    chess.QUEEN:  9.0,
    chess.KING:   4.0,   # king safety zone differs → meaningful in openings
}

# Approximate upper bound used for normalization.
# In a real opening position ~32 pieces occupy ~32 squares; if every piece
# were on a different square the distance would be ≤ 32 * 9 = 288.
_NORMALIZE_SCALE = 32.0


# ---------------------------------------------------------------------------
# Core building blocks
# ---------------------------------------------------------------------------

def _piece_value(piece: Optional[chess.Piece],
                 weights: dict[chess.PieceType, float]) -> float:
    if piece is None:
        return 0.0
    return weights.get(piece.piece_type, 1.0)


def board_distance(
    board1: chess.Board,
    board2: chess.Board,
    weights: Optional[dict[chess.PieceType, float]] = None,
    color_sensitive: bool = True,
) -> float:
    """
    Weighted Hamming distance over the 64 squares.

    For each square that holds different content in the two positions,
    the contribution is max(value_in_pos1, value_in_pos2).  Using the
    maximum rather than the sum avoids double-counting: a pawn that
    moved from e2→e4 contributes ~1 (the vacated e2 + the occupied e4
    each differ by a pawn of value 1, but we charge one unit per
    logical piece, not per square flip).

    Parameters
    ----------
    board1, board2
        Parsed chess.Board objects.
    weights
        Override DEFAULT_WEIGHTS.  Set a piece type to 0 to ignore it
        entirely (e.g. PAWN→0 to test whether *non-pawn* structure is
        identical).
    color_sensitive
        If False, treat White Knight ≡ Black Knight for the purpose of
        comparison (useful if you want side-independent structural
        distance).  Rarely needed for opening tools.

    Returns
    -------
    float ≥ 0; 0 means identical piece placement.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS

    distance = 0.0
    for sq in chess.SQUARES:
        p1 = board1.piece_at(sq)
        p2 = board2.piece_at(sq)
        if p1 == p2:
            continue
        if not color_sensitive:
            # strip colour before comparing
            if (p1 and p2 and
                    p1.piece_type == p2.piece_type):
                continue
        v1 = _piece_value(p1, weights)
        v2 = _piece_value(p2, weights)
        distance += max(v1, v2)

    return distance


def pawn_structure_distance(
    board1: chess.Board,
    board2: chess.Board,
) -> float:
    """
    Distance considering *only* pawn placement.
    Useful as a lightweight structural fingerprint.
    """
    pawn_only = {pt: (1.0 if pt == chess.PAWN else 0.0)
                 for pt in chess.PIECE_TYPES}
    return board_distance(board1, board2, weights=pawn_only)


def castling_distance(board1: chess.Board, board2: chess.Board) -> float:
    """
    Small penalty for differing castling availability.
    Each missing right that one side has and the other doesn't costs 0.5.
    """
    diff = board1.castling_rights ^ board2.castling_rights
    # diff is a bitmask; count set bits
    return 0.5 * bin(diff).count("1")


# ---------------------------------------------------------------------------
# High-level combined metric
# ---------------------------------------------------------------------------

@dataclass
class DistanceConfig:
    """Tune the combined metric for your repertoire style."""
    piece_weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # How much the pawn structure matters *on top of* the general distance.
    # 0 = treat pawns like any other piece (already included via piece_weights).
    # >0 = add an extra pawn-structure term, good for double-checking
    #       transpositions that land in pawn structures you don't want.
    pawn_structure_bonus: float = 0.0
    castling_penalty: float = 0.5   # weight on castling_distance
    stm_penalty: float = 0.5        # penalty when side-to-move differs


def position_distance(
    fen1: str,
    fen2: str,
    cfg: Optional[DistanceConfig] = None,
    ignore_castling: bool = False,
    ignore_stm: bool = False,
) -> float:
    """
    Full combined distance between two FEN positions.

    Typical values in an opening (for orientation):
      0          – identical
      1–4        – differ by a pawn move or two (same reply suggested)
      4–10       – differ by a piece development move
      10–25      – clearly different opening lines
      25+        – very different positions

    Parameters
    ----------
    fen1, fen2      FEN strings (half/full move clocks are stripped).
    cfg             Fine-grained weighting (defaults to DistanceConfig()).
    ignore_castling Don't penalise differing castling rights.
    ignore_stm      Don't penalise different side to move.
    """
    if cfg is None:
        cfg = DistanceConfig()

    # Strip move-clock fields so FEN comparison is purely positional.
    b1 = chess.Board(_strip_clocks(fen1))
    b2 = chess.Board(_strip_clocks(fen2))

    d = board_distance(b1, b2, weights=cfg.piece_weights)

    if cfg.pawn_structure_bonus > 0:
        d += cfg.pawn_structure_bonus * pawn_structure_distance(b1, b2)

    if not ignore_castling:
        d += cfg.castling_penalty * castling_distance(b1, b2)

    if not ignore_stm and b1.turn != b2.turn:
        d += cfg.stm_penalty

    return d


def position_similarity(
    fen1: str,
    fen2: str,
    cfg: Optional[DistanceConfig] = None,
    scale: float = _NORMALIZE_SCALE,
) -> float:
    """
    Similarity score in [0, 1].

    Uses an exponential decay:   sim = exp(-distance / scale)

    scale controls the "half-life":
      - scale=8   → two positions 8 apart score ~0.37 (feels different)
      - scale=32  → two positions 8 apart score ~0.78 (feels similar)

    For opening book lookup, scale=8–16 works well:
    positions that differ by a pawn pair still score > 0.6,
    positions that differ by a piece development score ~0.3.
    """
    d = position_distance(fen1, fen2, cfg=cfg)
    return math.exp(-d / scale)


# ---------------------------------------------------------------------------
# Opening-book search helper
# ---------------------------------------------------------------------------

@dataclass
class Match:
    fen: str
    distance: float
    similarity: float

    def __repr__(self) -> str:
        return (f"Match(sim={self.similarity:.3f}, dist={self.distance:.1f}, "
                f"fen={self.fen!r})")


def find_similar(
    query_fen: str,
    database: list[str],
    top_k: int = 5,
    max_distance: float = 12.0,
    cfg: Optional[DistanceConfig] = None,
    scale: float = _NORMALIZE_SCALE,
) -> list[Match]:
    """
    Return the top-k most similar positions from *database*.

    Parameters
    ----------
    query_fen    The position you want replies for.
    database     List of FEN strings that have known repertoire moves.
    top_k        Maximum results to return.
    max_distance Filter out anything farther than this.
                 12 ≈ "one full piece development move apart".
    cfg          Metric configuration.
    scale        Similarity decay scale (see position_similarity).

    Returns
    -------
    List of Match objects, sorted by ascending distance.
    """
    results: list[Match] = []
    for fen in database:
        d = position_distance(query_fen, fen, cfg=cfg)
        if d <= max_distance:
            sim = math.exp(-d / scale)
            results.append(Match(fen=fen, distance=d, similarity=sim))

    results.sort(key=lambda m: m.distance)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Convenience: cluster a position list by pawn skeleton
# ---------------------------------------------------------------------------

def pawn_skeleton(board: chess.Board) -> int:
    """
    Compact integer key: XOR of White and Black pawn bitboards.
    Positions sharing the same skeleton are pawn-structure twins
    regardless of piece placement.
    """
    return int(board.pawns)


def group_by_pawn_skeleton(fens: list[str]) -> dict[int, list[str]]:
    """
    Bucket a list of FEN strings by their pawn skeleton.
    Within each bucket, only piece development differs → very similar
    positions from the opening generator's perspective.
    """
    groups: dict[int, list[str]] = {}
    for fen in fens:
        b = chess.Board(_strip_clocks(fen))
        key = pawn_skeleton(b)
        groups.setdefault(key, []).append(fen)
    return groups


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_clocks(fen: str) -> str:
    """Return FEN with half-move clock and full-move number removed."""
    parts = fen.split()
    return " ".join(parts[:4])   # piece placement, STM, castling, en passant


# ---------------------------------------------------------------------------
# Quick demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ruy López — standard position after 1.e4 e5 2.Nf3 Nc6 3.Bb5
    RUY = "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"

    # Same position but Black played ...a6 (Morphy Defence)
    MORPHY = "r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"

    # Italian Game — completely different pawn-pawn-pawn development
    ITALIAN = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"

    # Sicilian — very different from Ruy López
    SICILIAN = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    print("=== Chess Position Similarity Demo ===\n")

    pairs = [
        ("Ruy López vs Morphy (...a6)",     RUY,     MORPHY),
        ("Ruy López vs Italian (Bc4)",      RUY,     ITALIAN),
        ("Ruy López vs Sicilian",           RUY,     SICILIAN),
    ]

    cfg_opening = DistanceConfig(
        piece_weights={**DEFAULT_WEIGHTS, chess.PAWN: 1.0},
        castling_penalty=0.5,
        stm_penalty=0.5,
    )

    for label, fen_a, fen_b in pairs:
        d   = position_distance(fen_a, fen_b, cfg=cfg_opening)
        sim = position_similarity(fen_a, fen_b, cfg=cfg_opening, scale=16.0)
        print(f"{label}")
        print(f"  distance={d:.1f}   similarity={sim:.3f}\n")

    print("--- Pawn structure only ---")
    for label, fen_a, fen_b in pairs:
        b1, b2 = chess.Board(_strip_clocks(fen_a)), chess.Board(_strip_clocks(fen_b))
        print(f"{label}: pawn_dist={pawn_structure_distance(b1, b2):.1f}")

    print("\n--- find_similar demo ---")
    db = [MORPHY, ITALIAN, SICILIAN]
    matches = find_similar(RUY, db, max_distance=20.0, scale=16.0,
                           cfg=cfg_opening)
    for m in matches:
        print(f"  {m}")