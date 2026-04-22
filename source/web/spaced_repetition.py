from __future__ import annotations

from copy import deepcopy
from enum import Enum
import json
import os
import random
import sys
from typing import Any, Iterable, Optional, TypeVar, Union

import chess
import chess.pgn
from chess.pgn import GameNode as Node
from dataclasses import dataclass

from source.core.caching import CacheDict
from source.core.traversal import iter_nodes
from source.web.board.contracts import Circle
from source.web.board.session import UCI
from source.web.scheduler_implem import NaiveScheduler

# from source.web.app import BoardHub

from ..core.boardtools import fen, node_moves, node_san, uci_from_lichess_to_pgn, uci_from_lichess_to_pgn
from ..core.options import SpacedRepetitionOptions, DEBUG_MODE
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path
from .pgn_export import export_pgn_subtree
from .variation_tree import node_at_path, path_from_root, build_variation_tree
from .scheduler_protocol import *

# TODO: add transpotitioning moves to the move list?

K = TypeVar("K")


class MoveGrade(Enum):
    CORRECT = 1
    INCORRECT = 2
    NO_MOVES = 3

@dataclass
class MoveSignal():
    move_grade: MoveGrade
    response_node: Optional[Node] = None
    msg: str = ""


class MoveInterpreter():
    def __init__(self, session: RepertoireSession, prompt_state: PromptState):
        self._session = session

        self._prompt = prompt_state

        # to be used in self.feedback()
        self._grades = []


    def interpret(self, uci: Any) -> Signal:
        if self._prompt.off_file:
            return self._handle_off_file_guess(uci)
        return self._handle_file_guess(uci)

    def summarize(self) -> Feedback:
        pass

    def _handle_file_guess(self, uci: str) -> None:
        expected_moves = self._session.variations(self._prompt.node)
        if not expected_moves:
            return MoveSignal(MoveGrade.NO_MOVES)

        chosen_node = next((n for n in expected_moves if n.move.uci() == uci_from_lichess_to_pgn(uci)), None)
        if chosen_node:
            self._grades.append(MoveSignal(MoveGrade.CORRECT))
            return MoveSignal(MoveGrade.CORRECT, chosen_node)

        # the user didn't guess -- prepare feedback on this
        expected_sans = ", ".join(
            node_san(n) for n in expected_moves
        )
        user_eval = self._evaluate_move(self._prompt.node, uci)
        best_expected_eval = None
        evals = [self._evaluate_move(self._prompt.node, n.move.uci())
                 for n in expected_moves]
        if evals:
            best_expected_eval = max(evals)

        msg = f"Wrong. Expected: {expected_sans}."
        if user_eval is not None:
            msg += f" Your move eval {user_eval:+.2f}."
            if best_expected_eval is not None:
                msg += f" File move eval {best_expected_eval:+.2f}."

        self._grades.append(MoveSignal(MoveGrade.INCORRECT))
        return MoveSignal(MoveGrade.INCORRECT, msg=msg)

    def _handle_off_file_guess(self, uci: str) -> None:
        ev = self._session.query(fen(self._prompt.node), "q-eval")
        eval, best_reply = ev.eval, ev.move

        user_ev = self._session.q_eval_move(self._prompt.node, uci)
        move_eval, reply_to_user = user_ev.eval, user_ev.move

        best_reply_san = node_san(self._prompt.node, best_reply) if best_reply else "None"
        san = node_san(self._prompt.node)
        msg = f"Off-file {san}. Your move: eval {move_eval:+.2f} after {reply_to_user}."
        if eval - move_eval < 0.2 or eval > 0.9*move_eval:
            msg += " Good guess!"
            grade = MoveGrade.CORRECT
        else:
            msg += f" Best was {best_reply_san} with evaluation {eval:+.2f}."
            grade = MoveGrade.INCORRECT # TODO: differentiate b/w off-file and file
        self._grades.append(grade)

        return MoveSignal(grade, msg=msg)

    
    def _evaluate_move(self, position: Union[chess.Board, Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(position, move).eval

class LineGenerator():
    def __init__(self, session: RepertoireSession, root: Node, start_range: int, prompt_state: PromptState,
                 probs_cache_name: str, non_file_freq: float) -> None:
        self._session = session
        self.non_file_move_freq = non_file_freq
        self._rng = random.Random()

        # updates whenever asked to generate a new prompt
        self._prompt_spec = None

        self._prompt = prompt_state

        # starting point for prompt generation
        self._root = root
        # max of how far we move from the root
        self.start_range = start_range

        self._edge_probs = CacheDict(lambda fen: {}, item_to_json=json.dumps,
                                     item_from_json=json.loads, auto_save=False)
        self._edge_probs.load_from_file(probs_cache_name)

        self._session.fill_the_TT(self._session.game)

    def is_finished(self) -> bool:
        return self._is_finished
    
    def finish_prompt(self) -> None:
        self._is_finished = True
        self._recompute_line_move_probs(self._prompt.node, -0.5)
        self._edge_probs.serialize()

    def start_prompt(self, spec_id: SpecId) -> None:
        self._spec_id = spec_id
        self._is_finished = False
        self._review_payload = None
        self._review_path = None
        self._search_move_payload = None

        if spec_id == "new":
            self._choose_random_prompt()
        else:
            return

        return self._prompt

    def _choose_random_prompt(self) -> PromptState:
        node = deepcopy(self._root)
        while not (success := self._choose_prompt(node)):
            # we may add some moves to node while choosing, so reset to the file contents
            node = deepcopy(self._root)

    def current_spec_id(self):
        return self._spec_id

    def current_prompt_id(self):
        return ' '.join(node_moves(self._prompt.node))

    def on_response(self, uci: str, signal: MoveSignal) -> None:
        if not self._prompt.off_file:
            if signal.move_grade == MoveGrade.CORRECT:
                self._prompt.node = signal.response_node
                self._advance_line(chosen=self._prompt.node)
            else:
                self._prompt.debug_msg = signal.msg
                return self._prompt

        else:
            # for now we don't do anything after an off-file guess
            self.finish_prompt()
            return

        if not self._is_finished:
            self._prompt.debug_msg+=signal.msg

        return self._prompt

    def _advance_line(self, chosen: Node) -> None:
        """
        Choose a move to continue along the line and
        update self._prompt accordingly.
        """
        assert chosen.turn() != self._session.options.side, "Chosen move should be ours"
        self._prompt.node = chosen
        next_node, selection_debug = self._choose_move(chosen)

        if next_node is False:
            self._prompt.debug_msg = selection_debug
            self.finish_prompt()
            return

        self._prompt.node = next_node
        self._prompt.debug_msg = selection_debug

        message = f"Correct: {node_san(chosen)}. Continue along the line."
        if selection_debug:
            self._prompt.debug_msg = f"{message} {selection_debug}"


    def _choose_prompt_line_length(self) -> int:
        return self._rng.randint(1, self.start_range)+1

    def _choose_prompt(self, node: Node) -> bool:
        """Simulate walking through a randomly chosen line. 
        Results in populating self._prompt.
        Returns False if the walk along a line failed."""
        selection_debug = ""
        self._prompt.off_file = False
        line_length = self._choose_prompt_line_length()

        self._prompt.anchor_node = node

        # we'll do line_length or line_length-1 steps total
        for step in range(line_length - 2):
            next_node, _ = self._choose_move(node, maybe_off_book=False)
            if next_node is False:
                return False
            node = next_node
            # if step == self._session.options.start_ply:
            #     self._prompt.anchor_node = node # TODO


        if node.turn() == self._session.options.side:
            next_node, _ = self._choose_move(node, maybe_off_book=False)
            if next_node is False:
                return False
            node = next_node

        # Final step: potentially off_book move for opponent's move
        assert node.turn() != self._session.options.side, f"Prompt selection should end on our turn {line_length}" # TODO: remove this after a while
        next_node, selection_debug = self._choose_move(node, maybe_off_book=True)
        if next_node is False:
            return False
        
        self._prompt.node = next_node
        self._prompt.debug_msg = selection_debug

        return True        

    def _choose_move(
        self,
        parent: Node,
        *,
        maybe_off_book: bool = False,
        use_engine: bool = False,
    ) -> tuple[Node, str]:
        """
        Chooses a move randomly to simulate a step along aline. 
        Determines changes in self._prompt.off_file.
        Returns the resulting node and a debug string.

        If a choice could not be made, returns (False, "").
        """
        off_book = maybe_off_book and self._rng.random() < self.non_file_move_freq

        children = self._session.variations(parent)
        if not children: # TODO: transp
            # if there are no moves for us in the file but we are still here, that's improper usage
            if parent.turn() == self._session.options.side:
                return False, ""
            # if there are no moves for them, we can try anyway
            off_book = True
            use_engine = True

        # a tiny optimization
        if parent.turn() == self._session.options.side and not self._session.options.check_alternatives:
            return children[0], "our move"
            
        if off_book:
            # Try to find an off-book move with probability non_file_move_freq
            off_book_move, off_book_debug = self._find_off_book_move(parent)
            if off_book_move is not None:
                child = self._session._add_variation(parent, off_book_move)
                self._prompt.off_file = True
                return child, off_book_debug
            elif use_engine:
                engine_move = self._session.query(fen(parent), "q-eval").move
                if engine_move:
                    child = self._session._add_variation(parent, engine_move)
                    self._prompt.off_file = True
                    return child, f"engine-suggested off-book move {engine_move}"
                else:
                # should only happen if it's mate
                    return False, ""
            # Fall through to normal logic

        if self._edge_probs[fen(parent)]:
            probs = self._edge_probs[fen(parent)]
        else:
            probs = self._get_moves_and_freqs(parent)
            self._edge_probs[fen(parent)] = probs
        choice = self._rng_choice(list(probs.keys()), list(probs.values()))
        
        # find the node corresponding to the choice
        for n in self._session.cache[fen(parent)].TTed:
            for m in n.variations:
                if m.uci() == choice:
                    return m, self._format_rng_weights(probs.keys(), probs.values())
                

    def _find_off_book_move(self, node: Node) -> tuple[Optional[chess.Move], str]:
        """Find an off-book DB move with frequency >= 5% and score_rate <= 75%."""
        move_weights = self._get_db_moves_and_nums(node)
        if not move_weights:
            return None, "no children"
        
        exclude = {m.uci() for m in self._session.variations(node)}

        # Filter candidates: frequency >= 5%, score_rate <= 75%
        candidates = []
        for uci, weight in move_weights.items():
            if uci_from_lichess_to_pgn(uci) in exclude:
                continue

            if self._session.move_freq(node, uci) < 0.05:
                continue

            score_rate = self._session.score_rate_move(node, uci)
            # don't prompt with stupid moves
            if score_rate > 0.75:
                continue

            candidates.append((chess.Move.from_uci(uci), weight))

        if not candidates:
            return None, "no candidates"

        moves, weights = zip(*candidates)
        move = self._rng_choice(list(moves), list(weights))
        debug_text = self._format_rng_weights(list(moves), list(weights))
        return move, debug_text
    
    def _get_moves_and_freqs(self, node: Node) -> dict[str, float]:
        """
        Return a dict UCI -> frequency for children of all nodes with
        the same position as 'node'.
        """
        moves = set()
        for n in self._session.cache[fen(node)].TTed:
            moves.update(m.move.uci() for m in n.variations)

        return self._child_freqs(node, moves)
    
    def _child_freqs(self, node: Node, variations: Iterable[UCI]) -> dict[UCI, float]:
        """
        Return a dict UCI -> frequency for moves of the board of 'node'
        that are present in 'variations'.
        """
        weights = self._child_nums(node, variations)
        total = sum(weights.values())
        if total == 0: # should not happen, really
            return {}
        return {uci: weights[uci] / total for uci in weights}
    
    
    def _child_nums(self, node: Node, variations: Iterable[UCI]) -> dict[UCI, int]:
        """
        Return the dict UCI -> weights for each move of the board of 'node' present in 'variations'. A weight 
        is the amount of move occurrences in the DB. (TODO: add masters' moves with higher weight)
        plus one. Plus one ensures 1) we give every move a chance; 2) we won't divide by 0 when normalizing.
        """
        if node.turn() == self._session.options.side:
            # TODO: we may want to assign higher weights to file's main line
            return {uci: 1.0 for uci in variations}
        
        move_weights = self._get_db_moves_and_nums(node)
        if not move_weights:
            return {uci: 1.0 for uci in variations}

        weights = {}
        for uci in variations:
            weights[uci] = move_weights.get(uci, 1.0)

        return weights
    
    def _get_db_moves_and_nums(self, position: Any) -> dict[UCI, float]:
        """
        Returns a dict mapping UCI strings to move counts (weights).
        """
        data = self._session.query(fen(position), "db_lichess")
        if not data or "moves" not in data:
            sys.stderr.write(f"No DB moves for {position}\n")
            return {}
        
        weights = {}
        for move_data in data.get("moves", []):
            uci = uci_from_lichess_to_pgn(move_data["uci"])
            count = move_data.get("white", 0) + move_data.get("draws", 0) + move_data.get("black", 0)
            weights[uci] = float(count)
        return weights
    
    
    def _recompute_line_move_probs(self, node: Node, diff: float) -> None:
        """
        Make moves of a line less (or more) likely to appear,
        thus adapting to user's need to see it again.
        """
        if not node: # can happen if "New" is clicked before we started
            return
        
        factor = 1.0 + diff

        while fen(self._session.game) != fen(node):
            move = node.move
            node = node.parent
            parent_dict = self._edge_probs[fen(node)]
            try:
                parent_dict[move.uci()] *= factor
            except KeyError: # can happen if the move is off-book
                pass
            normalize_freqs(parent_dict)


    def _rng_choice(self, items: list[K], weights: Optional[list[float]]=None) -> K:
        if not items:
            raise ValueError("No items to choose from")
        if len(items) != len(weights):
            raise ValueError("Items and weights must have the same length")

        if weights is None:
            weights = [1.0] * len(items)
        total = sum(weights)

        threshold = self._rng.random() * total
        cumulative = 0.0
        for item, weight in zip(items, weights):
            cumulative += weight
            if threshold <= cumulative:
                return item
        return items[-1]

    def _format_rng_weights(self, items: list[Any], weights: list[float]) -> str:
        if not items or not weights or len(items) != len(weights):
            return ""
        total = sum(weights)
        if total <= 0:
            return ""

        entries = []
        items = list(items)[:5]
        weights = list(weights)[:5]
        for item, weight in zip(items, weights):
            uci = None
            if hasattr(item, "move") and getattr(item, "move") is not None:
                uci = item.move.uci()
            elif isinstance(item, chess.Move):
                uci = item.uci()
            else:
                uci = str(item)
            entries.append(f"{uci}={weight:.1f}")

        if len(items) > 5:
            entries.append("...")

        probs = [weight / total for weight in weights]
        prob_entries = [f"{p:.1%}" for p in probs]
        return f"rng weights: {', '.join(entries)}; probs: {', '.join(prob_entries)}"

@dataclass
class PromptState:
    node: Node
    off_file: bool
    debug_msg: str
    anchor_node: Node

    def __bool__(self):
        return self.node is not None

class Hints():
    """
    Manages the state of hints (circles) for the current prompt.

    The logic basically is: for a pawn move, the first hint is all pawns on the board and the second hint is the pawn to move.
    For other moves, the first hint is the piece to move and the second hint is the target square.

    We stop providing additional hints when current hints already determine a single move.
    """
    def __init__(self, board: Union[Node, chess.Board], uci: str):
        if isinstance(board, Node):
            board = board.board()
        self.board = board
        self.starting_square = uci[:2]
        self.target_square = uci[2:4]
        self.determines_move = False
        self.circle_coords = []

    @property
    def circles(self) -> list[Circle]:
        return [Circle(c) for c in self.circle_coords]
    
    @property
    def piece_to_move(self):
        return self.board.piece_at(chess.parse_square(self.starting_square)).piece_type

    def add_hint(self):
        if self.determines_move:
            return
        
        if self.piece_to_move == chess.PAWN:
            # first hint
            if not self.circle_coords:
                side = self.board.turn
                our_pawns = self.board.pieces(chess.PAWN, side)
                self.circle_coords = [chess.square_name(sq) for sq in our_pawns]
                self.determines_move = self._count_moves_determined() == 1

            # second hint
            elif len(self.circle_coords) > 1:
                self.circle_coords = [self.starting_square]
                self.determines_move = self._count_moves_determined() == 1

            # final hint
            else:
                self.circle_coords.append(self.target_square)
                self.determines_move = True

        else:
            if not self.circle_coords:
                self.circle_coords.append(self.starting_square)
                self.determines_move = self._count_moves_determined() == 1

            elif len(self.circle_coords) == 1:
                self.circle_coords.append(self.target_square)
                self.determines_move = True

    def _count_moves_determined(self) -> int:
        return sum(self._count_moves_from_square(c) for c in self.circle_coords)
    
    def _count_moves_from_square(self, square_name):
        square_index = chess.parse_square(square_name)
        return sum(1 for move in self.board.legal_moves if move.from_square == square_index)
                
class PromptLog:
    """Passive storage of session history."""
    def __init__(self):
        self.prompts = CacheDict(lambda spec_id: [], item_to_json=json.dumps, item_from_json=json.loads)


    def record_prompt(self, prompt_id: PromptId, spec_id: SpecId) -> None:
        self.prompts[spec_id].append(prompt_id)

    def record_feedback(self, prompt_id: PromptId, feedback: Feedback) -> None:
        pass

    def serialize(self) -> str:
        self.prompts.serialize()


class SpacedRepetitionFeature:
    def __init__(self, options: SpacedRepetitionOptions, progress_reporter=None, report_cb=None) -> None:
        self.options = options
        self._session = RepertoireSession(
            options,
            progress_reporter=progress_reporter,
            report_cb=report_cb,
            default_cache_path=lambda: default_repertoire_cache_path(options),
        )

    def run(self) -> str:
        from .server import ensure_web_server
        from .app import sr_controller

        ensure_web_server(host="127.0.0.1", port=8000)
        sr_controller.start(self.options, self._session)
        return "Spaced repetition launched at http://127.0.0.1:8000/"

    def close(self) -> None:
        from .app import sr_controller

        sr_controller.stop()
        self._session.close()



class AppController:
    """
    Owns chess-related objects, reacts to user actions.
    """

    def __init__(self, hub: 'BoardHub') -> None:
        self._hub = hub

        self.active = False
        self._mode = "idle"  # idle | guess | review

        self._prompt = PromptState(node=None, off_file=False, debug_msg="", anchor_node=None)
        self._log : SessionLog = PromptLog()

        self._search_move_payload: Optional[dict[str, Any]] = None


    def _probs_cache_name(self) -> str:
        return default_repertoire_cache_path(base=os.path.join("cache", "sr_probs"), options=self._session.options)
      
    def _log_cache_name(self) -> str:
        return default_repertoire_cache_path(base=os.path.join("cache", "log"), options=self._session.options)

    def ui_state(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "mode": self._mode,
            "review": self._review_payload if self.active and self._mode == "review" else None,
            "searchMove": self._search_move_payload if self.active and self._mode == "review" else None,
        }

    def start(self, options: SpacedRepetitionOptions, session: Optional[RepertoireSession] = None) -> None:
        self._cfg = options
        self._session = session or RepertoireSession(
            options,
            default_cache_path=lambda: default_repertoire_cache_path(options),
        )
        self._orientation = "white" if options.play_white else "black"
        
        self._log.prompts.load_from_file(self._log_cache_name())
        self._generator : Generator = LineGenerator(self._session, self._session.starting_node, 
                                                    self._cfg.start_range, self._prompt, self._probs_cache_name(), self._cfg.non_file_move_frequency)
        self._interpreter : Interpreter = MoveInterpreter(self._session, self._prompt)
        self._rep_controller : RepetitionController = RepetitionController(NaiveScheduler(self._log), self._generator, 
                                                                           self._interpreter, self._log, lambda prompt: self.show_prompt(prompt))

        if options.preload_db:
            self._prefetch_db_stats()

        self.start_next_prompt()


    def start_next_prompt(self) -> None:
        # self._generator.finish_prompt()
        self.active = True
        self._mode = "guess"
        self._broadcast_ui_state()
        self._rep_controller.start_next_prompt()

    def show_prompt(self, prompt: str = None, **kwargs) -> None:
        self._mode = "guess"
        self._hub.set_from_node(
            prompt.node,
            orientation=self._orientation,
            message=prompt.debug_msg,
            allow_moves=True,
            **kwargs
        )

    def stop(self) -> None:
        self.active = False
        self._mode = "idle"
        self._games = []
        self._prompt = None
        self._close_session()

    def _broadcast_ui_state(self) -> None:
        self._hub.broadcast({"type": "sr_state", "sr": self.ui_state()})

    def _prefetch_db_stats(self) -> None:
        """Pre-warm the cache by querying DB stats that we will need."""
        def visit(node: Any):
            if not node.turn() == self._session.options.side:
                self._session.query(fen(node), "db_lichess")

        for game in self._games:
            self._session.traverse(game, visit=visit)


    def search_move(self):
        if self._mode != "review":
            return

        tree_root = self._session.game
        review_path = self._review_path

        query_node = node_at_path(self._session.game, list(review_path), self._session.variations)
        query_move = getattr(query_node, "move", None)
        if query_move is None:
            return

        end_ply = self._session.options.end_ply
        query_prev_uci = None
        query_next_uci = None

        parent = getattr(query_node, "parent", None)
        if parent is not None and getattr(parent, "move", None) is not None:
            query_prev_uci = parent.move.uci()

        query_children = [c for c in self._session.variations(query_node) if c.ply() <= end_ply]
        if query_children and getattr(query_children[0], "move", None) is not None:
            query_next_uci = query_children[0].move.uci()

        def safe_san(n: Optional[Node] = None) -> str:
            if DEBUG_MODE:
                return node_san(n) if n else ""
            try:
                return node_san(n)
            except Exception:
                return ""

        query_san = safe_san(query_node)
        query_prev_san = safe_san(parent)
        query_next_san = safe_san(query_children[0]) if query_children else ""

        results: list[dict[str, Any]] = []

        def visit(node: Node, path: list[int]) -> None:
            if getattr(node, "move", None) is not None and node.move == query_move:
                prev_node = getattr(node, "parent", None)
                prev_uci = None
                if prev_node is not None and getattr(prev_node, "move", None) is not None:
                    prev_uci = prev_node.move.uci()

                children = [c for c in self._session.variations(node) if c.ply() <= end_ply]
                next_node = children[0] if children else None
                next_uci = None
                if next_node is not None and getattr(next_node, "move", None) is not None:
                    next_uci = next_node.move.uci()

                results.append(
                    {
                        "path": list(path),
                        "prev": safe_san(prev_node),
                        "move": safe_san(node),
                        "next": safe_san(next_node),
                        "matchPrev": bool(query_prev_uci and prev_uci and prev_uci == query_prev_uci),
                        "matchNext": bool(query_next_uci and next_uci and next_uci == query_next_uci),
                    }
                )

            if node.ply() >= end_ply:
                return

            children = list(self._session.variations(node))
            for idx, child in enumerate(children):
                if child.ply() > end_ply:
                    continue
                visit(child, [*path, idx])

        visit(tree_root, [])

        self._search_move_payload = {
            "query": {
                "path": list(review_path),
                "move": query_san,
                "prev": query_prev_san,
                "next": query_next_san,
            },
            "results": results,
            "count": len(results),
        }
        self._broadcast_ui_state()

    def provide_hint(self) -> None:
        if self._mode != "guess":
            return
        
        try:
            expected_uci = self._session.variations(self._prompt.node)[0].move.uci()
        except Exception:
            expected_uci = self._session.query(fen(self._prompt.node), "q-eval").move.uci()

        if not hasattr(self, 'hints') or self.hints.board != self._prompt.node.board():
            self.hints = Hints(self._prompt.node, expected_uci)

        self.hints.add_hint()

        self.show_prompt(circles = self.hints.circles, message=str(self.hints.circle_coords))

    def handle_guess(self, uci: str) -> None:
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")
        
        continue_prompt = self._rep_controller.on_user_response(uci)
        if not continue_prompt:
            self._log.serialize()
            self._enter_review_mode(node=self._prompt.node, message="Correct. Browse the tree or click New.")


    def _mainline_node_at_ply(self, game: Any, ply: int) -> Any:
        node = game
        while getattr(node, "variations", None) and node.ply() < ply:
            node = node.variations[0]
        return node
    
    def give_up(self) -> None:
        if self._mode != "guess":
            return

        if self._prompt.node is not None:
            expected_moves = list(self._session.variations(self._prompt.node))
            if expected_moves:
                expected_sans = ", ".join(node_san(n) for n in expected_moves)
                message = f"Gave up. Expected: {expected_sans}. Browse the tree or click New."
            else:
                message = "Gave up. No moves in file here. Browse the tree or click New."
        else:
            message = "Gave up (off-file prompt). Browse the repertoire tree or click New."

        self._enter_review_mode(node=self._prompt.node, message=message)

    def goto_review_path(self, path: list[int]) -> None:
        if self._mode != "review":
            raise RuntimeError("Browsing is only available in review mode")

        node = node_at_path(self._session.starting_node, path, self._session.variations)

        self._review_path = list(path)
        self._review_payload["currentPath"] = list(path)
        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message="Browsing variations",
            allow_moves=False,
        )
        self._broadcast_ui_state()

    def prev_prompt(self) -> None:
        if len(self._prompt_history) > 1:
            self._mode = "guess"
            self._prompt = self._prompt_history[-2] # UPDATE
            # swap the last two
            self._prompt_history[-2:] = self._prompt_history[:-3:-1]
            self.show_prompt(message="Back to previous prompt. Make your move.")

    def _enter_review_mode(self, node: chess.pgn.GameNode, message: Optional[str] = "") -> None:
        self._mode = "review"
        self._search_move_payload = None
        end_ply = self._session.options.end_ply
        self._review_path = path_from_root(self._session.starting_node, node, self._session.variations)
        exported = export_pgn_subtree(
            self._session,
            self._session.starting_node,
            end_ply=end_ply,
            prefer_mainline_path=self._review_path,
        )
        tree = build_variation_tree(self._session.variations, self._session.starting_node, end_ply=end_ply)
        self._review_payload = {
            "fen": exported.fen,
            "pgn": exported.pgn,
            "initialPly": exported.initial_ply,
            "orientation": self._orientation,
            "tree": tree,
            "currentPath": self._review_path,
        }

        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state()


    def _close_session(self) -> None:
        self._session.close()
        self._session = None

def normalize_freqs(freqs: dict[Any, float]) -> None:
    total = sum(freqs.values())
    if total <= 0.0:
        return
    for k in freqs:
        freqs[k] /= total