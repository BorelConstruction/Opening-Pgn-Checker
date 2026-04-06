from __future__ import annotations

import importlib
import os
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional

import chess
import chess.pgn


@dataclass
class SpacedRepetitionConfig:
    input_pgn: str
    play_white: bool
    start_move: int
    end_move: int
    non_file_move_frequency: float = 0.0
    engine_path: str = ""


def _ply_from_move_number(move_number: int) -> int:
    return move_number * 2 - 1


class SpacedRepetitionController:
    """
    Stateful spaced repetition session that drives the web board.

    Flow:
    - start() chooses a random "our turn" node in the PGN and shows it
    - user guesses by making a move on the board
    - if correct: board shows the continuation, moves are disabled, user can Continue/New
    - if wrong: board resets back to the prompt and user can retry or New
    """

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self._rng = random.Random()

        self.active = False
        self._side: chess.Color = chess.WHITE
        self._orientation: str = "white"

        self._cfg: Optional[SpacedRepetitionConfig] = None
        self._prompt_nodes: list[Any] = []

        self._prompt_node: Optional[Any] = None
        self._prompt_board: Optional[chess.Board] = None
        self._prompt_off_file: bool = False
        self._awaiting_choice: bool = False
        self._after_our_move_node: Optional[Any] = None

        self._start_ply = 0
        self._end_ply = 10_000

        self._engine_path: str = ""
        self._engine: Optional[Any] = None

        self._db_stats_enabled = True
        self._db_client: Optional[Any] = None
        self._safe_get_games: Optional[Callable[..., Any]] = None
        self._total_games: Optional[Callable[[dict], int]] = None
        self._board_fen: Optional[Callable[[Any], str]] = None
        self._uci_from_pgn: Optional[Callable[[str], str]] = None
        self._db_stats_cache: dict[str, dict[str, int]] = {}
        self._path_weight_cache: dict[int, float] = {}

    def start(self, cfg: SpacedRepetitionConfig) -> None:
        self._cfg = cfg
        self._side = chess.WHITE if cfg.play_white else chess.BLACK
        self._orientation = "white" if cfg.play_white else "black"
        self._start_ply = _ply_from_move_number(cfg.start_move)
        self._end_ply = _ply_from_move_number(cfg.end_move)
        self._engine_path = cfg.engine_path or ""

        self._close_engine()
        self._db_stats_enabled = True
        self._db_client = None
        self._safe_get_games = None
        self._total_games = None
        self._board_fen = None
        self._uci_from_pgn = None
        self._db_stats_cache = {}
        self._path_weight_cache = {}

        games = _load_games(cfg.input_pgn)
        self._prompt_nodes = _collect_prompt_nodes(
            games,
            side=self._side,
            start_ply=self._start_ply,
            end_ply=self._end_ply,
        )
        if not self._prompt_nodes:
            raise ValueError("No guessable positions found in the selected ply range")

        self.active = True
        self.new_random(message="Spaced repetition started. Make your move.")

    def stop(self) -> None:
        self.active = False
        self._cfg = None
        self._prompt_nodes = []
        self._prompt_node = None
        self._prompt_board = None
        self._prompt_off_file = False
        self._awaiting_choice = False
        self._after_our_move_node = None
        self._close_engine()

    def new_random(self, *, message: str = "New position. Make your move.") -> None:
        if not self.active:
            return
        prompt_node, prompt_board, prompt_off_file = self._choose_random_prompt()
        self._prompt_node = prompt_node
        self._prompt_board = prompt_board
        self._prompt_off_file = prompt_off_file
        self._awaiting_choice = False
        self._after_our_move_node = None

        if prompt_node is not None:
            self._show_prompt(node=prompt_node, message=message)
        else:
            self._show_prompt(board=prompt_board, message=message)

    def continue_line(self) -> None:
        if not self.active:
            return
        if self._after_our_move_node is None:
            self.new_random(message="Line continuation is not available. New random position.")
            return

        node = self._after_our_move_node
        if not getattr(node, "variations", None):
            self.new_random(message="Line ended. New random position.")
            return

        opp = node.variations[0]
        if opp.ply() > self._end_ply:
            self.new_random(message="Reached end of range. New random position.")
            return

        if opp.board().turn != self._side or not getattr(opp, "variations", None):
            self.new_random(message="No further line to continue. New random position.")
            return

        self._prompt_node = opp
        self._prompt_board = None
        self._prompt_off_file = False
        self._awaiting_choice = False
        self._after_our_move_node = None
        self._show_prompt(self._prompt_node, message="Continue. Your move.")

    def handle_guess(self, uci: str) -> None:
        if not self.active:
            return
        if self._prompt_node is None and self._prompt_board is None:
            self.new_random(message="No active prompt. New position.")
            return
        if self._awaiting_choice:
            self._show_after_move(
                node=self._after_our_move_node,
                board=self._prompt_board,
                message="Choose Continue or New.",
            )
            return

        if self._prompt_node is not None:
            self._handle_file_guess(uci)
        else:
            self._handle_off_file_guess(uci)

    def _handle_file_guess(self, uci: str) -> None:
        if self._prompt_node is None:
            self.new_random(message="No active prompt. New position.")
            return

        expected_moves = list(getattr(self._prompt_node, "variations", []) or [])
        if not expected_moves:
            self.new_random(message="No moves in file here. New position.")
            return

        expected_ucis = {n.move.uci() for n in expected_moves if getattr(n, "move", None) is not None}
        if uci in expected_ucis:
            chosen = next(n for n in expected_moves if n.move and n.move.uci() == uci)
            san = _san_from_parent(self._prompt_node, chosen)
            self._after_our_move_node = chosen
            self._prompt_board = None
            self._prompt_off_file = False
            self._advance_line(chosen, san)
            return

        expected_sans = ", ".join(
            _san_from_parent(self._prompt_node, n) for n in expected_moves if n.move
        )
        user_eval = self._evaluate_move(self._prompt_node.board(), uci)
        best_expected_eval = None
        if user_eval is not None:
            evals = []
            for n in expected_moves:
                if not getattr(n, "move", None):
                    continue
                eval_value = self._evaluate_move(self._prompt_node.board(), n.move.uci())
                if eval_value is not None:
                    evals.append(eval_value)
            if evals:
                best_expected_eval = max(evals)

        msg = f"Wrong. Expected: {expected_sans}."
        if user_eval is not None:
            msg += f" Your move q-eval {user_eval:+.2f}."
            if best_expected_eval is not None:
                msg += f" File move q-eval {best_expected_eval:+.2f}."
        else:
            msg += " q-eval unavailable."

        self._show_prompt(self._prompt_node, message=msg)

    def _handle_off_file_guess(self, uci: str) -> None:
        if self._prompt_board is None:
            self.new_random(message="No active prompt. New position.")
            return

        board = self._prompt_board.copy()
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            self._show_prompt(board=board, message=f"Illegal move: {uci}. Try again.")
            return

        prev_board = board.copy()
        board.push(move)
        move_eval = self._evaluate_board(board)
        san = _san_from_board(prev_board, move)

        msg = f"Off-file response {san}."
        if move_eval is not None:
            msg += f" q-eval {move_eval:+.2f}. Click New."
        else:
            msg += " q-eval unavailable. Click New."

        self._awaiting_choice = True
        self._after_our_move_node = None
        self._show_after_move(board=board, message=msg)

    def _advance_line(self, chosen: Any, san: str) -> None:
        if not getattr(chosen, "variations", None):
            self.new_random(message=f"Correct: {san}. Line ended. New random position.")
            return

        opp = chosen.variations[0]
        if opp.ply() > self._end_ply:
            self.new_random(message=f"Correct: {san}. Reached range end. New random position.")
            return

        if opp.board().turn != self._side or not getattr(opp, "variations", None):
            self.new_random(message=f"Correct: {san}. No further guessable line. New random position.")
            return

        self._prompt_node = opp
        self._prompt_board = None
        self._prompt_off_file = False
        self._awaiting_choice = False
        self._after_our_move_node = None
        self._show_prompt(self._prompt_node, message=f"Correct: {san}. Continue along the line.")

    def _show_prompt(self, node: Any = None, board: Optional[chess.Board] = None, *, message: str) -> None:
        if node is not None:
            self._hub.set_from_node(
                node,
                orientation=self._orientation,
                message=message,
                allow_moves=True,
            )
            return

        if board is None:
            raise ValueError("Either node or board must be provided")

        self._hub.set_fen(
            board.fen(),
            orientation=self._orientation,
            message=message,
            allow_moves=True,
        )

    def _show_after_move(self, node: Any = None, board: Optional[chess.Board] = None, *, message: str) -> None:
        if node is None and board is None:
            self.new_random(message=message)
            return

        if node is not None:
            self._hub.set_from_node(
                node,
                orientation=self._orientation,
                message=message,
                allow_moves=False,
            )
            return

        self._hub.set_fen(
            board.fen(),
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )

    def _choose_random_prompt(self) -> tuple[Optional[Any], Optional[chess.Board], bool]:
        if self._cfg and self._cfg.non_file_move_frequency > 0.0:
            if self._rng.random() < self._cfg.non_file_move_frequency:
                off_file = self._sample_off_file_prompt()
                if off_file is not None:
                    return off_file

        return self._sample_file_prompt()

    def _sample_file_prompt(self) -> tuple[Optional[Any], Optional[chess.Board], bool]:
        prompt_node = self._select_weighted(self._prompt_nodes, [self._path_weight(n) for n in self._prompt_nodes])
        return prompt_node, None, False

    def _sample_off_file_prompt(self) -> Optional[tuple[Optional[Any], Optional[chess.Board], bool]]:
        candidates = [
            n for n in self._prompt_nodes
            if getattr(n, "variations", None) and n.ply() + 2 <= self._end_ply
        ]
        if not candidates:
            return None

        base_node = self._select_weighted(candidates, [self._path_weight(n) for n in candidates])
        if base_node is None:
            return None

        file_moves = list(getattr(base_node, "variations", []) or [])
        if not file_moves:
            return None

        chosen_file_move = self._select_weighted(
            file_moves,
            self._child_weights(base_node, file_moves),
        )
        if chosen_file_move is None or getattr(chosen_file_move, "move", None) is None:
            return None

        board = base_node.board().copy()
        board.push(chosen_file_move.move)
        exclude = {
            self._uci_for_stats(child.move)
            for child in getattr(chosen_file_move, "variations", [])
            if getattr(child, "move", None) is not None
        }

        off_book_move = self._select_off_book_reply(board, exclude)
        if off_book_move is None:
            return None

        board.push(off_book_move)
        return None, board, True

    def _child_weights(self, parent: Any, variations: list[Any]) -> list[float]:
        stats = self._db_stats_for_position(parent)
        if stats:
            weights = []
            for child in variations:
                if getattr(child, "move", None) is None:
                    weights.append(0.0)
                    continue
                count = stats.get(self._uci_for_stats(child.move))
                weights.append(float(count) if count is not None else 1.0)
            if any(w > 0 for w in weights):
                return weights
        return [1.0] * len(variations)

    def _select_weighted(self, items: list[Any], weights: list[float]) -> Any:
        if not items:
            return None
        if len(items) != len(weights):
            return self._rng.choice(items)

        total = sum(weights)
        if total <= 0:
            return self._rng.choice(items)

        threshold = self._rng.random() * total
        cumulative = 0.0
        for item, weight in zip(items, weights):
            cumulative += weight
            if threshold <= cumulative:
                return item
        return items[-1]

    def _path_weight(self, node: Any) -> float:
        key = id(node)
        if key in self._path_weight_cache:
            return self._path_weight_cache[key]

        if getattr(node, "parent", None) is None or getattr(node, "move", None) is None:
            self._path_weight_cache[key] = 1.0
            return 1.0

        parent_weight = self._path_weight(node.parent)
        weight = parent_weight * self._edge_probability(node.parent, node.move)
        self._path_weight_cache[key] = weight
        return weight

    def _edge_probability(self, parent: Any, move: chess.Move) -> float:
        stats = self._db_stats_for_position(parent)
        if stats:
            total = float(sum(stats.values()))
            if total > 0.0:
                uci = self._uci_for_stats(move)
                count = float(stats.get(uci, 0))
                if count > 0.0:
                    return count / total
                return 1.0 / max(len(getattr(parent, "variations", []) or []), 1) / 100.0
        variation_count = max(len(getattr(parent, "variations", []) or []), 1)
        return 1.0 / variation_count

    def _db_stats_for_position(self, position: Any) -> dict[str, int]:
        if not self._db_stats_enabled:
            return {}
        if self._safe_get_games is None:
            self._init_db_client()
        if self._safe_get_games is None or self._db_client is None:
            return {}

        board = position.board() if hasattr(position, "board") else position
        if not isinstance(board, chess.Board):
            return {}

        fen_str = self._board_fen(board) if self._board_fen is not None else board.fen()
        if fen_str in self._db_stats_cache:
            return self._db_stats_cache[fen_str]

        stats: dict[str, int] = {}
        try:
            data = self._safe_get_games(self._db_client, position=fen_str)
            for move in data.get("moves", []) or []:
                uci = move.get("uci")
                if uci:
                    stats[uci] = int(self._total_games(move)) if self._total_games is not None else 0
        except Exception:
            self._db_stats_enabled = False
            stats = {}

        self._db_stats_cache[fen_str] = stats
        return stats

    def _select_off_book_reply(self, board: chess.Board, exclude: set[str]) -> Optional[chess.Move]:
        candidates = [move for move in board.legal_moves if move.uci() not in exclude]
        if not candidates:
            return None

        stats = self._db_stats_for_position(board)
        if stats:
            weights = [float(stats.get(self._uci_for_stats(move), 0.0)) for move in candidates]
            if any(w > 0 for w in weights):
                return self._select_weighted(candidates, weights)

        return self._rng.choice(candidates)

    def _evaluate_move(self, board: chess.Board, uci: str) -> Optional[float]:
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            return None
        if move not in board.legal_moves:
            return None
        copied = board.copy()
        copied.push(move)
        return self._evaluate_board(copied)

    def _evaluate_board(self, board: chess.Board) -> Optional[float]:
        engine = self._get_engine()
        if engine is None:
            return None
        try:
            info = engine.analyse(board, chess.engine.Limit(time=0.25))
            score = info["score"].pov(self._side)
            value = score.score(mate_score=100000)
            if value is None:
                return 1000.0
            return float(value) / 100.0
        except Exception:
            return None

    def _get_engine(self) -> Optional[Any]:
        if self._engine is not None:
            return self._engine
        if not self._engine_path:
            return None
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self._engine_path)
            return self._engine
        except Exception:
            self._engine = None
            return None

    def _close_engine(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.close()
        except Exception:
            pass
        self._engine = None

    def _init_db_client(self) -> None:
        if self._db_client is not None and self._safe_get_games is not None and self._board_fen is not None:
            return
        try:
            import berserk  # type: ignore
            from ..core import database as core_database
            from ..core.boardtools import fen as boardtools_fen, uci_from_pgn_to_lichess
        except Exception:
            self._db_stats_enabled = False
            return

        token = os.getenv("LICHESS_TOKEN", "")
        if token:
            session = berserk.TokenSession(token)
            client = berserk.Client(session=session)
        else:
            client = berserk.Client()

        self._db_client = client.opening_explorer
        self._safe_get_games = core_database.safe_get_games
        self._total_games = core_database.total_games
        self._board_fen = boardtools_fen
        self._uci_from_pgn = uci_from_pgn_to_lichess

    def _uci_for_stats(self, move: chess.Move) -> str:
        if self._uci_from_pgn is None:
            try:
                from ..core.boardtools import uci_from_pgn_to_lichess
                self._uci_from_pgn = uci_from_pgn_to_lichess
            except Exception:
                self._uci_from_pgn = lambda u: u
        return self._uci_from_pgn(move.uci())


def _san_from_board(board: chess.Board, move: chess.Move) -> str:
    try:
        return board.san(move)
    except Exception:
        return move.uci()


def _san_from_parent(parent: Any, child: Any) -> str:
    try:
        b = parent.board()
        return b.san(child.move)
    except Exception:
        try:
            return child.move.uci()
        except Exception:
            return "?"


def _load_games(path: str) -> list[Any]:
    games: list[Any] = []
    with open(path, "r", encoding="utf-8") as f:
        while True:
            g = chess.pgn.read_game(f)
            if g is None:
                break
            games.append(g)
    return games


def _collect_prompt_nodes(games: list[Any], *, side: chess.Color, start_ply: int, end_ply: int) -> list[Any]:
    nodes: list[Any] = []
    for g in games:
        stack = [g]
        while stack:
            n = stack.pop()
            try:
                ply = n.ply()
            except Exception:
                ply = 0

            try:
                turn = n.board().turn
            except Exception:
                turn = side

            if start_ply <= ply <= end_ply and turn == side and getattr(n, "variations", None):
                nodes.append(n)

            for ch in getattr(n, "variations", []) or []:
                stack.append(ch)
    return nodes

