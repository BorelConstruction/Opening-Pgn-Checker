from typing import NamedTuple, Union, Optional

import chess
from chess.pgn import GameNode as Node
from chess import WHITE
from chess import BLACK


from .traversal import TraversalPolicy, traverse, propagator_post
from .options import DEBUG_MODE


BoardLike = Node | chess.Board | str


class MoveIdentity(NamedTuple):
    turn: chess.Color
    piece_type: int
    from_square: int
    to_square: int
    promotion: Optional[int]
    is_capture: bool


_PIECE_SYMBOLS = {
    "K": chess.KING,
    "Q": chess.QUEEN,
    "R": chess.ROOK,
    "B": chess.BISHOP,
    "N": chess.KNIGHT,
}
_PROMOTION_SYMBOLS = {
    "Q": chess.QUEEN,
    "R": chess.ROOK,
    "B": chess.BISHOP,
    "N": chess.KNIGHT,
}
_FILES = "abcdefgh"
_RANKS = "12345678"


_LEGACY_FIGURINE_TRANSLATION = str.maketrans(
    {
        "\ue024": "♔",
        "\ue025": "♕",
        "\ue026": "♖",
        "\ue027": "♗",
        "\ue028": "♘",
    }
)

def to_board(position: BoardLike) -> chess.Board:
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


def normalize_figurine(text: str) -> str:
    return text.translate(_LEGACY_FIGURINE_TRANSLATION)


def update_comment(node: Node, message: str, debug=False):
    if not debug or (debug and DEBUG_MODE):
        node.comment = (node.comment + " " + message).lstrip()

def fen(board: BoardLike) -> str:
    # ALL FENS SHOULD COME FROM THIS FUNCTION
    # or subtle bugs will arise
    # good enough as long as it's a solo project
    if isinstance(board, Node):
        board = board.board()
    if isinstance(board, chess.Board):
        board = board.fen()
    return fen_essential_part(board)

def fen_essential_part(fen: str) -> str:
    return ' '.join(fen.strip().split()[:4])

def side(fen: str) -> chess.Color:
    try:
        side = fen.split(' ')[1]
        return WHITE if side == 'w' else BLACK
    except IndexError:
        return "Invalid FEN format"
    
def whole_move_from_ply(ply: int) -> str:
    if ply % 2 == 0:
        return str(ply // 2) + "..."
    return str(ply // 2 + 1) + "."

def arrow_from_uci(uci: str, *args, **kwargs) -> chess.svg.Arrow:
    return chess.svg.Arrow(ord(uci[0])-97 + 8*(int(uci[1])-1), ord(uci[2])-97 + 8*(int(uci[3])-1), *args, **kwargs)

def uci_from_lichess_to_pgn(uci: str) -> str:
    if uci == 'e1h1':
        return 'e1g1'
    if uci == 'e8h8':
        return 'e8g8'
    if uci == 'e1a1':
        return 'e1c1'
    if uci == 'e8a8':
        return 'e8c8'
    return uci

def uci_from_pgn_to_lichess(uci: str) -> str:
    if uci == 'e1g1':
        return 'e1h1'
    if uci == 'e8g8':
        return 'e8h8'
    if uci == 'e1c1':
        return 'e1a1'
    if uci == 'e8c8':
        return 'e8a8'
    return uci

def find_node_by_position(node: Node, fen_str: str) -> Node:
    def visit(n: Node):
        if fen(n).startswith(fen_essential_part(fen_str)): # ==
            return n
        
    n = traverse(node, visit=visit, reasons_to_stop=lambda _, res: res is not None, post=propagator_post)
    if not n:
        raise ValueError(f"Starting position {fen_str} not found in the tree")
    return n

def opposite_side(side: chess.Color) -> chess.Color:
    return WHITE if side == BLACK else BLACK

def node_san(n: Node, move: Optional[Union[chess.Move, str]] = None) -> str:
    if move is not None:
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        b = n.board()
        return b.san(move)
    b = n.parent.board()
    return b.san(n.move)


def _is_square_name(value: str) -> bool:
    return len(value) == 2 and value[0] in _FILES and value[1] in _RANKS


def _parse_square_name(value: str) -> int:
    if not _is_square_name(value):
        raise ValueError(f"Invalid square: {value!r}")
    return chess.parse_square(value)


def _capture_split(body: str) -> tuple[str, str, bool]:
    capture_mark_count = body.count("x") + body.count(":")
    if capture_mark_count > 1:
        raise ValueError(f"Move notation has multiple capture markers: {body!r}")
    if capture_mark_count == 0:
        return body, "", False

    marker = "x" if "x" in body else ":"
    left, right = body.split(marker)
    if not left or not right:
        raise ValueError(f"Capture notation must include both sides: {body!r}")
    return left, right, True


def _parse_search_color(value: str) -> chess.Color:
    normalized = value.strip().lower()
    if normalized in ("w", "white"):
        return chess.WHITE
    if normalized in ("b", "black"):
        return chess.BLACK
    raise ValueError(f"Invalid move color: {value!r}")


def _infer_pawn_color(from_square: int, to_square: int) -> chess.Color:
    rank_delta = chess.square_rank(to_square) - chess.square_rank(from_square)
    if rank_delta in (1, 2):
        return chess.WHITE
    if rank_delta in (-1, -2):
        return chess.BLACK
    raise ValueError("Cannot infer pawn color from the supplied origin and destination")


def _infer_promotion_color(to_square: int) -> chess.Color:
    to_rank = chess.square_rank(to_square)
    if to_rank == 7:
        return chess.WHITE
    if to_rank == 0:
        return chess.BLACK
    raise ValueError("Cannot infer pawn color from a promotion outside the first or eighth rank")


def _infer_pawn_origin(from_file: int, to_square: int, turn: chess.Color) -> int:
    to_rank = chess.square_rank(to_square)
    from_rank = to_rank - 1 if turn == chess.WHITE else to_rank + 1
    if from_rank < 0 or from_rank > 7:
        to_name = chess.square_name(to_square)
        color_name = "white" if turn == chess.WHITE else "black"
        raise ValueError(f"Cannot infer {color_name} pawn origin for {to_name}")
    return chess.square(from_file, from_rank)


def _parse_move_origin(origin: str, piece_type: int, to_square: int, turn: chess.Color) -> int:
    if _is_square_name(origin):
        return chess.parse_square(origin)
    if len(origin) == 1 and origin in _FILES and piece_type == chess.PAWN:
        return _infer_pawn_origin(_FILES.index(origin), to_square, turn)
    raise ValueError(f"Invalid move origin: {origin!r}")


def parse_move_search_notation(notation: str) -> MoveIdentity:
    """Produce a MoveIdentity object from search notation."""
    parts = notation.strip().split()
    if not parts:
        raise ValueError("Move notation is required")
    if len(parts) > 2:
        raise ValueError('Move notation must contain a move and optional color, e.g. "Nc3xd5 W"')

    text = parts[0].replace("-", "").replace("=", "")
    if not text:
        raise ValueError("Move notation is required")
    turn: chess.Color | None = _parse_search_color(parts[1]) if len(parts) == 2 else None

    piece_type = chess.PAWN
    body = text
    first = body[0]
    if first in _PIECE_SYMBOLS:
        piece_type = _PIECE_SYMBOLS[first]
        body = body[1:]
    elif first == "p" or first == "P":
        body = body[1:]

    promotion = None
    if body and body[-1] in _PROMOTION_SYMBOLS:
        promotion = _PROMOTION_SYMBOLS[body[-1]]
        body = body[:-1]
        if piece_type != chess.PAWN:
            raise ValueError("Only pawn moves can include a promotion")

    if not body:
        raise ValueError(f"Move notation is missing squares: {notation!r}")

    left, right, is_capture = _capture_split(body)
    if is_capture:
        to_square = _parse_square_name(right)
        if turn is None:
            if piece_type != chess.PAWN:
                raise ValueError("Move notation must include a color")
            if _is_square_name(left):
                from_square = chess.parse_square(left)
                turn = _infer_pawn_color(from_square, to_square)
            elif promotion is not None:
                turn = _infer_promotion_color(to_square)
                from_square = _parse_move_origin(left, piece_type, to_square, turn)
            else:
                raise ValueError("Move notation must include a color")
        else:
            from_square = _parse_move_origin(left, piece_type, to_square, turn)
    elif _is_square_name(left):
        if turn is None:
            if piece_type != chess.PAWN or promotion is None:
                raise ValueError("Move notation must include a color")
            turn = _infer_promotion_color(chess.parse_square(left))
        to_square = chess.parse_square(left)
        if piece_type != chess.PAWN:
            raise ValueError(f"Move notation is missing a square: {notation!r}")
        from_square = _infer_pawn_origin(chess.square_file(to_square), to_square, turn)
    elif len(left) == 4 and _is_square_name(left[:2]) and _is_square_name(left[2:]):
        from_square = chess.parse_square(left[:2])
        to_square = chess.parse_square(left[2:])
        if turn is None:
            if piece_type != chess.PAWN:
                raise ValueError("Move notation must include a color")
            turn = _infer_pawn_color(from_square, to_square)
    else:
        raise ValueError(f"Invalid move notation: {notation!r}")

    if turn is None:
        raise RuntimeError("Move color was not resolved")

    return MoveIdentity(
        turn=turn,
        piece_type=piece_type,
        from_square=from_square,
        to_square=to_square,
        promotion=promotion,
        is_capture=is_capture,
    )


def move_identity(node: Node) -> MoveIdentity:
    move = node.move
    parent = node.parent
    if move is None or parent is None:
        raise ValueError("Cannot compare a move for a root node")

    board = parent.board()
    moving_piece = board.piece_at(move.from_square)
    if moving_piece is None:
        from_name = chess.square_name(move.from_square)
        raise RuntimeError(f"No piece on {from_name} before node move {move.uci()!r}")

    return MoveIdentity(
        turn=board.turn,
        piece_type=moving_piece.piece_type,
        from_square=move.from_square,
        to_square=move.to_square,
        promotion=move.promotion,
        is_capture=board.is_capture(move),
    )


def moves_are_equal(node1: Node, node2: Node) -> bool:
    return move_identity(node1) == move_identity(node2)


def uci_to_san(uci: str, board: chess.Board) -> str:
    return board.san(chess.Move.from_uci(uci))

def count_nodes(root_node, tp: Optional[TraversalPolicy] = None):
    count = 0
    def visit(ply):
        nonlocal count
        count += 1
    traverse(root_node, visit, tp=tp)
    return count

class FirstDifference(NamedTuple):
    ply: int
    # move: chess.Move
    move: str # san

def node_moves(n: Node, san: bool = True) -> list[str]:
    stack: list[str] = []
    cur = n
    while cur is not None and getattr(cur, "move", None) is not None:
        stack.append(node_san(cur) if san else cur.move.uci())
        cur = cur.parent
    stack.reverse()
    return stack

def first_difference(n1: Node, n2: Node) -> Optional[FirstDifference]:
    """
    Compare two move sequences ending at 'n1' and 'n2' and return the first move
    that differs in 'n1', together with the ply at which it occurs.

    If 'n2' is a prefix of 'n1', returns the next move from 'n1'.
    If there is no differing move in 'n1' (identical lines, or 'n1' is shorter),
    returns None.
    """
    stack1 = node_moves(n1)
    stack2 = node_moves(n2)
    
    common_len = min(len(stack1), len(stack2))
    for i in range(common_len):
        if stack1[i] != stack2[i]:
            return FirstDifference(i + 1, stack1[i])

    if len(stack1) > len(stack2):
        i = len(stack2)
        return FirstDifference(i + 1, stack1[i])

    return None

def board_seen_in_ancestors(board: chess.Board, node: chess.pgn.GameNode) -> bool:
    """
    Return True if 'board' matches the position at 'node' or any of its ancestors.
    """
    current_board = node.board()
    current = node
    while True:
        if current_board == board:
            return True
        if current.parent is None:
            return False
        current_board.pop()
        current = current.parent

def moves_to_algebraic(moves: list[str]) -> str:
    pairs = [
        f"{i + 1}. {' '.join(moves[i * 2:(i + 1) * 2])}"
        for i in range((len(moves) + 1) // 2)
    ]
    return ' '.join(pairs)

def ply_from_move_number(move_number: int) -> int:
    return move_number * 2 - 1

def child_by_uci(node: Node, uci: str):
    return next((c for c in node.variations if c.move.uci() == uci), None)

ERROR_NAGS = {
    chess.pgn.NAG_MISTAKE,       # $2
    chess.pgn.NAG_BLUNDER,       # $4 
    chess.pgn.NAG_DUBIOUS_MOVE,  # $6 
}

def move_is_marked_as_error(node: Node) -> bool:
    return bool(node.nags & ERROR_NAGS)

def graft_game(node: Node, pgn_str: str, comment: str | None = None) -> None:
    """
    Append game's moves as a variation on the node. Returns True if successful.
    """
    import io
    master_game = chess.pgn.read_game(io.StringIO(pgn_str))
    if master_game is None:
        return

    current = master_game
    for _ in range(node.ply()):
        current = current.variations[0]

    dst = node
    while current.variations:
        current = current.variations[0]
        child_in_file = child_by_uci(dst, current.move.uci())
        dst = child_in_file if child_in_file else dst.add_variation(current.move)

    dst.comment = comment
    pass
