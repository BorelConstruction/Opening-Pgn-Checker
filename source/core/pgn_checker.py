from collections import defaultdict, Counter
import datetime
import os
from platform import node
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from functools import partial  # for currying
from typing import Callable, NamedTuple, Optional, Union, TypeVar
from collections.abc import Callable
# from __future__ import annotations # to resolve Runner<->EvalProvider... I'll just annotate with a str.

VisitResultT = TypeVar("VisitResultT")

import chess
from chess.pgn import GameNode as Node
from chess import WHITE
from chess import BLACK


from .options import CheckerOptions, DEBUG_MODE
from .timer import clock
from .database import *
from .runner import *

# TODO: identify unobvious moves
# TODO: exclaims for their moves (if it doesn't drop the eval while the most popular one does?)
# TODO: min games depends on ply

# TODO: cap freq from above

# TODO: similar positions

# TODO: remember settings for every input file


STAT_SIGNIFICANCE_THRESHOLD = 15 # TODO: smarter choice
FREQ_MARK_THRESHOLD = 10


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


class PgnChecker(Runner):
    def __init__(self, options: CheckerOptions, progress_cb=None, report_cb=None):
        super().__init__(options, progress_cb, report_cb)

        self.options.side = WHITE if self.options.play_white else BLACK
        self.options.starting_pos = fen(self.options.starting_pos) # all fens are normalized
        self.options.adaptive_an = True # TODO

        self.set_output_pgn()

        self.convert_moves_to_plies()

    def convert_moves_to_plies(self):
        self.options.start_ply = ply_from_move_number(self.options.start_move)
        self.options.end_ply = ply_from_move_number(self.options.end_move)
        self.options.added_depth = (self.options.added_depth + 1) // 2

    def pipeline(self):
        # a skeleton for when the logic gets more complex
        pipeline = []
        if "fill_gaps" in self.options.actions:
            pipeline.append(self.find_fill_gaps)
        if "mark_moves" in self.options.actions:
            pipeline.append(self.mark_moves)
        if "seek_consistency" in self.options.actions:
            pipeline.append(self.seek_move_consistency)
        return pipeline

    def set_output_pgn(self):
        output_dir = "output pgns" # should we give the user a choice?
        input_stem = os.path.splitext(os.path.basename(self.options.input_pgn))[0]
        timestamp = datetime.datetime.now().strftime("%d-%m_%H-%M-%S")
        os.makedirs(output_dir, exist_ok=True)
        self.options.output_pgn = os.path.join(
            output_dir,
            f"{input_stem} -- {timestamp}.pgn",
        )

    def _default_cache_path(self) -> str:
        base = "cache"
        name = "cache"
        if self.options.input_pgn:
            name = os.path.splitext(os.path.basename(self.options.input_pgn))[0]
        return os.path.join(base, f"{name}.json")
    
    def make_headers(self, game):
        if game.headers["Event"] == '?':
            game.headers["Event"] = f'''plies {self.options.start_ply}-{self.options.end_ply}'''
        else:
            game.headers["Event"] = game.headers["Event"] + f''' | plies {self.options.start_ply}-{self.options.end_ply}'''
    
    @clock
    def run(self):
        try:
            with open(self.options.input_pgn, encoding="utf-8") as pgnFile:
                while True:
                    node = chess.pgn.read_game(pgnFile)

                    if node is None:
                        break

                    self.fill_the_TT(node)
                    
                    self.cache.enable_auto_save()
                    total = sum(1 for _ in self.cache if self.cache[_].TTed)
                    self.progress.set_total(total)
                    output_game = node  # no need to copy the way it currently works
                    self.make_headers(output_game)

                    self.set_starting_pos(output_game)
                    node = self.starting_node

                    sys.stderr.write('starting to traverse...')

                    for action in self.pipeline():
                        action(node)

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
    
    def fill_the_TT(self, root_node: Node):
        def visit(node: Node):
            self._record_position_in_TT(node)

        self._traverse(root_node, visit=visit)

    def find_fill_gaps(self, game_node: Node):
        self.report_message("Finding gaps...")
        gaps = self.find_gaps(game_node)
        self.report_message("Filling gaps...")
        self.progress.reset()
        self.fill_gaps(gaps)

    def find_gaps_local(self, node: Node) -> Optional[GapsInfo]:      
        pgn_ucis = [m.move.uci() for m in node.variations]

        if node.turn() == self.options.side:
            if not pgn_ucis:
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
            self.report(RunnerReport(kind="position", position=pos_snap,
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
        def post(node, child_results: list[GapsInfo], v_res):
            all_gaps = sum([c for c in child_results if c is not None], [])
            if node.ply() < self.options.start_ply:
                return all_gaps # only propagate the results
            gaps_local = self.find_gaps_local(node)
            if gaps_local:
                all_gaps.append(gaps_local)
            return all_gaps
        def reasons_to_stop(node, _): 
            return node.comment.startswith(('tr ', 'Tr ', 'Transp ', 'transp ', 'Transposes', 'transposes'))
        return self._traverse(game, post=post, reasons_to_stop=reasons_to_stop)

    def gaps_local(self, node: Node):
        gaps_info = self.find_gaps_local(node)
        if gaps_info:
            self.act_on_gap_data_local(node, gaps_info)

    def mark_move_local(self, log_node: Node):
        mark_fn = mark_based_on_freq_us if log_node.turn() == self.options.side else mark_based_on_freq_them
        for n in log_node.variations:
            if self.total_games(n) < FREQ_MARK_THRESHOLD:
                continue
            freq = self.move_freq(log_node, n.move)
            mark_fn(n, freq)

    def mark_moves(self, log_node):
        # TODO: if a move is frequent, promote it?
        self.report(RunnerReport(kind="position", position=PositionSnapshot("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 0)))
        self.report_message("Marking moves...")
        self.progress.reset()
        self._traverse(log_node, partial(PgnChecker.mark_move_local, self))

    @staticmethod
    def annotate_transposition(first_occurrence: Node, node: Node):
        diff = first_difference(first_occurrence, node)
        if diff is not None:
            update_comment(node, f"Tr to {whole_move_from_ply(diff.ply)}{diff.move}")
        else:
            update_comment(node, "Two branches with the same move -- fix this")

    def find_transposition_move(self, board: Union[Node, chess.Board]) -> Optional[chess.Move]:
        if isinstance(board, Node):
            board = board.board()
        for move in board.legal_moves:
            board.push(move)
            cached = self.cache.get(fen(board))
            board.pop()
            if cached is not None and cached.TTed:
                return move
        return None
    
    def seek_transposition(self, node: Node) -> Optional[tuple[chess.Move, str]]:
        '''
        Try to find a move that likely transposes back into the files.
        Currently very naive.
        
        Returns move and comment.
        '''
        stats = self.query(fen(node), "db_lichess")
        db_moves = stats.get("moves", [])
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
                        move2 = board.san(tr_move)
                        board.pop()
                        move1 = uci_to_san(most_popular_uci, board)
                        c = f"Likely Tr after {move1} and {move2}".upper()
                        return (move, c)

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
                        MoveChoice(tr_move, "tr", tr_eval, f"To Tr, {eval:.2f} > {tr_eval:.2f}..")]
        to_tr = self.seek_transposition(node)

        if to_tr: # TODO: abstract these two blocks
            to_tr_move, comment = to_tr
            board = node.board()
            board.push(to_tr_move)
            to_tr_eval = self.query(fen(board), "eval").best_eval()
            board.pop()
            if to_tr_eval >= eval - 0.10:
                eval = to_tr_eval
                return [MoveChoice(to_tr_move, "to_tr", to_tr_eval, comment)]
            else:
                return [self.better_engine_move(node),
                        MoveChoice(to_tr_move, "to_tr", to_tr_eval, f"To Tr, {eval:.2f} > {to_tr_eval:.2f}")]
        return [self.better_engine_move(node)]
    
    def generate_moves_them_db(self, stats: dict) -> list[MoveChoice]:
        moves = []
        db_moves = stats.get("moves", [])
        for m in db_moves:
            crit = gap_criterion(m, move_frequency(m, stats), self.options.freq_threshold, 
                                        self.options.min_games, pov=self.options.side) 
            if crit == 1:
                moves.append(MoveChoice(chess.Move.from_uci(uci_from_lichess_to_pgn(m['uci'])), None, "db"))
            if crit == 2:
                c = f"well-scoring, ".upper() + str(score_rate(m, self.options.side))[:4] + f" in {total_games(m)} games" if DEBUG_MODE else ""
                moves.append(MoveChoice(chess.Move.from_uci(uci_from_lichess_to_pgn(m['uci'])), None, "good", c))
        return moves

    def generate_moves_them(self, node: Node, maybe_use_engine: bool = False) -> list[MoveChoice]:
        moves = []
        stat_list = [self.query(fen(node), type) for type in self.options.db_types]
        for stats in stat_list:
            moves += self.generate_moves_them_db(stats)
        moves = remove_duplicates(moves, equality_rel=lambda m:m.move)

        # if no DB moves and option enabled, add an engine move
        if not moves and self.options.use_engine_for_them and maybe_use_engine:
            engine_move = self.query(fen(node), "q-eval").move
            c = "Engine".upper() if DEBUG_MODE else ""
            moves.append(MoveChoice(engine_move, "eng", None, c)) 

        return moves

    def add_moves_us(self, node: Node) -> tuple[Node, MoveChoice]:
        our_move_choices = self.generate_moves_us(node)
        best_choice = our_move_choices[0]
        best_move_child = self._add_variation(node, best_choice.move, to_main=True)

        if best_choice.reason == "tr":
            self.annotate_transposition(self.cache[fen(best_move_child)].TTed[0], best_move_child)
            return best_move_child, best_choice

        for choice in our_move_choices[1:]:
            child = self._add_variation(node, choice.move)
            update_comment(child, choice.comment)

        best_move_child.nags.update(self.nags_our_move(best_move_child))
        return best_move_child, best_choice

    def add_moves_them(self, node: Node) -> list[Node]:
        children = []
        opponent_move_choices = self.generate_moves_them(node, maybe_use_engine=True)

        for choice in opponent_move_choices:
            reply_child = self._add_variation(node, choice.move)
            update_comment(reply_child, choice.comment)
            children.append(reply_child)

        return children

    def add_sample_line(self, log_node: Node, depth: int = 5):
        sys.stderr.write(f"\nAdding sample line for {fen(log_node)}...")
        leaf_node = True
        best_move_child = log_node
        best_choice: Optional[MoveChoice] = None
        try:
            self.report_position(log_node, message=f"Adding sample line... Depth: {depth}." 
                                 + f"\n {moves_to_algebraic(node_moves(log_node))}")

            if log_node.turn() == self.options.side:
                e = self.query(fen(log_node), "eval").top(2) 
                # TODO: ^ reliable way to know in advance how many lines we will know, so that we never call top(1) before top(2)

                self.set_question_marks(log_node)

                best_move_child, best_choice = self.add_moves_us(log_node)
                if best_choice.reason == "tr":
                    return

                if self.only_move_criterion(fen(log_node)) and depth <= 0:  # don't want to finish with an obvious move
                    depth += 2

                if depth <= 0:  # even if depth was 0 we first added a move for ourselves
                    return

                if (e[0].eval > 2 and len(e) > 1 and e[1].eval > 2):
                    # self.we_are_winning
                    update_comment(best_move_child, f"Eval: {e[0].eval:.2f}", True)
                    return
                depth -= 1

            children = self.add_moves_them(best_move_child)
            depth -= 1
            for child in children:
                self.add_sample_line(child, depth=depth)
            leaf_node = not children

        finally: # add an evaluation nag at the end of the line
            if (
                leaf_node
                and best_choice is not None
                and best_choice.eval is not None
                and abs(best_choice.eval) > 0.3
            ):
                best_move_child.nags.add(
                    eval_to_nag(pov_eval_to_white_eval(best_choice.eval, self.options.side))
                )

        
    def set_question_marks(self, node: Node, eval_query="eval"):
        pp = node.parent.parent
        if pp is None:
            return
        eval_was = self.query(fen(pp), eval_query).best_eval()
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
    
    def make_move_coupling_dict(self, node: Node):
        # Key: previous move UCI (i.e., node.move at positions where it's our turn).
        # Value: reply nodes (children) that result from different replies in that position.
        self.move_coupling: dict[str, list[Node]] = defaultdict(list)
        def visit(n: Node):
            if n.turn() != self.options.side or n.move is None:
                return
            key = n.move.uci()
            for child in self.variations(n):
                self.move_coupling[key].append(child)
        self._traverse(node, visit)

    def move_replacements(self, move_coupling: dict[str, list[Node]], *, eval_eps: float = 0.15, sleep_s: float = 2.0):
        """
        Try to make replies more same for same opponent moves.

        Technically:
        For each move A of opponent, see if there is a move B that is suggested only once
        as a respose to A. If so, try replacing B with other moves suggested in the file.
        (Thus moving closer to the dream rule "always play this if they play that")
        """
        move_replacements = []
        for prev_move_uci, responses in move_coupling.items():
            if len(responses) <= 1:
                continue

            reply_ucis = [child.move.uci() for child in responses]
            counts = Counter(reply_ucis)
            unique_replies = [uci for uci, c in counts.items() if c == 1]
            if not unique_replies:
                continue

            all_replies = [uci for uci in counts.keys()]

            for unique_reply_uci in unique_replies:
                unique_node = next(
                    (child for child in responses if child.move and child.move.uci() == unique_reply_uci),
                    None,
                )

                base_eval = self.query(fen(unique_node), "q-eval").eval

                parent = unique_node.parent

                c = f"prev {prev_move_uci} | unique {unique_reply_uci} | "
                self.report_position(
                    parent,
                    message=(
                        f"{c}(bucket size {len(responses)}). "
                        f"q-eval after {unique_reply_uci}: {base_eval:+.2f}"
                    )
                )

                board = parent.board()
                for alt_reply_uci in all_replies:
                    if alt_reply_uci == unique_reply_uci:
                        continue
                    alt_move = chess.Move.from_uci(alt_reply_uci)

                    if alt_move not in board.legal_moves:
                        self.report_position(
                            parent,
                            message=f"{c}Try {alt_reply_uci}: illegal here (skipped).",
                        )
                        continue

                    alt_eval = self.q_eval_move(parent, alt_move).eval

                    diff = abs(alt_eval - base_eval)
                    status = "Replaceable" if diff <= eval_eps else ""
                    self.report_position(
                        parent,
                        message=(
                            f"{c}Try {alt_reply_uci}: q-eval {alt_eval:+.2f} (diff {diff:.2f}) {status}".rstrip()
                        ),
                    )
                    if status=="Replaceable":
                        move_replacements.append((parent, alt_move, diff))
                        # time.sleep(10)
                        break # only try to replace with one alternative


                if sleep_s and sleep_s > 0:
                    # time.sleep(sleep_s)
                    pass
        return move_replacements

    def seek_move_consistency(self, node: Node):
        self.make_move_coupling_dict(node)
        self.moves_added = 0
        l = self.move_replacements(self.move_coupling, eval_eps=0.15, sleep_s=1.0)
        for parent, alt_move, diff in self.progress.iter(l):
            child = next((child for child in parent.variations if child.move == alt_move), None)
            if child is not None:
                continue # already have this move as a variation, no need to add it again
            child = self._add_variation(parent, alt_move)
            update_comment(child, f"For consistency, diff is {diff:.2f}")
            self.add_sample_line(child)


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



def only_move_criterion(eval1: float, eval2: float):
    if eval1 - eval2 > max(1, eval1*0.5):
        return True
    return False


def gap_criterion(move_data: dict, move_freq: float, freq_threshold: float, min_games: int = 0, pov: chess.Color = WHITE) -> int:
    if score_rate(move_data, pov) > 0.75: # _datawe assume files don't need to consider moves that lose in practice
        return 0
    if total_games(move_data) < min_games or total_games(move_data) < min_games:
        return 0
    if move_freq >= freq_threshold:
        return 1
    if score_rate(move_data, pov) <= 0.4 and move_freq >= freq_threshold/3: # if a move scores well, consider it even if it is not very frequent
        # TODO: before going into this, check if the most common response transposes into something known
        return 2
    return 0

def remove_duplicates(lst: list, equality_rel: Optional[Callable] = lambda x:x) -> list:
    seen = []
    result = []
    for item in lst:
        if equality_rel(item) not in seen:
            seen.append(equality_rel(item))
            result.append(item)
    return result


if __name__ == "__main__":
    pass
