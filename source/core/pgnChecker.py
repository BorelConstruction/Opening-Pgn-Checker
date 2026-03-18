from collections import namedtuple
import copy
import datetime
import json
import os
import sys
import tempfile
import time
import traceback
import weakref
from dataclasses import dataclass, field
from functools import partial  # for currying
from typing import Callable, NamedTuple, Optional, Union, TypeVar, Generic
from collections.abc import Callable
# from __future__ import annotations # to resolve PgnChecker<->EvalProvider... I'll just annotate with a str.
from abc import ABC

VisitResultT = TypeVar("VisitResultT")

import berserk
import berserk.exceptions
import chess
from chess.pgn import GameNode as Node
# import chess.pgn
# import chess.svg
from chess import WHITE
from chess import BLACK


from .options import Options
from .timer import clock
from .caching import CacheDict

# TODO: trim obvious moves (don't add the last move if it's forced)
# TODO: identify unobvious moves
# TODO: exclaims for their moves (if it doesn't drop the eval while the most popular one does?)
# TODO: min games depends on ply

# TODO: node_count accounting for us only considering main lines

# TODO: cap freq from above

# TODO: let the engine play where no moves are found in the DB

# TODO: similar positions

# TODO: remember settings for every input file

# sys.stdout.reconfigure(encoding='utf-8')


ratings = ["2200", "2500"] # TODO: make parameters
speeds = ["blitz", "rapid", "classical"]

ratings_n = ["1900", "2200"]
speeds_n = ["blitz", "rapid", "classical"]

DEBUG_MODE = False

STAT_SIGNIFICANCE_THRESHOLD = 20

# output_pgns = ['Output.pgn']

sf_path = "C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe"

def add_debug_comment(node, message):
    if DEBUG_MODE:
        update_comment(node, message)

@dataclass(frozen=True)
class PositionSnapshot:
    fen: str
    ply: int
    last_move_uci: Optional[str] = None

@dataclass(frozen=True)
class CheckerReport:
    kind: str                    # "gap", "node", "warning"
    position: Optional[PositionSnapshot] = None
    message: Optional[str] = None

@dataclass
class MoveChoice:
    move: chess.Move
    reason: str
    eval: Optional[float] = None
    comment: Optional[str] = ''
    # actions: Callable[[Node], None] = lambda node: None # if want to do more complex actions than commmenting

    def __iter__(self):
        return iter((self.move, self.reason, self.eval, self.comment))
    

@dataclass
class GapsInfo:
    node: Node
    gaps: list[str] = field(default_factory=list)

    def add_gap(self, move: str):
        self.gaps.append(move)

    def __bool__(self):
        return bool(self.gaps)

    def __iter__(self):
        return iter(self.gaps)

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
    def from_dict(cls, checker: 'PgnChecker', payload: dict) -> "PosCache":
        payload["fen"] = fen(payload["fen"]) #####
        pc = cls(payload["fen"])
        data = payload.get("data", {})
        if "db_lichess" in data:
            pc._data["db_lichess"] = data["db_lichess"]
        if "eval" in data:
            pc._data["eval"] = EvalProvider.from_dict(checker, pc.fen, data["eval"])
        if "q-eval" in data:
            pc._data["q-eval"] = EngineEval.from_dict(data["q-eval"])
        return pc

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
    def __init__(self, checker: 'PgnChecker', fen: str):
        self._checker = checker   # gives access to engine, options, cache helpers
        self._fen = fen
        
        # TODO: remember depths

        self._multipvs = {}    # dict[int, list[EngineEval]]

    def to_dict(self) -> dict:
        multipvs = {}
        for amount, lines in self._multipvs.items():
            multipvs[str(amount)] = [self._engine_eval_to_dict(line) for line in lines]
        return {"multipvs": multipvs}

    @classmethod
    def from_dict(cls, checker: 'PgnChecker', fen: str, payload: dict) -> "EvalProvider":
        ev = cls(checker, fen)
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
            result = self._checker.engine_eval(self._fen, multipv=amount)
            for n in range(1, amount + 1):
                if n not in self._multipvs:
                    self._multipvs[n] = result[:n] # will make cache heavier, so make push this to retrieval if that becomes a problem
        return self._multipvs[amount]
    
    def best_move(self) -> str:
        return self.top(1)[0].move
    
    def best_eval(self) -> float:
        return self.top(1)[0].eval


class PgnChecker():
    def __init__(self, options: Options, progress_cb=None, report_cb=None):
        options.validate()
        self.N = 0
        self.options = copy.copy(options)
        self.options.side = WHITE if self.options.play_white else BLACK 
        self.options.adaptive_an = True # TODO
        self.engine_path = sf_path
        self._engine = None
        self._finalizer = weakref.finalize(self, self._cleanup)
        self.progress = Progress(progress_cb)
        self.report = report_cb or (lambda *_: None)
        self.cache = CacheDict(lambda fen: PosCache(fen))

        self.init_queries()
        self.set_output_pgn()
        

    def set_output_pgn(self):
        output_dir = "output pgns" # should we give the user a choice?
        input_stem = os.path.splitext(os.path.basename(self.options.input_pgn))[0]
        timestamp = datetime.datetime.now().strftime("%d-%m_%H-%M-%S")
        os.makedirs(output_dir, exist_ok=True)
        self.options.output_pgn = os.path.join(
            output_dir,
            f"{input_stem} -- {timestamp}.pgn",
        )

    def init_queries(self):
        self._queries = {
            # NOTE: will be a bug is self.opening_explorer changes
            # (as self.cache will then store the result relative to the old explorer)
            # If we expect this to happen, here and below such parameters have to be frozen (and not cached)
            "db_lichess": lambda fen: safe_get_games(self.opening_explorer, position=fen),

            "eval": lambda fen: EvalProvider(self, fen),

            # if we don't cache quick evals, results will be different every time
            "q-eval": lambda fen: quick_eval(self.engine, fen, pov=self.options.side)
        }

    def query(self, fen: str, type: str):
        return self.cache[fen].get(type, self._queries[type])

    def _default_cache_path(self) -> str:
        base = "cache"
        name = "cache"
        if self.options.input_pgn:
            name = os.path.splitext(os.path.basename(self.options.input_pgn))[0]
        return os.path.join(base, f"{name}.json")
    
    def load_cache(self, path: Optional[str] = None) -> bool:
        self.report_message("Loading cache...")
        path = path or self._default_cache_path()
        pos_cache_factory = lambda payload: PosCache.from_dict(self, payload)
        self.cache = CacheDict.from_dict(path, pos_cache_factory)

    def save_cache(self, path: Optional[str] = None):
        path = path or self._default_cache_path()
        self.cache.serialize(path)

    def report_message(self, msg: str):
        self.report(CheckerReport(kind = "msg", message=msg))

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

    @property # we want to start the engine if it is needed, but we also don't want to restart it every time
    def engine(self):
        if self._engine is None:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
        return self._engine
    
    def engine_eval(self, fen: str, multipv: int = 1):
        return evaluate_position(self.engine, fen, pov=self.options.side, options=self.options,
                                     adaptive=self.options.adaptive_an, multipv=multipv) # TODO
        

    def count_nodes(self, root_node):
        count = 0

        def visit(ply):
            nonlocal count
            count += 1

        self._traverse(root_node, 0, visit)
        return count

    def fill_the_TT(self, root_node: Node):
        def visit(node: Node):
            self._record_position_in_TT(node)

        self._traverse(root_node, visit=visit)


    def _traverse(self, node: Node,
                    visit: Optional[Callable[[Node], VisitResultT]] = None,
                    post: Optional[Callable] = None,
                    reasons_to_stop: Optional[Callable[[Node, Optional[VisitResultT]], bool]] = None):
        '''Traverse the subtree rooted at node
        in a way consistent with self.options'''
        tp = TraversalPolicy(
            start_ply=self.options.start_ply,
            end_ply=self.options.end_ply,
            check_alternatives=self.options.check_alternatives)
        return _traverse(node, visit, post, reasons_to_stop, tp, self.options.side, self.progress)

    def init_client(self):
        token = getattr(self.options, "_token", None)
        if token:
            lichessClient = berserk.Client(session=berserk.TokenSession(token))
        else:
            lichessClient = berserk.Client()
        self.opening_explorer = lichessClient.opening_explorer

    def set_starting_pos(self, node: Node):
        if not self.options.starting_pos:
            self.starting_node = node
            return node
        
        self.options.starting_pos = fen(self.options.starting_pos)
        def visit(n: Node):
            if fen(n).startswith(fen_essential_part(self.options.starting_pos)): # ==
                self.starting_node = n
                return n
        self._traverse(node, visit=visit, reasons_to_stop=lambda _, res: res is not None)
        if not hasattr(self, "starting_node"):
            raise ValueError(f"Starting position {self.options.starting_pos} not found in the PGN")

    @clock
    def run(self):
        self.load_cache()
        try:
            self.init_client()

            with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
                num = 0
                while True:
                    num += 1
                    node = chess.pgn.read_game(pgnFile)

                    if node is None:
                        break

                    self.fill_the_TT(node)
                    
                    self.cache.enable_auto_save()
                    total = sum(1 for _ in self.cache if self.cache[_].TTed)
                    self.progress.set_total(total)
                    output_game = node  # no need to copy they way it currently works
                    if node.headers["Event"] == '?':
                        output_game.headers["Event"] = f'''plies {self.options.start_ply}-{self.options.end_ply}'''
                    else:
                        output_game.headers["Event"] = node.headers["Event"] + f''' | plies {self.options.start_ply}-{self.options.end_ply}'''

                    self.set_starting_pos(output_game)
                    node = self.starting_node

                    sys.stderr.write('starting to traverse...')
                    self.find_fill_gaps(node)
                    self.mark_moves(node)
                    print(output_game, file=open(self.options.output_pgn, "a", encoding="utf-8"), end="\n\n") # "a" for adding

        except Exception as e:
            sys.stderr.write(f"Error: {traceback.format_exc()}\n")
            raise e

        finally:
            try:
                self.save_cache()
            except Exception as exc:
                sys.stderr.write(f"Failed to save cache: {exc}\n")
            self._finalizer()

        return f"Added {self.moves_added} moves"
    
    def find_fill_gaps(self, game_node: Node):
        self.report_message("Finding gaps...")
        gaps = self.find_gaps(game_node)
        self.report_message("Filling gaps...")
        self.progress.reset()
        self.fill_gaps(gaps)

    def find_local_gaps(self, node: Node) -> Optional[GapsInfo]:      
        pgn_ucis = [m.move.uci() for m in node.variations]

        if node.turn() == self.options.side:
            if not pgn_ucis:
                if True: # self.options.fill_gaps # 
                    node.parent.variations.remove(node) # ideally this does not belong here
                return GapsInfo(node.parent, [node.move.uci()])
            return
        
        return GapsInfo(node, [mc.move.uci() for mc in self.generate_moves_them(node) if
                not mc.move.uci() in pgn_ucis])
    
    def fill_gaps(self, gaps: list[GapsInfo]):
        self.moves_added = 0
        for gaps_info in self.progress.iter(gaps):
            node = gaps_info.node
            self.act_on_gap_data_local(node, gaps_info)


    def act_on_gap_data_local(self, log_node, gap_data: GapsInfo):
        annotate = False # should be True if we only do find_gaps
        arrows = []
        comment = ''

        for uci in gap_data:
            freq = self.move_freq(log_node, uci)
            if freq < 0:
                update_comment(log_node,"Move {} not found in the database".format(uci).upper(), True)
            pos_snap = PositionSnapshot(fen(log_node), log_node.ply(), last_move_uci=uci)
            self.report(CheckerReport(kind="position", position=pos_snap,
                                    message=f"Filling gaps... \n" + f"{100*freq:.0f}% of {self.total_games(fen(log_node))} games" +
                                    f"\nScore rate {100*self.score_rate_move(log_node, uci):.0f}%."))
            comment += uci + ': ' + (str(freq)[:4]) + ', '
            # arrows.append([chess.parse_square(m_uci[:2]), chess.parse_square(m_uci[2:4])])
            arrows.append(arrow_from_uci(uci, color=color_from_freq(freq)))
            child = self._add_variation(log_node, uci)
            child.starting_comment = 'SUGGESTED LINE:'
            self.add_sample_line(child, depth=self.options.added_depth)
        if annotate:
            update_comment(log_node, comment)
            log_node.set_arrows(arrows)


    def find_gaps(self, game: Node):
        def post(node, child_results: list[GapsInfo]):
            all_gaps = sum(child_results, [])
            if node.ply() < self.options.start_ply:
                return all_gaps # only propagate the results
            local_gaps = self.find_local_gaps(node) 
            if local_gaps:
                all_gaps.append(local_gaps)
            return all_gaps
        def reasons_to_stop(node, _): 
            return node.comment.startswith('tr') or node.comment.startswith('Tr')
        return self._traverse(game, post=post, reasons_to_stop=reasons_to_stop)

    def gaps_local(self, node: Node):
        gaps_info = self.find_local_gaps(node)
        if gaps_info:
            self.act_on_gap_data_local(node, gaps_info)

    def traverse_and_fill_gaps(self, node: Node,
            report_state=None) -> bool:
        self._traverse(node, partial(PgnChecker.gaps_local, self))

    def mark_move_local(self, log_node: Node):
        mark_fn = mark_based_on_freq_us if log_node.turn() == self.options.side else mark_based_on_freq_them
        for n in log_node.variations:
            freq = self.move_freq(log_node, n.move)
            mark_fn(n, freq)

    def mark_moves(self, log_node):
        # TODO: if a move is frequent, promote it?
        self.report(CheckerReport(kind="position", position=PositionSnapshot("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 0)))
        self.report_message("Marking moves...")
        self.progress.reset()
        self._traverse(log_node, partial(PgnChecker.mark_move_local, self))

    @staticmethod
    def annotate_transposition(first_occurrence: Node, node: Node):
        diff = first_difference(first_occurrence,node)
        update_comment(node, f"Transp to {diff.ply}.{diff.move.uci()}")

    def find_transposition_move(self, board: Union[Node, chess.Board]) -> Optional[chess.Move]:
        if isinstance(board, Node):
            board = board.board()
        for move in board.legal_moves:
            board.push(move)
            cached = self.cache.get(fen(board))
            # 'rnbqkb1r/pp3ppp/2p1pn2/3p4/2P5/1PN1PN2/P2P1PPP/R1BQKB1R b KQkq - 2 5'
            board.pop()
            if cached is not None and cached.TTed:
                return move
        return None
    
    def seek_transposition(self, node: Node) -> Optional[chess.Move]:
        stats = self.query(fen(node), "db_lichess")
        db_moves = stats.get("moves", [])
        board2 = node.board()
        for m in db_moves[:3]:
            move = chess.Move.from_uci(m['uci'])
            board = node.board()
            board.push(move) # lichess_to_pgn?
            stats_m = self.query(fen(board), "db_lichess") 
            if stats_m['moves']:
                most_popular_uci = stats_m['moves'][0]['uci']
                if self.move_freq(board, most_popular_uci) > 0.8:
                    board.push(chess.Move.from_uci(most_popular_uci))
                    tr_move = self.find_transposition_move(board)
                    if tr_move:
                        update_comment(node, f"Likely to transpose after {m['uci']} and {most_popular_uci}".upper()) # TODO: pass it further
                        return move

    def better_engine_move(self, node: Node) -> MoveChoice:
        top2 = self.query(fen(node), "eval").top(2) # we'll also need top(2) later for nags
        eval1, best_move1 = top2[0]
        if len(top2) == 1:
            return MoveChoice(best_move1, "eng", eval1)
        eval2, best_move2 = top2[1]
        stats = self.query(fen(node), "db_lichess")
        stats1 = stats_for_uci(stats, best_move1.uci())
        stats2 = stats_for_uci(stats, best_move2.uci())
        sr1 = score_rate(stats1, self.options.side) if stats1 else 0.5
        sr2 = score_rate(stats2, self.options.side) if stats2 else 0.5
        tg1 = total_games(stats1) if stats1 else 0
        tg2 = total_games(stats2) if stats2 else 0
        if (eval2 is not None and eval1 - eval2 < 0.1 
            and sr2 - sr1 > 0.1 and min(tg1, tg2) > STAT_SIGNIFICANCE_THRESHOLD):
            comment = f"Best move is close, but {best_move2.uci()} has a better score rate ({sr2*100:.1f}% vs {sr1*100:.1f}%)".upper()
            return MoveChoice(best_move2, "eng", eval2, comment)
        else:
            return MoveChoice(best_move1, "eng", eval1)            

    def generate_moves_us(self, node: Node) -> list[MoveChoice]:
        moves = []
        eval = self.query(fen(node), "eval").best_eval()
        tr_move = self.find_transposition_move(node)
        if tr_move:
            board = node.board()
            board.push(tr_move)
            tr_eval = self.query(fen(board), "eval").best_eval()
            board.pop()
            if tr_eval >= eval - 0.15:
                eval = tr_eval
                return [MoveChoice(tr_move, "tr", tr_eval)]
            else:
                return [self.better_engine_move(node),
                        MoveChoice(tr_move, "tr", tr_eval, f"To transp, {eval:.2f} > {tr_eval:.2f}.")]
        to_tr_move = self.seek_transposition(node)

        if to_tr_move: # TODO: abstract these two blocks
            board = node.board()
            board.push(to_tr_move)
            to_tr_eval = self.query(fen(board), "eval").best_eval()
            board.pop()
            if to_tr_eval >= eval - 0.10:
                eval = to_tr_eval
                return [MoveChoice(to_tr_move, "to_tr", to_tr_eval)]
            else:
                return [self.better_engine_move(node),
                        MoveChoice(to_tr_move, "to_tr", to_tr_eval, f"To transp, {eval:.2f} > {to_tr_eval:.2f}")]
        return [self.better_engine_move(node)]
    
    def generate_moves_them(self, node: Node, maybe_use_engine: bool = False) -> list[MoveChoice]:
        moves = []
        stats = self.query(fen(node), "db_lichess")
        db_moves = stats.get("moves", [])
        for m in db_moves:
            crit = gap_criterion(m, move_frequency(m, stats), self.options.freq_threshold, 
                                        self.options.min_games, pov=self.options.side) 
            if crit == 1:
                moves.append(MoveChoice(chess.Move.from_uci(uci_from_lichess_to_pgn(m['uci'])), None, "db"))
            if crit == 2:
                c = f"well-scoring, ".upper() + str(score_rate(m, self.options.side))[:4] + f" in {total_games(m)} games"
                moves.append(MoveChoice(chess.Move.from_uci(uci_from_lichess_to_pgn(m['uci'])), None, "good", c))

        # if no DB moves and option enabled, add an engine move
        if not moves and self.options.use_engine_for_them and maybe_use_engine:
            engine_move = self.query(fen(node), "q-eval").move
            c = "" if DEBUG_MODE else "Engine".upper()
            moves.append(MoveChoice(engine_move, "eng", None, c)) 

        return moves

    def add_sample_line(self, log_node: Node, depth: int = 5):
        sys.stderr.write(f"\nAdding sample line for {fen(log_node)}...")
        leaf_node = True
        try:
            e = self.query(fen(log_node), "eval").top(2) 
            # TODO: ^ reliable way to know in advance how many lines we will know, so that we never call top(1) before top(2)

            self.set_question_marks(log_node)

            our_move_choices = self.generate_moves_us(log_node)
            best_move_child = self._add_variation(log_node, our_move_choices[0].move, to_main=True)
            if our_move_choices[0].reason == "tr":
                self.annotate_transposition(self.cache[fen(best_move_child)].TTed, best_move_child)
                return
            
            for m in our_move_choices[1:]:
                child = self._add_variation(log_node, m.move)
                update_comment(child, m.comment)


            nags = self.nags_our_move(best_move_child)
            best_move_child.nags.update(nags)

            if self.only_move_criterion(fen(log_node)) and depth<=0: # don't want to finish with an obvious move
                depth += 2

            if depth <= 0:  # even if depth was 0 we first add a move for ourselves
                return

            if e[0].eval > 2 and len(e) > 1 and e[1].eval > 2:
                # if self.they_are_lost(node):
                update_comment(best_move_child, f"Eval: {e[0].eval:.2f}", True)
                return
           
            opponent_move_choices = self.generate_moves_them(best_move_child, maybe_use_engine=True)

            for move, reason, _, comment in opponent_move_choices:
                reply_child = self._add_variation(best_move_child, move)
                update_comment(reply_child, comment)                

                leaf_node = False

                self.add_sample_line(reply_child,depth=depth - 2) 

        finally: # add an evaluation nag at the end of the line
            if leaf_node and abs(our_move_choices[0].eval) > 0.3:
                best_move_child.nags.add(eval_to_nag(pov_eval_to_white_eval(our_move_choices[0].eval, self.options.side)))

        
    def set_question_marks(self, node: Node, eval_query="eval"):
        pp = node.parent.parent
        if pp is None:
            return
        eval_was = self.query(fen(pp), eval_query).best_eval() # TODO
        eval_became = self.query(fen(node), eval_query).best_eval()
        node.nags.update(compute_question_marks(eval_was, eval_became))

    def nags_our_move(self, node: Node):
        nags = []
        if self.move_is_important(node): # TODO: if the move is important, increase added depth, or show why the alternative is worse
            nags.append(chess.pgn.NAG_WHITE_ZUGZWANG)
        return nags 

    def move_is_important(self, node: Node):
        p = node.parent
        freq = self.move_freq(node)
        if freq == -1:
            return False # can't decide without db
        top_lines = self.query(fen(p), "eval").top(2)
        if len(top_lines) < 2:
            return False
        eval1 = top_lines[0].eval
        eval2 = top_lines[1].eval
        if (freq < 0.4 and
            eval1 - eval2 > 0.25 and
            eval1 - eval2 > 0.25*eval2):
            return True
        
        return False
    
    def only_move_criterion(self, fen: str) -> bool:
        evals = self.query(fen, "eval").top(2)
        if len(evals) < 2:
            return True
        return only_move_criterion(evals[0].eval, evals[1].eval)

    def _add_variation(self, node: Node, move: Union[str, chess.Board], to_main: bool = False):
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        if to_main:
            child = node.add_main_variation(move)
        else:
            child = node.add_variation(move)
        self._record_position_in_TT(child)
        self.moves_added += 1
        return child

    def _record_position_in_TT(self, node): # TODO: when do we add?
        if not self.cache[fen(node)].TTed:
            self.cache[fen(node)].TTed = node

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
    return fen.rstrip(" -0123456789")

def side(fen: str) -> chess.Color:
    try:
        side = fen.split(' ')[1]
        return WHITE if side == 'w' else BLACK
    except IndexError:
        return "Invalid FEN format"

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

def color_from_freq(freq) -> str:
    '''returns the color for an arrow, depending on
    move's frequency'''
    if freq > 0.5:
        return "red"
    if freq > 0.3:
        return "green"
    return "yellow"


def process_color_option(options):
    options.color = BLACK if options.color == 1 else WHITE

def negate_color(color):
    return WHITE if color==BLACK else BLACK

def pov_eval_to_white_eval(eval_pov: float, pov: chess.Color) -> float:
    return eval_pov if pov == WHITE else -eval_pov

def eval_to_nag(eval_pawns: float) -> int:
    a = abs(eval_pawns)

    if a < 0.32:
        return chess.pgn.NAG_QUIET_POSITION # =
    elif a < 0.75:
        return (chess.pgn.NAG_WHITE_SLIGHT_ADVANTAGE if eval_pawns > 0 
                else chess.pgn.NAG_BLACK_SLIGHT_ADVANTAGE)
    elif a < 1.8:
        return (chess.pgn.NAG_WHITE_MODERATE_ADVANTAGE if eval_pawns > 0 
                else chess.pgn.NAG_BLACK_MODERATE_ADVANTAGE)
    else:
        return (chess.pgn.NAG_WHITE_DECISIVE_ADVANTAGE if eval_pawns > 0 
                else chess.pgn.NAG_BLACK_DECISIVE_ADVANTAGE)


def compute_question_marks(eval_was: float, eval_became: float) -> list[int]:
    if eval_was > 2.5 or eval_became < 0.4:
        return []
    if eval_became - eval_was > 2:
        return [chess.pgn.NAG_BLUNDER]
    elif eval_became - eval_was > 0.9:
        return [chess.pgn.NAG_MISTAKE]
    elif eval_became - eval_was > 0.4:
        return [chess.pgn.NAG_DUBIOUS_MOVE]
    return []

    

def checkpoint_pgn(game, output_path: str):
    dir_ = os.path.dirname(output_path)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=dir_,
        delete=False
    ) as tmp:
        tmp.write(str(game))   # or game.accept(exporter)
        tmp.flush()
        os.fsync(tmp.fileno())

    os.replace(tmp.name, output_path)

TraversalPolicy = namedtuple("TraversalPolicy", ["start_ply", "end_ply", "check_alternatives"], defaults=(0, 1000, False))

# @dataclass
# class TraversalPolicy:
#     start_ply : int = 0
#     end_ply : int = 1000
#     check_alternatives: bool = False

def _traverse(node: Node,
                visit: Callable = None,
                post: Callable = None,
                reasons_to_stop: Callable = None,
                tp: TraversalPolicy = TraversalPolicy(),
                side: chess.Color = WHITE,
                progress = None):
    start_ply, end_ply, check_alternatives = tp

    child_results = []

    v_res = None
    if visit and start_ply <= node.ply() <= end_ply:
        v_res = visit(node)
        if progress:
            progress.step()
            # node.comment += f"Step {s}"

    if reasons_to_stop and reasons_to_stop(node, v_res):
        return child_results

    if node.ply() == end_ply:
        return child_results

    vars = node.variations
    if node.turn()==side and not check_alternatives:
        vars = vars[:1]

    for n in vars:
        child_results.append(_traverse(n, visit, post,
            reasons_to_stop, tp, side, progress))

    if post:
        if start_ply <= node.ply() <= end_ply:
            if progress:
                progress.step()
        return post(node, child_results)
    return child_results

def safe_get_games(opening_explorer: berserk.OpeningStatistic, *args, max_attempts=5, base_delay=30.0, **kwargs):
    '''Query the database, retrying if HTTP 429 is raised
        (which means we query too often)'''
    time.sleep(0.1)
    for attempt in range(max_attempts):
        try:
            sys.stderr.write("\n querying the DB...")
            games = opening_explorer.get_lichess_games(*args, **kwargs, ratings=ratings, speeds=speeds)
            return games

        except berserk.exceptions.ResponseError as e:
            if e.response is not None and e.response.status_code == 429:
                # exponential backoff
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                sys.stderr.write(f"\n 429, {attempt}")
        except Exception as e:
            # raise  # not a 429 → bubble up
            sys.stderr.write(f"\n {e}, {attempt}")

    raise RuntimeError("Too many 429s – giving up")

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

def uci_from_lichess_to_pgn(uci: str):
    if uci == 'e1h1':
        return 'e1g1'
    if uci == 'e8h8':
        return 'e8g8'
    if uci == 'e1a1':
        return 'e1c1'
    if uci == 'e8a8':
        return 'e8c8'
    return uci

def uci_from_pgn_to_lichess(uci: str): 
    if uci == 'e1g1':
        return 'e1h1'
    if uci == 'e8g8':
        return 'e8h8'
    if uci == 'e1c1':
        return 'e1a1'
    if uci == 'e8c8':
        return 'e8a8'
    return uci

def stats_for_uci(games: dict, uci: str):
    return next((m for m in games['moves'] if m['uci'] == uci_from_pgn_to_lichess(uci)), None) # {}

def arrow_from_uci(uci: str, *args, **kwargs) -> chess.svg.Arrow:
    return chess.svg.Arrow(ord(uci[0])-97 + 8*(int(uci[1])-1), ord(uci[2])-97 + 8*(int(uci[3])-1), *args, **kwargs)

def mark_based_on_freq_them(node: Node, freq: float): # should also depend on the number of games
    if 0.55 < freq <= 0.76:         # above that consider the move obious and don't mark
        mark = chess.svg.Arrow(node.move.to_square, node.move.to_square, color="red") # yes that's how you mark squares with this library clownface.png
        node.set_arrows([mark])
    elif 0.4 < freq <= 0.55:
        mark = chess.svg.Arrow(node.move.to_square, node.move.to_square, color="yellow")
        node.set_arrows([mark])

def mark_based_on_freq_us(node: Node, freq: float):
    if freq <= 0.15:
        mark = chess.svg.Arrow(node.move.from_square, node.move.to_square, color="green")
        node.set_arrows([mark])


def total_games(game_data: dict):
    return game_data['white'] + game_data['draws'] + game_data['black']

def total_decisive_games(game_data: dict):
    return game_data['white'] + game_data['black']

def score_rate(game_data: dict, side: Union[str, chess.Color]):
    if isinstance(side, chess.Color):
        side = 'white' if side == WHITE else 'black'
    return (game_data[side] + 0.5 * game_data['draws']) / total_games(game_data)

def win_rate(game_data: dict, side: Union[str, chess.Color]):
    if isinstance(side, chess.Color):
        side = 'white' if side == WHITE else 'black'
    return game_data[side]/total_decisive_games(game_data)

def move_frequency(move_data: dict, games: dict):
    return total_games(move_data)/total_games(games)

def move_freq_str(move_data: dict, games: dict):
    return total_games(move_data), total_games(games)

def only_move_criterion(eval1: float, eval2: float):
    if eval1 - eval2 > max(1, eval1*0.5):
        return True
    return False

def get_or_create_child(log_node, move):
    for child in log_node.variations:
        if child.move == move:
            return child
    return log_node.add_variation(move)


def initialize_engine(exePath, conf=None):
    sys.stderr.write(f"Initializing {exePath}\n")
    engine = chess.engine.SimpleEngine.popen_uci("C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe")
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


class FirstDifference(NamedTuple):
    ply: int
    move: chess.Move


def first_difference(n1: Node, n2: Node) -> Optional[FirstDifference]:
    """
    Compare two move sequences ending at `n1` and `n2` and return the first move
    that differs in `n1`, together with the ply at which it occurs.

    If `n2` is a prefix of `n1`, returns the next move from `n1`.
    If there is no differing move in `n1` (identical lines, or `n1` is shorter),
    returns None.
    """
    stack1: list[chess.Move] = []
    stack2: list[chess.Move] = []

    cur = n1
    while cur is not None and getattr(cur, "move", None) is not None:
        stack1.append(cur.move)
        cur = cur.parent

    cur = n2
    while cur is not None and getattr(cur, "move", None) is not None:
        stack2.append(cur.move)
        cur = cur.parent

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

def gap_criterion(move: str, move_freq: float, freq_threshold: float, min_games: int = 0, pov: chess.Color = WHITE) -> int:
    if score_rate(move, pov) > 0.75: # we assume files don't need to consider moves that lose in practice
        return 0
    if total_games(move) < min_games:
        return 0
    if move_freq >= freq_threshold:
        return 1
    if score_rate(move, pov) <= 0.4 and move_freq >= freq_threshold/3: # if a move scores well, consider it even if it is not very frequent
        # TODO: before going into this, check if the most common response transposes into something known
        return 2
    return 0


def close_engine(engine):
    print("\nClosing the engine...")
    if engine is not None: # hasattr?
        try:
            engine.close()
        except Exception as e:
            sys.stderr.write("Failed to close engine\n")
            sys.stderr.write(str(e))

if __name__ == "__main__":
    pass
