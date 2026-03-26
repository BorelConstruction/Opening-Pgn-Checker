from typing import Union

import chess
from chess.pgn import GameNode as Node
from chess import WHITE
from chess import BLACK


from .traversal import traverse
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

def find_node_by_position(node: Node, fen: str) -> Node:
    def visit(n: Node):
        if fen(n).startswith(fen_essential_part(fen)): # ==
            return n
        
    n = traverse(node, visit=visit, reasons_to_stop=lambda _, res: res is not None, post=visit)
    if not n:
        raise ValueError(f"Starting position {fen} not found in the tree")
    return n

def opposite_side(side: chess.Color) -> chess.Color:
    return WHITE if side == BLACK else BLACK