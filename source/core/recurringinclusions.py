"""
Builds and visualises a move-relationship graph for identifying
recurrent inclusions.

Nodes  : moves (UCI strings)
Edges  : ma -> mb, weighted by the average conditional frequency of mb
         following ma, across all positions in the traversal where ma appears.

Abstract dependencies (injected at construction):
  get_children(board) -> list[chess.Move]
      Which moves to traverse from the current position. Could be top-N by DB
      frequency, or exactly the moves in an opening file.

  get_db_stats(board) -> dict[chess.Move, int]
      Raw game counts for each move played from this position in the DB.
"""

import chess
import collections
from typing import Callable

import networkx as nx
from pyvis.network import Network


# Type aliases
Board       = chess.Board
Move        = chess.Move
DbStats     = dict[Move, int]
GetChildren = Callable[[Board], list[Move]]
GetDbStats  = Callable[[Board], DbStats]


class InclusionGraph:

    def __init__(
        self,
        get_children: GetChildren,
        get_db_stats:  GetDbStats,
    ):
        self.get_children = get_children
        self.get_db_stats  = get_db_stats

        # For each edge (ma, mb): list of conditional frequencies observed
        # at each position where ma was a traversed child.
        self._edge_observations: dict[tuple[str, str], list[float]] = \
            collections.defaultdict(list)

        self.graph: nx.DiGraph = nx.DiGraph()

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def build(self, start_board: Board, depth: int) -> None:
        """
        Traverse the opening tree from start_board up to depth half-moves,
        accumulating edge observations.
        """
        self._edge_observations.clear()
        self.graph.clear()
        self._traverse(start_board, depth)
        self._finalise_edges()

    def _traverse(self, board: Board, depth: int) -> None:
        if depth == 0:
            return

        children: list[Move] = self.get_children(board)
        if not children:
            return

        stats: DbStats = self.get_db_stats(board)
        total = sum(stats.values()) if stats else 0

        for ma in children:
            board.push(ma)

            # --- record edges ma -> mb for every response mb in DB ---
            response_stats: DbStats = self.get_db_stats(board)
            response_total = sum(response_stats.values()) if response_stats else 0

            if response_total > 0:
                for mb, mb_count in response_stats.items():
                    conditional_freq = mb_count / response_total
                    self._edge_observations[
                        (ma.uci(), mb.uci())
                    ].append(conditional_freq)

            self._traverse(board, depth - 1)
            board.pop()

    def _finalise_edges(self) -> None:
        """Average observations and populate the networkx graph."""
        for (ma_uci, mb_uci), freqs in self._edge_observations.items():
            weight = sum(freqs) / len(freqs)
            self.graph.add_edge(ma_uci, mb_uci, weight=weight, n=len(freqs))

    # ------------------------------------------------------------------
    # Visualisation
    # ------------------------------------------------------------------

    def visualise(
        self,
        output_path: str = "inclusion_graph.html",
        min_weight: float = 0.0,
        min_observations: int = 1,
    ) -> None:
        """
        Write an interactive pyvis graph to output_path.

        min_weight        : hide edges below this average conditional freq
        min_observations  : hide edges seen at fewer than this many positions
        """
        net = Network(height="800px", width="100%", directed=True)
        net.toggle_physics(True)

        for ma_uci, mb_uci, data in self.graph.edges(data=True):
            if data["weight"] < min_weight:
                continue
            if data["n"] < min_observations:
                continue

            # Node labels: SAN would be nicer but we don't have board context
            # here; UCI is unambiguous and good enough for inspection.
            for node in (ma_uci, mb_uci):
                if node not in net.get_nodes():
                    net.add_node(node, label=node)

            width = 1 + 8 * data["weight"]   # thin=rare, thick=dominant
            title = f"{ma_uci} → {mb_uci}\nweight={data['weight']:.2f}  n={data['n']}"
            net.add_edge(ma_uci, mb_uci, width=width, title=title)

        net.show(output_path, notebook=False)
        print(f"Graph written to {output_path}")

    
    """
inclusion_graph_lichess.py
--------------------------
Concrete get_children / get_db_stats implementations for InclusionGraph,
backed by a PGN opening file and the Lichess opening explorer via berserk.
"""

import chess.pgn
import collections

import berserk


# ---------------------------------------------------------------------------
# PGN index: FEN (stripped) -> set of moves that appear in the file
# ---------------------------------------------------------------------------

from .database import safe_get_games

def _strip_clocks(fen: str) -> str:
    return " ".join(fen.split()[:4])


def build_pgn_index(
    pgn_path: str,
    start_ply: int = 0,
    end_ply: int = 999,
) -> dict[str, set[chess.Move]]:
    """
    Parse all games (and variations) in the PGN, recording for each position
    (identified by stripped FEN) which moves appear, filtered to the ply range.

    Returns dict: stripped_fen -> set of chess.Move
    """
    index: dict[str, set[chess.Move]] = collections.defaultdict(set)

    with open(pgn_path, encoding="utf-8") as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            _index_node(game, index, start_ply, end_ply)

    return dict(index)


def _index_node(
    node: chess.pgn.GameNode,
    index: dict[str, set[chess.Move]],
    start_ply: int,
    end_ply: int,
) -> None:
    """Recursively walk all variations in a PGN game node."""
    board = node.board()
    ply = board.ply()

    for child in node.variations:
        move = child.move
        # if start_ply <= ply < end_ply:
        key = _strip_clocks(board.fen())
        index[key].add(move)
        _index_node(child, index, start_ply, end_ply)


# ---------------------------------------------------------------------------
# Concrete get_children
# ---------------------------------------------------------------------------

def make_get_children(pgn_index: dict[str, set[chess.Move]]) -> GetChildren:
    """
    Returns a get_children function that looks up the PGN index.
    """
    def get_children(board: chess.Board) -> list[chess.Move]:
        key = _strip_clocks(board.fen())
        return list(pgn_index.get(key, []))

    return get_children


# ---------------------------------------------------------------------------
# Concrete get_db_stats
# ---------------------------------------------------------------------------

def make_get_db_stats(
    opening_explorer: berserk.clients.OpeningExplorer,
    safe_get_games,
    **kwargs,          # passed through to safe_get_games (ratings, speeds, etc.)
) -> GetDbStats:
    """
    Returns a get_db_stats function that queries the Lichess opening explorer.

    The berserk response looks like:
      {"moves": [{"uci": "e2e4", "white": 100, "draws": 50, "black": 30}, ...]}

    We sum white+draws+black as the game count for each move.
    """
    def get_db_stats(board: chess.Board) -> dict[chess.Move, int]:
        fen = board.fen()
        try:
            response = safe_get_games(opening_explorer, position=fen, **kwargs)
        except Exception as e:
            print(f"DB query failed for {fen}: {e}")
            return {}

        result = {}
        for entry in response.get("moves", []):
            try:
                move = chess.Move.from_uci(entry["uci"])
                count = entry.get("white", 0) + entry.get("draws", 0) + entry.get("black", 0)
                if count > 0:
                    result[move] = count
            except Exception:
                continue
        return result

    return get_db_stats


# ---------------------------------------------------------------------------
# Convenience: build the whole thing in one call
# ---------------------------------------------------------------------------

def build_inclusion_graph(
    pgn_path: str,
    opening_explorer: berserk.clients.OpeningExplorer,
    safe_get_games,
    start_fen: str,
    start_ply: int,
    end_ply: int,
    **db_kwargs,
) -> InclusionGraph:
    """
    Build and return a populated InclusionGraph.

    Parameters
    ----------
    pgn_path          Path to the opening file PGN.
    opening_explorer  berserk OpeningExplorer client.
    safe_get_games    The rate-limited wrapper around opening_explorer.get_lichess_games.
    start_fen         FEN of the root position to start traversal from.
    start_ply         Only include PGN moves at or after this ply.
    end_ply           Only include PGN moves before this ply.
    **db_kwargs       Passed to safe_get_games (e.g. ratings, speeds).
    """
    print("Indexing PGN...")
    pgn_index = build_pgn_index(pgn_path, start_ply=start_ply, end_ply=end_ply)
    print(f"  {len(pgn_index)} positions indexed.")

    get_children = make_get_children(pgn_index)
    get_db_stats  = make_get_db_stats(opening_explorer, safe_get_games, **db_kwargs)

    graph = InclusionGraph(get_children=get_children, get_db_stats=get_db_stats)

    start_board = chess.Board(start_fen) if start_fen else chess.Board()
    depth = end_ply - start_ply

    print(f"Building graph (depth={depth})...")
    graph.build(start_board, depth=depth)
    print(f"  {graph.graph.number_of_nodes()} nodes, {graph.graph.number_of_edges()} edges.")

    return graph


# ---------------------------------------------------------------------------
# Test / demo entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    # --- configuration ---
    INPUT_PATH = "C:/Users/Vadim/Downloads/PgnChecker/input pgns/BenoniToCheck.pgn"
    START_FEN  = None               # None = standard starting position
    START_PLY  = 10
    END_PLY    = 40


    # --- berserk client ---
    token = '' # delete before pushing
    session = berserk.TokenSession(token)
    client  = berserk.Client(session)

    g = build_inclusion_graph(
        pgn_path=INPUT_PATH,
        opening_explorer=client.opening_explorer,
        safe_get_games=safe_get_games,   # assumed imported / in scope
        start_fen=START_FEN,
        start_ply=START_PLY,
        end_ply=END_PLY,
    )

    g.visualise(
        output_path="benoni_inclusion_graph.html",
        min_weight=0.1,
        min_observations=2,
    )