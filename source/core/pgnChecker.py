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
from abc import ABC, abstractmethod

import berserk
import berserk.exceptions
import chess
from chess.pgn import GameNode as Node
# import chess.pgn
# import chess.svg
from chess import WHITE as WHITE
from chess import BLACK as BLACK


from .options import Options
from .timer import clock
from .caching import CacheDict

# TODO: trim obvious moves (don't add the last move if it's forced)
# TODO: find unobvious moves
# TODO: exclaims for their moves (if it doesn't drop the eval while the most popular one does?)

# TODO: node_count accounting for us only considering main lines
# TODO: engine management

# TODO: lower move_freq_thresh for better moves
# TODO: cap freq from above

# TODO: let the engine play where no moves are found in the DB

# TODO: similar positions

# sys.stdout.reconfigure(encoding='utf-8')


# freq_threshold = 0.22
# game_amount_threshhold = 4
# starting_ply = 10
# end_ply = 40

ratings = ["2200", "2500"] # TODO: make parameters
speeds = ["blitz", "rapid", "classical"]

ratings_n = ["1900", "2200"]
speeds_n = ["blitz", "rapid", "classical"]

DEBUG_MODE = True

# output_pgns = ['Output.pgn']

sf_path = "C:\\Users\\Vadim\\Downloads\\stockfish-windows-x86-64-avx2.exe"

def add_debug_comment(node, message):
    if DEBUG_MODE:
        node.comment += f"{message}"

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

class Gap(NamedTuple):
    move: str
    freq: float
    game_num: int


@dataclass
class GapsInfo:
    node: Node
    gaps: list[Gap] = field(default_factory=list)

    def add_gap(self, move: str, freq: float, game_num: int):
        self.gaps.append(Gap(move, freq, game_num))

    def __bool__(self):
        return bool(self.gaps)

    def __iter__(self):
        return iter(self.gaps)

class PosCache:
    def __init__(self, fen: str):
        self.fen = fen
        self.TTed : bool = False # seen in the relevant part of the pgn file
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
        return {
            "fen": self.fen,
            # "TTed": self.TTed, # we don't want to cache this, this is cheap.
            # We actually want it to reset between runs, or we may find undexpected transpositions
            "data": data,
        }
    
    @classmethod
    def from_dict(cls, checker: 'PgnChecker', payload: dict) -> "PosCache":
        pc = cls(payload["fen"])
        # pc.TTed = bool(payload.get("TTed", False))
        pc.TTed = False
        data = payload.get("data", {})
        if "db_lichess" in data:
            pc._data["db_lichess"] = data["db_lichess"]
        if "eval" in data:
            pc._data["eval"] = EvalProvider.from_dict(checker, pc.fen, data["eval"])
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

            "eval": lambda fen: EvalProvider(self, fen)
            # evaluate_position(self.engine, position=fen, pov=self.options.side, options=self.options) # TODO: pov
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
            # self.cache[fen(node)] = PosCache(fen(node))
            if (self.options.start_ply <= node.ply() <= self.options.end_ply):
                # and node.turn() == self.options.side):
                self.cache[fen(node)].TTed = True

        self._traverse(root_node, visit=visit)


    def _traverse(self, node: Node,
                    visit: Callable = None,
                    post: Callable = None,
                    reasons_to_stop: Callable = None):
        # sys.stderr.write(str(node.ply()) + "\n")
        child_results = []

        if visit and self.options.start_ply <= node.ply() <= self.options.end_ply:
            visit(node)
            s = self.progress.step()
            # node.comment += f"Step {s}"

        if node.ply() == self.options.end_ply:
            return child_results


        vars = node.variations
        if node.turn()==self.options.side and not self.options.check_alternatives: # or we could leave only the main lines
            vars = vars[:1]

        for n in vars:
            child_results.append(self._traverse(n, visit, post))

        if post:
            if self.options.start_ply <= node.ply() <= self.options.end_ply:
                self.progress.step()
            return post(node, child_results)
        return child_results

    def init_client(self):
        token = getattr(self.options, "_token", None)
        if token:
            lichessClient = berserk.Client(session=berserk.TokenSession(token))
        else:
            lichessClient = berserk.Client()
        self.opening_explorer = lichessClient.opening_explorer

    @clock
    def run(self):
        self.load_cache()
        try:
            cache_size_after_load = len(self.cache)

            self.init_client()

            self.lines_added = 0

            with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
                    log_node = chess.pgn.read_game(pgnFile)
                    self.log_node = log_node
            with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
                num = 0
                while True:
                    num += 1
                    game = chess.pgn.read_game(pgnFile)

                    if game is None:
                        break

                    # total = self.count_nodes(game)
                    self.fill_the_TT(game)
                    self.cache.enable_auto_save()
                    cache_size_after_tt = len(self.cache)
                    total = cache_size_after_tt # - cache_size_after_load
                    self.progress.set_total(total)
                    node = game
                    # output_game = chess.pgn.Game()
                    output_game = node
                    if game.headers["Event"] == '?':
                        output_game.headers["Event"] = f'''plies {self.options.start_ply}-{self.options.end_ply}'''
                    else:
                        output_game.headers["Event"] = game.headers["Event"] + f''' | plies {self.options.start_ply}-{self.options.end_ply}'''
                    # output_game.headers["White"] = game.headers["White"]
                    # output_game.headers["Black"] = game.headers["Black"]
                    sys.stderr.write('starting to traverse...')
                    self.find_fill_gaps(node)
                    self.mark_moves(node)
                    print(output_game, file=open(self.options.output_pgn, "a", encoding="utf-8"), end="\n\n") # "a" for adding

        except Exception as e:
            sys.stderr.write(f"Error: {traceback.format_exc()}\n")
            raise e
            # close_engine(engine) # TODO: rewrite with "with" or something

        finally:
            try:
                self.save_cache()
            except Exception as exc:
                sys.stderr.write(f"Failed to save cache: {exc}\n")
            self._finalizer()

        return f"Added {self.lines_added} lines"
    
    def find_fill_gaps(self, game_node: Node):
        self.report_message("Finding gaps...")
        gaps = self.find_gaps(game_node)
        self.report_message("Filling gaps...")
        self.progress.reset()
        self.fill_gaps(gaps)

    def find_local_gaps(self, node: Node):
        gap_info = GapsInfo(node)

        if node.comment.startswith('tr') or node.comment.startswith('Tr'): # TODO: add to reasons_to_stop
            return False

        side = self.options.side
        opposite_side = negate_color(side)
        if node.turn() == opposite_side:
            pgn_moves = [n.move.uci() for n in node.variations]
            games = self.query(fen(node), "db_lichess")
            # if total_games(games) < self.options.min_games: # TODO
            #     # log_node.comment += f'Too few games ({total_games(games)}), returning...\n'
            #     return gap_found

            moves_to_analyze = []
            for m in games['moves']:
                m_uci = m['uci']

                if uci_from_lichess_to_pgn(m_uci) in pgn_moves:
                    continue

                freq = move_frequency(m, games)

                if freq >= self.options.freq_threshold and total_games(m) >= self.options.min_games:
                    gap_info.add_gap(m_uci, freq, total_games(m))

        return gap_info
    
    def fill_gaps(self, gaps: list[GapsInfo]):
        for gaps_info in self.progress.iter(gaps):
            node = gaps_info.node
            self.act_on_gap_data_local(node, gaps_info)

    def act_on_gap_data_local(self, log_node, gap_data: GapsInfo):
        annotate = False # should be True if we only do find_gaps
        arrows = []
        comment = ''

        for uci, freq, game_num in gap_data:
            pos_snap = PositionSnapshot(fen(log_node), log_node.ply(), last_move_uci=uci)
            self.report(CheckerReport(kind="position", position=pos_snap,
                                    message=f"Filling gaps... \n{game_num} games (" + str(freq)[2:4] + "%)"))
            comment += uci + ': ' + (str(freq)[:4]) + ', '
            # arrows.append([chess.parse_square(m_uci[:2]), chess.parse_square(m_uci[2:4])])
            arrows.append(arrow_from_uci(uci, color=color_from_freq(freq)))
            child = self._add_variation(log_node, uci)
            child.starting_comment = 'SUGGESTED LINE:'
            self.lines_added += 1
            self.add_sample_line(child, depth=self.options.added_depth)
        if annotate:
            log_node.comment += comment
            log_node.set_arrows(arrows)
                # if report_state:
                #     pos = PositionSnapshot(child.board().fen(), child.ply(), last_move_uci=move_uci)
                #     report_state(CheckerReport(kind="gap position", position=pos,
                #                               message=f"{game_amount} of {total_games(games)} games (" + str(game_amount/total_games(games))[2:4]
                #                               + f"%) \n{self.lines_added} lines added"))


    def find_gaps(self, game: Node):
        def post(node, child_results: list[GapsInfo]):
            all_gaps = sum(child_results, [])
            if node.ply() < self.options.start_ply:
                return all_gaps # only propagate the results
            local_gaps = self.find_local_gaps(node) 
            if local_gaps:
                all_gaps.append(local_gaps)
            return all_gaps
        return self._traverse(game, post=post)

    def gaps_local(self, node: Node):
        gaps_info = self.find_local_gaps(node)
        if gaps_info:
            self.act_on_gap_data_local(node, gaps_info)

    def traverse_and_fill_gaps(self, node: Node,
            report_state=None) -> bool:
        self._traverse(node, partial(PgnChecker.gaps_local, self))

    def mark_move_local(self, log_node: Node):
        pgn_moves = [n.move.uci() for n in log_node.variations]
        games = self.query(fen(log_node), "db_lichess")
        for m in games['moves']:
            m_uci = m['uci']
            freq = move_frequency(m, games)
            if uci_from_lichess_to_pgn(m_uci) in pgn_moves:
                move_obj = chess.Move.from_uci(uci_from_lichess_to_pgn(m_uci))
                target_node = log_node.variation(move_obj)
                if log_node.turn() == self.options.side:
                    mark_based_on_freq_us(target_node, freq)
                else:
                    # line1, line2 = quick_eval(self.engine, fen(target_node), pov=self.options.side, multipv=2)
                    # target_node.comment += f"Eval: {line1[0]:.2f}, {line2[0]:.2f} pawns. "
                    mark_based_on_freq_them(target_node, freq)

    def mark_moves(self, log_node):
        # TODO: if a move is frequent, promote it?
        self.report(CheckerReport(kind="position", position=PositionSnapshot("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 0)))
        self.report_message("Marking moves...")
        self.progress.reset()
        self._traverse(log_node, partial(PgnChecker.mark_move_local, self))

    @staticmethod
    def annotate_transposition(node: Node):
        node.comment = (node.comment + " Transp.").lstrip()

    def _find_transposition_move(self, node: Node) -> Optional[chess.Move]:
        board = node.board()
        for move in board.legal_moves:
            board.push(move)
            cached = self.cache.get(fen(board))
            board.pop()
            if cached is not None and cached.TTed:
                return move
        return None

    def add_sample_line(self, log_node: Node, depth: int = 5):
        leaf_node = True
        try:
            self.set_question_marks(log_node)

            eval, best_move = self.query(fen(log_node), "eval").top(2)[0] # top(2) because we'll need it later for nags

            tr_move = self._find_transposition_move(log_node)
            if tr_move:
                board = log_node.board()
                board.push(tr_move)
                tr_eval = self.query(fen(board), "eval").best_eval()
                board.pop()
                if tr_eval >= eval - 0.15:
                    self.annotate_transposition(log_node)
                    best_move_child = self._add_variation(log_node, tr_move, to_main=True)
                    eval = tr_eval
                    return
                else:
                    tr_child = self._add_variation(log_node, tr_move)
                    tr_child.comment += f"To transpose, ({tr_eval:.2f} vs {eval:.2f})."

            best_move_child = self._add_variation(log_node, best_move, to_main=True)
            best_fen = fen(best_move_child)
            self._record_position_in_TT(best_move_child)

            nags = self.nags_our_move(best_move_child)
            best_move_child.nags.update(nags)
            stats = self.query(best_fen, "db_lichess")

            # try:
            #     # log_node.comment += f'{stats["moves"][0]["uci"]} == {best_move.uci()} and {move_frequency(stats["moves"][0], stats)}'
            #     if stats["moves"][0]["uci"] == best_move.uci() and move_frequency(stats["moves"][0], stats) > 0.6:
            #         best_move_child.comment += ' obvious'
            # except IndexError:
            #     pass # could happen if we entered while there were still games and then "we" all resigned

            if depth <= 0:  # even if depth was 0 we first add a move for ourselves
                return

            # stats["moves"] = [{"uci": "e7e5", "white": ..., "black": ..., "draws": ..., "games": ...}, ...]
            opponent_moves = stats.get("moves", [])

            if total_games(stats) == 0:
                # best_move_child.comment += 'No games, returning...'
                return

            # 4. Filter replies by frequency
            for m in opponent_moves:
                # freq = m["games"] / total_games
                if (total_games(m) < self.options.min_games 
                    or move_frequency(m, stats) < self.options.freq_threshold):
                    continue

                reply_child = self._add_variation(best_move_child, m["uci"])

                leaf_node = False
                self.add_sample_line(reply_child,depth=depth - 2)
            
        finally: # add an evaluation nag at the end of the line
            if leaf_node and abs(eval) > 0.3:
                best_move_child.nags.add(eval_to_nag(pov_eval_to_white_eval(eval, self.options.side)))

        
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
        stats = self.query(fen(p), "db_lichess")
        our_move_uci = node.move.uci()
        our_move_freq = next((move_frequency(m,stats) for m in stats["moves"] 
                               if uci_from_lichess_to_pgn(m["uci"])==our_move_uci), 0)
        top_lines = self.query(fen(p), "eval").top(2)
        if len(top_lines) < 2:
            return False
        eval1 = top_lines[0].eval
        eval2, second_best_move = top_lines[1]
        if (our_move_freq < 0.4 and
            eval1 - eval2 > 0.25 and
            eval1 - eval2 > 0.25*eval2):
            return True
        return False

    def _add_variation(self, node: Node, move: Union[str, chess.Board], to_main: bool = False):
        if isinstance(move, str):
            move = chess.Move.from_uci(move)
        if to_main:
            child = node.add_main_variation(move)
        else:
            child = node.add_variation(move)
        self._record_position_in_TT(child)
        return child

    def _record_position_in_TT(self, node): # TODO: when do we add?
        self.cache[fen(node)].TTed = True

def fen(node: Union[Node, chess.Board]) -> str:
    if isinstance(node, chess.Board):
        return node.fen()
    return node.board().fen()

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

    if a < 0.35:
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

def quick_eval(engine, position: Union[str, chess.Board], pov=WHITE, multipv=1):
    # TODO: make a separate function for this processing?
    sys.stderr.write("\n Quick eval...")
    if isinstance(position, str):
            board = chess.Board(position)
    elif isinstance(position, chess.Board):
            board = position
    else:
        raise TypeError("position must be a FEN string or chess.Board")
    
    infos = analyse_time_limit(engine, board, time_limit=0.1, multipv=multipv)
    return process_engine_output(infos, board, pov)

def uci_from_lichess_to_pgn(uci: str): # TODO: match-case
    if uci == 'e1h1':
        return 'e1g1'
    if uci == 'e8h8':
        return 'e8g8'
    if uci == 'e1a1':
        return 'e1c1'
    if uci == 'e8a8':
        return 'e8c8'
    return uci

def uci_from_pgn_to_lichess(uci: str): # TODO: match-case
    if uci == 'e1g1':
        return 'e1h1'
    if uci == 'e8g8':
        return 'e8h8'
    if uci == 'e1c1':
        return 'e1a1'
    if uci == 'e8c8':
        return 'e8a8'
    return uci

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
    return game_data['white'] + game_data['draws'] + game_data['black']

def score_rate(game_data: dict, side: str):
    return game_data[side]/total_games(game_data)

def win_rate(game_data: dict, side: str):
    return game_data[side]/total_decisive_games(game_data)

def move_frequency(move: dict, games: dict):
    return total_games(move)/total_games(games)

def move_freq_str(move: dict, games: dict):
    return total_games(move), total_games(games)


def find_moves_with_property(node: Node, prop, opening_explorer: berserk.OpeningStatistic):
    # currently I don't want to abstract this way because of different things I want to do in the process,
    # e.g. color arrows or write more specific comments
    moves = [uci_from_pgn_to_lichess(n.move.uci()) for n in node.variations]
    fen = fen(node)
    # fen = curBoard.fen()
    games = safe_get_games(opening_explorer, position=fen)
    comment = ''
    for m in games['moves']:
        if prop(m):
            comment += m['uci'] + ', '
    return comment[:-2]


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
    
    sys.stderr.write("\n Evaluating the position...")
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

if __name__ == "__main__":
    pass
