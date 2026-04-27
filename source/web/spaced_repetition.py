from __future__ import annotations

from copy import deepcopy
from enum import Enum
import json
import os
import random
import sys
import threading
from typing import Any, Iterable, Optional, TypeVar, TypedDict, Union

import chess
import chess.pgn
from chess.pgn import GameNode as Node
from dataclasses import dataclass, field

from source.core.caching import CacheDict
from source.core.position_similarity import compare_positions
from source.core.traversal import TraversalPolicy, iter_nodes
from source.web.board.contracts import Circle
from source.web.board.session import UCI
from source.web.scheduler_implem import NaiveScheduler

# from source.web.app import BoardHub

from ..core.boardtools import fen, node_moves, node_san, uci_from_lichess_to_pgn
from ..core.options import SpacedRepetitionOptions, DEBUG_MODE
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path
from ..core.runner import quick_eval_lines
from .pgn_export import export_pgn_subtree
from .variation_tree import node_at_path, path_from_root, build_variation_tree
from .scheduler_protocol import *

# TODO: add transpotitioning moves to the move list?

K = TypeVar("K")


class MoveEntryData(TypedDict):
    probability: float
    ease: float


class PositionMoveData(TypedDict):
    moves: dict[UCI, MoveEntryData]
    blacklist: list[UCI]


class MoveGrade(Enum):
    CORRECT = 1
    INCORRECT = 2
    NO_MOVES = 3

@dataclass
class MoveSignal():
    move_grade: MoveGrade
    msg: str = ""
    eval_diff: Optional[float] = None
    rel_eval_diff: Optional[float] = None


@dataclass
class PromptMovePerformance:
    hint_requests: int = 0
    hint_revealed_move: bool = False
    errors: list[MoveSignal] = field(default_factory=list)

    def add_hint(self, determines_move: bool) -> None:
        self.hint_requests += 1
        self.hint_revealed_move = self.hint_revealed_move or determines_move

    def add_error(self, signal: MoveSignal) -> None:
        if signal.move_grade != MoveGrade.INCORRECT:
            raise ValueError("Only incorrect signals may be stored as move errors")
        self.errors.append(signal)


@dataclass(frozen=True)
class OffBookSelection:
    move: Optional[chess.Move]
    message: str = ""
    blacklist_exhausted: bool = False


class RepetitionEngine():
    """
    The class responsible for prompt generation and response interpretation.
    Prompts are generated move by move. A move may be randomnly chosen from a file,
    or picked by a chess engine.

    Keeps the result of generating in self._prompt and stores prompt-local
    performance data so move probabilities can be adjusted from real user behavior.
    """
    HINT_FACTOR_STEP = 0.10
    HINT_REVEALS_MOVE_FACTOR = 1.15
    ERROR_FACTOR_STEP = 0.25
    ERROR_EVAL_LOSS_SCALE = 0.25
    ERROR_EVAL_LOSS_CAP = 0.75
    MIN_MOVE_FACTOR = 0.05
    MOVE_EASE_CORRECT_STEP = 1.0
    MOVE_EASE_INCORRECT_STEP = 1.0
    LEARNED_EASE_THRESHOLD = 3.0
    LEARNED_PROMPT_EXTENSION_PROBABILITY = 0.90
    MAX_PROMPT_SELECTION_ATTEMPTS = 256
    MAX_OFF_BOOK_BLACKLIST_ATTEMPTS = 3

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

        self._move_probs = CacheDict(
            lambda position_fen: PositionMoveData(moves={}, blacklist=[]),
            item_to_json=self._moveprobs_item_to_json,
            item_from_json=self._moveprobs_item_from_json,
            auto_save=False,
        )
        self._move_probs.load_from_file(probs_cache_name)

        self._session.fill_the_TT(self._session.game)
        self._grades: list[MoveSignal] = []
        self._move_performance: dict[int, PromptMovePerformance] = {}
        self._current_hints: Optional[Hints] = None
        self._temporary_nodes: list[Node] = []

    def summarize(self) -> Feedback:
        if not self._grades:
            return Feedback(0.0)

        correct = sum(1 for signal in self._grades if signal.move_grade == MoveGrade.CORRECT)
        return Feedback(correct / len(self._grades))

    def expected_uci(self) -> Optional[UCI]:
        if self._prompt.node is None:
            return None

        expected_node = self._current_expected_node()
        if expected_node is not None:
            return expected_node.move.uci()

        best_move = self._session.query(fen(self._prompt.node), "q-eval").move
        if best_move is None:
            return None
        if isinstance(best_move, chess.Move):
            return best_move.uci()
        return best_move

    def get_hint_circles(self) -> list[Circle]:
        if self._prompt.node is None:
            raise RuntimeError("Cannot provide a hint without an active prompt")

        expected_uci = self.expected_uci()
        if expected_uci is None:
            return []

        if not self._hint_matches_current_prompt(expected_uci):
            self._current_hints = Hints(self._prompt.node, expected_uci)

        self._current_hints.add_hint()

        self._record_hint(self._current_hints.determines_move)

        return self._current_hints.circles

    def is_finished(self) -> bool:
        return self._is_finished
    
    def finish_prompt(self, ease=0.25) -> None:
        self._is_finished = True
        self._recompute_line_move_probs(self._prompt.node, -ease)
        self._move_probs.serialize()

    def set_start(self, root: Optional[Node] = None, start_range: Optional[int] = None) -> None:
        if root:
            self._root = root
        if start_range:
            self.start_range = start_range

    def start_prompt(self, spec_id: SpecId) -> None:
        self._clear_temporary_nodes()
        self._spec_id = spec_id
        self._is_finished = False
        self._review_payload = None
        self._review_path = None
        self._search_move_payload = None
        self._grades = []
        self._move_performance = {}
        self._current_hints = None
        self._prompt.message = ""
        self._prompt.off_file = False
        self._prompt.anchor_node = None

        if spec_id == "new":
            self._choose_random_prompt()
        else:
            raise ValueError(f"Unsupported spec_id: {spec_id!r}")

        return self._prompt

    def _choose_random_prompt(self) -> PromptState:
        self._clear_temporary_nodes()
        for _ in range(self.MAX_PROMPT_SELECTION_ATTEMPTS):
            try:
                success = self._choose_prompt(self._root)
            except Exception:
                self._clear_temporary_nodes()
                raise

            if success:
                return self._prompt

            # we may add some temporary moves while choosing, so reset to the file contents
            self._clear_temporary_nodes()

        raise RuntimeError("Could not generate a prompt. No eligible prompt moves remained.")

    def current_spec_id(self):
        return self._spec_id

    def current_prompt_id(self):
        return ' '.join(node_moves(self._prompt.node))

    def _moveprobs_item_to_json(
        self,
        item: tuple[str, PositionMoveData],
    ) -> dict[str, Any]:
        position_fen, position_data = item
        return {
            "fen": position_fen,
            "moves": position_data["moves"],
            "blacklist": list(position_data["blacklist"]),
        }

    def _moveprobs_item_from_json(
        self,
        payload: Any,
    ) -> tuple[str, PositionMoveData]:
        if isinstance(payload, str):
            payload = json.loads(payload)

        if isinstance(payload, dict):
            position_fen = payload["fen"]
            raw_move_probs = payload.get("moves", {})
            raw_blacklist = payload.get("blacklist", [])
        else:
            position_fen, raw_move_probs = payload
            raw_blacklist = []

        move_probs: dict[UCI, MoveEntryData] = {}
        blacklist: list[UCI] = []
        for raw_uci in raw_blacklist:
            if raw_uci not in blacklist:
                blacklist.append(raw_uci)

        for uci, raw_entry in raw_move_probs.items():
            if isinstance(raw_entry, dict):
                move_probs[uci] = MoveEntryData(
                    probability=float(raw_entry["probability"]),
                    ease=float(raw_entry.get("ease", 0.0)),
                )
                if raw_entry.get("blacklisted", False) and uci not in blacklist:
                    blacklist.append(uci)
            else:
                move_probs[uci] = MoveEntryData(probability=float(raw_entry), ease=0.0)

        return position_fen, PositionMoveData(moves=move_probs, blacklist=blacklist)

    def _move_probs_for(self, parent: Node) -> PositionMoveData:
        return self._move_probs[fen(parent)]

    def _moves_for(self, parent: Node) -> dict[UCI, MoveEntryData]:
        position_data = self._move_probs_for(parent)
        move_probs = position_data["moves"]
        if not move_probs:
            move_probs.update(
                {
                    move_uci: MoveEntryData(probability=float(probability), ease=0.0)
                    for move_uci, probability in self._get_moves_and_freqs(parent).items()
                }
            )
        return move_probs

    def _blacklist_for(self, parent: Node) -> list[UCI]:
        return self._move_probs_for(parent)["blacklist"]

    def _move_entry(self, parent: Node, uci: UCI) -> MoveEntryData:
        move_probs = self._moves_for(parent)
        if uci not in move_probs:
            raise RuntimeError(f"Missing move data for {uci!r} from position {fen(parent)!r}")

        return move_probs[uci]

    def _is_learned_move(self, parent: Node, uci: UCI) -> bool:
        return self._move_entry(parent, uci)["ease"] >= self.LEARNED_EASE_THRESHOLD

    def _is_blacklisted_move(self, parent: Node, uci: UCI) -> bool:
        position_data = self._move_probs.get(fen(parent))
        if position_data is None:
            return False
        return uci in position_data["blacklist"]

    def _blacklist_move(self, parent: Node, uci: UCI) -> None:
        blacklist = self._blacklist_for(parent)
        if uci not in blacklist:
            blacklist.append(uci)

    def blacklist_current_move(self) -> None:
        self._blacklist_move(self._prompt.node.parent, self._prompt.node.move.uci())
        self.finish_prompt()

    def on_response(self, uci: str) -> PromptState:
        if self._prompt.off_file:
            signal = self._handle_off_file_guess(uci)
            self._prompt.message = signal.msg
            if signal.move_grade == MoveGrade.CORRECT:
                self._current_hints = None
                self.finish_prompt()
            return self._prompt

        signal = self._handle_file_guess(uci)
        if signal.move_grade != MoveGrade.CORRECT:
            self._prompt.message = signal.msg
            return self._prompt

        chosen_node = self._session.child_for_move(
            self._prompt.node,
            uci_from_lichess_to_pgn(uci),
        )
        self._prompt.node = chosen_node
        self._current_hints = None
        self._advance_line(chosen=chosen_node)

        if not self._is_finished:
            self._prompt.message += signal.msg

        return self._prompt

    def _current_expected_node(self) -> Optional[Node]:
        if self._prompt.node is None:
            return None

        expected_moves = self._session.variations(self._prompt.node)
        if not expected_moves:
            return None
        return expected_moves[0]

    def _move_performance_for(self, node: Node) -> PromptMovePerformance:
        node_id = id(node)
        if node_id not in self._move_performance:
            self._move_performance[node_id] = PromptMovePerformance()
        return self._move_performance[node_id]

    def _current_feedback_node(self) -> Optional[Node]:
        if self._prompt.node is None:
            return None

        expected_node = self._current_expected_node()
        if expected_node is not None:
            return expected_node
        return self._prompt.node

    def _record_hint(self, determines_move: bool) -> None:
        target = self._current_feedback_node()
        if target is None:
            return
        self._move_performance_for(target).add_hint(determines_move)

    def _record_error(self, signal: MoveSignal) -> None:
        target = self._current_feedback_node()
        if target is None:
            return
        self._move_performance_for(target).add_error(signal)

    def _hint_matches_current_prompt(self, expected_uci: UCI) -> bool:
        if self._current_hints is None or self._prompt.node is None:
            return False

        return (
            fen(self._current_hints.board) == fen(self._prompt.node)
            and self._current_hints.starting_square == expected_uci[:2]
            and self._current_hints.target_square == expected_uci[2:4]
        )

    def _handle_file_guess(self, uci: UCI) -> MoveSignal:
        expected_moves = self._session.variations(self._prompt.node)
        if not expected_moves:
            signal = MoveSignal(MoveGrade.NO_MOVES)
            self._grades.append(signal)
            return signal

        chosen_node = next(
            (n for n in expected_moves if n.move.uci() == uci_from_lichess_to_pgn(uci)),
            None,
        )
        if chosen_node is not None:
            self._move_entry(self._prompt.node, chosen_node.move.uci())["ease"] += self.MOVE_EASE_CORRECT_STEP
            signal = MoveSignal(MoveGrade.CORRECT)
            self._grades.append(signal)
            return signal

        expected_sans = ", ".join(node_san(n) for n in expected_moves)
        user_eval = self._evaluate_move(self._prompt.node, uci)
        evals = [self._evaluate_move(self._prompt.node, n.move.uci()) for n in expected_moves]
        best_expected_eval = max(evals) if evals else None

        msg = f"Wrong. Expected: {expected_sans}."
        if best_expected_eval is not None:
            msg += f" Your move eval {user_eval:+.2f}. File move eval {best_expected_eval:+.2f}."

        eval_diff = None if best_expected_eval is None else user_eval - best_expected_eval
        rel_eval_diff = None
        if best_expected_eval not in (None, 0):
            rel_eval_diff = eval_diff / best_expected_eval

        signal = MoveSignal(
            MoveGrade.INCORRECT,
            msg=msg,
            eval_diff=eval_diff,
            rel_eval_diff=rel_eval_diff,
        )
        self._grades.append(signal)
        target = self._current_feedback_node()
        if target is not None and target.parent is not None and target.move is not None:
            self._move_entry(target.parent, target.move.uci())["ease"] -= self.MOVE_EASE_INCORRECT_STEP
        self._record_error(signal)
        return signal

    def _handle_off_file_guess(self, uci: UCI) -> MoveSignal:
        ev = self._session.query(fen(self._prompt.node), "q-eval")
        expected_eval, best_reply = ev.eval, ev.move

        user_ev = self._session.q_eval_move(self._prompt.node, uci)
        move_eval, reply_to_user = user_ev.eval, user_ev.move

        best_reply_san = node_san(self._prompt.node, best_reply) if best_reply else "None"
        san = node_san(self._prompt.node)
        eval_gap = expected_eval - move_eval
        msg = f"Off-file {san}. Your move: eval {move_eval:+.2f} after {reply_to_user}."
        if uci == best_reply.uci() or eval_gap <= 0.2 or move_eval > 0.8*expected_eval:
            msg += f"Best was {best_reply_san} with evaluation {expected_eval:+.2f}. Good job!"
            grade = MoveGrade.CORRECT
        else:
            msg += (
                f" Best was {best_reply_san} with evaluation {expected_eval:+.2f}. "
                "Try again."
            )
            grade = MoveGrade.INCORRECT

        signal = MoveSignal(
            grade,
            msg=msg,
            eval_diff=move_eval - expected_eval,
        )
        self._grades.append(signal)
        if grade == MoveGrade.INCORRECT:
            self._record_error(signal)
        return signal

    def _evaluate_move(self, position: Union[chess.Board, Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(position, move).eval

    def _skip_current_prompt_position(self) -> tuple[Node | bool, str]:
        if self._prompt.node is None:
            raise RuntimeError("Cannot skip a prompt position without an active prompt")

        learned_node, _ = self._choose_move(self._prompt.node, maybe_off_book=False)
        if learned_node is False:
            return False, ""

        return self._choose_move(learned_node, maybe_off_book=True)

    def _advance_prompt_candidate(self) -> bool:
        while True:
            if self._prompt.off_file:
                return True

            expected_node = self._current_expected_node()
            if expected_node is None:
                return True
            if not self._is_learned_move(self._prompt.node, expected_node.move.uci()):
                return True
            if self._rng.random() >= self.LEARNED_PROMPT_EXTENSION_PROBABILITY:
                return True

            next_node, selection_debug = self._skip_current_prompt_position()
            if next_node is False:
                return True
            self._prompt.node = next_node
            self._prompt.message = selection_debug

    def _advance_line(self, chosen: Node) -> None:
        """
        Choose a move to continue along the line and
        update self._prompt accordingly.
        """
        assert chosen.turn() != self._session.options.side, "Chosen move should be ours"
        self._prompt.node = chosen
        next_node, selection_debug = self._choose_move(chosen)

        if next_node is False:
            self._prompt.message = selection_debug
            self.finish_prompt()
            return

        self._prompt.node = next_node
        self._prompt.message = selection_debug
        if not self._advance_prompt_candidate():
            self._prompt.message = selection_debug
            self.finish_prompt()
            return

        message = f"Correct: {node_san(chosen)}. Continue along the line."
        if self._prompt.message:
            self._prompt.message = f"{message} {self._prompt.message}"
        else:
            self._prompt.message = message


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
        self._prompt.message = selection_debug

        return self._advance_prompt_candidate()

    def _choose_move(
        self,
        parent: Node,
        *,
        maybe_off_book: bool = False,
        use_engine: bool = False,
    ) -> tuple[Node | bool, str]:
        """
        Chooses a move randomly to simulate a step along aline. 
        Determines changes in self._prompt.off_file.
        Returns the resulting node and a debug string.

        If a choice could not be made, returns (False, ...).
        """
        off_book = maybe_off_book and self._rng.random() < self.non_file_move_freq

        children = self._session.variations(parent)
        move_probs = self._moves_for(parent)
        blacklisted_moves = set(self._blacklist_for(parent))
        eligible_moves = {
            uci: entry
            for uci, entry in move_probs.items()
            if uci not in blacklisted_moves
        }

        if not children or not eligible_moves: # TODO: transp
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
            off_book_selection = self._select_off_book_move(parent, use_engine=use_engine)
            if off_book_selection.move is not None:
                child = self._add_temporary_node(parent, off_book_selection.move)
                self._prompt.off_file = True
                return child, off_book_selection.message
            if off_book_selection.blacklist_exhausted:
                return False, off_book_selection.message
            if use_engine:
                return False, off_book_selection.message
            # Fall through to normal logic

        if not eligible_moves:
            return False, ""

        probabilities = [entry["probability"] for entry in eligible_moves.values()]
        choice = self._rng_choice(list(eligible_moves.keys()), probabilities)

        message = ""
        if DEBUG_MODE:
            message = self._format_rng_weights(eligible_moves.keys(), probabilities)
        return self._session.child_for_move(parent, choice), message

    def _add_temporary_node(
        self,
        parent: Node,
        move: chess.Move | str,
    ) -> Node:
        child = self._session._add_variation(parent, move)
        self._temporary_nodes.append(child)
        return child

    def _clear_temporary_nodes(self) -> None:
        while self._temporary_nodes:
            self._session.remove_variation(self._temporary_nodes.pop())

    def _select_off_book_move(
        self,
        node: Node,
        *,
        use_engine: bool,
    ) -> OffBookSelection:
        db_selection = self._choose_db_off_book_move(node)
        if db_selection.move is not None or db_selection.blacklist_exhausted:
            return db_selection
        if not use_engine:
            return db_selection
        return self._choose_engine_off_book_move(node)

    def _choose_db_off_book_move(self, node: Node) -> OffBookSelection:
        candidates = self._off_book_db_candidates(node)
        if not candidates:
            return OffBookSelection(None, "no candidates")

        remaining = list(candidates)
        for _ in range(self.MAX_OFF_BOOK_BLACKLIST_ATTEMPTS):
            if not remaining:
                break
            choice_idx = self._rng_weighted_index([weight for _, weight in remaining])
            moves = [move for move, _ in remaining]
            weights = [weight for _, weight in remaining]
            move = moves[choice_idx]
            debug_text = self._format_rng_weights(moves, weights)
            if not self._is_blacklisted_move(node, move.uci()):
                return OffBookSelection(move, debug_text)
            remaining = remaining[choice_idx + 1 :]

        return OffBookSelection(
            None,
            "No non-blacklisted off-book DB moves are available.",
            blacklist_exhausted=True,
        )

    def _choose_engine_off_book_move(self, node: Node) -> OffBookSelection:
        engine_lines = quick_eval_lines(
            self._session.engine,
            fen(node),
            pov=self._session.options.side,
            multipv=self.MAX_OFF_BOOK_BLACKLIST_ATTEMPTS,
        )
        if not engine_lines:
            return OffBookSelection(None, "no engine move")

        for idx, line in enumerate(engine_lines, start=1):
            move = line.move
            if not isinstance(move, chess.Move):
                move = chess.Move.from_uci(str(move))
            if self._is_blacklisted_move(node, move.uci()):
                continue

            debug_msg = f"engine-suggested off-book move {move}"
            if idx > 1:
                debug_msg += f" (choice #{idx})"
            return OffBookSelection(move, debug_msg)

        return OffBookSelection(
            None,
            "No non-blacklisted engine off-book moves are available.",
            blacklist_exhausted=True,
        )

    def _off_book_db_candidates(self, node: Node) -> list[tuple[chess.Move, float]]:
        """Find an off-book DB move with frequency >= 5% and score_rate <= 75%."""
        move_weights = self._get_db_moves_and_nums(node)
        if not move_weights:
            return []
        
        exclude = {m.uci() for m in self._session.variations(node)}

        # Filter candidates: frequency >= 5%, score_rate <= 75%
        candidates: list[tuple[chess.Move, float]] = []
        for uci, weight in move_weights.items():
            if uci in exclude:
                continue

            if self._session.move_freq(node, uci) < 0.05:
                continue

            score_rate = self._session.score_rate_move(node, uci)
            # don't prompt with stupid moves
            if score_rate > 0.75:
                continue

            candidates.append((chess.Move.from_uci(uci), weight))

        return candidates
    
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

    def _move_factor(self, node: Node, base_factor: float) -> float:
        performance = self._move_performance.get(id(node))
        if performance is None:
            return max(self.MIN_MOVE_FACTOR, base_factor)

        factor = base_factor
        if performance.hint_requests:
            factor += self.HINT_FACTOR_STEP * performance.hint_requests
        if performance.hint_revealed_move:
            factor = max(factor, self.HINT_REVEALS_MOVE_FACTOR)
        if performance.errors:
            factor += self.ERROR_FACTOR_STEP * len(performance.errors)
            eval_loss = max(self._error_eval_loss(signal) for signal in performance.errors)
            factor += min(self.ERROR_EVAL_LOSS_CAP, eval_loss * self.ERROR_EVAL_LOSS_SCALE)

        return max(self.MIN_MOVE_FACTOR, factor)

    def _error_eval_loss(self, signal: MoveSignal) -> float:
        if signal.eval_diff is None:
            return 0.0
        return max(0.0, -signal.eval_diff)

    def _pending_prompt_factor(self, base_factor: float) -> float:
        if self._prompt.node is None:
            return base_factor
        if self._prompt.node.turn() != self._session.options.side:
            return base_factor

        target = self._current_feedback_node()
        if target is None:
            return base_factor

        return self._move_factor(target, base_factor)
    
    
    def _recompute_line_move_probs(self, node: Node, diff: float) -> None:
        """
        Make moves of a line less (or more) likely to appear,
        thus adapting to user's need to see it again.
        """
        if not node: # can happen if "New" is clicked before we started
            return
        
        base_factor = max(self.MIN_MOVE_FACTOR, 1.0 + diff)
        running_factor = self._pending_prompt_factor(base_factor)

        while fen(self._session.game) != fen(node):
            child = node
            move = child.move
            node = child.parent
            running_factor = max(running_factor, self._move_factor(child, base_factor))
            parent_dict = self._moves_for(node)
            if move.uci() not in parent_dict:
                continue
            # Keep easy lines de-emphasized by default, but pull difficult prompt
            # prefixes back into the queue when the user needed help on a later move.
            parent_dict[move.uci()]["probability"] *= running_factor
            total = sum(entry["probability"] for entry in parent_dict.values())
            if total <= 0.0:
                continue
            for entry in parent_dict.values():
                entry["probability"] /= total


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

    def _rng_weighted_index(self, weights: list[float]) -> int:
        if not weights:
            raise ValueError("No weights to choose from")

        total = sum(weights)
        if total <= 0.0:
            raise ValueError("Weights must sum to a positive number")

        threshold = self._rng.random() * total
        cumulative = 0.0
        for idx, weight in enumerate(weights):
            cumulative += weight
            if threshold <= cumulative:
                return idx
        return len(weights) - 1

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


        message = ""
        probs = [weight / total for weight in weights]
        prob_entries = [f"{p:.1%}" for p in probs]
        for e, p in zip(entries, prob_entries):
            message += f"\n{e}                    {p}"

        if len(items) > 5:
            message += "\n..."

        return message

@dataclass
class PromptState:
    node: Node
    off_file: bool
    message: str
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


MAX_REVIEW_TREE_DEPTH_FROM_VIEW_ROOT = 5


@dataclass(frozen=True)
class ReviewDbStatsTask:
    request_id: int
    position_fen: str


class AppController:
    """
    Owns chess-related objects, reacts to user actions.
    """

    def __init__(self, hub: 'BoardHub') -> None:
        self._hub = hub

        self.active = False
        self._mode = "idle"  # idle | guess | review

        self._prompt = PromptState(node=None, off_file=False, message="", anchor_node=None)
        self._log : SessionLog = PromptLog()

        self._review_base_root_path: list[int] = []
        self._review_view_root_path: list[int] = []
        self._search_move_payload: Optional[dict[str, Any]] = None
        self._review_db_stats_lock = threading.Lock()
        self._review_db_stats_pending: Optional[ReviewDbStatsTask] = None
        self._review_db_stats_inflight = False
        self._review_db_stats_request_id = 0


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
        self._prompt = PromptState(node=None, off_file=False, message="", anchor_node=None)
        
        self._log.prompts.load_from_file(self._log_cache_name())
        self._rep_engine: RepetitionEngine = RepetitionEngine(
            self._session,
            self._session.starting_node,
            self._cfg.start_range,
            self._prompt,
            self._probs_cache_name(),
            self._cfg.non_file_move_frequency,
        )
        self._rep_controller = RepetitionController(
            NaiveScheduler(self._log),
            self._rep_engine,
            self._log,
        )

        if options.preload_db:
            self._prefetch_db_stats()

        self.start_next_prompt()


    def start_next_prompt(self) -> None:
        self.active = True
        self._mode = "guess"
        self._broadcast_ui_state()
        self._rep_controller.start_next_prompt()
        self.show_prompt()

    def show_prompt(self, prompt: str = None, **kwargs) -> None:
        if prompt is None:
            prompt = self._rep_controller.get_prompt_view()

        self._mode = "guess"
        self._hub.set_from_node(
            prompt.node,
            orientation=self._orientation,
            message=prompt.message,
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

    def _next_review_db_stats_request_id(self) -> int:
        self._review_db_stats_request_id += 1
        return self._review_db_stats_request_id

    def _loading_review_db_stats(self, position: Union[Node, chess.Board, str]) -> dict[str, Any]:
        position_fen = fen(position)
        return {
            "fen": position_fen,
            "loading": True,
            "totalGames": 0,
            "white": 0,
            "draws": 0,
            "black": 0,
            "moves": [],
        }

    def _error_review_db_stats(self, position: Union[Node, chess.Board, str], exc: Exception) -> dict[str, Any]:
        position_fen = fen(position)
        return {
            "fen": position_fen,
            "totalGames": 0,
            "white": 0,
            "draws": 0,
            "black": 0,
            "moves": [],
            "error": str(exc),
        }

    def _db_result_count(self, data: dict[str, Any], key: str) -> int:
        value = data.get(key, 0)
        if not isinstance(value, int):
            raise TypeError(f"Database stats field {key!r} must be an int")
        return value

    def _build_review_db_stats(self, position: Union[Node, chess.Board, str], stats: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(stats, dict):
            raise TypeError("Database stats payload must be a dict")

        position_fen = fen(position)
        position_white = self._db_result_count(stats, "white")
        position_draws = self._db_result_count(stats, "draws")
        position_black = self._db_result_count(stats, "black")

        raw_moves = stats.get("moves", [])
        if not isinstance(raw_moves, list):
            raise TypeError("Database stats payload must contain a moves list")

        board = chess.Board(position_fen)
        moves: list[dict[str, Any]] = []
        for move_data in raw_moves:
            lichess_uci = move_data["uci"]

            white = self._db_result_count(move_data, "white")
            draws = self._db_result_count(move_data, "draws")
            black = self._db_result_count(move_data, "black")

            uci = uci_from_lichess_to_pgn(lichess_uci)
            san = board.san(chess.Move.from_uci(uci))
            moves.append(
                {
                    "uci": uci,
                    "san": san,
                    "gameCount": white + draws + black,
                    "white": white,
                    "draws": draws,
                    "black": black,
                }
            )

        return {
            "fen": position_fen,
            "totalGames": position_white + position_draws + position_black,
            "white": position_white,
            "draws": position_draws,
            "black": position_black,
            "moves": moves,
        }

    def _review_db_stats(
        self,
        position: Union[Node, chess.Board, str],
        *,
        cached_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        position_fen = fen(position)
        if cached_only:
            stats = self._session.query(position_fen, "db_lichess", cache_only=True)
            if stats is None:
                return None
        else:
            stats = self._session.query(position_fen, "db_lichess")
        return self._build_review_db_stats(position_fen, stats)

    def _set_review_db_stats(self, db_stats: dict[str, Any]) -> None:
        self._review_payload["dbStats"] = db_stats

    def _prepare_review_db_stats(self, node: Node) -> Optional[ReviewDbStatsTask]:
        position_fen = fen(node)
        request_id = self._next_review_db_stats_request_id()
        self._review_payload["dbStatsRequestId"] = request_id

        cached_stats = self._review_db_stats(position_fen, cached_only=True)
        if cached_stats is not None:
            self._set_review_db_stats(cached_stats)
            return None

        self._set_review_db_stats(self._loading_review_db_stats(position_fen))
        return ReviewDbStatsTask(
            request_id=request_id,
            position_fen=position_fen,
        )

    def _queue_review_db_stats(self, task: ReviewDbStatsTask) -> None:
        with self._review_db_stats_lock:
            self._review_db_stats_pending = task
            if self._review_db_stats_inflight:
                return
            self._review_db_stats_inflight = True

        worker = threading.Thread(target=self._review_db_stats_worker, name="review-db-stats", daemon=True)
        worker.start()

    def _broadcast_review_db_stats(self, request_id: int) -> None:
        if self._review_payload is None:
            raise RuntimeError("Review payload is not initialized")

        self._hub.broadcast(
            {
                "type": "sr_review_db_stats",
                "review": {
                    "requestId": request_id,
                    "dbStats": self._review_payload.get("dbStats"),
                },
            }
        )

    def _review_db_stats_worker(self) -> None:
        while True:
            with self._review_db_stats_lock:
                task = self._review_db_stats_pending
                if task is None:
                    self._review_db_stats_inflight = False
                    return
                self._review_db_stats_pending = None

            try:
                db_stats = self._review_db_stats(task.position_fen)
            except Exception as exc:
                db_stats = self._error_review_db_stats(task.position_fen, exc)

            if not self.active or self._mode != "review" or self._review_payload is None:
                continue

            if self._review_payload.get("dbStatsRequestId") != task.request_id:
                continue

            self._set_review_db_stats(db_stats)
            self._broadcast_review_db_stats(task.request_id)

    def _broadcast_review_navigation(self) -> None:
        
        if not self.active or self._mode != "review":
            raise RuntimeError("Review navigation broadcast requires active review mode")
        if self._review_payload is None or self._review_path is None:
            raise RuntimeError("Review navigation payload is not initialized")

        self._hub.broadcast(
            {
                "type": "sr_review_nav",
                "review": {
                    "currentPath": list(self._review_path),
                    "viewRootPath": list(self._review_view_root_path),
                    "dbStatsRequestId": self._review_payload.get("dbStatsRequestId"),
                    "dbStats": self._review_payload.get("dbStats"),
                },
            }
        )

    def _common_path_prefix_length(self, left: list[int], right: list[int]) -> int:
        limit = min(len(left), len(right))
        prefix_len = 0
        while prefix_len < limit and left[prefix_len] == right[prefix_len]:
            prefix_len += 1
        return prefix_len

    def _review_view_root_path_for(self, current_path: list[int]) -> list[int]:
        current_view_root = list(self._review_view_root_path)
        shared_prefix_len = self._common_path_prefix_length(current_view_root, current_path)
        min_root_len = max(shared_prefix_len, len(current_path) - MAX_REVIEW_TREE_DEPTH_FROM_VIEW_ROOT)

        if list(current_path)[:len(self._review_base_root_path)] == list(self._review_base_root_path):
            min_root_len = max(min_root_len, len(self._review_base_root_path))

        return list(current_path[:min_root_len])

    def _prefetch_db_stats(self) -> None:
        """Pre-warm the cache by querying DB stats that we will need."""
        def visit(node: Any):
            if not node.turn() == self._session.options.side:
                self._session.query(fen(node), "db_lichess")

        for game in self._games:
            self._session.traverse(game, visit=visit)


    def search_nodes_by_move(self):
        """
        Search for nodes with the same move as in the current review position
        and display the results.
        Results are displayed with the previous and the following moves, emphasized if
        the same as those in the current review node.
        Each result also carries similarity data relative to the current position.
        """
        if self._mode != "review":
            return

        root = self._session.game
        review_path = getattr(self, "_review_path", None)

        end_ply = self._session.options.end_ply

        try:
            query_node = node_at_path(root, list(review_path), self._session.variations)
        except Exception:
            return

        query_move = query_node.move
        if query_move is None:
            return

        def safe_san(n: Optional[Node] = None) -> str:
            if DEBUG_MODE:
                return node_san(n) if n else ""
            try:
                return node_san(n)
            except Exception:
                try:
                    return n.move.uci()
                except Exception:
                    return ""

        query_parent = query_node.parent
        query_prev_uci = query_parent.move.uci() if query_parent.move is not None else None
        query_children = [c for c in self._session.variations(query_node) if c.ply() <= end_ply]
        query_next = query_children[0] if query_children else None
        query_next_uci = query_next.move.uci() if query_next else None

        tp = TraversalPolicy(start_ply=1, end_ply=end_ply, get_children=self._session.variations)
        results: list[dict[str, Any]] = []
        for node in iter_nodes(root, tp):
            if node.move != query_move:
                continue

            path = path_from_root(root, node, self._session.variations)

            parent = node.parent
            prev_uci = parent.move.uci() if getattr(parent, "move", None) is not None else None

            children = [c for c in self._session.variations(node) if c.ply() <= end_ply]
            next_node = children[0] if children else None
            next_uci = next_node.move.uci() if next_node else None
            similarity = compare_positions(query_node.board(), node)

            results.append(
                {
                    "path": list(path),
                    "prev": safe_san(parent),
                    "move": safe_san(node),
                    "next": safe_san(next_node),
                    "matchPrev": bool(query_prev_uci and prev_uci and prev_uci == query_prev_uci),
                    "matchNext": bool(query_next_uci and next_uci and next_uci == query_next_uci),
                    "distance": round(similarity.distance, 4),
                    "similarity": round(similarity.similarity, 4),
                }
            )

        results.sort(
            key=lambda item: (-item["similarity"], item["distance"], item["path"])
        )

        self._search_move_payload = {
            "query": {
                "path": list(review_path),
                "move": safe_san(query_node),
                "prev": safe_san(query_parent),
                "next": safe_san(query_next),
                "distance": 0.0,
                "similarity": 1.0,
            },
            "results": results,
            "count": len(results),
        }
        self._broadcast_ui_state()

    def provide_hint(self) -> None:
        if self._mode != "guess":
            return

        self.show_prompt(circles=self._rep_engine.get_hint_circles())

    def handle_guess(self, uci: str) -> None:
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")
        
        continue_prompt = self._rep_controller.on_user_response(uci)
        if not continue_prompt:
            self._log.serialize()
            self._enter_review_mode(node=self._prompt.node)
        else:
            self.show_prompt()


    def _mainline_node_at_ply(self, game: Any, ply: int) -> Any:
        node = game
        while getattr(node, "variations", None) and node.ply() < ply:
            node = node.variations[0]
        return node
    
    def give_up(self) -> None:
        if self._mode != "guess":
            return

        self._reveal_prompt_in_review()

    def finish_prompt(self, ease: float = 0.25) -> None:
        if self._mode != "guess":
            return

        self._rep_engine.finish_prompt(ease=ease)
        self._reveal_prompt_in_review()

    def blacklist_current_move(self) -> None:
        if self._mode != "guess":
            return

        blacklisted_uci = self._rep_engine.blacklist_current_move()
        self._enter_review_mode(
            node=self._prompt.node,
            message=f"Blacklisted {blacklisted_uci}. Browse the tree or click New.",
        )

    def _reveal_prompt_in_review(self) -> None:
        if self._mode != "guess":
            return

        if self._prompt.node is not None:
            expected_uci = self._rep_engine.expected_uci()
            if expected_uci:
                message = f"Expected: {expected_uci}. Browse the tree or click New."
            else:
                message = "No expected moves here. Browse the tree or click New."
        else:
            message = "Off-file prompt. Browse the repertoire tree or click New."

        self._enter_review_mode(node=self._prompt.node, message=message)

    def goto_review_path(self, path: list[int]) -> None:
        if self._mode != "review":
            raise RuntimeError("Browsing is only available in review mode")
        if self._review_payload is None:
            raise RuntimeError("Review payload is not initialized")

        root = self._session.game
        node = node_at_path(root, path, self._session.variations)

        self._review_path = list(path)
        self._review_payload["currentPath"] = list(path)
        self._review_view_root_path = self._review_view_root_path_for(self._review_path)
        self._review_payload["viewRootPath"] = list(self._review_view_root_path)
        stats_task = self._prepare_review_db_stats(node)
        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message="Browsing variations",
            allow_moves=False,
        )
        self._broadcast_review_navigation()
        if stats_task is not None:
            self._queue_review_db_stats(stats_task)

    def study_from_here(self) -> None:
        if self._mode != "review":
            raise RuntimeError("Study root can only be changed in review mode")

        node = node_at_path(self._session.game, list(self._review_path), self._session.variations)
        position_fen = fen(node)
        self._cfg.starting_fen = position_fen
        self._session.options.starting_fen = position_fen
        self._session.starting_node = node
        self._rep_engine.set_start(root=node)
        self._enter_review_mode(node=node, message="Study root updated. Click New to practice from here.")

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
        self._review_path = path_from_root(self._session.game, node, self._session.variations)
        self._review_base_root_path = path_from_root(self._session.game, self._session.starting_node, self._session.variations)
        self._review_view_root_path = list(self._review_base_root_path)
        self._review_view_root_path = self._review_view_root_path_for(self._review_path)
        exported = export_pgn_subtree(
            self._session,
            self._session.game,
            end_ply=end_ply,
            prefer_mainline_path=self._review_path,
        )
        tree = build_variation_tree(self._session.variations, self._session.game, end_ply=end_ply)
        self._review_payload = {
            "fen": exported.fen,
            "pgn": exported.pgn,
            "initialPly": exported.initial_ply,
            "orientation": self._orientation,
            "tree": tree,
            "currentPath": self._review_path,
            "viewRootPath": list(self._review_view_root_path),
            "dbStatsRequestId": None,
            "dbStats": None,
        }
        stats_task = self._prepare_review_db_stats(node)

        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state()
        if stats_task is not None:
            self._queue_review_db_stats(stats_task)


    def _close_session(self) -> None:
        self._session.close()
        self._session = None

def normalize_freqs(freqs: dict[Any, float]) -> None:
    total = sum(freqs.values())
    if total <= 0.0:
        return
    for k in freqs:
        freqs[k] /= total
