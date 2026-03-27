import copy
import sys
import weakref
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional, Union, TypeVar
from collections.abc import Callable
# from __future__ import annotations # to resolve Runner<->EvalProvider... I'll just annotate with a str.
from abc import ABC, abstractmethod

VisitResultT = TypeVar("VisitResultT")

import berserk
import chess
from chess.pgn import GameNode as Node
from chess import WHITE
from chess import BLACK


from .options import CoreOptions, DEBUG_MODE
from .timer import clock
from .caching import CacheDict
from .database import *
from .traversal import traverse, TraversalPolicy, mainline_children
from .boardtools import *

# sys.stdout.reconfigure(encoding='utf-8')

sf_path = "C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe"

def add_debug_comment(node, message):
    if DEBUG_MODE:
        update_comment(node, message)

@dataclass(frozen=True)
class PositionSnapshot:
    fen: str
    ply: Optional[int] = None
    last_move_uci: Optional[str] = None

@dataclass(frozen=True)
class RunnerReport:
    kind: str                    # "gap", "node", "warning"
    position: Optional[PositionSnapshot] = None
    message: Optional[str] = None


class PosCache:
    def __init__(self, fen: str):
        self.fen = fen
        self.TTed : Node = None # "seen in the relevant part of the pgn file"
        self._data = {}

    def get(self, label, query_fn):
        if label not in self._data:
            self._data[label] = query_fn(self.fen)
        return self._data[label]

    def to_dict(self) -> dict:
        data = {}
        if "db_lichess" in self._data:
            data["db_lichess"] = self._data["db_lichess"]
        if "db_masters" in self._data:
            data["db_masters"] = self._data["db_masters"]
        if "eval" in self._data:
            eval_provider = self._data["eval"]
            if hasattr(eval_provider, "to_dict"):
                data["eval"] = eval_provider.to_dict()
        if "q-eval" in self._data:
            data["q-eval"] = self._data["q-eval"].to_dict()
        return {
            "fen": self.fen,
            # "TTed": self.TTed, # we don't want to cache this, this is cheap.
            # We actually want it to reset between runs, or we may find undexpected transpositions
            "data": data,
        }
    
    @classmethod
    def from_dict(cls, runner: 'Runner', payload: dict) -> "PosCache":
        payload["fen"] = fen(payload["fen"]) #####
        pc = cls(payload["fen"])
        data = payload.get("data", {})
        if "db_lichess" in data:
            pc._data["db_lichess"] = data["db_lichess"]
        if "db_masters" in data:
            pc._data["db_masters"] = data["db_masters"]
        if "eval" in data:
            pc._data["eval"] = EvalProvider.from_dict(runner, pc.fen, data["eval"])
        if "q-eval" in data:
            pc._data["q-eval"] = EngineEval.from_dict(data["q-eval"])
        return pc


class Progress:
    def __init__(self, emit=None, step_len=0.02):
        self.emit = emit or (lambda *_: None)

        self.done = 0
        self.total = None
        self.step_len = step_len

    # ---------- loop / cyclic progress ----------

    def iter(self, items):
        total = len(items)
        old_total = self.total
        old_done = self.done

        self.total = total
        self.done = 0

        self._emit()
        try:
            for item in items:
                yield item
                self.done += 1
                self._emit()
        finally:
            self.total = old_total
            self.done = old_done

    # ---------- traversal / step-based progress ----------

    def step(self, n=1):
        self.done += n
        self._emit()
        return self.done

    def set_total(self, n):
        self.reset()
        self.total = n

    def reset(self):
        self.done = 0
        self._emit()

    # ---------- internal ----------

    def _emit(self):
        if self.total:
            step_absolute = int(self.total * 0.02) + 1
            if self.done % step_absolute == 0:
                self.emit(self.done, self.total)
        else:
            # indeterminate progress; emit activity pulse
            self.emit(0, 0) # TODO



class QueryResult(ABC):
    """
    Lazy result of a query(node, kind).
    Concrete subclasses decide how and when computation happens.
    """
    pass


class EngineEval(NamedTuple):
    eval: float
    move: str

    def to_dict(self) -> dict:
        move = self.move.uci() if isinstance(self.move, chess.Move) else self.move
        return {"eval": self.eval, "move": move}

    @classmethod
    def from_dict(cls, payload: dict) -> "EngineEval":
        move = payload["move"]
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        return cls(payload["eval"], move)

class EvalProvider(QueryResult):
    def __init__(self, runner: 'Runner', fen: str):
        self._runner = runner   # gives access to engine, options, cache helpers
        self._fen = fen
        
        # TODO: remember depths

        self._multipvs = {}    # dict[int, list[EngineEval]]

    def to_dict(self) -> dict:
        multipvs = {}
        for amount, lines in self._multipvs.items():
            multipvs[str(amount)] = [self._engine_eval_to_dict(line) for line in lines]
        return {"multipvs": multipvs}

    @classmethod
    def from_dict(cls, runner: 'Runner', fen: str, payload: dict) -> "EvalProvider":
        ev = cls(runner, fen)
        multipvs = {}
        for amount, lines in payload.get("multipvs", {}).items():
            try:
                key = int(amount)
            except (TypeError, ValueError):
                continue
            multipvs[key] = [cls._engine_eval_from_dict(line) for line in lines]
        ev._multipvs = multipvs
        return ev

    @staticmethod
    def _engine_eval_to_dict(line: EngineEval) -> dict:
        move = line.move
        if isinstance(move, chess.Move):
            move = move.uci()
        return {"eval": line.eval, "move": move}

    @staticmethod
    def _engine_eval_from_dict(payload: dict) -> EngineEval:
        move = payload["move"]
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        return EngineEval(payload["eval"], move)

    def top(self, amount: int) -> list[EngineEval]:
        if amount not in self._multipvs:
            result = self._runner.engine_eval(self._fen, multipv=amount)
            for n in range(1, amount + 1):
                if n not in self._multipvs:
                    self._multipvs[n] = result[:n] # will make cache heavier, so make push this to retrieval if that becomes a problem
        return self._multipvs[amount]
    
    def best_move(self) -> str:
        return self.top(1)[0].move
    
    def best_eval(self) -> float:
        return self.top(1)[0].eval


class Runner(ABC):
    def __init__(self, options: CoreOptions, progress_cb=None, report_cb=None):
        options.validate()

        self.options = copy.copy(options)

        self._finalizer = weakref.finalize(self, self._cleanup)

        self.progress = Progress(progress_cb)
        self.report = report_cb or (lambda *_: None)

        self.normalize_fens() # has to be done before cache loading

        self.cache = CacheDict(lambda fen: PosCache(fen))
        self.load_cache()

        self.engine_path = sf_path
        self._engine = None
        self.init_client()
        self.init_queries()


    def normalize_fens(self):
        if hasattr(self.options, "starting_pos"):
            self.options.starting_pos = fen(self.options.starting_pos)

    def init_queries(self):
        self._queries = {
            # NOTE: will be a bug is self.opening_explorer changes
            # (as self.cache will then store the result relative to the old explorer)
            # If we expect this to happen, here and below such parameters have to be frozen (and not cached)
            "db_lichess": lambda fen: safe_get_games(self.opening_explorer, position=fen),

            "db_masters": lambda fen: safe_get_games(self.opening_explorer, position=fen, lichess=False),

            "eval": lambda fen: EvalProvider(self, fen),

            # if we don't cache quick evals, results will be different every time
            "q-eval": lambda fen: quick_eval(self.engine, fen, pov=self.options.side)
        }

    def query(self, fen: str, type: str):
        if fen in self.cache:
            sys.stderr.write(f"Using cache for {fen}\n")
        return self.cache[fen].get(type, self._queries[type])

    @abstractmethod
    def _default_cache_path(self) -> str:
        pass

    @abstractmethod
    def run(self):
        pass
    
    def load_cache(self, path: Optional[str] = None) -> bool:
        self.report_message("Loading cache...")
        path = path or self._default_cache_path()
        pos_cache_factory = lambda payload: PosCache.from_dict(self, payload)
        self.cache = CacheDict.from_dict(path, pos_cache_factory)

    def save_cache(self, path: Optional[str] = None):
        path = path or self._default_cache_path()
        self.cache.serialize(path)

    def report_message(self, msg: str):
        self.report(RunnerReport(kind = "msg", message=msg))

    def report_position(self, node: Node, message: Optional[str] = None):
        self.report(RunnerReport(kind = "position", position=PositionSnapshot(fen(node)), message=message))

    def _cleanup(self):
        if self._engine is not None:
            close_engine(self._engine)
        self._engine = None

    def close(self):
        self._finalizer()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @property # we want to start the engine if it is needed, but we also don't want to restart it every time
    def engine(self):
        if self._engine is None:
            self._engine = init_engine(self.engine_path)
        return self._engine
    
    def engine_eval(self, fen: str, multipv: int = 1):
        return evaluate_position(self.engine, fen, pov=self.options.side, options=self.options,
                                     adaptive=self.options.adaptive_an, multipv=multipv) # TODO
        

    def init_client(self):
        token = getattr(self.options, "_token", None)
        if token:
            lichessClient = berserk.Client(session=berserk.TokenSession(token))
        else:
            lichessClient = berserk.Client()
        self.opening_explorer = lichessClient.opening_explorer

    def _traverse(self, node: Node,
                    visit: Optional[Callable[[Node], VisitResultT]] = None,
                    post: Optional[Callable] = None,
                    reasons_to_stop: Optional[Callable[[Node, Optional[VisitResultT]], bool]] = None,
                    get_children: Optional[Callable[[Node], list[Node]]] = lambda n: n.variations):
        '''Traverse the subtree rooted at node
        in a way consistent with self.options'''
        mainline_sides = () if self.options.check_alternatives else (self.options.side,)
        get_children = mainline_children(mainline_sides)
        tp = TraversalPolicy(
            start_ply=self.options.start_ply,
            end_ply=self.options.end_ply,
            get_children=get_children)
        return traverse(node, visit, post, reasons_to_stop, tp, self.progress)
    
    
    # ============ Universal helper functions ============
    def move_freq(self, board: Union[Node, chess.Board], move: Optional[Union[chess.Move, str]] = None) -> float:
        if isinstance(move, chess.Move):
            move = move.uci()
        if isinstance(board, Node):
            if move is None:
                move = board.move
            board = board.board()
        if move is None:
            raise ValueError("Expected a move or a Node")
        stats = self.query(fen(board), "db_lichess")
        md = stats_for_uci(stats, move)
        if not md:
            return -1
        return move_frequency(md, stats)
    
    def total_games(self, board: Union[Node, chess.Board, str]) -> int:
        if isinstance(board, Node):
            board = board.board()
        if isinstance(board, chess.Board):
            board = fen(board)
        stats = self.query(board, "db_lichess")
        return total_games(stats)
    
    def total_games_move(self, board: Union[Node, chess.Board, str], move: Union[chess.Move, str]) -> int:
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        if isinstance(board, Node):
            board = board.board()
        if isinstance(board, str):
            board = chess.Board(board)
        board.push(move)
        return self.total_games(board)

    def score_rate_pos(self, board: Union[Node, chess.Board, str]) -> float:
        if isinstance(board, Node):
            board = board.board()
        if isinstance(board, chess.Board):
            board = fen(board)
        stats = self.query(board, "db_lichess")
        if not stats:
            sys.stderr.write(f"No stats for {board}\n")
            return -0.5 # TODO
        return score_rate(stats, self.options.side)
    
    def score_rate_move(self, board: Union[Node, chess.Board, str], move: Union[chess.Move, str]) -> float:
        if isinstance(move, chess.Move):
            move = move.uci()
        if isinstance(board, Node):
            board = board.board()
        if isinstance(board, chess.Board):
            board = fen(board)
        stats = self.query(board, "db_lichess")
        md = stats_for_uci(stats, move)
        if not md:
            sys.stderr.write(f"No stats for {board} with move {move}\n")
            return -0.5
        return score_rate(md, self.options.side)

    
    def set_starting_pos(self, game: chess.pgn.GameNode):
        if self.options.starting_pos:
            self.starting_node = find_node_by_position(game, self.options.starting_pos)
        else:
            self.starting_node = game

    def count_nodes(self, root_node):
        count = 0
        def visit(ply):
            nonlocal count
            count += 1
        self._traverse(root_node, visit)
        return count


  
def init_engine(exe_path: str, conf=None):
    sys.stderr.write(f"Initializing {exe_path}\n")
    engine = chess.engine.SimpleEngine.popen_uci(exe_path)
    return engine


def evaluate_position(engine: chess.engine.SimpleEngine,
                      position: Union[str, chess.Board],
                      pov: chess.Color = None,
                      multipv: int = 1,
                      options=None,
                      adaptive=True,
                      time_limit=0.1) -> EngineEval:
    """
    Args:
        fen_string (str): The position in Forsyth-Edwards Notation (FEN).
        time_limit (float): The time (in seconds) the engine spends analyzing.

    Returns:
        float: The evaluation score in pawn units (by default relative to the side to move, because we
        expect that the engine plays on our side).
    """
    if isinstance(position, str):
            board = chess.Board(position)
    elif isinstance(position, chess.Board):
            board = position
    else:
        raise TypeError("position must be a FEN string or chess.Board")
    
    sys.stderr.write(f"\n Evaluating the position... {fen(board)}")
    # Use 'with' statement for proper engine startup and cleanup
    # with chess.engine.SimpleEngine.popen_uci("C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe") as engine:
    if adaptive:
        infos = analyse_adaptive(engine, board, min_depth=options.min_depth, max_depth=options.max_depth, multipv=multipv)
    else:
        infos = analyse_time_limit(engine, board, time_limit=time_limit, multipv=multipv)
    # Get the list of EngineEval objects
    return process_engine_output(infos, board, pov)

def process_engine_output(infos, board, pov=WHITE):
    lines = []
    for info in infos:
        score = info["score"].pov(pov)
        pv = info["pv"]

        evaluation_cp = score.score(mate_score=100000)
        evaluation_pawns = evaluation_cp / 100.0
        
        lines.append(EngineEval(evaluation_pawns, pv[0]))
    return lines

def analyse_adaptive(engine, board: chess.Board, min_depth=8, max_depth=14, multipv: int = 1) -> list:
    last_score = None

    for depth in range(min_depth, max_depth + 1):
        info = engine.analyse(board, chess.engine.Limit(depth=depth)) # we want to adapt based on the best move's eval, so multipv=1
        score = info["score"].white().score(mate_score=100000)

        if last_score is not None and abs(score - last_score) < 15:
            break

        last_score = score

    if multipv == 1:
        return [info]
    else:
        info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
        return info
    
def analyse_time_limit(engine, board: chess.Board, time_limit=0.1, multipv: int = 1) -> list:
    return engine.analyse(board, chess.engine.Limit(time=time_limit), multipv=multipv)


def close_engine(engine):
    print("\nClosing the engine...")
    if engine is not None: # hasattr?
        try:
            engine.close()
        except Exception as e:
            sys.stderr.write("Failed to close engine\n")
            sys.stderr.write(str(e))


def stats_for_uci(games: dict, uci: str):
    return next((m for m in games['moves'] if m['uci'] == uci_from_pgn_to_lichess(uci)), None) # {}


def quick_eval(engine, position: Union[str, chess.Board], pov=WHITE, multipv=1) -> list[EngineEval]:
    # TODO: make a separate function for this processing?
    if isinstance(position, str):
            board = chess.Board(position)
    elif isinstance(position, chess.Board):
            board = position
    else:
        raise TypeError("position must be a FEN string or chess.Board")
    
    sys.stderr.write(f"\n Quick eval for {fen(board)}")
    
    infos = analyse_time_limit(engine, board, time_limit=0.1, multipv=multipv)
    return process_engine_output(infos, board, pov)[0] # for now only return one EngineEval