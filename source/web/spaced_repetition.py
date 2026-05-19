from __future__ import annotations

from collections import defaultdict
from enum import Enum
import json
import os
import random
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Optional, TypeVar, TypedDict, Union

import chess
import chess.pgn
from chess.pgn import GameNode as Node
from dataclasses import dataclass, replace

from source.core.caching import CacheDict
from source.core.position_similarity import compare_positions
from source.core.traversal import TraversalPolicy, iter_nodes
from source.web.board.contracts import Circle
from source.web.board.session import UCI
from source.web.scheduler_implem import NaiveScheduler

# from source.web.app import BoardHub

from ..core.boardtools import BoardLike, fen, node_moves, node_san, side, to_board, uci_from_lichess_to_pgn
from ..core.options import SpacedRepetitionOptions, DEBUG_MODE, save_settings
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path
from ..core.runner import quick_eval_lines
from .pgn_export import export_pgn_subtree
from .variation_tree import node_at_path, path_from_root, build_variation_tree
from .scheduler_protocol import *

# TODO: add transpotitioning moves to the move list?

K = TypeVar("K")

SEARCH_MOVE_BOOST_FACTOR = 2.0
SEARCH_MOVE_BOOST_MIN_SIMILARITY = 0.8


class MoveEntryData(TypedDict):
    weight: float
    performance: tuple[int, int]


class PositionMoveData(TypedDict):
    moves: dict[UCI, MoveEntryData] | None
    blacklist: list[UCI]


class MoveCorrectness(Enum):
    CORRECT = 1
    INCORRECT = 2
    ALTERNATIVE = 3
    UNDEF = 4

@dataclass
class MoveGrade():
    correctness: MoveCorrectness
    msg: str = ""
    eval_diff: Optional[float] = None
    rel_eval_diff: Optional[float] = None


@dataclass
class PromptMovePerformance:
    successes: int = 0
    attempts: int = 0
    gave_up: bool = False

    def add_giveup(self) -> None:
        if self.gave_up:
            return
        self.attempts += 1
        self.gave_up = True

    def add_hint(self, determines_move: bool) -> None:
        if self.gave_up:
            return
        self.attempts += 1
        if determines_move:
            self.gave_up = True

    def add_incorrect_attempt(self) -> None:
        if self.gave_up:
            return
        self.attempts += 1

    def add_correct_attempt(self) -> None:
        if self.gave_up:
            return
        self.successes += 1
        self.attempts += 1


@dataclass(frozen=True)
class OffBookSelection:
    move: Optional[chess.Move]
    message: str = ""
    blacklist_exhausted: bool = False


@dataclass(frozen=True)
class WeightSyncSummary:
    positions_checked: int
    moves_added: int
    moves_removed: int


DEBUG_WEIGHT_TREE_FORWARD_PLIES = 6


class RepetitionEngine():
    """
    The class responsible for prompt generation and response interpretation.
    Prompts are generated move by move. A move may be randomnly chosen from a file,
    or picked by a chess engine.

    Keeps the result of generating in self._prompt and stores prompt-local
    performance data so cached move performance can be updated from real user behavior.
    """
    DEFAULT_RIGHT_PROBABILITY = 0.5
    LEARNED_RIGHT_THRESHOLD = 0.85
    LEARNED_MIN_ATTEMPTS = 3
    LOCAL_UNLEARNED_RIGHT_THRESHOLD = 0.9
    LEARNED_MOVE_SKIP_PROBABILITY = 0.90
    MAX_PROMPT_SELECTION_ATTEMPTS = 100
    MAX_OFF_BOOK_BLACKLIST_ATTEMPTS = 3
    MAX_GLOBAL_PROMPT_LENGTH = 10

    def __init__(self, session: RepertoireSession, root: Node, start_range: int,
                 probs_cache_name: str, non_file_freq: float, local_generation: bool) -> None:
        self._session = session
        self.non_file_move_freq = non_file_freq
        self._rng = random.Random()
        self.local_generation = local_generation

        # updates whenever asked to generate a new prompt
        self._prompt_spec = None

        self._prompt = PromptState(node=None, off_file=False, message="", anchor_node=None)

        # starting point for prompt generation
        self._root = root
        # max of how far we move from the root, in MOVES
        self.start_range = start_range

        self._pos_drill_data = CacheDict(
            lambda position_fen: PositionMoveData(moves=None, blacklist=[]),
            item_to_json=self._moveprobs_item_to_json,
            item_from_json=self._moveprobs_item_from_json,
            auto_save=False,
        )
        self._pos_drill_data.load_from_file(probs_cache_name)

        self._session.fill_the_TT(self._session.game)
        self._grades: list[MoveGrade] = []
        self._pending_move_performance: dict[tuple[str, UCI], PromptMovePerformance] = {}
        self._current_hints: Optional[Hints] = None
        self._temporary_nodes: list[Node] = []
        self._global_successes = 0
        self._global_attempts = 0

        self._refresh_global_performance_totals()
        self._refresh_prompt_dicts()

    def summarize(self) -> Feedback:
        if not self._grades:
            return Feedback(0.0)

        total_loss = sum(self._grade_eval_loss(grade) for grade in self._grades)
        return Feedback(total_loss / len(self._grades))

    def expected_uci(self) -> Optional[UCI]:
        prpt_data = self._prompt
        if prpt_data.node_index is not None and prpt_data.prechosen_path is not None:
            try:
                return prpt_data.prechosen_path[prpt_data.node_index+1]
            except IndexError:
                pass

        if prpt_data.node is None:
            return None

        expected_node = self._current_expected_node()
        if expected_node is not None:
            return expected_node.move.uci()

        best_move = self._session.query(fen(prpt_data.node), "q-eval").move
        if best_move is None:
            return None
        return best_move.uci()
    
    def make_prompt_dict_global(self, node: Node | None = None) -> dict[tuple[str], float]:
        if self._session.options.check_alternatives:
            raise ValueError("Global prompt choice if currently only available for mainline choices.")
        
        prompt_dict_global = defaultdict(float)
        prompt_dict_global[()] = 1.0
        path_from_root = []
        def visit(n: Node):
            nonlocal path_from_root
            try:
                path_from_root.append(n.move.uci())
                prompt_dict_global[tuple(path_from_root)] = prompt_dict_global[tuple(path_from_root[:-1])]
            except Exception:
                pass
            if n.turn() == self._session.options.side:
                return
            children_uci_weights = self._get_moves_for_(n)
            for child in self._session.variations(n):
                if child.move.uci() not in children_uci_weights:
                    continue
                path_from_root.append(child.move.uci())
                prompt_dict_global[tuple(path_from_root)] = \
                    prompt_dict_global[tuple(path_from_root[:-2])] * children_uci_weights[child.move.uci()]["weight"]
                path_from_root.pop()
                
        def post(n, p, v):
            if path_from_root:
                path_from_root.pop()
        self._session.traverse(node, visit=visit, post=post)
        return prompt_dict_global

    def make_prompt_dict_relative(self):
        root_path = node_moves(self._root, san=False)
        root_weight = self._prompt_dict_global[tuple(root_path)]

        if root_weight < 10**-6:
            return self.make_prompt_dict_global(self._root)
    
        l = len(root_path)
        return {k[l:]: v / root_weight for k, v in self._prompt_dict_global.items() 
                                      if k[:l] == tuple(root_path)}

    def _refresh_prompt_dicts(self) -> None:
        # TODO: lazy?
        self._prompt_dict_global = self.make_prompt_dict_global()
        self._prompt_dict_relative = self.make_prompt_dict_relative()

    def _refresh_global_performance_totals(self) -> None:
        successes = 0
        attempts = 0
        for position_data in self._pos_drill_data.values():
            moves = position_data["moves"]
            if moves is None:
                continue
            for entry in moves.values():
                move_successes, move_attempts = entry["performance"]
                successes += move_successes
                attempts += move_attempts
        self._global_successes = successes
        self._global_attempts = attempts

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
    
    def finish_prompt(self, gave_up: bool = False) -> None:
        self._is_finished = True
        if gave_up:
            if tested_move := self._current_tested_move():
                parent, move_uci = tested_move
                self._move_performance_for(parent, move_uci).add_giveup()
        self._commit_current_tested_move()
        self._pos_drill_data.serialize()

    def sync_move_weights_with_pgn(self) -> WeightSyncSummary:
        positions_checked = 0
        moves_added = 0
        moves_removed = 0

        self._clear_temporary_nodes()

        for position_fen, position_data in list(self._pos_drill_data.items()):
            positions_checked += 1
            move_weights = position_data["moves"]
            if move_weights is None:
                continue
            current_moves = self._pgn_move_ucis_for_position(position_fen)

            stale_moves = [uci for uci in move_weights if uci not in current_moves]
            for uci in stale_moves:
                del move_weights[uci]
            moves_removed += len(stale_moves)

            retained_weights = dict(move_weights)
            missing_moves = sorted(uci for uci in current_moves if uci not in retained_weights)
            if missing_moves:
                added_weight = self._added_move_weight(position_fen, retained_weights)
                for uci in missing_moves:
                    move_weights[uci] = MoveEntryData(weight=added_weight, performance=(0, 0))
                moves_added += len(missing_moves)

        self._refresh_global_performance_totals()
        self._pos_drill_data.serialize()
        self._refresh_prompt_dicts()
        return WeightSyncSummary(
            positions_checked=positions_checked,
            moves_added=moves_added,
            moves_removed=moves_removed,
        )

    def _pgn_move_ucis_for_position(self, position_fen: str) -> set[UCI]:
        position_cache = self._session.cache[position_fen]

        moves: set[UCI] = set()
        for node in position_cache.TTed:
            moves.update(child.move.uci() for child in node.variations)
        return moves

    def _added_move_weight(
        self,
        position_fen: str,
        sibling_weights: dict[UCI, MoveEntryData],
    ) -> float:
        """Determine the weight for a new PGN move. First try to use siblings, then parent."""
        if sibling_weights:
            return sum(entry["weight"] for entry in sibling_weights.values())

        node = self._session.cache[position_fen].TTed[0]
        parent = node.parent
        if not hasattr(parent, "parent"):
            return 1.0
        return self._get_moves_for_(parent.parent)[parent.move.uci()]["weight"]

    def set_start(self, root: Optional[Node] = None, start_range: Optional[int] = None) -> None:
        if root is not None:
            if root != self._root:
                self._prompt_dict_relative = None
            self._root = root
        if start_range is not None:
            self.start_range = start_range

    def _reset_prompt_state(self) -> None:
        self._clear_temporary_nodes()
        self._spec_id = None
        self._is_finished = False
        self._review_payload = None
        self._review_path = None
        self._search_move_payload = None
        self._grades = []
        self._pending_move_performance = {}
        self._current_hints = None
        self._prompt = PromptState(node=None, off_file=False, message="", anchor_node=None)

    def _relative_move_path_for_node(self, node: Node) -> Optional[list[UCI]]:
        root_moves = node_moves(self._root, san=False)
        node_path = node_moves(node, san=False)
        if node_path[:len(root_moves)] != root_moves:
            return None
        return node_path[len(root_moves):]

    def _prompt_child_for_move(self, parent: Node, move: chess.Move | UCI) -> Node:
        try:
            return self._session.child_for_move(parent, move)
        except RuntimeError:
            return self._add_temporary_node(parent, move)

    def _create_prompt_from_id(self, prompt_id: PromptLineId) -> PromptState:
        full_moves = list(prompt_id.moves)

        node = self._session.game
        prechosen_path: list[UCI] = []
        anchor_node = node if fen(node) == prompt_id.start_fen else None
        anchor_index = -1 if anchor_node is not None else None

        for san in full_moves:
            move = node.board().parse_san(san)
            prechosen_path.append(move.uci())
            node = self._prompt_child_for_move(node, move)
            if anchor_node is None and fen(node) == prompt_id.start_fen:
                anchor_node = node
                anchor_index = len(prechosen_path) - 1

        if anchor_node is None or anchor_index is None:
            raise ValueError(f"Prompt id {prompt_id!r} does not resolve to a known prompt start")

        return PromptState(
            node=anchor_node,
            off_file=False,
            message="",
            anchor_node=anchor_node,
            prechosen_path=tuple(prechosen_path),
            node_index=anchor_index,
        )

    def start_prompt(self, spec_id: SpecId) -> PromptState:
        self._reset_prompt_state()

        if spec_id == "new":
            self._spec_id = spec_id
            self._choose_random_prompt()
        else:
            raise ValueError(f"Unsupported spec_id: {spec_id!r}")

        return self._prompt

    def start_prompt_by_id(self, prompt_id: PromptLineId, spec_id: SpecId = "history") -> PromptState:
        self._reset_prompt_state()
        self._spec_id = spec_id
        self._prompt = self._create_prompt_from_id(prompt_id)
        return self._prompt

    def _choose_random_prompt(self) -> PromptState:
    
        self._clear_temporary_nodes()
        for _ in range(self.MAX_PROMPT_SELECTION_ATTEMPTS):
            try:
                success = self._try_choose_prompt(self._root, complete=True)
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
        return PromptLineId(fen(self._prompt.anchor_node), tuple(node_moves(self._prompt.node)))

    def _set_prompt_start(self) -> None:
        self._prompt.anchor_node = self._prompt.node

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
            if "moves" not in payload:
                raise KeyError(f"Missing moves payload for position {position_fen!r}")
            raw_pos_drill_data = payload["moves"]
            raw_blacklist = payload.get("blacklist", [])
        else:
            position_fen, raw_pos_drill_data = payload
            raw_blacklist = []

        move_probs: dict[UCI, MoveEntryData] = {}
        blacklist: list[UCI] = []
        for raw_uci in raw_blacklist:
            if raw_uci not in blacklist:
                blacklist.append(raw_uci)

        if raw_pos_drill_data is None:
            return position_fen, PositionMoveData(moves=None, blacklist=blacklist)
        if not isinstance(raw_pos_drill_data, dict):
            raise TypeError(f"Moves payload for position {position_fen!r} must be a dict or null")

        for uci, raw_entry in raw_pos_drill_data.items():
            if isinstance(raw_entry, dict):
                raw_weight = raw_entry.get("weight", raw_entry.get("prob"))
                if raw_weight is None:
                    raise KeyError(f"Missing move weight for {uci!r} from position {position_fen!r}")
                move_probs[uci] = MoveEntryData(
                    weight=float(raw_weight),
                    performance=self._performance_from_json(raw_entry.get("performance", (0, 0))),
                )
                if raw_entry.get("blacklisted", False) and uci not in blacklist:
                    blacklist.append(uci)
            else:
                move_probs[uci] = MoveEntryData(weight=float(raw_entry), performance=(0, 0))

        return position_fen, PositionMoveData(moves=move_probs, blacklist=blacklist)

    def _performance_from_json(self, raw_performance: Any) -> tuple[int, int]:
        successes, attempts = raw_performance
        return successes, attempts


    def _get_moves_for_(self, parent: Node, blacklist_included: bool = False) -> dict[UCI, MoveEntryData]:
        position_data = self._pos_drill_data[fen(parent)]
        if position_data["moves"] is None:
            position_data["moves"] = {
                    move_uci: MoveEntryData(weight=float(freq), performance=(0, 0))
                    for move_uci, freq in self._get_moves_and_freqs(parent).items()
                }
            
        return {m:p for m, p in position_data["moves"].items() if blacklist_included or m not in position_data["blacklist"]}

    def _blacklist_for(self, position: BoardLike) -> list[UCI]:
        return self._pos_drill_data[fen(position)]["blacklist"]

    def _is_learned_move(self, parent: Node, uci: UCI) -> bool:
        try:
            attempts = self._move_attempts(parent, uci)
            return (
                attempts >= self.LEARNED_MIN_ATTEMPTS
                and self._move_right_probability(parent, uci) > self.LEARNED_RIGHT_THRESHOLD
            )
        except KeyError:
            return False

    def _is_blacklisted_move(self, position: BoardLike, uci: UCI) -> bool:
        position_data = self._pos_drill_data.get(fen(position))
        if position_data is None:
            return False
        return uci in position_data["blacklist"]

    def _blacklist_move(self, parent: Node, uci: UCI) -> None:
        blacklist = self._blacklist_for(parent)
        if uci not in blacklist:
            blacklist.append(uci)

    def blacklist_current_move(self) -> UCI:
        blacklisted_uci = self._prompt.node.move.uci()
        self._blacklist_move(self._prompt.node.parent, blacklisted_uci)
        self.finish_prompt()
        return blacklisted_uci

    def _remove_temporary_node(self, node: Node) -> None:
        try:
            self._temporary_nodes.remove(node)
        except ValueError as exc:
            raise RuntimeError("Temporary node was not registered for cleanup") from exc
        self._session.remove_variation(node)

    def _resume_from_off_file_prompt(self) -> None:
        temporary_node = self._prompt.node
        if temporary_node is None or temporary_node.parent is None:
            raise RuntimeError("Off-file prompt has no parent to resume from")
        if not self._prompt.off_file:
            raise RuntimeError("Cannot resume when the prompt is not off-file")

        self._remove_temporary_node(temporary_node)
        self._prompt.node = temporary_node.parent
        self._prompt.off_file = False

    def _has_file_continuation(self, parent: Node) -> bool:
        if self._prompt.node_index is not None and self._prompt.prechosen_path is not None:
            child_index = self._prompt.node_index + 1
            if child_index >= len(self._prompt.prechosen_path):
                return False

            expected_uci = self._prompt.prechosen_path[child_index]
            return any(child.move.uci() == expected_uci for child in self._prompt_variations(parent))

        children = self._prompt_variations(parent)
        if not children:
            return False
        if parent.turn() == self._session.options.side:
            return True

        move_weights = self._get_moves_for_(parent)
        blacklisted_moves = set(self._blacklist_for(parent))
        return any(
            child.move.uci() in move_weights and child.move.uci() not in blacklisted_moves
            for child in children
        )

    def _maybe_choose_off_file_prompt(self, parent: Node) -> tuple[Node | None, str]:
        if parent.turn() == self._session.options.side:
            return None, ""
        if self._rng.random() >= self.non_file_move_freq:
            return None, ""

        off_book_selection = self._select_off_book_move(parent, use_engine=False)
        if off_book_selection.move is None:
            return None, ""

        child = self._add_temporary_node(parent, off_book_selection.move)
        self._prompt.off_file = True
        return child, off_book_selection.message

    def on_response(self, uci: str) -> PromptState:
        if self._prompt.off_file:
            grade = self._handle_off_file_guess(uci)
            if grade.correctness == MoveCorrectness.CORRECT:
                self._current_hints = None
                if self._prompt.node is None or self._prompt.node.parent is None:
                    self._prompt.message = grade.msg
                    self.finish_prompt()
                    return self._prompt
                if not self._has_file_continuation(self._prompt.node.parent):
                    self._prompt.message = grade.msg
                    self.finish_prompt()
                    return self._prompt

                self._resume_from_off_file_prompt()
                self._advance_line()
                self._prompt.message = f"{grade.msg} {self._prompt.message}".strip()
                return self._prompt

            self._prompt.message = grade.msg
            return self._prompt

        grade = self._handle_file_guess(uci)
        if grade.correctness in (MoveCorrectness.INCORRECT, MoveCorrectness.ALTERNATIVE):
            self._prompt.message = grade.msg
            return self._prompt
        if grade.correctness == MoveCorrectness.UNDEF:
            self.finish_prompt()
            return self._prompt

        self._commit_current_tested_move()
        chosen_node = self._session.child_for_move(
            self._prompt.node,
            uci_from_lichess_to_pgn(uci),
        )
        self._prompt.node = chosen_node
        if self._prompt.node_index is not None:
            self._prompt.node_index += 1
        self._current_hints = None
        self._advance_line()

        if not self._is_finished:
            self._prompt.message += grade.msg

        return self._prompt

    def _current_expected_node(self) -> Optional[Node]:
        if self._prompt.node is None:
            return None

        expected_moves = self._prompt_variations(self._prompt.node)
        if not expected_moves:
            return None
        return expected_moves[0]

    def _prompt_variations(self, position: BoardLike) -> list[Node]:
        # Prompt generation and validation are position-based, so they should
        # see TT-backed continuations.
        return self._session.variations(position, use_TT=True)

    def _move_performance_for(self, parent: BoardLike, move_uci: UCI) -> PromptMovePerformance:
        """Return prompt-local performance for a move, creating it on first use."""
        key = (fen(parent), move_uci)
        performance = self._pending_move_performance.get(key)
        if performance is None:
            performance = PromptMovePerformance()
            self._pending_move_performance[key] = performance
        return performance

    def _pop_move_performance(self, parent: BoardLike, move_uci: UCI) -> Optional[PromptMovePerformance]:
        return self._pending_move_performance.pop((fen(parent), move_uci), None)

    def _move_entry(self, parent: BoardLike, move_uci: UCI) -> MoveEntryData:
        moves = self._get_moves_for_(parent, blacklist_included=True)
        if move_uci not in moves:
            raise KeyError(f"Missing move data for {move_uci!r} from position {fen(parent)!r}")
        return moves[move_uci]

    def _global_right_probability(self) -> float:
        if self._global_attempts <= 0:
            return self.DEFAULT_RIGHT_PROBABILITY
        return self._global_successes / self._global_attempts

    def _move_successes_attempts(self, parent: BoardLike, move_uci: UCI) -> tuple[int, int]:
        return self._move_entry(parent, move_uci)["performance"]

    def _move_attempts(self, parent: BoardLike, move_uci: UCI) -> int:
        return self._move_successes_attempts(parent, move_uci)[1]

    def _move_right_probability(self, parent: BoardLike, move_uci: UCI) -> float:
        successes, attempts = self._move_successes_attempts(parent, move_uci)
        if attempts <= 0:
            return self._global_right_probability()
        return successes / attempts

    def _move_wrong_probability(self, parent: BoardLike, move_uci: UCI) -> float:
        return 1.0 - self._move_right_probability(parent, move_uci)

    def _current_tested_move(self) -> Optional[tuple[Node, UCI]]:
        """Return the move the current prompt is asking the user to find."""
        parent = self._prompt.node
        if parent is None:
            return None

        expected_uci = self.expected_uci()
        if expected_uci is None:
            return None
        if expected_uci not in self._get_moves_for_(parent, blacklist_included=True):
            return None
        return parent, expected_uci

    def _record_hint(self, determines_move: bool) -> None:
        tested_move = self._current_tested_move()
        if tested_move is None:
            return
        parent, move_uci = tested_move
        self._move_performance_for(parent, move_uci).add_hint(determines_move)

    def _record_incorrect_attempt(self) -> None:
        tested_move = self._current_tested_move()
        if tested_move is None:
            return
        parent, move_uci = tested_move
        self._move_performance_for(parent, move_uci).add_incorrect_attempt()

    def _record_correct_attempt(self) -> None:
        tested_move = self._current_tested_move()
        if tested_move is None:
            return
        parent, move_uci = tested_move
        self._move_performance_for(parent, move_uci).add_correct_attempt()

    def _commit_current_tested_move(self) -> None:
        """Persist prompt-local stats before advancing away from the current tested move."""
        tested_move = self._current_tested_move()
        if tested_move is None:
            return
        parent, move_uci = tested_move
        performance = self._pop_move_performance(parent, move_uci)
        if performance is None or performance.attempts == 0:
            return

        entry = self._move_entry(parent, move_uci)
        successes, attempts = entry["performance"]
        entry["performance"] = (
            successes + performance.successes,
            attempts + performance.attempts,
        )
        self._global_successes += performance.successes
        self._global_attempts += performance.attempts

    def _hint_matches_current_prompt(self, expected_uci: UCI) -> bool:
        if self._current_hints is None or self._prompt.node is None:
            return False

        return (
            fen(self._current_hints.board) == fen(self._prompt.node)
            and self._current_hints.starting_square == expected_uci[:2]
            and self._current_hints.target_square == expected_uci[2:4]
        )

    def _handle_file_guess(self, uci: UCI) -> MoveGrade:
        uci = uci_from_lichess_to_pgn(uci)
        prpt_data = self._prompt
    
        expected_uci = self.expected_uci()
        if not expected_uci:
            grade = MoveGrade(MoveCorrectness.UNDEF)
            self._grades.append(grade)
            return grade

        if expected_uci == uci:
            self._record_correct_attempt()
            grade = MoveGrade(MoveCorrectness.CORRECT)
            self._grades.append(grade)
            return grade
        if not self._session.options.check_alternatives: # TODO: this is currently leaky. We use the knowledge of
            # how expected_uci is constructed. Need some alternative_moves and a design that ensures their accord
            self._session.options.check_alternatives = True
            chosen_alternative_node = next(
                (c for c in prpt_data.node.variations if c.move.uci() == uci),
                None,
            )
            if chosen_alternative_node is not None:
                self._record_incorrect_attempt()
                grade = MoveGrade(MoveCorrectness.ALTERNATIVE,
                                  msg = "Not the main move. Change the settings to explore alternatives")
                self._grades.append(grade)
                return grade

        expected_moves = [expected_uci] # only this for now
        expected_sans = ", ".join(expected_moves)
        user_ev = self._session.q_eval_move(prpt_data.node, uci)
        eval, move = user_ev.eval, user_ev.move
        evals = [self._evaluate_move(prpt_data.node, m) for m in expected_moves]
        best_expected_eval = max(evals) if evals else None

        msg = f"Wrong. Expected: {expected_sans}."
        if best_expected_eval is not None:
            msg += f" Your move eval {eval:+.2f} after {move.uci()}. File move eval {best_expected_eval:+.2f}."

        eval_diff = None if best_expected_eval is None else eval - best_expected_eval
        rel_eval_diff = None
        if best_expected_eval not in (None, 0):
            rel_eval_diff = eval_diff / best_expected_eval

        grade = MoveGrade(
            MoveCorrectness.INCORRECT,
            msg=msg,
            eval_diff=eval_diff,
            rel_eval_diff=rel_eval_diff,
        )
        self._grades.append(grade)
        self._record_incorrect_attempt()
        return grade

    def _handle_off_file_guess(self, uci: UCI) -> MoveGrade:
        ev = self._session.query(fen(self._prompt.node), "q-eval")
        expected_eval, best_reply = ev.eval, ev.move

        user_ev = self._session.q_eval_move(self._prompt.node, uci)
        move_eval, reply_to_user = user_ev.eval, user_ev.move

        best_reply_san = node_san(self._prompt.node, best_reply) if best_reply else "None"
        san = node_san(self._prompt.node)
        eval_gap = expected_eval - move_eval
        msg = f"Off-file {san}. Your move: eval {move_eval:+.2f} after {reply_to_user}."
        if uci == best_reply.uci() or eval_gap <= 0.2 or move_eval > 0.8*expected_eval:
            msg += f" Best was {best_reply_san} with evaluation {expected_eval:+.2f}. Good job!"
            grade = MoveCorrectness.CORRECT
        else:
            msg += (
                f" Best was {best_reply_san} with evaluation {expected_eval:+.2f}. "
                "Try again."
            )
            grade = MoveCorrectness.INCORRECT
        msg = msg.strip()

        grade = MoveGrade(
            grade,
            msg=msg,
            eval_diff=move_eval - expected_eval,
        )
        self._grades.append(grade)
        if grade.correctness == MoveCorrectness.INCORRECT:
            self._record_incorrect_attempt()
        return grade

    def _evaluate_move(self, position: Union[chess.Board, Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(position, move).eval

    def _skip_current_prompt_position(self) -> tuple[Node | bool, str]:
        if self._prompt.node is None:
            raise RuntimeError("Cannot skip a prompt position without an active prompt")

        learned_node, _ = self._choose_move(self._prompt.node, maybe_off_book=False)
        if learned_node is False:
            return False, ""

        next_node, selection_debug = self._choose_move(learned_node, off_book=True)
        if next_node is False:
            return False, selection_debug

        if self._prompt.node_index is not None:
            self._prompt.node_index += 1
            if not self._prompt.off_file:
                self._prompt.node_index += 1
        return next_node, selection_debug

    def _advance_skipping_lrned_moves(self) -> bool:
        """
        Advance self._prmpt ~until a non-learned move.
        Returns False if prompt generation failed after skipping (e.g. out of moves).
        """
        i = 0
        while True:
            if self._prompt.off_file:
                return True

            expected_node = self._current_expected_node()
            if expected_node is None:
                return True
            if not self._is_learned_move(self._prompt.node, expected_node.move.uci()):
                return True
            if self._rng.random() >= self.LEARNED_MOVE_SKIP_PROBABILITY * (1/2)**i:
                return True
            i += 1

            next_node, selection_debug = self._skip_current_prompt_position()
            if next_node is False:
                return True
            self._prompt.node = next_node
            self._prompt.message = selection_debug

    def _advance_line(self) -> None:
        """
        Assuming self._prompt.node is set for them to move,
        choose a move for them to continue along the line (or off-file) and
        update self._prompt accordingly.
        If the line cannot be continued, updates the state to "prompt finished".
        """
        if self._prompt.node is None:
            raise RuntimeError("Cannot advance a prompt without an active node")
        if self._prompt.off_file:
            raise RuntimeError("Cannot advance the file line while on an off-file prompt")

        if self._prompt.node_index is not None:
            if self._prompt.node.turn() != self._session.options.side:
                next_node, selection_debug = self._maybe_choose_off_file_prompt(self._prompt.node)
                if next_node is not None:
                    self._prompt.node = next_node
                    self._prompt.message = selection_debug
                    return

            try:
                child_index = self._prompt.node_index+1
                next_node = self._session.child_for_move(
                    self._prompt.node,
                    self._prompt.prechosen_path[child_index],
                )
                if next_node is None:
                    self._prompt.off_file = True
                    next_node = self._add_temporary_node(self._prompt.node, self._prompt.prechosen_path[child_index])
                else:
                    self._prompt.node_index += 1

                self._prompt.node = next_node
                return
            except IndexError:
                self._prompt.node_index = None

        parent = self._prompt.node
        next_node, selection_debug = self._choose_move(parent, maybe_off_book=True)

        if next_node is False:
            self._prompt.message = selection_debug
            self.finish_prompt()
            return

        self._prompt.node = next_node
        self._prompt.message = selection_debug
        if self._prompt.off_file:
            return
        if not self._advance_skipping_lrned_moves():
            self._prompt.message = selection_debug
            self.finish_prompt()
            return

        message = f"Correct: {node_san(self._prompt.node)}. Continue along the line."
        if self._prompt.message:
            self._prompt.message = f"{message} {self._prompt.message}"
        else:
            self._prompt.message = message


    def _choose_start_ply_offset(self) -> int:
        """Choose how many plies down the line the prompt should start, relative to self._root."""
        return self._rng.randint(0, 2*self.start_range)+1 # converted to plies
    
    def _choose_prompt_globally(self) -> bool:
        self._prompt_dict_relative = self._prompt_dict_relative or self.make_prompt_dict_relative()

        all_prompts = self._prompt_dict_relative
        possible_prompt_keys = set()

        # Length from the anchor to the final opponent move, in plies.
        prpt_len = self._rng.randint(1, self.MAX_GLOBAL_PROMPT_LENGTH)
        if prpt_len % 2 == 0:
            prpt_len -= 1
        # make sure we are anchored at our move
        possible_start_index = int(self._root.turn() == self._session.options.side)
        # i is offset from root. So root ---i---> anchor ---prpt_len---> prompt end
        for i in range(possible_start_index, self.start_range*2+1, 2):
            possible_prompt_keys.update([k[:i+prpt_len] for k in all_prompts.keys() if len(k) >= i+prpt_len])
        scored_prompt_keys = [
            (prompt_key, self._expected_damage_for_prompt_path(prompt_key, prpt_len))
            for prompt_key in possible_prompt_keys
        ]
        scored_prompt_keys = [item for item in scored_prompt_keys if item[1] >= 0.0]
        scored_prompt_keys.sort(key=lambda item: item[1], reverse=True)
        preferred_prompt_keys = [prompt_key for prompt_key, _ in scored_prompt_keys[:6]]

        if not preferred_prompt_keys:
            return False

        chosen_path = self._rng_choice(preferred_prompt_keys)


        # determine the anchor node and index for the prompt
        offset = len(chosen_path) - prpt_len
        anchor = self._root
        for j in range(offset):
            anchor = self._session.child_for_move(anchor, chosen_path[j])
        index = offset - 1
        
        # make sure we are anchored at their move
        assert anchor.turn() == self._session.options.side

        self._prompt.prechosen_path = chosen_path
        self._prompt.node = anchor
        self._prompt.anchor_node = anchor
        self._prompt.node_index = index
        msg = f"Complete prompt: {self._prompt.prechosen_path}" if DEBUG_MODE else ""
        self._prompt.message = msg
        return True

    def _try_choose_prompt(self, node: Optional[Node] = None, complete: bool = False) -> bool:
        # TODO: settle the logic for node vs self._root. We could in theory ask 
        # to study from a given node but only once. Then do we rebuild the local_prompt_dict?
        # Kind of silly, maybe it is really only "in theory" that this parameter is useful.
        if self.local_generation:
            return self._choose_prompt_locally(node)
        return self._choose_prompt_globally()

    def _choose_prompt_locally(self, node: Optional[Node] = None) -> bool:
        """Simulate walking through a randomly chosen line. 
        Results in populating self._prompt.
        Returns False if the walk along a line failed."""
        node = node or self._root

        selection_debug = ""
        self._prompt.off_file = False
        line_length = self._choose_start_ply_offset()

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

        # Final step: land on the next file move for the opponent.
        assert node.turn() != self._session.options.side, f"Prompt selection should end on our turn {line_length}" # TODO: remove this after a while
        next_node, selection_debug = self._choose_move(node, maybe_off_book=False)
        if next_node is False:
            return False
        
        self._prompt.node = next_node
        self._prompt.message = selection_debug

        if not self._advance_skipping_lrned_moves():
            return False
        self._set_prompt_start()
        return True

    def _expected_damage_for_prompt_path(
        self,
        prompt_path: tuple[UCI, ...],
        prompt_length: int,
    ) -> float:
        anchor_offset = len(prompt_path) - prompt_length
        node = self._root
        try:
            for move_uci in prompt_path[:anchor_offset]:
                node = self._session.child_for_move(node, move_uci)
        except RuntimeError:
            return -1.0

        if node.turn() != self._session.options.side:
            raise RuntimeError("Global prompt anchors must start on the side-to-move position")

        move_probability = 1.0
        expected_damage = 0.0
        for move_uci in prompt_path[anchor_offset:]:
            try:
                if node.turn() == self._session.options.side:
                    expected_damage += self._move_wrong_probability(node, move_uci) * move_probability
                else:
                    move_probability *= self._get_moves_for_(node)[move_uci]["weight"]
            except KeyError:
                return -1.0
            try:
                node = self._session.child_for_move(node, move_uci)
            except RuntimeError:
                return -1.0

        return expected_damage

    def _choose_move(
        self,
        parent: Node,
        *,
        off_book: bool = False,
        maybe_off_book: bool = False,
        use_engine: bool = False,
    ) -> tuple[Node | bool, str]:
        """
        Chooses a move randomly to simulate a step along a line. 
        Determines changes in self._prompt.off_file.
        Returns the resulting node and a debug string.

        If a choice could not be made, returns (False, ...).
        """
        off_book = off_book or (maybe_off_book and self._rng.random() < self.non_file_move_freq)

        children = self._prompt_variations(parent)
        move_weights = self._get_moves_for_(parent)
        blacklisted_moves = set(self._blacklist_for(parent))
        eligible_moves = {
            uci: entry
            for uci, entry in move_weights.items()
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
        # TODO: if check_alternatives

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

        unlearned_weights = {}
        fallback_weights = {}
        if parent.turn() == self._session.options.side:
            for move_uci, entry in eligible_moves.items():
                right_probability = self._move_right_probability(parent, move_uci)
                if right_probability < self.LOCAL_UNLEARNED_RIGHT_THRESHOLD:
                    unlearned_weights[move_uci] = entry["weight"]
                fallback_weights[move_uci] = (1.0 - right_probability) * entry["weight"]
        else:
            for move_uci, entry in eligible_moves.items():
                child = self._session.child_for_move(parent, move_uci)
                expected_node = next(iter(self._prompt_variations(child)), None)
                if expected_node is None:
                    right_probability = self._global_right_probability()
                else:
                    response_uci = expected_node.move.uci()
                    try:
                        right_probability = self._move_right_probability(child, response_uci)
                    except KeyError:
                        right_probability = self._global_right_probability()
                if right_probability < self.LOCAL_UNLEARNED_RIGHT_THRESHOLD:
                    unlearned_weights[move_uci] = entry["weight"]
                fallback_weights[move_uci] = (1.0 - right_probability) * entry["weight"]

        weights_dict = unlearned_weights or fallback_weights
        if sum(weights_dict.values()) <= 0.0:
            weights_dict = {move_uci: entry["weight"] for move_uci, entry in eligible_moves.items()}
        sys.stderr.write(f"Choosing move... selection_weights: {[(k, round(v, 3)) for k, v in weights_dict.items()]}\n")
        choice = self._rng_choice(weights_dict)

        message = ""
        if DEBUG_MODE:
            message = self._format_rng_weights(weights_dict)
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
        position: BoardLike,
        *,
        use_engine: bool,
    ) -> OffBookSelection:        
        db_selection = self._choose_db_off_book_move(position)
        if db_selection.move is not None or db_selection.blacklist_exhausted:
            return db_selection
        if not use_engine:
            return db_selection
        return self._choose_engine_off_book_move(position)

    def _choose_db_off_book_move(self, position: BoardLike) -> OffBookSelection:
        candidates = self._off_book_db_candidates(position)
        if not candidates:
            return OffBookSelection(None, "no non-blacklisted candidates")
        
        cand_dict = dict(candidates)
        move = self._rng_choice(cand_dict)
        debug_text = self._format_rng_weights(cand_dict)
        return OffBookSelection(move, debug_text)

    def _choose_engine_off_book_move(self, position: BoardLike) -> OffBookSelection:
        for i in range(self.MAX_OFF_BOOK_BLACKLIST_ATTEMPTS):
            engine_lines = quick_eval_lines(
                self._session.engine,
                fen(position),
                pov=self._session.options.side,
                multipv=i+1
            )
            if not engine_lines:
                return OffBookSelection(None, "no engine move")

            for line in engine_lines:
                move = line.move
                if self._is_blacklisted_move(position, move.uci()):
                    continue

                debug_msg = f"engine-suggested off-book move {move}"
                return OffBookSelection(move, debug_msg)

        return OffBookSelection(
            None,
            "No non-blacklisted engine off-book moves are available.",
            blacklist_exhausted=True,
        )

    def _off_book_db_candidates(self, position: BoardLike) -> list[tuple[chess.Move, float]]:
        """Find off-book non-BL DB moves with frequency >= 5% and score_rate <= 75%."""
        move_weights = self._get_db_moves_and_nums(position)
        if not move_weights:
            return []

        position_board = to_board(position)
        exclude = set(n.move.uci() for n in self._prompt_variations(position))
        exclude.update(self._blacklist_for(position))

        # Filter candidates: frequency >= 5%, score_rate <= 75%
        candidates: list[tuple[chess.Move, float]] = []
        for uci, weight in move_weights.items():
            if uci in exclude:
                continue

            if self._session.move_freq(position_board, uci) < 0.05:
                continue

            score_rate = self._session.score_rate_move(position_board, uci)
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
        moves = set(child.move.uci() for child in self._prompt_variations(node))
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
    
    
    def _child_nums(self, node: Node, variations: Iterable[UCI]) -> dict[UCI, float]:
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
    
    def _get_db_moves_and_nums(self, position: BoardLike) -> dict[UCI, float]:
        """
        Returns a dict mapping UCI strings to raw DB game counts.
        """
        data = None
        try:
            data = self._session.query(fen(position), "db_lichess")
        except Exception:
            pass
        if not data or "moves" not in data:
            sys.stderr.write(f"No DB moves for {position}\n")
            return {}
        
        weights = {}
        for move_data in data.get("moves", []):
            uci = uci_from_lichess_to_pgn(move_data["uci"])
            count = move_data.get("white", 0) + move_data.get("draws", 0) + move_data.get("black", 0)
            weights[uci] = float(count)
        return weights

    def boost_search_move(
        self,
        root: Node,
        results: list[dict[str, Any]],
        move_uci: UCI,
        target_position: Any,
    ) -> int:
        updated = 0
        boosted_positions: set[str] = set()
        for result in results:
            path = result.get("path")
            if not isinstance(path, list) or not all(isinstance(i, int) for i in path):
                raise TypeError("Search move result path must be a list of integers")

            node = node_at_path(root, path, self._session.variations)
            if compare_positions(target_position, node).similarity < SEARCH_MOVE_BOOST_MIN_SIMILARITY:
                continue

            parent = node.parent
            if parent is None:
                raise RuntimeError("Search move result is missing a parent node")

            parent_fen = fen(parent)
            if parent_fen in boosted_positions:
                continue
            boosted_positions.add(parent_fen)

            move_weights = self._get_moves_for_(parent)
            if move_uci not in move_weights:
                raise RuntimeError(f"Missing move data for {move_uci!r} from position {parent_fen!r}")

            move_weights[move_uci]["weight"] *= SEARCH_MOVE_BOOST_FACTOR
            updated += 1

        self._refresh_prompt_dicts()
        self._pos_drill_data.serialize()
        return updated

    def _grade_eval_loss(self, grade: MoveGrade) -> float:
        if grade.correctness == MoveCorrectness.CORRECT or grade.eval_diff is None:
            return 0.0
        return max(0.0, -grade.eval_diff)


    def _rng_choice(self, weights_dict: dict[K, float] | list[K]) -> K:
        if not weights_dict:
            raise ValueError("No items to choose from")

        if isinstance(weights_dict, list):
            weights_dict = {k: 1 for k in weights_dict}

        threshold = self._rng.random() * sum(weights_dict.values())
        cumulative = 0.0
        items_list = list(weights_dict.items())
        for choice, weight in items_list:
            cumulative += weight
            if threshold <= cumulative:
                return choice
        return items_list[-1][0]


    def _format_rng_weights(self, weights_dict: dict[Any, float]) -> str:
        items = list(weights_dict.keys())
        weights = list(weights_dict.values())
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

    # Debug-only helpers below derive tree state from nodes and prompt metadata.
    # They do not participate in prompt selection or performance updates.
    def _path_is_prefix(self, prefix: tuple[UCI, ...], path: tuple[UCI, ...]) -> bool:
        return prefix == path[:len(prefix)]

    def _debug_prompt_path(self) -> tuple[UCI, ...] | None:
        if self._spec_id != "new" or self._prompt.prechosen_path is None:
            return None
        return tuple(self._prompt.prechosen_path)

    def _sorted_weighted_moves_for(
        self,
        parent: Node,
    ) -> list[tuple[UCI, float, tuple[int, int], str, Optional[Node]]]:
        entries: list[tuple[UCI, float, tuple[int, int], str, Optional[Node]]] = []
        for move_uci, entry in self._get_moves_for_(parent).items():
            try:
                san = node_san(parent, move_uci)
            except Exception:
                san = move_uci
            try:
                child = self._session.child_for_move(parent, move_uci)
            except RuntimeError:
                child = None
            entries.append((move_uci, entry["weight"], entry["performance"], san, child))
        entries.sort(key=lambda item: (-item[1], item[3], item[0]))
        return entries

    def _build_debug_children(
        self,
        parent: Node,
        path: tuple[UCI, ...],
        remaining_prefix: tuple[UCI, ...],
        remaining_forward: int,
        *,
        current_path: tuple[UCI, ...],
        anchor_path: tuple[UCI, ...],
        prompt_path: tuple[UCI, ...] | None,
    ) -> list[dict[str, Any]]:
        if remaining_prefix:
            move_uci = remaining_prefix[0]
            move_weights = self._get_moves_for_(parent)
            move_entry = move_weights.get(move_uci)
            weight = None if move_entry is None else move_entry["weight"]
            performance = (0, 0) if move_entry is None else move_entry["performance"]
            try:
                child = self._session.child_for_move(parent, move_uci)
            except RuntimeError:
                child = None
            try:
                san = node_san(parent, move_uci)
            except Exception:
                san = move_uci
            return [
                self._build_debug_move_node(
                    parent,
                    move_uci,
                    weight,
                    performance,
                    san,
                    child,
                    (*path, move_uci),
                    remaining_prefix=remaining_prefix[1:],
                    remaining_forward=remaining_forward,
                    current_path=current_path,
                    anchor_path=anchor_path,
                    prompt_path=prompt_path,
                )
            ]

        if remaining_forward <= 0:
            return []

        children: list[dict[str, Any]] = []
        for move_uci, weight, performance, san, child in self._sorted_weighted_moves_for(parent):
            children.append(
                self._build_debug_move_node(
                    parent,
                    move_uci,
                    weight,
                    performance,
                    san,
                    child,
                    (*path, move_uci),
                    remaining_prefix=(),
                    remaining_forward=remaining_forward - 1,
                    current_path=current_path,
                    anchor_path=anchor_path,
                    prompt_path=prompt_path,
                )
            )
        return children

    def _build_debug_move_node(
        self,
        parent: Node,
        move_uci: UCI,
        weight: Optional[float],
        performance: tuple[int, int],
        san: str,
        child: Optional[Node],
        path: tuple[UCI, ...],
        *,
        remaining_prefix: tuple[UCI, ...],
        remaining_forward: int,
        current_path: tuple[UCI, ...],
        anchor_path: tuple[UCI, ...],
        prompt_path: tuple[UCI, ...] | None,
    ) -> dict[str, Any]:
        board = parent.board()
        move_number = board.fullmove_number
        color = "white" if parent.turn() == chess.WHITE else "black"
        children: list[dict[str, Any]] = []
        if child is not None:
            children = self._build_debug_children(
                child,
                path,
                remaining_prefix,
                remaining_forward,
                current_path=current_path,
                anchor_path=anchor_path,
                prompt_path=prompt_path,
            )

        return {
            "kind": "move",
            "path": list(path),
            "ply": parent.ply() + 1,
            "moveNumber": move_number,
            "color": color,
            "san": san,
            "uci": move_uci,
            "weight": weight,
            "showWeightLabel": parent.turn() != self._session.options.side,
            "performance": list(performance),
            "children": children,
            "onCurrentPath": self._path_is_prefix(path, current_path),
            "isCurrent": path == current_path,
            "isAnchor": path == anchor_path,
            "onPromptPath": (
                prompt_path is not None
                and len(path) > len(anchor_path)
                and self._path_is_prefix(path, prompt_path)
            ),
        }

    def _build_debug_tree_payload(
        self,
        *,
        root: Node,
        root_label: str,
        prefix_path: tuple[UCI, ...],
        current_path: tuple[UCI, ...],
        anchor_path: tuple[UCI, ...],
        prompt_path: tuple[UCI, ...] | None,
    ) -> dict[str, Any]:
        return {
            "title": "Weight / performance visualizer",
            "tree": {
                "kind": "position",
                "label": root_label,
                "ply": root.ply(),
                "children": self._build_debug_children(
                    root,
                    (),
                    prefix_path,
                    DEBUG_WEIGHT_TREE_FORWARD_PLIES,
                    current_path=current_path,
                    anchor_path=anchor_path,
                    prompt_path=prompt_path,
                ),
                "onCurrentPath": True,
                "isCurrent": len(current_path) == 0,
                "isAnchor": len(anchor_path) == 0,
                "onPromptPath": False,
            },
        }

    def debug_guess_tree_payload(self) -> dict[str, Any]:
        anchor_path_list = self._relative_move_path_for_node(self._prompt.anchor_node) if self._prompt.anchor_node is not None else None
        current_path_list = self._relative_move_path_for_node(self._prompt.node) if self._prompt.node is not None else None
        prompt_path = self._debug_prompt_path()
        if anchor_path_list is None or current_path_list is None:
            root = self._prompt.anchor_node or self._prompt.node or self._root
            return self._build_debug_tree_payload(
                root=root,
                root_label="Active prompt position",
                prefix_path=(),
                current_path=(),
                anchor_path=(),
                prompt_path=None,
            )

        return self._build_debug_tree_payload(
            root=self._root,
            root_label="Study root",
            prefix_path=tuple(anchor_path_list),
            current_path=tuple(current_path_list),
            anchor_path=tuple(anchor_path_list),
            prompt_path=prompt_path,
        )

    def debug_review_tree_payload(self, node: Node) -> dict[str, Any]:
        relative_path = self._relative_move_path_for_node(node)
        root = self._root
        root_label = "Study root"
        prefix_path: tuple[UCI, ...] = ()
        current_path: tuple[UCI, ...] = ()
        anchor_path: tuple[UCI, ...] = ()
        prompt_path = self._debug_prompt_path()
        if relative_path is None:
            root = node
            root_label = "Active review position"
            prompt_path = None
        else:
            prefix_path = tuple(relative_path)
            current_path = tuple(relative_path)
            anchor_path_list = self._relative_move_path_for_node(self._prompt.anchor_node) if self._prompt.anchor_node is not None else None
            if anchor_path_list is not None:
                anchor_path = tuple(anchor_path_list)

        return self._build_debug_tree_payload(
            root=root,
            root_label=root_label,
            prefix_path=prefix_path,
            current_path=current_path,
            anchor_path=anchor_path,
            prompt_path=prompt_path,
        )

@dataclass
class PromptState:
    node: Node
    off_file: bool
    message: str
    anchor_node: Node
    prechosen_path: tuple[str] | None = None
    node_index: int | None = None # None means we are not following the prechosen path

    def __bool__(self):
        return self.node is not None


@dataclass(frozen=True)
class PromptLineId:
    start_fen: str
    moves: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "startFen": self.start_fen,
            "moves": list(self.moves),
        }

    @classmethod
    def from_json(cls, payload: Any) -> "PromptLineId":
        return cls(payload["startFen"], tuple(payload["moves"]))


@dataclass
class PromptLogEntry:
    spec_id: SpecId
    prompt_id: PromptLineId
    prompt_time: float
    performance: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {
            "specId": self.spec_id,
            "promptId": self.prompt_id.to_json(),
            "promptTime": self.prompt_time,
            "performance": self.performance,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "PromptLogEntry":
        return cls(
            spec_id=payload["specId"],
            prompt_id=PromptLineId.from_json(payload["promptId"]),
            prompt_time=float(payload["promptTime"]),
            performance=payload.get("performance"),
        )

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
        self.entries: list[PromptLogEntry] = []
        self._save_path: Optional[str] = None

    def record_prompt(self, prompt_id: PromptId, spec_id: SpecId) -> None:
        self.entries.append(
            PromptLogEntry(
                spec_id=spec_id,
                prompt_id=prompt_id,
                prompt_time=time.time(),
            )
        )

    @property
    def _last_entry(self) -> PromptLogEntry:
        return self.entries[-1]

    def record_feedback(self, prompt_id: PromptId, feedback: Feedback) -> None:
        self._last_entry.performance = float(feedback.quality)
        self.serialize()

    def load_from_file(self, path: str) -> bool:
        self._save_path = path
        self.entries = []
        if not os.path.exists(path):
            sys.stderr.write(f"\nPrompt log file does not exist, starting empty {path}.\n")
            return False

        with open(path, "r", encoding="utf-8") as f:
            self.entries = [PromptLogEntry.from_json(entry) for entry in json.load(f)]
        return True

    def serialize(self) -> None:
        if self._save_path is None:
            raise ValueError("Prompt log path is not set")

        dir_ = os.path.dirname(self._save_path)
        os.makedirs(dir_, exist_ok=True) if dir_ else None

        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=dir_ if dir_ else None,
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                json.dump([entry.to_json() for entry in self.entries], tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._save_path)
        except Exception:
            if tmp_name is not None:
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            raise


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

        self._log = PromptLog()

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

    def _history_child_for_san(self, node: Node, san: str) -> Optional[tuple[int, Node]]:
        for variation_index, child in enumerate(self._session.variations(node)):
            if node_san(child) == san:
                return variation_index, child
        return None

    def _history_moves(self, prompt_id: PromptLineId) -> list[dict[str, Any]]:
        board = self._session.game.board()
        node: Optional[Node] = self._session.game
        path: Optional[list[int]] = []
        prompt_started = fen(board) == prompt_id.start_fen
        moves: list[dict[str, Any]] = []

        for san in prompt_id.moves:
            move_number = board.fullmove_number
            color = "white" if board.turn == chess.WHITE else "black"
            board.push_san(san)
            if node is not None:
                child = self._history_child_for_san(node, san)
                if child is None:
                    node = None
                    path = None
                else:
                    variation_index, node = child
                    path = [*path, variation_index]
            if prompt_started or fen(board) == prompt_id.start_fen:
                prompt_started = True
                moves.append(
                    {
                        "san": san,
                        "moveNumber": move_number,
                        "color": color,
                        "fen": board.fen(),
                        "path": None if path is None else list(path),
                    }
                )

        return moves

    def _history_moves_payload(self, prompt_id: PromptLineId) -> list[dict[str, Any]]:
        return [
            {
                "san": move["san"],
                "moveNumber": move["moveNumber"],
                "color": move["color"],
                "path": move["path"],
                "fen": None if move["path"] is not None else move["fen"],
            }
            for move in self._history_moves(prompt_id)
        ]

    def _history_entry_payload(self, entry: PromptLogEntry) -> dict[str, Any]:
        return {
            "specId": entry.spec_id,
            "promptId": entry.prompt_id.to_json(),
            "promptTime": entry.prompt_time,
            "performance": entry.performance,
            "moves": self._history_moves_payload(entry.prompt_id),
        }

    def _history_payload(self) -> dict[str, Any]:
        entries = [
            self._history_entry_payload(entry)
            for entry in reversed(self._log.entries)
        ]
        return {
            "count": len(entries),
            "entries": entries,
        }

    def _debug_tree_payload(self) -> Optional[dict[str, Any]]:
        if not DEBUG_MODE or not self.active:
            return None

        try:
            if self._mode == "guess":
                return self._rep_engine.debug_guess_tree_payload()
            if self._mode == "review":
                if self._review_path is None:
                    raise RuntimeError("Review mode requires an active review path")
                node = node_at_path(self._session.game, list(self._review_path), self._session.variations)
                return self._rep_engine.debug_review_tree_payload(node)
        except Exception as exc:
            return {
                "title": "Weight visualizer",
                "error": str(exc),
            }
        return None

    def ui_state(self, include_history: bool = True) -> dict[str, Any]:
        state = {
            "active": self.active,
            "mode": self._mode,
            "review": self._review_payload if self.active and self._mode == "review" else None,
            "searchMove": self._search_move_payload if self.active and self._mode == "review" else None,
            "debugTree": self._debug_tree_payload(),
        }
        if hasattr(self, "_cfg"):
            state["startRange"] = self._cfg.start_range
        if include_history and self.active:
            state["history"] = self._history_payload()
        return state

    def start(self, options: SpacedRepetitionOptions, session: Optional[RepertoireSession] = None) -> None:
        try:
            self._cfg = options
            self._session = session or RepertoireSession(
                options,
                default_cache_path=lambda: default_repertoire_cache_path(options),
            )
            self._orientation = "white" if options.play_white else "black"
            
            self._log.load_from_file(self._log_cache_name())
            self._session.cache.autosave_interval = 600
            self._rep_engine: RepetitionEngine = RepetitionEngine(
                self._session,
                self._session.starting_node,
                self._cfg.start_range,
                self._probs_cache_name(),
                self._cfg.non_file_move_frequency,
                self._cfg.local_generation
            )
            self._rep_controller = RepetitionController(
                NaiveScheduler(self._log),
                self._rep_engine,
                self._log,
            )

            if options.preload_db:
                self._prefetch_db_stats()

            self.start_next_prompt()
        finally: # TODO: make sure to find the approptiate moment to save the cache
            try:
                self._session.save_cache()
            except Exception as exc:
                sys.stderr.write(f"Failed to save cache: {exc}\n")
            self._session.close()


    def start_next_prompt(self) -> None:
        self.active = True
        self._mode = "guess"
        self._rep_controller.start_next_prompt()
        self.show_prompt()

    def show_prompt(self, prompt: PromptState | None = None, **kwargs) -> None:
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
        self._broadcast_ui_state(include_history=False)

    def stop(self) -> None:
        self.active = False
        self._mode = "idle"
        self._games = []
        self._close_session()

    def _broadcast_ui_state(self, include_history: bool = True) -> None:
        self._hub.broadcast({"type": "sr_state", "sr": self.ui_state(include_history=include_history)})

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
                    "debugTree": self._debug_tree_payload(),
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

        
        self._session.traverse(self._session.game, visit=visit)


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
        query_move_uci = query_move.uci()

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
                "uci": query_move_uci,
                "move": safe_san(query_node),
                "prev": safe_san(query_parent),
                "next": safe_san(query_next),
                "distance": 0.0,
                "similarity": 1.0,
            },
            "results": results,
            "count": len(results),
            "canBoost": True,
        }
        self._broadcast_ui_state()

    def show_search_move_more_often(self) -> None:
        if not self._search_move_payload.get("canBoost", False):
            raise RuntimeError("Search move results were already boosted")

        query = self._search_move_payload["query"]
        move_uci = query["uci"]
        query_path = query["path"]
        if not isinstance(move_uci, str) or not move_uci:
            raise TypeError("Search move query must contain a non-empty UCI string")
        if not isinstance(query_path, list) or not all(isinstance(i, int) for i in query_path):
            raise TypeError("Search move query path must be a list of integers")
        results = self._search_move_payload.get("results")
        if not isinstance(results, list):
            raise TypeError("Search move results must be a list")

        target_node = node_at_path(self._session.game, query_path, self._session.variations)
        self._rep_engine.boost_search_move(self._session.game, results, move_uci, target_node)
        self._search_move_payload["canBoost"] = False
        self._broadcast_ui_state()

    def _format_weight_sync_message(self, summary: WeightSyncSummary) -> str:
        added_label = "move" if summary.moves_added == 1 else "moves"
        removed_label = "move" if summary.moves_removed == 1 else "moves"
        return (
            f"Updated weights. Checked {summary.positions_checked} cached positions, "
            f"added {summary.moves_added} {added_label}, removed {summary.moves_removed} {removed_label}."
        )

    def _show_status_message(self, message: str) -> None:
        if self._mode == "guess":
            prompt = self._rep_controller.get_prompt_view()
            if prompt.node is None:
                raise RuntimeError("Guess mode requires an active prompt node")

            rendered_message = f"{prompt.message} {message}".strip() if prompt.message else message
            circles = self._rep_engine._current_hints.circles if self._rep_engine._current_hints is not None else None
            self._hub.set_from_node(
                prompt.node,
                orientation=self._orientation,
                message=rendered_message,
                allow_moves=True,
                circles=circles,
            )
            return

        if self._mode == "review":
            if self._review_path is None:
                raise RuntimeError("Review mode requires a current review path")
            node = node_at_path(self._session.game, list(self._review_path), self._session.variations)
            self._hub.set_from_node(
                node,
                orientation=self._orientation,
                message=message,
                allow_moves=False,
            )
            return

        board_state = self._hub.get_state()
        current_fen = board_state.get("fen")
        if not isinstance(current_fen, str) or not current_fen:
            raise RuntimeError("Board state does not contain a current FEN")
        self._hub.set_fen(
            current_fen,
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )

    def update_weights(self) -> None:
        if not self.active:
            return

        summary = self._rep_engine.sync_move_weights_with_pgn()
        self._show_status_message(self._format_weight_sync_message(summary))
        self._broadcast_ui_state(include_history=False)

    def provide_hint(self) -> None:
        if self._mode != "guess":
            return

        self.show_prompt(circles=self._rep_engine.get_hint_circles())

    def _finalize_finished_prompt(self) -> None:
        self._rep_controller.finalize_current_prompt()

    def handle_guess(self, uci: str) -> None:
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")
        
        continue_prompt = self._rep_controller.on_user_response(uci)
        if not continue_prompt:
            prompt = self._rep_controller.get_prompt_view()
            self._enter_review_mode(node=prompt.node, message=prompt.message)
        else:
            self.show_prompt()


    def _mainline_node_at_ply(self, game: Any, ply: int) -> Any:
        node = game
        while getattr(node, "variations", None) and node.ply() < ply:
            node = node.variations[0]
        return node
    
    def give_up(self) -> None:
        self.finish_prompt(gave_up=True)

    def finish_prompt(self, gave_up: bool = False) -> None:
        if self._mode != "guess":
            return

        self._rep_engine.finish_prompt(gave_up=gave_up)
        self._finalize_finished_prompt()
        self._reveal_prompt_in_review()

    def blacklist_current_move(self) -> None:
        if self._mode != "guess":
            return

        blacklisted_uci = self._rep_engine.blacklist_current_move()
        self._finalize_finished_prompt()
        prompt = self._rep_controller.get_prompt_view()
        self._enter_review_mode(
            node=prompt.node,
            message=f"Blacklisted {blacklisted_uci}. Browse the tree or click New.",
        )

    def _reveal_prompt_in_review(self) -> None:
        if self._mode != "guess":
            return

        prompt = self._rep_controller.get_prompt_view()

        if prompt.node is not None:
            expected_uci = self._rep_engine.expected_uci()
            if expected_uci:
                message = f"Expected: {expected_uci}. Browse the tree or click New."
            else:
                message = "No expected moves here. Browse the tree or click New."
        else:
            message = "Off-file prompt. Browse the repertoire tree or click New."

        self._enter_review_mode(node=prompt.node, message=message)

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

    def study_from_here(self, start_range: int) -> None:
        if self._mode != "review":
            raise RuntimeError("Study root can only be changed in review mode")

        node = node_at_path(self._session.game, list(self._review_path), self._session.variations)
        position_fen = fen(node)
        save_settings(
            replace(self._cfg, starting_fen=position_fen, start_range=start_range),
            SpacedRepetitionOptions,
        )
        self._cfg.starting_fen = position_fen
        self._cfg.start_range = start_range
        self._session.options.starting_fen = position_fen
        self._session.starting_node = node
        self._rep_engine.set_start(root=node, start_range=start_range)
        self._enter_review_mode(
            node=node,
            message=(
                f"Study root updated. Click New to practice from here within "
                f"{start_range} {'move' if start_range == 1 else 'moves'}."
            ),
        )

    def study_history_prompt(self, prompt_id: PromptLineId, spec_id: SpecId) -> None:
        self.active = True
        self._mode = "guess"
        self._rep_controller.start_prompt_by_id(prompt_id, spec_id)
        self.show_prompt()

    def _show_history_board(self, position_fen: str, message: str) -> None:
        self._mode = "idle"
        self._review_payload = None
        self._search_move_payload = None
        self._hub.set_fen(
            position_fen,
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state(include_history=False)

    def goto_history_move(self, path: Optional[list[int]], position_fen: Optional[str], san: str) -> None:
        message = f"History: {san}. Click New to continue practicing."
        if path is not None:
            if self._mode == "review":
                self.goto_review_path(path)
                return
            node = node_at_path(self._session.game, path, self._session.variations)
            self._enter_review_mode(node=node, message=message, include_history=False)
            return

        if position_fen is None:
            raise ValueError("History navigation requires either a path or a FEN")
        self._show_history_board(position_fen, message)

    def _enter_review_mode(
        self,
        node: chess.pgn.GameNode,
        message: Optional[str] = "",
        *,
        include_history: bool = True,
    ) -> None:
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
        self._broadcast_ui_state(include_history=include_history)
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
