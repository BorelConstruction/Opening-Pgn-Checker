from typing import NamedTuple, Union, Optional

import chess
from chess.pgn import GameNode as Node
from chess import WHITE
from chess import BLACK


from .traversal import traverse, propagator_post
from .options import DEBUG_MODE

def update_comment(node: Node, message: str, debug=False):
    if not debug or (debug and DEBUG_MODE):
        node.comment = (node.comment + " " + message).lstrip()

def fen(board: Union[Node, chess.Board, str]) -> str:
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
    return str(ply // 2) + "."

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

def node_san(n: Node) -> str:
    b = n.parent.board()
    return b.san(n.move)

def uci_to_san(uci: str, board: chess.Board) -> str:
    return board.san(chess.Move.from_uci(uci))

class FirstDifference(NamedTuple):
    ply: int
    # move: chess.Move
    move: str

def node_moves(n: Node) -> list[chess.Move]:
    stack: list[chess.Move] = []
    cur = n
    while cur is not None and getattr(cur, "move", None) is not None:
        stack.append(node_san(cur))
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
    stack1: list[chess.Move] = []
    stack2: list[chess.Move] = []

    stack1 = node_moves(n1)
    stack2 = node_moves(n2)

    stack1.reverse()
    stack2.reverse()

    common_len = min(len(stack1), len(stack2))
    for i in range(common_len):
        if stack1[i] != stack2[i]:
            return FirstDifference(i + 1, stack1[i])

    if len(stack1) > len(stack2):
        i = len(stack2)
        return FirstDifference(i + 1, stack1[i])

    return None

def moves_to_algebraic(moves: list[str]) -> str:
    pairs = [
        f"{i + 1}. {' '.join(moves[i * 2:(i + 1) * 2])}"
        for i in range((len(moves) + 1) // 2)
    ]
    return ' '.join(pairs)

def ply_from_move_number(move_number: int) -> int:
    return move_number * 2 - 1