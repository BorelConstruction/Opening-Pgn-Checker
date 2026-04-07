from __future__ import annotations

import os
import random
import sys
from typing import Any, Optional, Union

import chess
import chess.pgn

from ..core.boardtools import fen, ply_from_move_number
from ..core.options import SpacedRepetitionOptions
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path


class SpacedRepetitionFeature:
    def __init__(self, options: SpacedRepetitionOptions, progress_cb=None, report_cb=None) -> None:
        self.options = options
        self.session = RepertoireSession(
            options,
            progress_cb=progress_cb,
            report_cb=report_cb,
            default_cache_path=lambda: default_repertoire_cache_path(options),
        )

    def run(self) -> str:
        from .server import ensure_web_server
        from .app import sr_controller

        ensure_web_server(host="127.0.0.1", port=8000)
        sr_controller.start(self.options, self.session)
        return "Spaced repetition launched at http://127.0.0.1:8000/"

    def close(self) -> None:
        from .app import sr_controller

        sr_controller.stop()
        self.session.close()



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

        self._cfg = SpacedRepetitionOptions()
        self._games: list[Any] = []

        self._prompt_node: Optional[Any] = None
        self._prompt_board: Optional[chess.Board] = None
        self._prompt_off_file: bool = False
        self._awaiting_choice: bool = False
        self._after_our_move_node: Optional[Any] = None

    def start(self, options: SpacedRepetitionOptions, session: Optional[RepertoireSession] = None) -> None:
        self._cfg = options
        self._session = session or RepertoireSession(
            options,
            default_cache_path=lambda: default_repertoire_cache_path(options),
        )
        self._side = chess.WHITE if options.play_white else chess.BLACK
        self._orientation = "white" if options.play_white else "black"


        self._games = _load_games(options.input_pgn)
        if not self._games:
            raise ValueError("No games found in the input PGN")

        if options.preload_db:
            self._prefetch_db_stats()

        self.active = True
        self.new_random(message="Spaced repetition started. Make your move.")

    def stop(self) -> None:
        self.active = False
        self._cfg = None
        self._games = []
        self._prompt_node = None
        self._prompt_board = None
        self._prompt_off_file = False
        self._awaiting_choice = False
        self._after_our_move_node = None
        self._close_session()

    def _prefetch_db_stats(self) -> None:
        """Pre-warm the cache by querying DB stats through session traversal."""
        def visit(node: Any):
            if node.turn() == self._side:
                self._session.query(fen(node), "db_lichess")

        for game in self._games:
            self._session.traverse(game, visit=visit)

    def _get_move_weights(self, position: Any) -> dict[str, float]:
        """
        Returns a dict mapping UCI strings to move counts (weights).
        """
        data = self._session.query(fen(position), "db_lichess")
        if not data or "moves" not in data:
            sys.stderr.write(f"No DB moves for {position}\n")
            return {}
        
        weights = {}
        for move_data in data.get("moves", []):
            uci = move_data["uci"]
            if uci:
                count = move_data.get("white", 0) + move_data.get("draws", 0) + move_data.get("black", 0)
                weights[uci] = float(count)
        return weights

    def new_random(self, *, message: str = "New position. Make your move.") -> None:
        if not self.active:
            return
        prompt_node, prompt_board, prompt_off_file, prompt_debug = self._choose_random_prompt()
        self._prompt_node = prompt_node
        self._prompt_board = prompt_board
        self._prompt_off_file = prompt_off_file
        self._awaiting_choice = False
        self._after_our_move_node = None

        if prompt_debug:
            message = f"{message} {prompt_debug}"

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
        children = self._session.variations(node)
        if not children:
            self.new_random(message="Line ended. New random position.")
            return

        opp = children[0]
        if opp.ply() > self._session.options.end_ply:
            self.new_random(message="Reached end of range. New random position.")
            return

        if opp.turn() != self._side or not self._session.variations(opp):
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

        expected_moves = list(self._session.variations(self._prompt_node))
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
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            self._show_prompt(board=board, message=f"Illegal move: {uci}. Try again.")
            return
        
        if move not in board.legal_moves:
            self._show_prompt(board=board, message=f"Illegal move: {uci}. Try again.")
            return

        ev = self._session.q_eval_move(board, move)
        move_eval, reply = ev.eval, ev.move
        san = _san_from_board(board, move)

        # Get best move at current position
        best_eval_obj = self._session.query(fen(self._prompt_board), "q-eval")
        best_move = best_eval_obj.move
        best_eval = best_eval_obj.eval
        best_san = self._prompt_board.san(best_move)

        msg = f"Off-file {san}. Evaluation {move_eval:+.2f} after {reply}."
        msg += f" Best was {best_san} with evaluation {best_eval:+.2f}."
        msg += " Try again or click New."

        self._show_prompt(board=board, message=msg)

    def _advance_line(self, chosen: Any, san: str) -> None:
        chosen_variations = list(self._session.variations(chosen))
        if not chosen_variations:
            self.new_random(message=f"Correct: {san}. Line ended. New random position.")
            return

        opp, advance_debug = self._choose_move(chosen)

        if opp is None:
            self.new_random(message=f"Correct: {san}. Line ended. New random position.")
            return

        if opp.turn() != self._side or not self._session.variations(opp):
            self.new_random(message=f"Correct: {san}. No further guessable line. New random position.")
            return

        self._prompt_node = opp
        self._prompt_board = None
        self._prompt_off_file = False
        self._awaiting_choice = False
        self._after_our_move_node = None
        message = f"Correct: {san}. Continue along the line."
        if advance_debug:
            message = f"{message} {advance_debug}"
        self._show_prompt(self._prompt_node, message=message)

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

    def _choose_random_prompt(self) -> tuple[Optional[Any], Optional[chess.Board], bool, str]:
        """Pick a random game and navigate to start_ply, then choose a prompt."""        
        for _ in range(len(self._games)):
            game = self._rng.choice(self._games)
            node = self._mainline_node_at_ply(game, self._session.options.start_ply)
            prompt = self._choose_prompt(node)
            if prompt[0] is not None or prompt[1] is not None:
                return prompt
        
        # Fallback: try the first game
        node = self._mainline_node_at_ply(self._games[0], self._session.options.start_ply)
        return self._choose_prompt(node)

    def _choose_prompt_line_length(self, node: Any) -> int:
        remaining = self._session.options.end_ply - node.ply()
        if remaining <= 0:
            return 0
        # Choose a short line length for prompt sampling.
        return min(max(1, self._rng.randint(1, 5)), remaining)

    def _choose_prompt(self, node: Any) -> tuple[Optional[Any], Optional[chess.Board], bool, str]:
        """Simulate walking through a line."""
        selection_debug = ""
        line_length = self._choose_prompt_line_length(node)

        # Walk through line_length-1 moves
        for step in range(line_length - 1):
            if node.ply() >= self._session.options.end_ply:
                break

            children = list(self._session.variations(node))
            if not children:
                break

            next_node, choose_debug = self._choose_move(node, off_book=False)
            selection_debug = choose_debug
            if next_node is None:
                break
            node = next_node

        # Final step: try off_book move on opponent's move (or return if no moves)
        if node.ply() < self._session.options.end_ply:
            children = list(self._session.variations(node))
            if children and node.turn() != self._side:
                # Opponent's turn - try off-book move
                next_node, choose_debug = self._choose_move(node, off_book=True)
                if next_node is not None:
                    selection_debug = choose_debug
                    # If off-book move was found, it's a Move object; otherwise it's a Node
                    if isinstance(next_node, chess.Move):
                        board = node.board().copy()
                        board.push(next_node)
                        return None, board, True, selection_debug
                    else:
                        node = next_node

        # Return final node if it's our turn
        if node is not None and node.turn() == self._side and self._session.variations(node):
            return node, None, False, selection_debug

        return None, None, False, selection_debug

    def _choose_move(
        self,
        parent: Any,
        *,
        off_book: bool = False,
    ) -> tuple[Optional[Any], str]:
        children = list(self._session.variations(parent))
        if not children:
            return None, ""

        # a tiny optimization
        if parent.turn() == self._side and not self._session.options.check_alternatives:
            return children[0], "our move"
            
        weights = self._child_weights(parent, children)

        if off_book:
            # Try to find an off-book move with probability non_file_move_frequency
            if self._rng.random() < self._cfg.non_file_move_frequency:
                board = parent.board() if hasattr(parent, "board") else parent
                off_book_move, off_book_debug = self._find_off_book_move(board)
                if off_book_move is not None:
                    return off_book_move, off_book_debug
            # Fall through to normal logic

        choice = self._rng_choice(children, weights)
        return choice, self._format_rng_weights(children, weights)

    def _mainline_node_at_ply(self, game: Any, ply: int) -> Any:
        node = game
        while getattr(node, "variations", None) and node.ply() < ply:
            node = node.variations[0]
        return node

    def _find_off_book_move(self, node: chess.Node) -> tuple[Optional[chess.Move], str]:
        """Find an off-book DB move with frequency >= 5% and score_rate <= 75%."""
        move_weights = self._get_move_weights(node)
        if not move_weights:
            return None, ""
        
        total_weight = sum(move_weights.values())
        if total_weight <= 0:
            return None, ""
        
        exclude = {m.uci() for m in self._session.variations(node)}

        # Filter candidates: frequency >= 5%, score_rate <= 75%
        candidates = []
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

        if not candidates:
            return None, ""

        # Select from candidates using weights
        moves, weights = zip(*candidates)
        move = self._rng_choice(list(moves), list(weights))
        debug_text = self._format_rng_weights(list(moves), list(weights))
        return move, debug_text

    def _child_weights(self, parent: Any, variations: list[Any]) -> list[float]:
        if isinstance(parent, chess.Board):
            turn = parent.turn
        else:
            turn = parent.turn() # thanks python-chess
        
        if turn == self._side:
            # TODO: we may want to assign higher weights to file's main line
            return [1.0] * len(variations)
        
        move_weights = self._get_move_weights(parent)
        if not move_weights:
            return [1.0] * len(variations)

        weights = []
        for child in variations:
            if getattr(child, "move", None) is None:
                weights.append(0.0)
                continue
            uci = child.move.uci()
            weights.append(move_weights.get(uci, 0.0))

        if any(w > 0 for w in weights):
            return weights
        return [1.0] * len(variations)

    def _rng_choice(self, items: list[Any], weights: list[float]) -> Any:
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

    def _format_rng_weights(self, items: list[Any], weights: list[float]) -> str:
        if not items or not weights or len(items) != len(weights):
            return ""
        total = sum(weights)
        if total <= 0:
            return ""

        entries = []
        for item, weight in zip(items[:5], weights[:5]):
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

        probs = [weight / total for weight in weights[:5]]
        prob_entries = [f"{p:.1%}" for p in probs]
        return f"rng weights: {', '.join(entries)}; probs: {', '.join(prob_entries)}"

    def _edge_probability(self, parent: Any, move: chess.Move) -> float:
        move_weights = self._get_move_weights(parent)
        if not move_weights:
            # No DB data; fall back to uniform
            variation_count = max(len(self._session.variations(parent)), 1)
            return 1.0 / variation_count

        total_weight = sum(move_weights.values())
        if total_weight <= 0:
            variation_count = max(len(self._session.variations(parent)), 1)
            return 1.0 / variation_count

        uci = move.uci()
        move_count = move_weights.get(uci, 0.0)
        if move_count <= 0:
            return 0.0  # Move not in database
        return move_count / total_weight


    def _evaluate_move(self, board: Union[chess.Board, chess.Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(board, move).eval


    def _close_session(self) -> None:
        self._session.close()
        self._session = None


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



