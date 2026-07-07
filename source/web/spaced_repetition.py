from __future__ import annotations

from enum import Enum
import json
import os
import random
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Literal, Optional, TypeVar, TypedDict, Union, Dict, Tuple

import chess
import chess.pgn
from chess.pgn import GameNode as Node
from dataclasses import dataclass, field, replace

from source.core.caching import CacheDict
from source.core.position_similarity import compare_positions
from source.core.traversal import TraversalPolicy, iter_nodes, mainline_children, traverse as traverse_nodes
from source.web.board.contracts import Arrow, Circle
from source.web.board.session import UCI
from source.web.scheduler_implem import NaiveScheduler

# from source.web.app import BoardHub

from ..core.boardtools import (
    BoardLike,
    arrow_from_uci,
    fen,
    move_identity,
    move_is_marked_as_error,
    moves_are_equal,
    node_moves,
    node_san,
    parse_move_search_notation,
    side,
    to_board,
    uci_from_lichess_to_pgn,
)
from ..core.options import SpacedRepetitionOptions, DEBUG_MODE, save_settings
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path
from .pgn_export import export_pgn_subtree
from .variation_tree import node_at_path, path_from_root, build_variation_tree
from .scheduler_protocol import *
from .memory_model import (
    DELAY_BEFORE_GUESSED_MEANS_REMEMBERS,
    MemoryModel,
    NaiveMemoryModel,
    PerformanceRecord,
)

K = TypeVar("K")
MarkKind = Literal["blacklist", "skip", "bookmark"]


class RequiredPositionDrillData(TypedDict):
    past_performance: dict[UCI, list[PerformanceRecord]]
    blacklist: list[UCI]
    leads_to_skip: list[UCI]


class PositionDrillData(RequiredPositionDrillData, total=False):
    accepted_alternatives: list[UCI]
    bookmarked_moves: list[UCI]


class PositionModelData(TypedDict):
    moves: dict[UCI, MemoryModel] | None


class MarkedMoveData(TypedDict):
    mark: MarkKind
    fen: str
    uci: UCI


class HardestMoveData(TypedDict):
    fen: str
    uci: UCI
    wrong_count: int
    attempt_count: int
    last_attempt_time: float


class MoveCorrectness(Enum):
    CORRECT = 1
    INCORRECT = 2
    ALTERNATIVE = 3
    UNDEF = 4

    def __bool__(self) -> bool:
        return self == MoveCorrectness.CORRECT

@dataclass
class MoveGrade():
    correctness: MoveCorrectness
    msg: str = ""
    eval_diff: float | None = None
    rel_eval_diff: float | None = None
    opponent_reply: UCI | None = None


@dataclass
class AttemptsForMove:
    tested_fen: str
    tested_move_uci: UCI
    prompt_time: float
    grades: list[MoveGrade] = field(default_factory=list)
    hint_used: bool = False

    def add_grade(self, grade: MoveGrade) -> None:
        self.grades.append(grade)

    def add_hint(self) -> None:
        self.hint_used = True


def perf_success_from_attempt_history(
    attempts: AttemptsForMove,
    previous_records: list[PerformanceRecord],
) -> bool | None:
    """Convert raw attempt data into a boolean interpretation of
    success/failure of recall. None means could not be determined.
    """
    if attempts.hint_used:
        return False
    if MoveCorrectness.INCORRECT in (grade.correctness for grade in attempts.grades):
        return False
    
    return True


@dataclass(frozen=True)
class OffBookSelection:
    move: Optional[chess.Move]
    message: str = ""
    blacklist_exhausted: bool = False


DEBUG_WEIGHT_TREE_FORWARD_PLIES = 6


class RepetitionEngine():
    """
    The class responsible for prompt generation and response interpretation.
    Prompts are generated move by move. A move may be randomnly chosen from a file,
    or picked by a chess engine.

    Keeps the result of generating in self._prompt_state and stores prompt-local
    grades grouped by tested move.

    "prompt" is a sequence of moves starting from self._root.
    "performance" is what feeds into memory models -- right/wrong + time for every move.
    "attempts" is broader: has eval diff, hint history etc.
    """
    LEARNED_RIGHT_THRESHOLD = 0.85
    PROGRESS_RECALL_THRESHOLD = 0.75
    LEARNED_MOVE_SKIP_PROBABILITY = 0.90
    MAX_PROMPT_SELECTION_ATTEMPTS = 100
    MAX_OFF_BOOK_BLACKLIST_ATTEMPTS = 3
    MAX_GLOBAL_PROMPT_LENGTH = 10

    def __init__(
        self,
        session: RepertoireSession,
        root: Node,
        start_range: int,
        pos_drill_cache_name: str,
        model_cache_name: str,
        non_file_freq: float,
        local_generation: bool,
        prompt_len_interval: tuple[int, int] = (3, 10),
    ) -> None:
        self._session = session
        self.non_file_move_freq = non_file_freq
        self._rng = random.Random()
        self.local_generation = local_generation
        self.prompt_len_interval = prompt_len_interval

        # updates whenever asked to generate a new prompt
        self._prompt_spec = None

        self._prompt_state = PromptState(node=None, message="", anchor_node=None)
        self._pending_alternative_uci: Optional[UCI] = None

        self.set_start(root=root, start_range=start_range)

        self._pos_drill_data = CacheDict(
            lambda position_fen: PositionDrillData(
                past_performance={},
                blacklist=[],
                leads_to_skip=[],
            ),
            item_to_json=self._pos_drill_item_to_json,
            item_from_json=self._pos_drill_item_from_json,
            auto_save=False,
        )
        self._pos_drill_data.load_from_file(pos_drill_cache_name)

        self._movemodel_data = CacheDict(
            lambda position_fen: PositionModelData(moves=None),
            item_to_json=self._model_item_to_json,
            item_from_json=self._model_item_from_json,
            auto_save=False,
        )
        self._movemodel_data.load_from_file(model_cache_name)

        # when we fill the TT, we want the whole PGN to be present, as in practice
        # PGNs sometimes contain relevant parts that techincally start with an "alternative" move
        # ("for how to play against 10...Re8, see line after 7.0-0")
        # Later, we use prompt_variations as the truth source of relevant children, which avoid querying with alternatives
        # Ugly but PGNs are used messy ways, I don't see a better design
        old_check_alternatives = self._session.options.check_alternatives
        self._session.options.check_alternatives = True
        self._session.fill_the_TT(self._session.game)
        self._session.options.check_alternatives = old_check_alternatives

        self._prompt_performance: list[AttemptsForMove] = []

    def summarize(self) -> Feedback:
        grades = [grade for attempts in self._prompt_performance for grade in attempts.grades]
        if not grades:
            return Feedback(0.0)

        total_loss = sum(self._grade_eval_loss(grade) for grade in grades)
        return Feedback(total_loss / len(grades))

    def expected_uci(self, use_engine: bool = False) -> Optional[UCI]:
        prpt_data = self._prompt_state
        if prpt_data.off_file:
            if use_engine:
                best_move = self._session.query(prpt_data.off_file_fen, "q-eval").best_move()
                if best_move is not None:
                    return best_move.uci()
            return None

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

        if use_engine:
            best_move = self._session.query(fen(prpt_data.node), "q-eval").best_move()
            if best_move is not None:
                return best_move.uci()
        return None

    def make_prompt_dict(
        self,
        root: Node,
        prompt_length: int,
    ) -> dict[tuple[UCI, ...], float]:
        if self._session.options.check_alternatives:
            raise ValueError("Global prompt choice if currently only available for mainline choices.")

        prompt_dict = {}
        prompt_dict[()] = (1.0, 0.0)
        path_from_root = []
        def visit(n: Node):
            nonlocal path_from_root
            
            if n is not root:
                path_from_root.append(n.move.uci())
            if len(path_from_root) > prompt_length:
                return
            
            if n.turn() == self._session.options.side:
                children_uci_moves = {n.move.uci() for n in self._prompt_variations(n.parent)}
                if n.move.uci() in children_uci_moves:
                    path_probability = prompt_dict[tuple(path_from_root[:-2])][0]
                    damage_before = prompt_dict[tuple(path_from_root[:-2])][1]
                    prompt_dict[tuple(path_from_root)] = (
                        path_probability * self._session.move_freq(n.parent, n.move.uci()),
                        damage_before + path_probability * self._expected_damage_of_move(n.parent, n.move.uci()),
                    )

        def post(n, p, v):
            nonlocal path_from_root
            if path_from_root:
                path_from_root.pop()
        
        self._session.traverse(root, visit=visit, post=post,
                               get_children=self._prompt_variations)
        return prompt_dict

    def get_hint_circles(self) -> list[Circle]:
        if self._prompt_state.node is None:
            raise RuntimeError("Cannot provide a hint without an active prompt")

        expected_uci = self.expected_uci(use_engine=True)
        if expected_uci is None:
            return []

        prompt_position = self._current_prompt_position()
        if not self._prompt_hints_match_current_prompt(expected_uci):
            self._prompt_state.hints = Hints(prompt_position, expected_uci)

        if self._prompt_state.hints is None:
            raise RuntimeError("Prompt hints should be initialized for the current prompt")
        self._prompt_state.hints.add_hint()

        return self._prompt_state.hints.circles

    def is_finished(self) -> bool:
        return self._is_finished
    
    def finish_prompt(self, gave_up: bool = False) -> None:
        self._clear_pending_alternative()
        self._finalize_current_move_performance()
        self._is_finished = True
        self._commit_prompt_performance(gave_up=gave_up)
        self._pos_drill_data.serialize()
        self._movemodel_data.serialize()

    def set_start(self, root: Optional[Node] = None, start_range: Optional[int] = None) -> None:
        """ Set the starting point for prompt generation and
            the max of how far we move from the root, in MOVES."""
        if root is not None:
            if root.turn() == self._session.options.side:
                try: 
                    self._root = self._session.variations(root)[0]
                except IndexError:
                    raise RuntimeError("Cannot start from a leaf node")
            else:
                self._root = root
        if start_range is not None:
            self.start_range = start_range

    def _reset_prompt_state(self) -> None:
        self._spec_id = None
        self._is_finished = False
        self._review_payload = None
        self._review_path = None
        self._search_move_payload = None
        self._prompt_performance = []
        self._prompt_state = PromptState(node=None, message="", anchor_node=None)
        self._clear_pending_alternative()

    def _relative_move_path_for_node(self, node: Node) -> Optional[list[UCI]]:
        root_moves = node_moves(self._root, san=False)
        node_path = node_moves(node, san=False)
        if node_path[:len(root_moves)] != root_moves:
            return None
        return node_path[len(root_moves):]

    def _child_for_move(
        self,
        parent: Node,
        move: chess.Move | UCI,
    ) -> Optional[Node]:
        move_uci = move.uci() if isinstance(move, chess.Move) else move

        for child in self._session.variations(parent, use_TT=True):
            if child.move.uci() != move_uci:
                continue
            return child

        return None

    def _tt_child_for_move(
        self,
        position: BoardLike,
        move: chess.Move | UCI,
    ) -> Optional[Node]:
        move_uci = move.uci() if isinstance(move, chess.Move) else move
        cached = self._session.cache.get(fen(position))
        if cached is None:
            return None

        for node in cached.TTed:
            for child in node.variations:
                if child.move is not None and child.move.uci() == move_uci:
                    return child
        return None

    def _current_prompt_position(self) -> BoardLike:
        if self._prompt_state.off_file:
            return self._prompt_state.off_file_fen
        if self._prompt_state.node is None:
            raise RuntimeError("Prompt position is not initialized")
        return self._prompt_state.node

    def _set_off_file_position(
        self,
        parent: Node,
        move: chess.Move | UCI,
    ) -> None:
        move_uci = move.uci() if isinstance(move, chess.Move) else move
        board = parent.board()
        board.push(chess.Move.from_uci(move_uci))
        self._prompt_state.off_file_fen = fen(board)
        self._prompt_state.node = parent
        self._prompt_state.hints = None

    def _clear_off_file_state(self) -> None:
        self._prompt_state.off_file_fen = None
        self._prompt_state.hints = None
        self._prompt_state.feedback = []

    def _infer_off_file_move(self, parent: Node) -> UCI:
        # Note: off-file blacklisting currently assumes the
        # off-file prompt position is exactly one legal move from a file node.
        # If this assumption breaks, this hacky code will need to change.
        target_fen = self._prompt_state.off_file_fen
        board = parent.board()

        for move in board.legal_moves:
            board.push(move)
            if fen(board) == target_fen:
                return move.uci()
            board.pop()

    def _create_prompt_from_id(self, prompt_id: PromptLineId) -> PromptState:
        full_moves = list(prompt_id.moves)

        node = self._session.game
        board = node.board()
        prechosen_path: list[UCI] = []
        anchor_node = node if fen(node) == prompt_id.start_fen else None
        anchor_index = -1 if anchor_node is not None else None

        for san in full_moves:
            move = board.parse_san(san)
            prechosen_path.append(move.uci())
            if anchor_node is None:
                child = self._child_for_move(node, move)
                if child is None:
                    raise ValueError(f"Prompt id {prompt_id!r} no longer resolves to a file line")
                node = child
                if fen(node) == prompt_id.start_fen:
                    anchor_node = node
                    anchor_index = len(prechosen_path) - 1
            board.push(move)

        if anchor_node is None or anchor_index is None:
            raise ValueError(f"Prompt id {prompt_id!r} does not resolve to a known prompt start")

        return PromptState(
            node=anchor_node,
            message="",
            anchor_node=anchor_node,
            prechosen_path=tuple(prechosen_path),
            node_index=anchor_index,
        )

    def start_prompt(self, spec_id: SpecId) -> PromptState:
        if DEBUG_MODE:
            seed = random.SystemRandom().randint(0, 2**32 - 1)
            # seed = 354357997 # paste the latest seed to reproduce behavior
            self._rng.seed(seed)
            sys.stderr.write(f"RNG seed: {seed}\n")

        self._reset_prompt_state()

        if spec_id == "new":
            self._spec_id = spec_id
            self._choose_random_prompt()
        else:
            raise ValueError(f"Unsupported spec_id: {spec_id!r}")

        return self._prompt_state

    def start_prompt_by_id(self, prompt_id: PromptLineId, spec_id: SpecId = "by id") -> PromptState:
        self._reset_prompt_state()
        self._spec_id = spec_id
        self._prompt_state = self._create_prompt_from_id(prompt_id)
        self._activate_prompt_state()
        return self._prompt_state

    def _choose_random_prompt(self) -> PromptState:
        for _ in range(self.MAX_PROMPT_SELECTION_ATTEMPTS):
            try:
                success = self._try_choose_prompt(self._root, complete=True)
            except Exception:
                raise

            if success:
                return self._prompt_state

        raise RuntimeError("Could not generate a prompt. No eligible prompt moves remained.")

    def current_spec_id(self):
        return self._spec_id

    def current_prompt_id(self):
        if self._prompt_state.anchor_node is None or self._prompt_state.node is None:
            raise RuntimeError("Cannot build a prompt id without anchor and current nodes")
        return PromptLineId(fen(self._prompt_state.anchor_node), tuple(node_moves(self._prompt_state.node)))

    def _set_prompt_start(self) -> None:
        self._prompt_state.anchor_node = self._prompt_state.node

    def _activate_prompt_state(self) -> None:
        self._prompt_state.prompt_time = time.time()
        self._prompt_state.hints = None
        self._prompt_state.feedback = []

    def _pos_drill_item_to_json(
        self,
        item: tuple[str, PositionDrillData],
    ) -> dict[str, Any]:
        position_fen, position_data = item
        payload = {
            "fen": position_fen,
            "past_performance": {
                move_uci: [
                    record.to_json()
                    for record in performance
                ]
                for move_uci, performance in position_data["past_performance"].items()
            },
            "blacklist": list(position_data["blacklist"]),
            "leads_to_skip": list(position_data["leads_to_skip"]),
        }
        accepted_alternatives = position_data.get("accepted_alternatives", [])
        if accepted_alternatives:
            payload["accepted_alternatives"] = list(accepted_alternatives)
        bookmarked_moves = position_data.get("bookmarked_moves", [])
        if bookmarked_moves:
            payload["bookmarked_moves"] = list(bookmarked_moves)
        return payload

    def _pos_drill_item_from_json(
        self,
        payload: Any,
    ) -> tuple[str, PositionDrillData]:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise TypeError("Move cache payload must be a dict")

        position_fen = payload["fen"]
        raw_past_performance = payload["past_performance"]
        raw_blacklist = payload["blacklist"]
        raw_leads_to_skip = payload["leads_to_skip"]
        raw_accepted_alternatives = payload.get("accepted_alternatives", [])
        raw_bookmarked_moves = payload.get("bookmarked_moves", [])

        past_performance: dict[UCI, list[PerformanceRecord]] = {}
        blacklist: list[UCI] = []
        leads_to_skip: list[UCI] = []
        accepted_alternatives: list[UCI] = []
        bookmarked_moves: list[UCI] = []

        if not isinstance(raw_past_performance, dict):
            raise TypeError(f"Performance payload for position {position_fen!r} must be a dict")
        if not isinstance(raw_blacklist, list):
            raise TypeError(f"Blacklist payload for position {position_fen!r} must be a list")
        if not isinstance(raw_leads_to_skip, list):
            raise TypeError(f"Skip payload for position {position_fen!r} must be a list")
        if not isinstance(raw_accepted_alternatives, list):
            raise TypeError(f"Accepted alternatives payload for position {position_fen!r} must be a list")
        if not isinstance(raw_bookmarked_moves, list):
            raise TypeError(f"Bookmarked moves payload for position {position_fen!r} must be a list")

        for uci, raw_history in raw_past_performance.items():
            if not isinstance(raw_history, list):
                raise TypeError(
                    f"Performance history for {uci!r} from position {position_fen!r} must be a list"
                )
            past_performance[uci] = [
                PerformanceRecord.from_json(record)
                for record in raw_history
            ]

        for raw_uci in raw_blacklist:
            if raw_uci not in blacklist:
                blacklist.append(raw_uci)
        for raw_uci in raw_leads_to_skip:
            if raw_uci not in leads_to_skip:
                leads_to_skip.append(raw_uci)
        for raw_uci in raw_accepted_alternatives:
            if raw_uci not in accepted_alternatives:
                accepted_alternatives.append(raw_uci)
        for raw_uci in raw_bookmarked_moves:
            if raw_uci not in bookmarked_moves:
                bookmarked_moves.append(raw_uci)

        position_data = PositionDrillData(
            past_performance=past_performance,
            blacklist=blacklist,
            leads_to_skip=leads_to_skip,
        )
        if accepted_alternatives:
            position_data["accepted_alternatives"] = accepted_alternatives
        if bookmarked_moves:
            position_data["bookmarked_moves"] = bookmarked_moves
        return position_fen, position_data

    def _model_item_to_json(
        self,
        item: tuple[str, PositionModelData],
    ) -> dict[str, Any]:
        position_fen, position_data = item
        raw_moves = position_data["moves"]
        serialized_moves = None
        if raw_moves is not None:
            serialized_moves = {
                move_uci: model.to_json()
                for move_uci, model in raw_moves.items()
            }
        return {
            "fen": position_fen,
            "moves": serialized_moves,
        }

    def _model_item_from_json(
        self,
        payload: Any,
    ) -> tuple[str, PositionModelData]:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise TypeError("Model cache payload must be a dict")

        position_fen = payload["fen"]
        raw_moves = payload["moves"]
        if raw_moves is None:
            return position_fen, PositionModelData(moves=None)
        if not isinstance(raw_moves, dict):
            raise TypeError(f"Model payload for position {position_fen!r} must be a dict or null")

        moves = {
            move_uci: MemoryModel.from_json(model_payload)
            for move_uci, model_payload in raw_moves.items()
        }
        return position_fen, PositionModelData(moves=moves)

    def _new_movemodel(self) -> MemoryModel:
        return NaiveMemoryModel()

    def _blacklist_for(self, position: BoardLike) -> list[UCI]:
        return self._pos_drill_data[fen(position)]["blacklist"]

    def _leads_to_skip_for(self, position: BoardLike) -> list[UCI]:
        return self._pos_drill_data[fen(position)]["leads_to_skip"]

    def _accepted_alternatives_for(self, position: BoardLike) -> list[UCI]:
        position_data = self._pos_drill_data.get(fen(position))
        if position_data is None:
            return []
        return position_data.get("accepted_alternatives", [])

    def _accepted_alternatives_for_update(self, position: BoardLike) -> list[UCI]:
        return self._pos_drill_data[fen(position)].setdefault("accepted_alternatives", [])

    def _prompt_position_is_learned(self, prompt_node: Node) -> bool:
        if prompt_node.parent is None or prompt_node.move is None:
            return False
        parent = prompt_node.parent
        move_uci = prompt_node.move.uci()
        return self._move_predict_success(parent, move_uci) > self.LEARNED_RIGHT_THRESHOLD


    def _blacklist_move(self, parent: Node, uci: UCI) -> None:
        blacklist = self._blacklist_for(parent)
        if uci not in blacklist:
            blacklist.append(uci)

    def _mark_move_to_skip(self, parent: Node, uci: UCI) -> None:
        leads_to_skip = self._leads_to_skip_for(parent)
        if uci not in leads_to_skip:
            leads_to_skip.append(uci)

    def _record_accepted_alternative(self, parent: BoardLike, uci: UCI) -> None:
        accepted_alternatives = self._accepted_alternatives_for_update(parent)
        if uci in accepted_alternatives:
            return
        accepted_alternatives.append(uci)
        self._pos_drill_data.serialize()

    def _bookmarked_moves_for(self, position: BoardLike) -> list[UCI]:
        position_data = self._pos_drill_data.get(fen(position))
        if position_data is None:
            return []
        return position_data.get("bookmarked_moves", [])

    def _bookmarked_moves_for_update(self, position: BoardLike) -> list[UCI]:
        return self._pos_drill_data[fen(position)].setdefault("bookmarked_moves", [])

    def _bookmark_move(self, parent: BoardLike, uci: UCI) -> None:
        bookmarked = self._bookmarked_moves_for_update(parent)
        if uci not in bookmarked:
            bookmarked.append(uci)
            self._pos_drill_data.serialize()

    def bookmark_move(self, parent: BoardLike, uci: UCI) -> None:
        self._bookmark_move(parent, uci)

    def move_is_bookmarked(self, parent: BoardLike, uci: UCI) -> bool:
        return uci in self._bookmarked_moves_for(parent)

    def _unbookmark_move(self, position_fen: str, move_uci: UCI) -> None:
        position_data = self._pos_drill_data.get(position_fen)
        if position_data is None:
            return
        bookmarked = position_data.get("bookmarked_moves", [])
        if move_uci in bookmarked:
            bookmarked.remove(move_uci)
            self._pos_drill_data.serialize()

    def unbookmark_move(self, position_fen: str, move_uci: UCI) -> None:
        self._unbookmark_move(position_fen, move_uci)

    def marked_moves(self) -> list[MarkedMoveData]:
        marked: list[MarkedMoveData] = []
        for position_fen, position_data in self._pos_drill_data.items():
            marked.extend(
                MarkedMoveData(mark="blacklist", fen=position_fen, uci=uci)
                for uci in position_data["blacklist"]
            )
            marked.extend(
                MarkedMoveData(mark="skip", fen=position_fen, uci=uci)
                for uci in position_data["leads_to_skip"]
            )
            marked.extend(
                MarkedMoveData(mark="bookmark", fen=position_fen, uci=uci)
                for uci in position_data.get("bookmarked_moves", [])
            )
        marked.sort(key=lambda item: (item["mark"], item["fen"], item["uci"]))
        return marked

    def hardest_moves(self) -> list[HardestMoveData]:
        hardest: list[HardestMoveData] = []
        for position_fen, position_data in self._pos_drill_data.items():
            for move_uci, history in position_data["past_performance"].items():
                wrong_count = sum(1 for record in history if not record.success)
                if wrong_count == 0:
                    continue
                hardest.append(
                    HardestMoveData(
                        fen=position_fen,
                        uci=move_uci,
                        wrong_count=wrong_count,
                        attempt_count=len(history),
                        last_attempt_time=history[-1].attempt_time,
                    )
                )

        hardest.sort(
            key=lambda item: (
                -item["wrong_count"],
                -item["attempt_count"],
                -item["last_attempt_time"],
                item["fen"],
                item["uci"],
            )
        )
        return hardest

    def file_nodes_for_position_move(self, position_fen: str, move_uci: UCI) -> list[Node]:
        nodes: list[Node] = []
        for parent in self._session.cache[fen(position_fen)].TTed:
            for child in self._session.variations(parent):
                if child.move is not None and child.move.uci() == move_uci:
                    nodes.append(child)
        return nodes

    def file_nodes_for_marked_move(self, position_fen: str, move_uci: UCI) -> list[Node]:
        return self.file_nodes_for_position_move(position_fen, move_uci)

    def unmark_move(self, mark: MarkKind, position_fen: str, move_uci: UCI) -> None:
        position_data = self._pos_drill_data.get(position_fen)
        if position_data is None:
            raise KeyError(f"Unknown marked move position {position_fen!r}")

        if mark == "blacklist":
            mark_list = position_data["blacklist"]
        elif mark == "skip":
            mark_list = position_data["leads_to_skip"]
        elif mark == "bookmark":
            mark_list = position_data.get("bookmarked_moves", [])
        else:
            raise ValueError(f"Unsupported move mark: {mark!r}")

        try:
            mark_list.remove(move_uci)
        except ValueError as exc:
            raise KeyError(f"Move {move_uci!r} is not marked as {mark!r}") from exc

        self._pos_drill_data.serialize()

    def _move_is_marked_to_skip(self, position: BoardLike, uci: UCI) -> bool:
        position_data = self._pos_drill_data.get(fen(position))
        if position_data is None:
            return False
        return uci in position_data["leads_to_skip"]

    def blacklist_current_move(self) -> UCI:
        self._clear_pending_alternative()
        prompt_node = self._prompt_state.node
        if prompt_node is None:
            raise RuntimeError("Cannot blacklist without an active prompt node")

        if self._prompt_state.off_file:
            blacklisted_uci = self._infer_off_file_move(prompt_node)
            self._blacklist_move(prompt_node, blacklisted_uci)
            self._clear_off_file_state()
            self._prompt_state.prechosen_path = None
            self._prompt_state.node_index = None

            if not self._is_finished:
                self._advance_line()

            self._prompt_state.message = (
                f"Blacklisted {blacklisted_uci}. {self._prompt_state.message}"
            ).strip()
            return blacklisted_uci

        parent = prompt_node.parent
        if parent is None or prompt_node.move is None:
            raise RuntimeError("Cannot blacklist the root position")

        blacklisted_uci = prompt_node.move.uci()
        should_reset_anchor = prompt_node is self._prompt_state.anchor_node

        self._blacklist_move(parent, blacklisted_uci)
        self._prompt_state.node = parent
        self._clear_off_file_state()
        self._prompt_state.prechosen_path = None
        self._prompt_state.node_index = None

        if not self._is_finished and should_reset_anchor:
            self._set_prompt_start()

        if not self._is_finished:
            self._advance_line()

        self._prompt_state.message = (
            f"Blacklisted {blacklisted_uci}. {self._prompt_state.message}"
        ).strip()
        return blacklisted_uci

    def blacklist_current_line(self) -> UCI: # TODO: remove
        """Blacklists the first move of the current prompt line.
        (Note that technically the current position may still appear, but via a different line.)"""
        self._clear_pending_alternative()
        prompt_start_node = self._prompt_state.node
        while prompt_start_node is not self._root:
            blacklisted_uci = prompt_start_node.move.uci()
            prompt_start_node = prompt_start_node.parent
        self._blacklist_move(prompt_start_node.parent, blacklisted_uci)
        self._prompt_state.message = (
            f"Blacklisted the line starting with {blacklisted_uci}. {self._prompt_state.message}"
        ).strip()
        return blacklisted_uci

    def skip_current_move(self) -> None:
        self._clear_pending_alternative()
        prompt_node = self._prompt_state.node
        if prompt_node is None:
            raise RuntimeError("Cannot skip without an active prompt node")
        
        if self._prompt_state.off_file:
            if self._try_get_on_file_by_transposition():
                return
            self._prompt_state.message = (
            "Off-file position. Can't skip --"
            "no transposition back into the file was found."
            )
            self.finish_prompt()
            return

        self._mark_move_to_skip(prompt_node.parent, prompt_node.move.uci())
        self._skip_learned_prompt_positions()

    def _off_file_transposition_node(self, off_file_fen: str, move: chess.Move) -> Node:
        board = to_board(off_file_fen)
        board.push(move)
        cached = self._session.cache.get(fen(board))
        if cached is None or not cached.TTed:
            raise RuntimeError(f"Move {move.uci()!r} did not transpose to a file position")
        return cached.TTed[0]

    def _try_get_on_file_by_transposition(self) -> bool:
        off_file_fen = self._prompt_state.off_file_fen
        if off_file_fen is None:
            raise RuntimeError("Cannot transpose an off-file skip without an off-file FEN")

        board = to_board(off_file_fen)
        transp_node = self._session.find_transpositioning_move(board)
        if transp_node is None:
            return False

        self._prompt_state.node = transp_node
        self._clear_off_file_state()
        self._prompt_state.prechosen_path = None
        self._prompt_state.node_index = None

        transposition_message = (
            f"Skipped the off-file prompt by transposing back to the file with {node_san(transp_node)}."
        )
        if not self._is_finished:
            self._advance_line()
        self._prompt_state.message = (
            f"{transposition_message} {self._prompt_state.message}"
        ).strip()
        return True

    def _resume_from_off_file_prompt(self) -> None:
        if not self._prompt_state.off_file:
            raise RuntimeError("Cannot resume when the prompt is not off-file")

        self._clear_off_file_state()

    def _has_file_continuation(self, parent: Node) -> bool:
        if self._prompt_state.node_index is not None and self._prompt_state.prechosen_path is not None:
            child_index = self._prompt_state.node_index + 1
            if child_index < len(self._prompt_state.prechosen_path):
                expected_uci = self._prompt_state.prechosen_path[child_index]
                return any(child.move.uci() == expected_uci for child in self._prompt_variations(parent))

        children = self._prompt_variations(parent)
        return bool(children)

    def _maybe_choose_off_book_prompt(self, parent: Node) -> tuple[bool, str]:
        if parent.turn() == self._session.options.side:
            return False, ""
        if self._rng.random() >= self.non_file_move_freq:
            return False, ""

        off_book_selection = self._select_off_book_move(parent, use_engine=False)
        if off_book_selection.move is None:
            return False, ""

        self._set_off_file_position(parent, off_book_selection.move)
        return True, off_book_selection.message

    def on_response(self, uci: str) -> PromptState:
        self._prompt_state.feedback = []

        self._clear_pending_alternative()
        if self._prompt_state.off_file:
            grade = self._handle_off_file_guess(uci)
            if grade.correctness == MoveCorrectness.CORRECT:
                if self._prompt_state.node is None:
                    self._prompt_state.message = grade.msg
                    self.finish_prompt()
                    return self._prompt_state
                if not self._has_file_continuation(self._prompt_state.node):
                    self._prompt_state.message = grade.msg
                    self.finish_prompt()
                    return self._prompt_state

                self._resume_from_off_file_prompt()
                self._advance_line()
                self._prompt_state.message = f"{grade.msg} {self._prompt_state.message}".strip()
                return self._prompt_state

            self._prompt_state.message = grade.msg
            return self._prompt_state

        grade = self._handle_file_guess(uci)
        if grade.correctness in (MoveCorrectness.INCORRECT, MoveCorrectness.ALTERNATIVE):
            self._prompt_state.message = grade.msg


            arrow_color = "red" if grade.eval_diff < -0.7 else "yellow"
            self._prompt_state.feedback += [Arrow.from_uci(uci, "blue")]
            self._prompt_state.feedback += [Arrow.from_uci(grade.opponent_reply, arrow_color)]
            return self._prompt_state
        self._save_grade(grade)
        if grade.correctness == MoveCorrectness.UNDEF:
            self.finish_prompt()
            return self._prompt_state

        chosen_node = self._prompt_child_for_move(
            self._prompt_state.node,
            uci_from_lichess_to_pgn(uci),
        )
        if chosen_node is None:
            self.finish_prompt()
            return self._prompt_state
        return self._continue_with_file_child(chosen_node, grade)

    def _continue_with_file_child(self, chosen_node: Node, grade: MoveGrade) -> PromptState:
        self._finalize_current_move_performance()
        self._prompt_state.node = chosen_node
        if self._prompt_state.node_index is not None:
            self._prompt_state.node_index += 1
        self._advance_line()

        if not self._is_finished:
            self._prompt_state.message += grade.msg

        return self._prompt_state

    def _current_expected_node(self) -> Optional[Node]:
        if self._prompt_state.node is None:
            return None

        if self._prompt_state.node_index is not None and self._prompt_state.prechosen_path is not None:
            child_index = self._prompt_state.node_index + 1
            if child_index < len(self._prompt_state.prechosen_path):
                expected_uci = self._prompt_state.prechosen_path[child_index]
                return self._child_for_move(self._prompt_state.node, expected_uci)

        expected_moves = self._prompt_variations(self._prompt_state.node)
        if not expected_moves:
            return None
        return expected_moves[0]

    def _prompt_variations(self, position: BoardLike) -> list[Node]:
        """The source of truth for the next moves to choose from."""
        if position.turn() == self._session.options.side:
            children = list(self._session.variations(position, use_TT=False))
            seen_ucis = {
                child.move.uci()
                for child in children
                if child.move is not None
            }
            for accepted_uci in self._accepted_alternatives_for(position):
                if accepted_uci in seen_ucis:
                    continue
                child = self._tt_child_for_move(position, accepted_uci)
                if child is None or child.move is None:
                    continue
                children.append(child)
                seen_ucis.add(accepted_uci)
            return children
        # ^ we imagine that for us we don't want TT continuations
        # I have in mind a picture of a pgn file with different moves for a node
        # as alternatives; but it is hard to imagine where a user wants to be tested
        # on a move that happends elsewhere in the file.

        # Opponent move selection can still use TT
        return [n for n in self._session.variations(position, use_TT=True)
                if n.move.uci() not in self._blacklist_for(position)]

    def _prompt_child_for_move(self, parent: BoardLike, move: chess.Move | UCI) -> Optional[Node]:
        move_uci = move.uci() if isinstance(move, chess.Move) else move
        for child in self._prompt_variations(parent):
            if child.move is not None and child.move.uci() == move_uci:
                return child
        return None

    def _clear_pending_alternative(self) -> None:
        self._pending_alternative_uci = None

    def pending_alternative_uci(self) -> Optional[UCI]:
        return self._pending_alternative_uci

    def _existing_performance_history(
        self,
        parent: BoardLike,
        move_uci: UCI,
    ) -> list[PerformanceRecord] | None:
        position_data = self._pos_drill_data.get(fen(parent))
        if position_data is None:
            return None
        return position_data["past_performance"].get(move_uci)

    def _performance_history(self, parent: BoardLike, move_uci: UCI) -> list[PerformanceRecord]:
        position_fen = fen(parent)
        return self._pos_drill_data[position_fen]["past_performance"].setdefault(move_uci, [])

    def _movemodel(self, parent: BoardLike, move_uci: UCI) -> MemoryModel:
        position_fen = fen(parent)
        position_data = self._movemodel_data[position_fen]
        if position_data["moves"] is None:
            position_data["moves"] = {}
        return position_data["moves"].setdefault(move_uci, self._new_movemodel())

    def _existing_movemodel(self, parent: BoardLike, move_uci: UCI) -> MemoryModel | None:
        position_data = self._movemodel_data.get(fen(parent))
        if position_data is None or position_data["moves"] is None:
            return None
        return position_data["moves"].get(move_uci)

    def _move_predict_success(self, parent: BoardLike, move_uci: UCI) -> float:
        history = self._existing_performance_history(parent, move_uci) or []
        move_model = self._existing_movemodel(parent, move_uci) or self._new_movemodel()
        return move_model.predict_success(history)

    def _memorization_crit_for_progr_feedback(
        self,
        parent: BoardLike,
        move_uci: UCI,
        history: list[PerformanceRecord],
        now: float,
    ) -> bool:
        """The criteria for counting a move as "learned" for the progress feedback are:
        1) The user got the move right in the latest attempt
        2) It is not just in short-term memory
        3) The chance to recall is high"""
        if not history:
            return False
        latest_repetition = history[-1]
        if not latest_repetition.success:
            return False
        if now - latest_repetition.attempt_time <= DELAY_BEFORE_GUESSED_MEANS_REMEMBERS:
            return False
        return self._move_predict_success(parent, move_uci) > self.PROGRESS_RECALL_THRESHOLD

    def progress_payload(self) -> dict[str, Any]:
        learned_moves = 0
        initially_missed_moves = 0
        short_term_moves = 0
        tested_moves = 0
        learnable_moves = 0
        seen: set[tuple[str, UCI]] = set()
        now = time.time()

        def visit(node: Node) -> None:
            nonlocal learned_moves
            nonlocal initially_missed_moves
            nonlocal short_term_moves
            nonlocal tested_moves
            nonlocal learnable_moves

            if node.parent is None or node.move is None:
                return
            if node.turn() != self._session.options.side:
                return

            parent = node.parent
            move_uci = node.move.uci()
            position_fen = fen(parent)
            move_key = (position_fen, move_uci)
            if move_key in seen:
                return
            seen.add(move_key)
            learnable_moves += 1

            history = self._existing_performance_history(parent, move_uci)
            if not history:
                return

            tested_moves += 1
            if history[0].success:
                return

            initially_missed_moves += 1
            latest_repetition = history[-1]
            if (
                latest_repetition.success
                and now - latest_repetition.attempt_time <= DELAY_BEFORE_GUESSED_MEANS_REMEMBERS
            ):
                short_term_moves += 1
                return

            if self._memorization_crit_for_progr_feedback(parent, move_uci, history, now):
                learned_moves += 1

        self._session.traverse(self._session.game, visit=visit)

        return {
            "learnedMoves": learned_moves,
            "initiallyMissedMoves": initially_missed_moves,
            "shortTermMoves": short_term_moves,
            "testedMoves": tested_moves,
            "learnableMoves": learnable_moves,
            "recallThreshold": self.PROGRESS_RECALL_THRESHOLD,
            "delaySeconds": DELAY_BEFORE_GUESSED_MEANS_REMEMBERS,
        }


    def _current_prompt_origin_move(self) -> Optional[tuple[Node, UCI]]:
        """Return the move from a file node that produced the current prompt."""
        prompt_node = self._prompt_state.node
        if prompt_node is None:
            return None

        if self._prompt_state.off_file:
            return prompt_node, self._infer_off_file_move(prompt_node)

        if prompt_node.parent is None or prompt_node.move is None:
            return None
        if prompt_node.turn() != self._session.options.side:
            return None

        parent = prompt_node.parent
        move_uci = prompt_node.move.uci()
        return parent, move_uci

    def _current_performance_move(self) -> Optional[tuple[Node, UCI]]:
        """Return the current prompt move only if it should update memory data."""
        if self._prompt_state.off_file:
            return None
        return self._current_prompt_origin_move()

    def _attempts_for_move(self, parent: BoardLike, move_uci: UCI) -> AttemptsForMove:
        position_fen = fen(parent)
        for attempts in self._prompt_performance:
            if attempts.tested_fen == position_fen and attempts.tested_move_uci == move_uci:
                return attempts

        attempts = AttemptsForMove(
            tested_fen=position_fen,
            tested_move_uci=move_uci,
            prompt_time=self._prompt_state.prompt_time,
        )
        self._prompt_performance.append(attempts)
        return attempts

    def _current_attempts_for_move(self) -> Optional[AttemptsForMove]:
        tested_move = self._current_performance_move()
        if tested_move is None:
            return None
        parent, move_uci = tested_move
        return self._attempts_for_move(parent, move_uci)

    def _save_grade(self, grade: MoveGrade) -> None:
        attempts = self._current_attempts_for_move()
        if attempts is None:
            return
        attempts.add_grade(grade)

    def _finalize_current_move_performance(self) -> None:
        if self._prompt_state.hints is None:
            return
        attempts = self._current_attempts_for_move()
        if attempts is None:
            return
        attempts.add_hint()

    def _commit_prompt_performance(self, *, gave_up: bool) -> None:
        records_by_move: dict[tuple[str, UCI], PerformanceRecord] = {}

        # update performance history
        for attempts in self._prompt_performance:
            history = self._performance_history(attempts.tested_fen, attempts.tested_move_uci)
            success = perf_success_from_attempt_history(attempts, history)
            if success is not None:
                key = (attempts.tested_fen, attempts.tested_move_uci)
                records_by_move[key] = PerformanceRecord(success, attempts.prompt_time)

        if gave_up:
            tested_move = self._current_performance_move()
            if tested_move is not None:
                parent, move_uci = tested_move
                records_by_move[(fen(parent), move_uci)] = PerformanceRecord(False, self._prompt_state.prompt_time)

        # update models
        for (position_fen, move_uci), new_record in records_by_move.items():
            history = self._performance_history(position_fen, move_uci)
            model = self._movemodel(position_fen, move_uci)
            model.update(new_record.success, history)
            history.append(new_record)

    def _prompt_hints_match_current_prompt(self, expected_uci: UCI) -> bool:
        if self._prompt_state.hints is None or self._prompt_state.node is None:
            return False

        return (
            fen(self._prompt_state.hints.board) == fen(self._current_prompt_position())
            and self._prompt_state.hints.starting_square == expected_uci[:2]
            and self._prompt_state.hints.target_square == expected_uci[2:4]
        )

    def _handle_file_guess(self, uci: UCI) -> MoveGrade:
        uci = uci_from_lichess_to_pgn(uci)
        prpt_data = self._prompt_state
    
        expected_uci = self.expected_uci()
        if not expected_uci:
            return MoveGrade(MoveCorrectness.UNDEF)

        if self._prompt_child_for_move(prpt_data.node, uci) is not None:
            return MoveGrade(MoveCorrectness.CORRECT)
        if not self._session.options.check_alternatives:
            chosen_alternative_node = self._tt_child_for_move(prpt_data.node, uci)
            if (chosen_alternative_node is not None and
                not move_is_marked_as_error(chosen_alternative_node)):
                self._pending_alternative_uci = uci
                return MoveGrade(
                    MoveCorrectness.ALTERNATIVE,
                    msg="       Not the main move. Explore this anyway?",
                )

        expected_moves = [expected_uci] # only this for now
        expected_sans = ", ".join(expected_moves)
        user_ev = self._session.q_eval_move(prpt_data.node, uci)
        move_eval, reply_move = user_ev.best_eval(), user_ev.best_move()
        evals = [self._evaluate_move(prpt_data.node, m) for m in expected_moves]
        best_expected_eval = max(evals) if evals else None

        msg = f"Wrong. Expected: {expected_sans}."
        if best_expected_eval is not None:
            msg += f" Your move eval {move_eval:+.2f} after {reply_move.uci()}. File move eval {best_expected_eval:+.2f}."

        eval_diff = None if best_expected_eval is None else move_eval - best_expected_eval
        rel_eval_diff = None
        if best_expected_eval not in (None, 0):
            rel_eval_diff = eval_diff / best_expected_eval

        return MoveGrade(
            MoveCorrectness.INCORRECT,
            msg=msg,
            eval_diff=eval_diff,
            rel_eval_diff=rel_eval_diff,
            opponent_reply=reply_move.uci()
        )

    def _handle_off_file_guess(self, uci: UCI) -> MoveGrade:
        # TODO: if we transposed, contunue along the file?
        uci = uci_from_lichess_to_pgn(uci)
        if not self._prompt_state.off_file:
            raise RuntimeError("Cannot handle an off-file guess while on file")

        off_file_fen = self._prompt_state.off_file_fen
        off_file_board = to_board(off_file_fen)
        ev = self._session.query(off_file_fen, "q-eval")
        expected_eval, best_reply = ev.best_eval(), ev.best_move()
        if best_reply is None:
            return MoveGrade(MoveCorrectness.UNDEF, msg="No engine reply for the off-file position.")

        best_reply_san = off_file_board.san(best_reply)
        user_ev = self._session.q_eval_move(off_file_board, uci)
        user_move_eval, reply_to_user = user_ev.best_eval(), user_ev.best_move()

        eval_gap = expected_eval - user_move_eval
        msg = f"Off-file position. Your move: eval {user_move_eval:+.2f} after {reply_to_user}."
        opponent_reply = reply_to_user.uci()
        if uci == best_reply.uci() or eval_gap <= 0.2 or user_move_eval > 0.8*expected_eval:
            msg += f" Best was {best_reply_san} with evaluation {expected_eval:+.2f}. Good job!"
            grade = MoveCorrectness.CORRECT
        else:
            msg += (
                f" Best was {best_reply_san} with evaluation {expected_eval:+.2f}. "
                "Try again."
            )
            if user_move_eval > 2: # okay let's not consider it a mistake if we are still winning
                grade = MoveCorrectness.ALTERNATIVE
            else:    
                grade = MoveCorrectness.INCORRECT
        msg = msg.strip()

        return MoveGrade(
            grade,
            msg=msg,
            eval_diff=user_move_eval - expected_eval,
            opponent_reply=opponent_reply
        )

    def accept_pending_alternative(self) -> PromptState:
        parent = self._prompt_state.node
        if parent is None or self._prompt_state.off_file:
            raise RuntimeError("Cannot accept an alternative without an active file prompt")

        pending_uci = self._pending_alternative_uci
        if pending_uci is None:
            raise RuntimeError("No alternative move is pending")

        chosen_node = self._tt_child_for_move(parent, pending_uci)
        if chosen_node is None:
            raise RuntimeError(f"Pending alternative {pending_uci!r} no longer resolves to a file move")

        self._record_accepted_alternative(parent, pending_uci)
        self._clear_pending_alternative()

        grade = MoveGrade(
            MoveCorrectness.CORRECT,
            msg=" Accepted alternative saved.",
        )
        self._save_grade(grade)
        return self._continue_with_file_child(chosen_node, grade)

    def _evaluate_move(self, position: Union[chess.Board, Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(position, move).best_eval()

    def _should_skip_current_prompt_position(self, skip_index: int) -> bool:
        prompt_node = self._prompt_state.node
        if prompt_node is None or self._prompt_state.off_file:
            return False
        if self._spec_id == "history":
            return False
        if self._current_expected_node() is None:
            return False
        uci = prompt_node.move.uci()
        if prompt_node.parent is None or prompt_node.move is None:
            return False
        if self._move_is_marked_to_skip(prompt_node.parent, uci):
            return True
        if not self._prompt_position_is_learned(prompt_node):
            return False
        if sum(perf.success for perf in 
               self._performance_history(prompt_node.parent, uci)[-3:]) < 3:
            return False

        skip_probability = self.LEARNED_MOVE_SKIP_PROBABILITY * (0.4 ** skip_index)
        return self._rng.random() < skip_probability

    def _advance_past_current_prompt_position(self) -> None:
        """
        Skip the current prompt by auto-playing its expected response, then
        advance along the normal line-selection path.
        """
        expected_node = self._current_expected_node()
        if expected_node is None:
            self.finish_prompt()
            return

        self._prompt_state.node = expected_node
        if self._prompt_state.node_index is not None:
            self._prompt_state.node_index += 1

        self._advance_line(try_skip=False)

    def _skip_learned_prompt_positions(self) -> None:
        """
        Repeatedly skip learned prompt positions by following the current
        expected reply and then advancing to the next prompt candidate.
        """
        skip_index = 0
        while self._should_skip_current_prompt_position(skip_index):
            skip_index += 1
            self._advance_past_current_prompt_position()
            if self._is_finished or self._prompt_state.off_file:
                return

    def _advance_line(self, try_skip: bool = True) -> None:
        """
        Assuming self._prompt_state.node is set for them to move,
        choose a move for them to continue along the line (or off-file) and
        update self._prompt_state accordingly.
        If the line cannot be continued, updates the state to "prompt finished".
        """
        if self._prompt_state.off_file:
            raise RuntimeError("Cannot advance the file line while on an off-file prompt")

        if self._prompt_state.node_index is not None:
            if self._prompt_state.node.turn() != self._session.options.side:
                selected_off_file, selection_debug = self._maybe_choose_off_book_prompt(self._prompt_state.node)
                if selected_off_file:
                    self._prompt_state.message = selection_debug
                    self._activate_prompt_state()
                    return

            try:
                child_index = self._prompt_state.node_index+1
                next_node = self._child_for_move(
                    self._prompt_state.node,
                    self._prompt_state.prechosen_path[child_index],
                )
                if next_node is None:
                    self._set_off_file_position(
                        self._prompt_state.node,
                        self._prompt_state.prechosen_path[child_index],
                    )
                else:
                    self._prompt_state.node_index += 1
                    self._prompt_state.node = next_node

                if self._prompt_state.off_file:
                    self._activate_prompt_state()
                    return
                
                if try_skip:
                    self._skip_learned_prompt_positions()

                if self._is_finished or self._prompt_state.off_file:
                    if self._prompt_state.off_file:
                        self._activate_prompt_state()
                    return

                self._activate_prompt_state()
                return
            except IndexError:
                self._prompt_state.node_index = None

        parent = self._prompt_state.node
        next_node, selection_debug = self._choose_move_randomly(parent, maybe_off_book=True)

        if next_node is False:
            self._prompt_state.message = selection_debug
            self.finish_prompt()
            return

        self._prompt_state.node = next_node
        self._prompt_state.message = selection_debug
        if self._prompt_state.off_file:
            self._activate_prompt_state()
            return
        if try_skip:
            self._skip_learned_prompt_positions()
        if self._is_finished or self._prompt_state.off_file:
            if self._prompt_state.off_file:
                self._activate_prompt_state()
            return

        message = f"Correct: {node_san(self._prompt_state.node)}. Continue along the line."
        if self._prompt_state.message:
            self._prompt_state.message = f"{message} {self._prompt_state.message}"
        else:
            self._prompt_state.message = message
        self._activate_prompt_state()


    def _choose_start_ply_offset(self) -> int:
        """Choose how many plies down the line the prompt should start, relative to self._root."""
        return self._rng.randint(0, 2*self.start_range)+1 # converted to plies
    
    def _choose_prompt_globally(self) -> bool:
        # Length from the anchor to the final opponent move, in plies.
        prpt_len = self._rng.randint(*self.prompt_len_interval)
        if prpt_len % 2 == 0:
            prpt_len -= 1

        prompt_quality_dict = self.make_prompt_dict(self._root, prpt_len + self.start_range * 2)
        choose_from : Dict[Tuple[Tuple[int, ...], int], float] = {}
        all_possible_paths = list(prompt_quality_dict.keys())
        # i is offset from root. So root ---i---> anchor ---prpt_len---> prompt end
        for i in range(0, self.start_range * 2, 2):
            for path in all_possible_paths:
                if i > len(path):
                    continue
                total_damage = prompt_quality_dict[path][1]
                start_damage_cutoff = prompt_quality_dict[path[:i-1]][1] if i > 0 else 0
                choose_from[(path, i)] = \
                total_damage - start_damage_cutoff
        possible_keys = [k for k in choose_from.keys() if len(k[0]) - k[1] <= prpt_len]
        # ^ the only way a short prompt can "win" is if it does not have a file continuation, which
        # is exactly the case where we could miss it if we did ""== prpt_len" and min prpt len is too high
        possible_keys.sort(key=lambda k: choose_from[k], reverse=True)
        candidate_amount = min(max(1, len(possible_keys) // 3), 10)
        preferred_prompt_keys = possible_keys[:candidate_amount]

        if not preferred_prompt_keys:
            return False

        chosen_path, offset = self._rng_choice(preferred_prompt_keys)

        anchor = self._root
        for j in range(offset+1):
            anchor = self._child_for_move(anchor, chosen_path[j])
            if anchor is None:
                raise RuntimeError(f"Missing file child for prompt path move {chosen_path[j]!r}")
        
        # make sure we are anchored at their move
        assert anchor.turn() == self._session.options.side

        self._prompt_state.prechosen_path = chosen_path
        self._prompt_state.node = anchor
        self._prompt_state.anchor_node = anchor
        self._prompt_state.node_index = offset
        msg = f"Complete prompt: {self._prompt_state.prechosen_path}, \
                expected damage {choose_from[(chosen_path, offset)]:.3f}" if DEBUG_MODE else ""
        self._prompt_state.message = msg

        self._skip_learned_prompt_positions()

        if self._is_finished:
            return False
        self._set_prompt_start()
        self._activate_prompt_state()
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
        Results in populating self._prompt_state.
        Returns False if the walk along a line failed."""
        node = node or self._root

        selection_debug = ""
        self._clear_off_file_state()
        line_length = self._choose_start_ply_offset()

        self._prompt_state.anchor_node = node

        # we'll do line_length or line_length-1 steps total
        for step in range(line_length - 2):
            next_node, _ = self._choose_move_randomly(node, maybe_off_book=False)
            if next_node is False:
                return False
            node = next_node
            # if step == self._session.options.start_ply:
            #     self._prompt_state.anchor_node = node # TODO


        if node.turn() == self._session.options.side:
            next_node, _ = self._choose_move_randomly(node, maybe_off_book=False)
            if next_node is False:
                return False
            node = next_node

        # Final step: land on the next file move for the opponent.
        next_node, selection_debug = self._choose_move_randomly(node, maybe_off_book=False)
        if next_node is False:
            return False
        
        self._prompt_state.node = next_node
        self._prompt_state.message = selection_debug

        self._skip_learned_prompt_positions()
        if self._is_finished:
            return False
        self._set_prompt_start()
        self._activate_prompt_state()
        return True

    def _move_probability(self, position: BoardLike, uci: UCI) -> float:
        """The probability of the opponent playing the move, according to the DB.
        A correction is to ensure a minimum probability: we trust that a move was
        added to a file for a reason."""
        return max(self._session.move_freq(position, uci), 0.05)

    def _expected_damage_of_move(self, position: BoardLike, uci: UCI):
        """How damaging it is not to know the reply to a move.
        Intended for determining learning priority.
        Currently equals (chance to get the move)*(chance to get it wrong).
        Ideally should also incorporate (damage from getting it wrong)."""
        if self._move_is_marked_to_skip(position, uci):
            return 0.00
        move_probability = self._move_probability(position, uci)
        return (1-self._move_predict_success(position, uci)) * move_probability

    def _choose_move_randomly(
        self,
        parent: Node,
        *,
        off_book: bool = False,
        maybe_off_book: bool = False,
        use_engine: bool = False,
    ) -> tuple[Node | bool, str]:
        """
        Chooses a move randomly to simulate a step along a line. 
        May set self._prompt_state.off_file_fen for off-file prompts.
        Returns the resulting node and a debug string.

        If a choice could not be made, returns (False, ...).
        """
        off_book = off_book or (maybe_off_book and self._rng.random() < self.non_file_move_freq)

        if parent.turn() == self._session.options.side:
            children = self._prompt_variations(parent)
            if not children:
                return False, ""
            if not self._session.options.check_alternatives and len(children) == 1:
                return children[0], ""    
            choice = self._rng_choice(children)
            message = ""
            if DEBUG_MODE:
                message = self._format_rng_weights({child.move.uci(): 1.0 for child in children})
            return choice, message

        move_ucis = [child.move.uci() for child in self._prompt_variations(parent)]

        if not move_ucis:
            # if there are no moves for them, we can try anyway
            off_book = True
            use_engine = True

        if off_book:
            # Try to find an off-book move with probability non_file_move_freq
            off_book_selection = self._select_off_book_move(parent, use_engine=use_engine)
            if off_book_selection.move is not None:
                self._set_off_file_position(parent, off_book_selection.move)
                return parent, off_book_selection.message
            if off_book_selection.blacklist_exhausted:
                return False, off_book_selection.message
            if use_engine:
                return False, off_book_selection.message
            # Fall through to normal logic

        if not move_ucis:
            return False, ""

        weights_dict = {uci: self._expected_damage_of_move(parent, uci) for uci in move_ucis}

        sys.stderr.write(f"Choosing move... selection_weights: {[(k, round(v, 3)) for k, v in weights_dict.items()]}\n")
        choice = self._rng_choice(weights_dict)

        message = ""
        if DEBUG_MODE:
            message = self._format_rng_weights(weights_dict)
        child = self._child_for_move(parent, choice)
        if child is None:
            raise RuntimeError(f"Chosen move {choice!r} does not resolve to a file child")
        return child, message

    def _select_off_book_move(
        self,
        position: BoardLike,
        *,
        use_engine: bool,
    ) -> OffBookSelection:        
        selection = self._choose_db_off_book_move(position)
        if selection.move is None:
            if use_engine:
                selection = self._choose_engine_off_book_move(position)
        if selection.move is not None:
            self._last_off_book_move = (selection.move.uci(), fen(position))
        return selection

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
            engine_lines = self._session.query(fen(position), "q-eval").top(i + 1)
            if not engine_lines:
                return OffBookSelection(None, "no engine move")

            for line in engine_lines:
                move = line.move
                if not self._off_book_move_is_fine(position, move.uci()):
                    continue

                debug_msg = f"engine-suggested off-book move {move}"
                return OffBookSelection(move, debug_msg)

        return OffBookSelection(
            None,
            "No non-blacklisted engine off-book moves are available.",
            blacklist_exhausted=True,
        )

    def _off_book_db_candidates(self, position: BoardLike) -> list[tuple[chess.Move, float]]:
        """Find off-book non-BL DB moves with frequency >= 5%, score_rate <= 75%, and no obvious replies."""
        db_move_counts = self._get_db_moves_and_nums(position)
        if not db_move_counts:
            return []

        exclude = set(n.move.uci() for n in self._prompt_variations(position))
        wont_work = lambda uci: (uci in exclude or not self._off_book_move_is_fine(position, uci))

        # Filter candidates: frequency >= 5%, score_rate <= 75%
        candidates: list[tuple[chess.Move, float]] = []
        for uci, count in db_move_counts.items():
            position_board = to_board(position)

            if wont_work(uci):
                continue

            if self._session.move_freq(position_board, uci) < 0.05:
                continue

            score_rate = self._session.score_rate_move(position_board, uci)
            # don't prompt with stupid moves
            if score_rate > 0.75:
                continue

            # don't prompt if the reply is obvious
            position_board.push_uci(uci)
            stats = self._session.query(fen(position_board), "db_lichess")
            match stats["moves"]:
                case [popular, *_] if self._session.move_freq(position_board, popular["uci"]) > 0.9:
                    continue

            candidates.append((chess.Move.from_uci(uci), count))

        return candidates

    def _off_book_move_is_fine(self, position: BoardLike, move_uci: UCI) -> bool:
        """Initial screening for an off-book move."""
        if move_uci in self._blacklist_for(position):
            return False
        if (move_uci, fen(position)) == getattr(self, "_last_off_book_move", None):
            return False
        return True
        
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
        if self._spec_id != "new" or self._prompt_state.prechosen_path is None:
            return None
        return tuple(self._prompt_state.prechosen_path)

    def _debug_move_selection_metrics(
        self,
        parent: Node,
        move_uci: UCI,
    ) -> tuple[Optional[float], Optional[float], float]:
        if parent.turn() == self._session.options.side:
            return None, None, 1.0

        move_freq = self._move_probability(parent, move_uci)
        recall_probability = self._move_predict_success(parent, move_uci)
        return move_freq, recall_probability, move_freq * (1.0 - recall_probability)

    def _sorted_debug_moves_for(
        self,
        parent: Node,
    ) -> list[tuple[UCI, Optional[float], Optional[float], MemoryModel | None, str, Optional[Node]]]:
        entries: list[tuple[UCI, Optional[float], Optional[float], float, MemoryModel | None, str, Optional[Node]]] = []
        for child_node in self._prompt_variations(parent):
            move_uci = child_node.move.uci()
            move_freq, recall_probability, selection_priority = self._debug_move_selection_metrics(parent, move_uci)
            try:
                san = node_san(parent, move_uci)
            except Exception:
                san = move_uci
            try:
                child = self._child_for_move(parent, move_uci)
            except RuntimeError:
                child = None
            entries.append((
                move_uci,
                move_freq,
                recall_probability,
                selection_priority,
                self._existing_movemodel(parent, move_uci),
                san,
                child,
            ))
        entries.sort(key=lambda item: (-item[3], item[5], item[0]))
        return [
            (move_uci, move_freq, recall_probability, move_model, san, child)
            for move_uci, move_freq, recall_probability, _priority, move_model, san, child in entries
        ]

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
            move_freq, recall_probability, _selection_priority = self._debug_move_selection_metrics(parent, move_uci)
            move_model = self._existing_movemodel(parent, move_uci)
            try:
                child = self._child_for_move(parent, move_uci)
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
                    move_freq,
                    recall_probability,
                    move_model,
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
        for move_uci, move_freq, recall_probability, move_model, san, child in self._sorted_debug_moves_for(parent):
            children.append(
                self._build_debug_move_node(
                    parent,
                    move_uci,
                    move_freq,
                    recall_probability,
                    move_model,
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
        move_freq: Optional[float],
        recall_probability: Optional[float],
        move_model: Optional[MemoryModel],
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
        performance = None
        if move_model is not None:
            history = self._existing_performance_history(parent, move_uci) or []
            if history:
                performance = move_model.debug_payload()
                performance["predictSuccess"] = move_model.predict_success(history)
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
            "moveFreq": move_freq,
            "recallProbability": recall_probability,
            "showPriorityLabels": parent.turn() != self._session.options.side,
            "performance": performance,
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
            "title": "Selection / performance visualizer",
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
        anchor_path_list = self._relative_move_path_for_node(self._prompt_state.anchor_node) if self._prompt_state.anchor_node is not None else None
        current_path_list = self._relative_move_path_for_node(self._prompt_state.node) if self._prompt_state.node is not None else None
        prompt_path = self._debug_prompt_path()
        if anchor_path_list is None or current_path_list is None:
            root = self._prompt_state.anchor_node or self._prompt_state.node or self._root
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
            prefix_path=tuple(current_path_list),
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
            anchor_path_list = self._relative_move_path_for_node(self._prompt_state.anchor_node) if self._prompt_state.anchor_node is not None else None
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
    node: Node | None
    message: str
    anchor_node: Node | None
    off_file_fen: str | None = None
    prechosen_path: tuple[str, ...] | None = None
    node_index: int | None = None # None means we are not following the prechosen path
    prompt_time: float | None = None
    hints: Hints | None = None
    feedback: list[Arrow] = field(default_factory=list)

    @property
    def off_file(self) -> bool:
        return self.off_file_fen is not None

    def __bool__(self):
        return self.node is not None


@dataclass(frozen=True)
class PromptLineId:
    # TODO: keep the moves starting only from start_fen
    start_fen: str
    moves: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "startFen": self.start_fen,
            "moves": list(self.moves),
        }

    @classmethod
    def from_json(cls, payload: Any) -> "PromptLineId":
        if not isinstance(payload, dict):
            raise TypeError("Prompt line id payload must be a dict")

        start_fen = payload["startFen"]
        moves = payload["moves"]
        if not isinstance(start_fen, str):
            raise TypeError("Prompt line id startFen must be a string")
        if not isinstance(moves, list) or not all(isinstance(move, str) for move in moves):
            raise TypeError("Prompt line id moves must be a list of strings")

        return cls(start_fen, tuple(moves))

    def prompt_len(self) -> int:
        board = chess.Board()
    
        for i, san in enumerate(self.moves):
            if board.fen().split(' ')[0] == self.start_fen.split(' ')[0]:
                return len(self.moves) - i
            board.push_san(san)
        
        # Check after the last move
        if board.fen().split(' ')[0] == self.start_fen.split(' ')[0]:
            return 0
        
        return -1


@dataclass
class PromptLogEntry:
    spec_id: SpecId
    prompt_id: PromptLineId
    prompt_time: float
    performance: Optional[float] = None
    bookmarked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "specId": self.spec_id,
            "promptId": self.prompt_id.to_json(),
            "promptTime": self.prompt_time,
            "performance": self.performance,
            "bookmarked": self.bookmarked,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "PromptLogEntry":
        raw_bookmarked = payload.get("bookmarked", False)
        if not isinstance(raw_bookmarked, bool):
            raise TypeError("Prompt log entry bookmarked must be a boolean")
        return cls(
            spec_id=payload["specId"],
            prompt_id=PromptLineId.from_json(payload["promptId"]),
            prompt_time=float(payload["promptTime"]),
            performance=payload.get("performance"),
            bookmarked=raw_bookmarked,
        )
    
    def prompt_len(self) -> int:
        return self.prompt_id.prompt_len()

class Hints():
    """
    Manages the state of hints (circles) for the current prompt.

    The logic basically is: for a pawn move, the first hint is all pawns on the board and the second hint is the pawn to move.
    For other moves, the first hint is the piece to move and the second hint is the target square.

    We stop providing additional hints when current hints already determine a single move.
    """
    def __init__(self, board: BoardLike, uci: str):
        self.board = to_board(board)
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

    def _require_prompt_line_id(self, prompt_id: PromptId) -> PromptLineId:
        if not isinstance(prompt_id, PromptLineId):
            raise TypeError(f"Expected PromptLineId, got {type(prompt_id).__name__}")
        return prompt_id

    def is_bookmarked(self, prompt_id: PromptId) -> bool:
        line_id = self._require_prompt_line_id(prompt_id)
        return any(entry.bookmarked for entry in self.entries if entry.prompt_id == line_id)

    def record_prompt(self, prompt_id: PromptId, spec_id: SpecId) -> None:
        line_id = self._require_prompt_line_id(prompt_id)
        self.entries.append(
            PromptLogEntry(
                spec_id=spec_id,
                prompt_id=line_id,
                prompt_time=time.time(),
                bookmarked=self.is_bookmarked(line_id),
            )
        )

    def __len__(self) -> int:
        return len(self.entries)
    
    def average_prompt_len(self) -> float:
        if not self.entries:
            return 0.0
        return sum(entry.prompt_len() for entry in self.entries) / len(self.entries)

    @property
    def _last_entry(self) -> PromptLogEntry:
        return self.entries[-1]

    def record_feedback(self, prompt_id: PromptId, feedback: Feedback) -> None:
        self._last_entry.performance = float(feedback.quality)
        self.serialize()

    def set_bookmarked(self, prompt_id: PromptId, bookmarked: bool) -> bool:
        line_id = self._require_prompt_line_id(prompt_id)
        matching_entries = [
            entry
            for entry in self.entries
            if entry.prompt_id == line_id
        ]
        if not matching_entries:
            raise ValueError("Cannot bookmark a prompt that is not in history")

        old_bookmarks = [
            (entry, entry.bookmarked)
            for entry in matching_entries
        ]
        changed = False
        for entry in matching_entries:
            if entry.bookmarked == bookmarked:
                continue
            entry.bookmarked = bookmarked
            changed = True

        if changed:
            try:
                self.serialize()
            except Exception:
                for entry, old_bookmarked in old_bookmarks:
                    entry.bookmarked = old_bookmarked
                raise
        return changed

    def bookmark_latest_prompt(self) -> bool:
        if not self.entries:
            raise RuntimeError("Cannot bookmark latest prompt without history entries")
        return self.set_bookmarked(self._last_entry.prompt_id, True)

    def bookmarked_prompt_ids(self) -> list[PromptLineId]:
        prompt_ids: list[PromptLineId] = []
        seen: set[PromptLineId] = set()
        for entry in reversed(self.entries):
            if not entry.bookmarked or entry.prompt_id in seen:
                continue
            prompt_ids.append(entry.prompt_id)
            seen.add(entry.prompt_id)
        return prompt_ids

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
        self._bookmark_current_prompt_pending = False

        self._review_payload: Optional[dict[str, Any]] = None
        self._review_path: Optional[list[int]] = None
        self._review_base_root_path: list[int] = []
        self._review_view_root_path: list[int] = []
        self._review_show_alternatives = False
        self._search_move_payload: Optional[dict[str, Any]] = None
        self._review_db_stats_lock = threading.Lock()
        self._review_db_stats_pending: Optional[ReviewDbStatsTask] = None
        self._review_db_stats_inflight = False
        self._review_db_stats_request_id = 0

    def _review_children_for(self, show_alternatives: bool, node: Node) -> list[Node]:
        if show_alternatives:
            return list(node.variations)

        return mainline_children((self._session.options.side,))(node)

    def _review_children(self, node: Node) -> list[Node]:
        return self._review_children_for(self._review_show_alternatives, node)

    def _review_node_at_path(
        self,
        path: list[int],
        *,
        show_alternatives: Optional[bool] = None,
    ) -> Node:
        if show_alternatives is None:
            get_children = self._review_children
        else:
            get_children = lambda node: self._review_children_for(show_alternatives, node)
        return node_at_path(self._session.game, path, get_children)

    def _review_path_from_root(self, node: Node) -> list[int]:
        return path_from_root(self._session.game, node, self._review_children)

    def _visible_review_node(self, node: Node) -> Node:
        cur = node
        while True:
            try:
                self._review_path_from_root(cur)
                return cur
            except ValueError:
                parent = getattr(cur, "parent", None)
                if parent is None:
                    return self._session.game
                cur = parent

    def _review_base_path(self) -> list[int]:
        try:
            return self._review_path_from_root(self._session.starting_node)
        except ValueError:
            return []


    def _pos_drill_cache_name(self) -> str:
        return default_repertoire_cache_path(base=os.path.join("cache", "sr_pos_drill"), options=self._session.options)

    def _model_cache_name(self) -> str:
        return default_repertoire_cache_path(base=os.path.join("cache", "sr_models"), options=self._session.options)

    def _log_cache_name(self) -> str:
        return default_repertoire_cache_path(base=os.path.join("cache", "log"), options=self._session.options)

    def _history_child_for_san(
        self,
        node: Node,
        san: str,
        get_children: Callable[[Node], list[Node]],
    ) -> Optional[tuple[int, Node]]:
        for variation_index, child in enumerate(get_children(node)):
            if node_san(child) == san:
                return variation_index, child
        return None

    def _history_moves(
        self,
        prompt_id: PromptLineId,
        get_children: Callable[[Node], list[Node]],
    ) -> list[dict[str, Any]]:
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
                child = self._history_child_for_san(node, san, get_children)
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

    def _history_moves_payload(
        self,
        prompt_id: PromptLineId,
        get_children: Callable[[Node], list[Node]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "san": move["san"],
                "moveNumber": move["moveNumber"],
                "color": move["color"],
                "path": move["path"],
                "fen": None if move["path"] is not None else move["fen"],
            }
            for move in self._history_moves(prompt_id, get_children)
        ]

    def _history_move_payload_from_board(
        self,
        board: chess.Board,
        move_uci: UCI,
        path: Optional[list[int]],
    ) -> dict[str, Any]:
        move = chess.Move.from_uci(move_uci)
        if move not in board.legal_moves:
            raise ValueError(f"Move {move_uci!r} is not legal from {board.fen()!r}")

        move_number = board.fullmove_number
        color = "white" if board.turn == chess.WHITE else "black"
        san = board.san(move)
        result_board = board.copy(stack=False)
        result_board.push(move)
        return {
            "san": san,
            "moveNumber": move_number,
            "color": color,
            "path": path,
            "fen": None if path is not None else result_board.fen(),
        }

    def _history_move_payload_from_node(self, node: Node, path: list[int]) -> dict[str, Any]:
        if node.parent is None or node.move is None:
            raise ValueError("Cannot build a history move payload for a root node")
        return self._history_move_payload_from_board(node.parent.board(), node.move.uci(), path)

    def _fallback_history_move_payload(self, position_fen: str, move_uci: UCI) -> dict[str, Any]:
        return self._history_move_payload_from_board(to_board(position_fen), move_uci, None)

    def _hardest_move_targets_payload(
        self,
        hardest_move: HardestMoveData,
        get_children: Callable[[Node], list[Node]],
    ) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        seen_paths: set[tuple[int, ...]] = set()

        for node in self._rep_engine.file_nodes_for_position_move(
            hardest_move["fen"],
            hardest_move["uci"],
        ):
            if node.ply() > self._session.options.end_ply:
                continue

            try:
                path = path_from_root(self._session.game, node, get_children)
            except ValueError:
                continue

            path_key = tuple(path)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            targets.append(self._history_move_payload_from_node(node, path))

        if not targets:
            targets.append(self._fallback_history_move_payload(hardest_move["fen"], hardest_move["uci"]))
        return targets

    def _hardest_move_entry_payload(
        self,
        hardest_move: HardestMoveData,
        get_children: Callable[[Node], list[Node]],
    ) -> dict[str, Any]:
        return {
            "fen": hardest_move["fen"],
            "uci": hardest_move["uci"],
            "wrongCount": hardest_move["wrong_count"],
            "attemptCount": hardest_move["attempt_count"],
            "lastAttemptTime": hardest_move["last_attempt_time"],
            "moves": self._hardest_move_targets_payload(hardest_move, get_children),
        }

    def _history_entry_payload(
        self,
        entry: PromptLogEntry,
        get_children: Callable[[Node], list[Node]],
    ) -> dict[str, Any]:
        return {
            "specId": entry.spec_id,
            "promptId": entry.prompt_id.to_json(),
            "promptTime": entry.prompt_time,
            "performance": entry.performance,
            "bookmarked": entry.bookmarked,
            "moves": self._history_moves_payload(entry.prompt_id, get_children),
        }

    def _bookmark_entry_payload(
        self,
        prompt_id: PromptLineId,
        get_children: Callable[[Node], list[Node]],
    ) -> dict[str, Any]:
        return {
            "promptId": prompt_id.to_json(),
            "bookmarked": True,
            "moves": self._history_moves_payload(prompt_id, get_children),
        }

    def history_payload(self) -> dict[str, Any]:
        get_children = self._review_children if self._mode == "review" else self._session.variations
        entries = [
            self._history_entry_payload(entry, get_children)
            for entry in reversed(self._log.entries)
        ]
        return {
            "count": len(entries),
            "entries": entries,
        }

    def bookmarks_payload(self) -> dict[str, Any]:
        get_children = self._review_children if self._mode == "review" else self._session.variations
        entries = [
            self._bookmark_entry_payload(prompt_id, get_children)
            for prompt_id in self._log.bookmarked_prompt_ids()
        ]
        return {
            "count": len(entries),
            "entries": entries,
        }

    def hardest_moves_payload(self) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Hardest moves are only available after spaced repetition starts")

        get_children = self._review_children if self._mode == "review" else self._session.variations
        entries = [
            self._hardest_move_entry_payload(hardest_move, get_children)
            for hardest_move in self._rep_engine.hardest_moves()
        ]
        return {
            "count": len(entries),
            "entries": entries,
        }

    def progress_payload(self) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Progress is only available after spaced repetition starts")
        return self._rep_engine.progress_payload()

    def _debug_tree_payload(self) -> Optional[dict[str, Any]]:
        if not DEBUG_MODE or not self.active:
            return None

        try:
            if self._mode == "guess":
                return self._rep_engine.debug_guess_tree_payload()
            if self._mode == "review":
                if self._review_path is None:
                    raise RuntimeError("Review mode requires an active review path")
                node = self._review_node_at_path(list(self._review_path))
                return self._rep_engine.debug_review_tree_payload(node)
        except Exception as exc:
            return {
                "title": "Selection visualizer",
                "error": str(exc),
            }
        return None

    def _build_review_tree_payload(self) -> dict[str, Any]:
        if not self.active or self._mode != "review":
            raise RuntimeError("Review tree payload requires active review mode")
        return build_variation_tree(
            self._review_children,
            self._session.game,
            end_ply=self._session.options.end_ply,
            is_move_bookmarked=self._rep_engine.move_is_bookmarked,
        )

    def _refresh_review_tree_payload(self) -> None:
        if self._mode != "review" or self._review_payload is None:
            return
        self._review_payload["tree"] = self._build_review_tree_payload()

    def ui_state(self) -> dict[str, Any]:
        pending_alternative = None
        if self.active and self._mode == "guess":
            pending_uci = self._rep_engine.pending_alternative_uci()
            if pending_uci is not None:
                pending_alternative = {"uci": pending_uci}

        state = {
            "active": self.active,
            "mode": self._mode,
            "currentSpecId": self._rep_engine.current_spec_id() if self.active else None,
            "bookmarkQueued": self._bookmark_current_prompt_pending if self.active and self._mode == "guess" else False,
            "pendingAlternative": pending_alternative,
            "review": self._review_payload if self.active and self._mode == "review" else None,
            "reviewShowsAlternatives": self._review_show_alternatives,
            "searchMove": self._search_move_payload if self.active and self._mode == "review" else None,
            "debugTree": self._debug_tree_payload(),
        }
        if hasattr(self, "_cfg"):
            state["startRange"] = self._cfg.start_range
        return state

    def start(self, options: SpacedRepetitionOptions, session: Optional[RepertoireSession] = None) -> None:
        try:
            self._cfg = options
            self._review_show_alternatives = bool(options.check_alternatives)
            self._session = session or RepertoireSession(
                options,
                default_cache_path=lambda: default_repertoire_cache_path(options),
            )
            self._session.starting_node = self._session.starting_node or self._session.game
            self.board_orientation = "white" if options.play_white else "black"
            
            self._log.load_from_file(self._log_cache_name())
            self._bookmark_current_prompt_pending = False
            self._session.cache.autosave_interval = 600
            self._rep_engine: RepetitionEngine = RepetitionEngine(
                self._session,
                self._session.starting_node,
                self._cfg.start_range,
                self._pos_drill_cache_name(),
                self._model_cache_name(),
                self._cfg.non_file_move_frequency,
                self._cfg.local_generation,
                self._prompt_log_len_range()
            )
            self._rep_controller = RepetitionController(
                NaiveScheduler(self._log),
                self._rep_engine,
                self._log,
            )

            if options.preload_db:
                self._prefetch_db_stats()

            self.start_next_prompt()
        finally: # TODO: make sure to find the appropriate moment to save the cache
            try:
                self._session.save_cache()
            except Exception as exc:
                sys.stderr.write(f"Failed to save cache: {exc}\n")
            if self._session is not None: 
                self._session.close()

    def _prompt_log_len_range(self) -> tuple[int, int]:
        """Interval for the global prompt lenths, in plies, for the Engine."""
        # TODO: I don't think this belongs here, but Engine currently does not
        # have access to Log. Need to fix this once a proper redesign path becomes clear.
        if len(self._log) < 20: # small history -- can't conclude
            return (self._cfg.min_prompt_len, self._cfg.max_prompt_len)
        avg = int(self._log.average_prompt_len())
        avg = avg if avg % 2 == 0 else avg + 1 # round to even
        return (avg - 2, avg + 2)


    def _commit_pending_bookmark_to_latest_prompt(self) -> None:
        if not self._bookmark_current_prompt_pending:
            return

        try:
            self._log.bookmark_latest_prompt()
        except Exception as exc:
            if DEBUG_MODE:
                raise
            sys.stderr.write(f"Failed to save bookmark: {exc}\n")
            self._hub.broadcast({"type": "error", "message": f"Failed to save bookmark: {exc}"})
        self._bookmark_current_prompt_pending = False

    def bookmark_current_prompt(self) -> None:
        if not self.active or self._mode != "guess":
            raise RuntimeError("Can only bookmark the active prompt in guess mode")
        self._bookmark_current_prompt_pending = True
        self._broadcast_ui_state()

    def set_prompt_bookmark(self, prompt_id: PromptLineId, bookmarked: bool) -> None:
        self._log.set_bookmarked(prompt_id, bookmarked)


    def start_next_prompt(self) -> None:
        self.active = True
        self._mode = "guess"
        self._bookmark_current_prompt_pending = False
        self._rep_controller.start_next_prompt()
        self.show_prompt()

    def show_prompt(
        self,
        prompt: PromptState | None = None,
        **kwargs,
    ) -> None:
        if prompt is None:
            prompt = self._rep_controller.get_prompt_view()

        self._mode = "guess"
        if prompt.off_file:
            self._hub.set_fen(
                prompt.off_file_fen,
                orientation=self.board_orientation,
                message=prompt.message,
                allow_moves=True,
                arrows=prompt.feedback,
                **kwargs
            )
            self._broadcast_ui_state()
            return

        if prompt.node is None:
            raise RuntimeError("Cannot show prompt without a file node or off-file FEN")

        self._hub.set_from_node(
            prompt.node,
            orientation=self.board_orientation,
            message=prompt.message,
            allow_moves=True,
            arrows=prompt.feedback,
            **kwargs
        )
        self._broadcast_ui_state()

    def stop(self) -> None:
        self.active = False
        self._mode = "idle"
        self._games = []
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


    def _safe_node_san(self, node: Optional[Node] = None) -> str:
        if node is None:
            return ""
        if DEBUG_MODE:
            return node_san(node)
        try:
            return node_san(node)
        except Exception:
            move = getattr(node, "move", None)
            return move.uci() if move is not None else ""

    def _first_review_child(self, node: Node) -> Optional[Node]:
        children = [
            child
            for child in self._review_children(node)
            if child.ply() <= self._session.options.end_ply
        ]
        return children[0] if children else None

    def _review_move_result(
        self,
        node: Node,
        *,
        target_node: Optional[Node] = None,
        query_prev_uci: Optional[UCI] = None,
        query_next_uci: Optional[UCI] = None,
    ) -> dict[str, Any]:
        path = self._review_path_from_root(node)
        parent = node.parent
        prev_uci = parent.move.uci() if parent is not None and parent.move is not None else None

        next_node = self._first_review_child(node)
        next_uci = next_node.move.uci() if next_node is not None and next_node.move is not None else None

        result: dict[str, Any] = {
            "path": list(path),
            "prev": self._safe_node_san(parent),
            "move": self._safe_node_san(node),
            "next": self._safe_node_san(next_node),
        }
        if parent is not None and node.move is not None:
            move_uci = node.move.uci()
            result["fen"] = fen(parent)
            result["uci"] = move_uci
            result["bookmarked"] = self._rep_engine.move_is_bookmarked(parent, move_uci)
        if query_prev_uci is not None:
            result["matchPrev"] = bool(prev_uci and prev_uci == query_prev_uci)
        if query_next_uci is not None:
            result["matchNext"] = bool(next_uci and next_uci == query_next_uci)
        if target_node is not None:
            similarity = compare_positions(target_node.board(), node)
            result["distance"] = round(similarity.distance, 4)
            result["similarity"] = round(similarity.similarity, 4)
        return result

    def _set_search_move_payload(
        self,
        *,
        target_node: Node,
        matches_move: Callable[[Node], bool],
        query: dict[str, Any],
        query_prev_uci: Optional[UCI] = None,
        query_next_uci: Optional[UCI] = None,
        empty_message: Optional[str] = None,
    ) -> None:
        tp = TraversalPolicy(
            start_ply=1,
            end_ply=self._session.options.end_ply,
            get_children=self._review_children,
        )
        results: list[dict[str, Any]] = []
        for node in iter_nodes(self._session.game, tp):
            if not matches_move(node):
                continue

            results.append(
                self._review_move_result(
                    node,
                    target_node=target_node,
                    query_prev_uci=query_prev_uci,
                    query_next_uci=query_next_uci,
                )
            )

        results.sort(
            key=lambda item: (-item["similarity"], item["distance"], item["path"])
        )

        self._search_move_payload = {
            "kind": "searchMove",
            "query": query,
            "results": results,
            "count": len(results),
        }
        if empty_message is not None:
            self._search_move_payload["emptyMessage"] = empty_message
        self._broadcast_ui_state()

    def search_nodes_by_move_notation(self, notation: str) -> None:
        """
        Search visible review nodes by algebraic-style move notation.
        Similarity is still measured against the current review position.
        """
        if self._mode != "review":
            return

        review_path = getattr(self, "_review_path", None)
        if review_path is None:
            raise RuntimeError("Cannot search for a review move without an active review path")

        stripped_notation = notation.strip()
        query_identity = parse_move_search_notation(stripped_notation)
        target_node = self._review_node_at_path(list(review_path))
        self._set_search_move_payload(
            target_node=target_node,
            matches_move=lambda node: move_identity(node) == query_identity,
            query={"move": stripped_notation, "notation": stripped_notation},
            empty_message=f"No occurrences found for {stripped_notation}.",
        )

    def search_nodes_by_move(self) -> None:
        """
        Search for nodes with the same move as in the current review position
        and display the results.
        Results are displayed with the previous and the following moves, emphasized if
        the same as those in the current review node.
        Each result also carries similarity data relative to the current position.
        """
        if self._mode != "review":
            self.finish_prompt()

        review_path = getattr(self, "_review_path", None)
        if review_path is None:
            raise RuntimeError("Cannot search for a review move without an active review path")

        query_node = self._review_node_at_path(list(review_path))

        query_move = query_node.move
        if query_move is None:
            return
        query_move_uci = query_move.uci()

        query_parent = query_node.parent
        query_prev_uci = query_parent.move.uci() if query_parent is not None and query_parent.move is not None else None
        query_next = self._first_review_child(query_node)
        query_next_uci = query_next.move.uci() if query_next is not None and query_next.move is not None else None

        query = self._review_move_result(query_node, target_node=query_node)
        query["uci"] = query_move_uci
        self._set_search_move_payload(
            target_node=query_node,
            matches_move=lambda node: moves_are_equal(node, query_node),
            query=query,
            query_prev_uci=query_prev_uci,
            query_next_uci=query_next_uci,
        )

    def _marked_move_results(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[tuple[MarkKind, str, UCI, tuple[int, ...]]] = set()

        for marked in self._rep_engine.marked_moves():
            for node in self._rep_engine.file_nodes_for_marked_move(marked["fen"], marked["uci"]):
                if node.ply() > self._session.options.end_ply:
                    continue
                try:
                    result = self._review_move_result(node)
                except ValueError:
                    continue

                path = result.get("path")
                if not isinstance(path, list) or not all(isinstance(i, int) for i in path):
                    raise TypeError("Marked move result path must be a list of integers")

                key = (marked["mark"], marked["fen"], marked["uci"], tuple(path))
                if key in seen:
                    continue
                seen.add(key)

                result.update(marked)
                results.append(result)

        mark_order: dict[MarkKind, int] = {"blacklist": 0, "skip": 1, "bookmark": 2}
        results.sort(key=lambda item: (mark_order[item["mark"]], item["path"]))
        return results

    def _marked_moves_payload(self, *, bookmarks_only: bool = False) -> dict[str, Any]:
        results = self._marked_move_results()
        if bookmarks_only:
            results = [result for result in results if result.get("mark") == "bookmark"]
            title = "Bookmarked moves"
            empty_message = "No bookmarked moves in the current repertoire."
        else:
            title = "Marked moves"
            empty_message = "No marked moves in the current repertoire."

        return {
            "kind": "markedMoves",
            "query": {
                "title": title,
                "bookmarksOnly": bookmarks_only,
            },
            "results": results,
            "count": len(results),
            "emptyMessage": empty_message,
        }

    def show_marked_moves(self) -> None:
        if self._mode != "review":
            return

        self._search_move_payload = self._marked_moves_payload()
        self._broadcast_ui_state()

    def show_bookmarked_moves(self) -> None:
        if self._mode != "review":
            return

        self._search_move_payload = self._marked_moves_payload(bookmarks_only=True)
        self._broadcast_ui_state()

    def _refresh_after_move_mark_change(self) -> None:
        if self._mode != "review":
            return

        self._refresh_review_tree_payload()
        if self._search_move_payload and self._search_move_payload.get("kind") == "markedMoves":
            query = self._search_move_payload.get("query", {})
            bookmarks_only = isinstance(query, dict) and query.get("bookmarksOnly") is True
            self._search_move_payload = self._marked_moves_payload(bookmarks_only=bookmarks_only)
        self._broadcast_ui_state()

    def unmark_move(self, mark: str, position_fen: str, move_uci: UCI) -> None:
        mark_kind: MarkKind
        if mark == "blacklist":
            mark_kind = "blacklist"
        elif mark == "skip":
            mark_kind = "skip"
        elif mark == "bookmark":
            mark_kind = "bookmark"
        else:
            raise ValueError(f"Unsupported move mark: {mark!r}")

        self._rep_engine.unmark_move(mark_kind, position_fen, move_uci)
        self._refresh_after_move_mark_change()

    def bookmark_move(self, position_fen: str, move_uci: UCI) -> None:
        self._rep_engine.bookmark_move(position_fen, move_uci)
        self._refresh_after_move_mark_change()

    def unbookmark_move(self, position_fen: str, move_uci: UCI) -> None:
        self._rep_engine.unbookmark_move(position_fen, move_uci)
        self._refresh_after_move_mark_change()

    def provide_hint(self) -> None:
        if self._mode != "guess":
            return

        self.show_prompt(circles=self._rep_engine.get_hint_circles())

    def _finalize_finished_prompt(self) -> None:
        self._rep_controller.finalize_current_prompt()
        self._commit_pending_bookmark_to_latest_prompt()

    def handle_guess(self, uci: str) -> None:
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")
        
        continue_prompt = self._rep_controller.on_user_response(uci)
        if not continue_prompt:
            self._commit_pending_bookmark_to_latest_prompt()
            prompt = self._rep_controller.get_prompt_view()
            if prompt.node is None:
                raise RuntimeError("Cannot enter review without a file node")
            self._enter_review_mode(node=prompt.node, message=prompt.message)
        else:
            self.show_prompt()

    def accept_pending_alternative(self) -> None:
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")

        continue_prompt = self._rep_controller.accept_pending_alternative()
        if not continue_prompt:
            self._commit_pending_bookmark_to_latest_prompt()
            prompt = self._rep_controller.get_prompt_view()
            if prompt.node is None:
                raise RuntimeError("Cannot enter review without a file node")
            self._enter_review_mode(node=prompt.node, message=prompt.message)
            return

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

    def finish_prompt_and_start_new(self) -> None:
        if self._mode != "guess":
            return

        self._rep_engine.finish_prompt()
        self._finalize_finished_prompt()
        self.start_next_prompt()

    def blacklist_current_move(self) -> None:
        if self._mode != "guess":
            return

        self._rep_engine.blacklist_current_move()
        if self._rep_engine.is_finished():
            self._finalize_finished_prompt()
            prompt = self._rep_controller.get_prompt_view()
            if prompt.node is None:
                raise RuntimeError("Cannot enter review without a file node")
            self._enter_review_mode(node=prompt.node, message=prompt.message)
            return

        self.show_prompt()

    def blacklist_current_line(self) -> None:
        if self._mode != "guess":
            return

        self._rep_engine.blacklist_current_line()
        self._rep_engine.finish_prompt()
        self._finalize_finished_prompt()
        self._reveal_prompt_in_review()

    def skip_current_move(self) -> None:
        """Skips the current move user has to enter (main line) now and from now on.

        The idea is save the user from having to enter the moves that are too obvious
        or file-specific (i.e. file has only one of many acceptable move orders).
        
        Off-file prompts can only be skipped when a reply transposes back into the file."""
        if self._mode != "guess":
            return

        was_off_file = self._rep_controller.get_prompt_view().off_file
        self._rep_engine.skip_current_move()
        if self._rep_engine.is_finished():
            self._finalize_finished_prompt()
            if was_off_file:
                prompt = self._rep_controller.get_prompt_view()
                if prompt.node is None:
                    raise RuntimeError("Cannot enter review without a file node")
                self._enter_review_mode(node=prompt.node, message=prompt.message)
                return
            self.start_next_prompt()
            return
        self.show_prompt()

    def _reveal_prompt_in_review(self) -> None:
        if self._mode != "guess":
            return

        prompt = self._rep_controller.get_prompt_view()
        if prompt.node is None:
            raise RuntimeError("Cannot reveal a prompt without a file node")

        expected_uci = self._rep_engine.expected_uci()
        if expected_uci:
            message = f"Expected: {expected_uci}. Browse the tree or click New."
        else:
            message = "No expected moves here. Browse the tree or click New."

        self._enter_review_mode(node=prompt.node, message=message)

    def goto_review_path(self, path: list[int]) -> None:
        if self._mode != "review":
            raise RuntimeError("Browsing is only available in review mode")
        if self._review_payload is None:
            raise RuntimeError("Review payload is not initialized")

        node = self._review_node_at_path(path)

        self._review_path = list(path)
        self._review_payload["currentPath"] = list(path)
        self._review_view_root_path = self._review_view_root_path_for(self._review_path)
        self._review_payload["viewRootPath"] = list(self._review_view_root_path)
        stats_task = self._prepare_review_db_stats(node)
        self._hub.set_from_node(
            node,
            orientation=self.board_orientation,
            message="Browsing variations",
            allow_moves=False,
        )
        self._broadcast_review_navigation()
        if stats_task is not None:
            self._queue_review_db_stats(stats_task)

    def study_from_here(self, start_range: int) -> None:
        if self._mode != "review":
            raise RuntimeError("Study root can only be changed in review mode")

        node = self._review_node_at_path(list(self._review_path))
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

    def study_history_prompt(self, prompt_id: PromptLineId, spec_id: SpecId | None = None) -> None:
        self.active = True
        self._mode = "guess"
        self._bookmark_current_prompt_pending = False
        self._rep_controller.start_prompt_by_id(prompt_id, spec_id)
        self.show_prompt()

    def _show_history_board(self, position_fen: str, message: str) -> None:
        self._mode = "idle"
        self._review_payload = None
        self._search_move_payload = None
        self._hub.set_fen(
            position_fen,
            orientation=self.board_orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state()

    def goto_history_move(self, path: Optional[list[int]], position_fen: Optional[str], san: str) -> None:
        message = f"History: {san}. Click New to continue practicing."
        if path is not None:
            if self._mode == "review":
                self.goto_review_path(path)
                return
            node = node_at_path(self._session.game, path, self._session.variations)
            self._enter_review_mode(node=node, message=message)
            return

        if position_fen is None:
            raise ValueError("History navigation requires either a path or a FEN")
        self._show_history_board(position_fen, message)

    def set_review_show_alternatives(self, enabled: bool) -> None:
        if self._review_show_alternatives == enabled:
            self._broadcast_ui_state()
            return

        current_node: Optional[Node] = None
        if self.active and self._mode == "review":
            if self._review_path is None:
                raise RuntimeError("Review mode requires a current review path")
            current_node = self._review_node_at_path(
                list(self._review_path),
                show_alternatives=self._review_show_alternatives,
            )

        self._review_show_alternatives = enabled

        if current_node is None:
            self._broadcast_ui_state()
            return

        review_node = current_node if enabled else self._visible_review_node(current_node)
        if enabled:
            message = "Showing alternatives."
        elif review_node is current_node:
            message = "Hiding alternatives."
        else:
            message = "Hiding alternatives. Moved to the nearest visible position."

        self._enter_review_mode(node=review_node, message=message)

    def _enter_review_mode(
        self,
        node: chess.pgn.GameNode,
        message: Optional[str] = "",
    ) -> None:
        self._mode = "review"
        self._search_move_payload = None
        end_ply = self._session.options.end_ply
        try:
            self._review_path = self._review_path_from_root(node)
        except ValueError:
            if self._review_show_alternatives:
                raise
            self._review_show_alternatives = True
            self._review_path = self._review_path_from_root(node)

        self._review_base_root_path = self._review_base_path()
        self._review_view_root_path = list(self._review_base_root_path)
        self._review_view_root_path = self._review_view_root_path_for(self._review_path)
        exported = export_pgn_subtree(
            self._session,
            self._session.game,
            end_ply=end_ply,
            prefer_mainline_path=self._review_path,
            get_children=self._review_children,
        )
        tree = build_variation_tree(
            self._review_children,
            self._session.game,
            end_ply=end_ply,
            is_move_bookmarked=self._rep_engine.move_is_bookmarked,
        )
        self._review_payload = {
            "fen": exported.fen,
            "pgn": exported.pgn,
            "initialPly": exported.initial_ply,
            "orientation": self.board_orientation,
            "tree": tree,
            "currentPath": self._review_path,
            "viewRootPath": list(self._review_view_root_path),
            "dbStatsRequestId": None,
            "dbStats": None,
        }
        stats_task = self._prepare_review_db_stats(node)

        self._hub.set_from_node(
            node,
            orientation=self.board_orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state()
        if stats_task is not None:
            self._queue_review_db_stats(stats_task)


    def _close_session(self) -> None:
        self._session.close()
        self._session = None
