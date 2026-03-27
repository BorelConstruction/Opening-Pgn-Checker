"""
Builds and visualizes a move-relationship graph for identifying
recurrent inclusions.

Nodes  : moves (UCI strings)
Edges  : ma -> mb, weighted by the average conditional frequency of mb
         following ma, across all positions in the traversal where ma appears.

Abstract dependencies (implemented by subclasses):
  get_children(node) -> list[chess.pgn.GameNode]
      Which child nodes to traverse from the current node.

  get_db_stats(node) -> dict[str, int]
      Raw game counts for each response move (UCI) from this node.
"""

import sys
import os

import chess
import chess.pgn
import collections
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Union, Optional, Any
from hashlib import sha1

import networkx as nx
from pyvis.network import Network

from .traversal import traverse, TraversalPolicy, default_children, mainline_children
from .runner import Runner, fen
from .boardtools import *


# Type aliases
Board       = chess.Board
Move        = chess.Move
Node        = chess.pgn.GameNode
DbStats     = dict[str, int]
GetChildrenFunc = Callable[[Node], list[Node]]
GetDbStatsFunc  = Callable[[Node], DbStats]
EdgeFilterFunc  = Callable[[str, str, dict[str, Any]], bool]
EdgeWidthFunc   = Callable[[str, str, dict[str, Any]], float]


def get_or_create_child(node, move: Union[str, Move]):
    if isinstance(move, str):
        move = chess.Move.from_uci(move)
    for child in node.variations:
        if child.move == move:
            return child
    return node.add_variation(move)



@dataclass(frozen=True)
class MoveStat:
    uci: str
    count: int
    total: int
    conditional_freq: float


EdgeObservationWeightFunc = Callable[[Node, MoveStat], float]


class InclusionGraph(ABC):
    """
    Raw logic of the inclusion graph building process.

    Subclasses implement:
      - get_children(node)
      - get_db_stats(node)
    """
    def __init__(
        self,
        report: Optional[Callable[[Node, str], None]] = None,
        edge_observation_weight: Optional[EdgeObservationWeightFunc] = None,
    ):
        self.report = report
        self._edge_observation_weight: EdgeObservationWeightFunc = (
            edge_observation_weight or (lambda _node, move_stat: move_stat.conditional_freq)
        )

        # Cache of positions already counted during traversal (fen-essentials).
        self._position_cache: set[str] = set()

        # For each edge (ma, mb): list of conditional frequencies observed
        # at each position where ma was a traversed child.
        self._edge_observations: dict[tuple[str, str], list[float]] = \
            collections.defaultdict(list)

        self.graph: nx.DiGraph = nx.DiGraph()

    @abstractmethod
    def get_children(self, node: Node) -> list[Node]:
        raise NotImplementedError

    @abstractmethod
    def get_db_stats(self, node: Node) -> DbStats:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def build(self, root: Node, start: int = 0, end : int = None, progress=None) -> None:
        """
        Traverse the opening tree from root up to depth half-moves,
        accumulating edge observations.
        """
        self._edge_observations.clear()
        self.graph.clear()
        self._position_cache.clear()
        self._traverse(root, start, end, progress=progress)
        sys.stderr.write("\n Finalizing edges...\n")
        self._finalize_edges()
        sys.stderr.write("\n Done...\n")
        

    def _traverse(self, node: Node, start_ply, end_ply, progress=None) -> None:
        def visit(node: Node) -> None:

            for ma in self.get_children(node):
                pos = fen(ma)
                if pos in self._position_cache:
                    continue
                self._position_cache.add(pos)

                # --- record edges ma -> mb for every response mb in DB ---
                response_stats: DbStats = self.get_db_stats(ma)
                response_total = sum(response_stats.values()) if response_stats else 0

                if response_total > 0:
                    for mb_uci, mb_count in response_stats.items():
                        conditional_freq = mb_count / response_total
                        move_stat = MoveStat(
                            uci=mb_uci,
                            count=mb_count,
                            total=response_total,
                            conditional_freq=conditional_freq,
                        )
                        self._edge_observations[
                            (ma.move.uci(), mb_uci)
                        ].append(self._edge_observation_weight(ma, move_stat))

                        our_move = node.turn() == WHITE # opposite_color
                        colors = ["red", "blue"] if our_move else ["blue", "red"]

                        self.graph.add_node(ma.move.uci(), color=colors[0])
                        self.graph.add_node(mb_uci, color=colors[1])

                        
                
                if progress and progress.done % 10 == 0:
                    if self.report:
                        self.report(node, f"{response_total} games.")

        tp = TraversalPolicy(
            start_ply=start_ply,
            end_ply=end_ply,
            get_children=self.get_children
        )

        traverse(node, visit, tp=tp, progress=progress)

    def _finalize_edges(self) -> None:
        """Average observations and populate the networkx graph."""
        for (ma_uci, mb_uci), freqs in self._edge_observations.items():
            weight = sum(freqs) / len(freqs)
            self.graph.add_edge(ma_uci, mb_uci, weight=weight, n=len(freqs))

    # ------------------------------------------------------------------
    # Visualisation
    # ------------------------------------------------------------------

    def visualize(
        self,
        output_path: str = "inclusion_graph.html",
        edge_filter: Optional[EdgeFilterFunc] = None,
        edge_width: Optional[EdgeWidthFunc] = None,
    ) -> None:
        """
        Write an interactive pyvis graph to output_path.
        """
        net = Network(height="800px", width="100%", directed=True)
        net.toggle_physics(True)

        for ma_uci, mb_uci, data in self.graph.edges(data=True):
            if edge_filter and (not edge_filter(ma_uci, mb_uci, data)):
                continue
            # Node labels: SAN would be nicer but we don't have board context
            # here; UCI is unambiguous and good enough for inspection.
            for node in (ma_uci, mb_uci):
                if node not in net.get_nodes():
                    color = self.graph.nodes[node].get("color", "gray")
                    net.add_node(node, label=node, color=color)

            width = edge_width(ma_uci, mb_uci, data) if edge_width else 1.0
            title = f"{ma_uci} → {mb_uci}\nweight={data['weight']:.2f}  n={data['n']}"
            net.add_edge(ma_uci, mb_uci, width=width, title=title)

        net.show(output_path, notebook=False)
        print(f"Graph written to {output_path}")


class PgnInclusionGraph(InclusionGraph):
    """
    Concrete InclusionGraph that delegates get_children/get_db_stats to callables.
    """

    def __init__(
        self,
        *,
        get_children: GetChildrenFunc,
        get_db_stats: GetDbStatsFunc,
        report: Optional[Callable[[Node, str], None]] = None,
        edge_observation_weight: Optional[EdgeObservationWeightFunc] = None,
    ):
        super().__init__(
            report=report,
            edge_observation_weight=edge_observation_weight or (lambda _node, _move_stat: 1.0),
        )
        self._get_children = get_children
        self._get_db_stats = get_db_stats

    def get_children(self, node: Node) -> list[Node]:
        return self._get_children(node)

    def get_db_stats(self, node: Node) -> DbStats:
        return self._get_db_stats(node)

    def visualize(
        self,
        output_path: str = "inclusion_graph.html",
        min_weight: float = 0.0,
        min_observations: int = 4,
        *,
        edge_filter: Optional[EdgeFilterFunc] = None,
        edge_width: Optional[EdgeWidthFunc] = None,
    ) -> None:
        nodes_to_show = {n for n in self.graph.nodes if self.graph.out_degree(n) < 10}

        def default_filter(ma_uci: str, _mb_uci: str, data: dict[str, Any]) -> bool:
            if data["weight"] < min_weight:
                return False
            if data["n"] < min_observations:
                return False
            if ma_uci not in nodes_to_show:
                return False
            return True

        def default_width(_ma_uci: str, _mb_uci: str, data: dict[str, Any]) -> float:
            return float(data["n"])

        super().visualize(
            output_path=output_path,
            edge_filter=edge_filter or default_filter,
            edge_width=edge_width or default_width,
        )



class DBInclusionGraph(InclusionGraph):
    """
    InclusionGraph backed by DB stats (e.g. Lichess explorer).

    Differences vs PGN graph:
      1) width depends on data["weight"]
      2) get_children selects DB moves by frequency >= self.frequency_threshold
      3) edge weight is determined by move frequency (conditional frequency)
    """

    def __init__(
        self,
        *,
        list_moves: Callable[[Node], list[str]],
        move_freq: Callable[[Node, str], float],
        total_games_move: Callable[[Node, str], int],
        frequency_threshold: float,
        min_games: int = 0,
        report: Optional[Callable[[Node, str], None]] = None,
        edge_observation_weight: Optional[EdgeObservationWeightFunc] = None,
    ):
        super().__init__(report=report, edge_observation_weight=edge_observation_weight)
        self._list_moves = list_moves
        self._move_freq = move_freq
        self._total_games_move = total_games_move
        self.frequency_threshold = frequency_threshold
        self.min_games = min_games

    def get_db_stats(self, node: Node) -> DbStats:
        ucis = self._list_moves(node)
        return {uci: self._total_games_move(node, uci) for uci in ucis}

    def get_children(self, node: Node) -> list[Node]:
        children: list[Node] = []
        for uci in self._list_moves(node):
            freq = self._move_freq(node, uci)
            if freq < self.frequency_threshold:
                continue
            count = self._total_games_move(node, uci)
            if count < self.min_games:
                continue
            child = get_or_create_child(node, uci)
            children.append(child)
        return children

    def visualize(
        self,
        output_path: str = "inclusion_graph.html",
        min_weight: float = 0.0,
        min_observations: int = 4,
        *,
        edge_filter: Optional[EdgeFilterFunc] = None,
        edge_width: Optional[EdgeWidthFunc] = None,
    ) -> None:
        nodes_to_show = {n for n in self.graph.nodes if self.graph.out_degree(n) < 4}

        def default_filter(ma_uci: str, _mb_uci: str, data: dict[str, Any]) -> bool:
            if data["weight"] < min_weight:
                return False
            if data["n"] < min_observations:
                return False
            if ma_uci not in nodes_to_show:
                return False
            return True

        def default_width(_ma_uci: str, _mb_uci: str, data: dict[str, Any]) -> float:
            return 1.0 + 8.0 * float(data["weight"])

        super().visualize(
            output_path=output_path,
            edge_filter=edge_filter or default_filter,
            edge_width=edge_width or default_width,
        )


class InclusionGraphRunner(Runner):
    '''
    Manages InclusionGraph building and visualisation.
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


    def make_inclusion_graph(self, freq_thresh: float, min_game_num: int, depth: int) -> InclusionGraph:

        if self.options.input_pgn:
            return self.make_graph_pgn(depth)
        else:
            return self.make_graph_db(freq_thresh, min_game_num, depth)
        
    def make_graph_db(self, freq_thresh: float, min_game_num: int, depth: int) -> InclusionGraph:

        def list_moves(node: chess.pgn.GameNode) -> list[str]:
            raw = self.query(fen(node), "db_lichess")
            return [m.get("uci") for m in raw.get("moves", []) if m.get("uci")]

        root = chess.pgn.Game()
        root.setup(self.options.starting_pos)

        g = DBInclusionGraph(
            list_moves=list_moves,
            move_freq=self.move_freq,
            total_games_move=self.total_games_move,
            frequency_threshold=freq_thresh,
            min_games=min_game_num,
            report=self.report_position,
        )
        g.build(root, end=depth, progress=self.progress)
        return g
    
    def make_graph_pgn(self, depth: int) -> InclusionGraph:
        
        def get_db_stats(node: chess.pgn.GameNode) -> dict[str, int]:
            ucis = [m.uci() for m in node.variations]
            return {uci: 1 for uci in ucis}

        with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
            node = chess.pgn.read_game(pgnFile)
            self.set_starting_pos(node)

        g = PgnInclusionGraph(get_children=default_children, get_db_stats=get_db_stats, report=self.report_position)
        g.build(self.starting_node, end=depth+self.starting_node.ply(), progress=self.progress)
        return g
    
    def _default_cache_path(self) -> str:
        base = os.path.join("cache", "graph")
        name = sha1(self.options.starting_pos.encode()).hexdigest()[:10]
        print(name)
        return os.path.join(base, f"{name}.json")
    
    def set_starting_pos(self, game: chess.pgn.GameNode):
        self.starting_node = find_node_by_position(game, self.options.starting_pos)
    
    def run(self):
        try:
            g = self.make_inclusion_graph(self.options.freq_threshold, self.options.min_games, self.options.depth)
            g.visualize(        output_path="inclusion_graph.html",
                min_weight=0.1,
                min_observations=self.options.min_observations,)
            return f"{g.graph.number_of_nodes()} nodes, {g.graph.number_of_edges()} edges."
        finally:
            try:
                self.save_cache()
            except Exception as exc:
                print(f"Failed to save cache: {exc}\n")
            self._finalizer()
    
    """
inclusion_graph_lichess.py
--------------------------
Concrete get_children / get_db_stats implementations for InclusionGraph,
backed by a PGN opening file and the Lichess opening explorer via berserk.
"""

import berserk


# ---------------------------------------------------------------------------
# PGN index: FEN (stripped) -> set of moves that appear in the file
# ---------------------------------------------------------------------------

from .database import safe_get_games


# ---------------------------------------------------------------------------
# Concrete get_db_stats
# ---------------------------------------------------------------------------

def make_get_db_stats(
    opening_explorer: berserk.clients.OpeningExplorer,
    safe_get_games,
    **kwargs,          # passed through to safe_get_games (ratings, speeds, etc.)
) -> GetDbStatsFunc:
    """
    Returns a get_db_stats function that queries the Lichess opening explorer.

    The berserk response looks like:
      {"moves": [{"uci": "e2e4", "white": 100, "draws": 50, "black": 30}, ...]}
    """
    def get_db_stats(node: Node) -> dict[str, int]:
        fen = node.board().fen()
        response = safe_get_games(opening_explorer, position=fen, **kwargs)

        result = {}
        for entry in response.get("moves", []):
            try:
                move = entry["uci"]
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

    get_children = lambda node: node.variations
    get_db_stats  = make_get_db_stats(opening_explorer, safe_get_games, **db_kwargs)

    with open(pgn_path, encoding="utf-8") as pgnFile:
        root = chess.pgn.read_game(pgnFile)

    graph = PgnInclusionGraph(get_children=get_children, get_db_stats=get_db_stats)

    depth = end_ply - start_ply #############

    print(f"Building graph...")
    graph.build(root, start_ply, end_ply)
    print(f"  {graph.graph.number_of_nodes()} nodes, {graph.graph.number_of_edges()} edges.")

    return graph


# ---------------------------------------------------------------------------
# Test / demo entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    # --- configuration ---
    INPUT_PATH = "C:/Users/Vadim/Downloads/PgnChecker/input pgns/BenoniToCheck.pgn"
    START_PLY  = 10
    END_PLY    = 30


    # --- berserk client ---
    token = '' # delete before pushing
    session = berserk.TokenSession(token)
    client  = berserk.Client(session)

    g = build_inclusion_graph(
        pgn_path=INPUT_PATH,
        opening_explorer=client.opening_explorer,
        safe_get_games=safe_get_games,   # assumed imported / in scope
        start_ply=START_PLY,
        end_ply=END_PLY,
    )

    g.visualize(
        output_path="benoni_inclusion_graph.html",
        min_weight=0.1,
        min_observations=2,
    )
