from __future__ import annotations

import random
import sys
from typing import Any, Optional, Union

import chess
import chess.pgn
from chess.pgn import GameNode as Node
from dataclasses import dataclass

from ..core.boardtools import fen, node_san, uci_from_lichess_to_pgn
from ..core.options import SpacedRepetitionOptions
from ..core.repertoire import RepertoireSession, default_repertoire_cache_path
from .pgn_export import export_pgn_subtree
from .variation_tree import node_at_path, path_from_root

@dataclass
class PromptState:
    node: Optional[Node]
    board: Optional[chess.Board]
    off_file: bool
    debug_msg: str
    anchor_node: Optional[Node] = None


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
        self._mode = "idle"  # idle | guess | review

        self._cfg = SpacedRepetitionOptions()
        self._games: list[Any] = []

        self._prompt: PromptState | None = None
        self._session: RepertoireSession | None = None
        self._side = chess.WHITE
        self._orientation = "white"

        self._current_game: chess.pgn.Game | None = None
        self._tree_root: chess.pgn.GameNode | None = None
        self._review_payload: dict[str, Any] | None = None
        self._review_path: list[int] | None = None

        self._after_our_move_node: Optional[Any] = None

    def ui_state(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "mode": self._mode,
            "review": self._review_payload if self.active and self._mode == "review" else None,
        }

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
        self._mode = "guess"
        self.new_random(message="Spaced repetition started. Make your move.")

    def stop(self) -> None:
        self.active = False
        self._mode = "idle"
        self._games = []
        self._prompt = None
        self._after_our_move_node = None
        self._current_game = None
        self._tree_root = None
        self._review_payload = None
        self._review_path = None
        self._close_session()
        self._broadcast_ui_state()

    def _ensure_active(self) -> None:
        if not self.active:
            raise RuntimeError("Spaced repetition is not active")
        if self._session is None:
            raise RuntimeError("Spaced repetition session is not initialized")

    def _broadcast_ui_state(self) -> None:
        self._hub.broadcast({"type": "sr_state", "sr": self.ui_state()})

    def _prefetch_db_stats(self) -> None:
        """Pre-warm the cache by querying DB stats that we will need."""
        def visit(node: Any):
            if not node.turn() == self._side:
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
        self._ensure_active()

        self._mode = "guess"
        self._review_payload = None
        self._review_path = None
        self._after_our_move_node = None
        self._prompt = self._choose_random_prompt()

        if self._prompt.debug_msg:
            message = f"{message} {self._prompt.debug_msg}"

        if self._prompt.node is not None:
            self._show_prompt(node=self._prompt.node, message=message)
        else:
            self._show_prompt(board=self._prompt.board, message=message)

        self._broadcast_ui_state()

    def continue_line(self) -> None:
        self._ensure_active()
         
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
         
        self._prompt.node = opp
        self._prompt.board = None
        self._prompt.off_file = False
        self._prompt.anchor_node = opp
        self._after_our_move_node = None
        self._show_prompt(self._prompt.node, message="Continue. Your move.")

    def handle_guess(self, uci: str) -> None:        
        self._ensure_active()
        if self._mode != "guess":
            raise RuntimeError("Not currently in guess mode")
        if self._prompt is None:
            raise RuntimeError("No active prompt")

        if self._prompt.node is not None:
            self._handle_file_guess(uci)
        else:
            self._handle_off_file_guess(uci)

    def _handle_file_guess(self, uci: str) -> None:
        expected_moves = list(self._session.variations(self._prompt.node))
        if not expected_moves:
            self.new_random(message="No moves in file here. New position.")
            return

        expected_ucis = {n.move.uci() for n in expected_moves}
        if uci in expected_ucis:
            chosen_node = next(n for n in expected_moves if n.move and n.move.uci() == uci)
            san = node_san(chosen_node)
            self._after_our_move_node = chosen_node
            self._prompt.board = None
            self._prompt.off_file = False
            self._advance_line(chosen_node, san)
            return

        expected_sans = ", ".join(
            node_san(n) for n in expected_moves
        )
        user_eval = self._evaluate_move(self._prompt.node.board(), uci)
        best_expected_eval = None
        evals = [self._evaluate_move(self._prompt.node.board(), n.move.uci())
                 for n in expected_moves]
        best_expected_eval = max(evals)

        msg = f"Wrong. Expected: {expected_sans}."
        if user_eval is not None:
            msg += f" Your move eval {user_eval:+.2f}."
            if best_expected_eval is not None:
                msg += f" File move eval {best_expected_eval:+.2f}."

        self._show_prompt(self._prompt.node, message=msg)

    def _handle_off_file_guess(self, uci: str) -> None:
        board = self._prompt.board
        move = chess.Move.from_uci(uci)
        
        if move not in board.legal_moves:
            self._show_prompt(board=board, message=f"Illegal move: {uci}. Try again.")
            return

        ev = self._session.q_eval_move(self._prompt.board, move)
        move_eval, reply = ev.eval, ev.move
        san = _san_from_board(board, move)
        reply_san = self._prompt.board.san(reply)

        msg = f"Off-file {san}. Evaluation {move_eval:+.2f} after {reply}."
        msg += f" Best was {reply_san} with evaluation {move_eval:+.2f}."
        msg += " Try again or click New."

        self._show_prompt(board=board, message=msg)

    def _advance_line(self, chosen: Any, san: str) -> None:
        chosen_variations = list(self._session.variations(chosen))
        if not chosen_variations:
            self._enter_review_mode(
                node=chosen,
                message=f"Correct: {san}. Line ended. Browse the tree or click New.",
            )
            return

        opp, advance_debug = self._choose_move(chosen)

        if opp is False:
            self._enter_review_mode(
                node=chosen,
                message=f"Correct: {san}. Line ended. Browse the tree or click New.",
            )
            return

        if opp.ply() > self._session.options.end_ply:
            self._enter_review_mode(
                node=chosen,
                message=f"Correct: {san}. Reached end of range. Browse the tree or click New.",
            )
            return

        if opp.turn() != self._side or not self._session.variations(opp):
            self._enter_review_mode(
                node=opp,
                message=f"Correct: {san}. No further guessable line. Browse the tree or click New.",
            )
            return

        self._prompt = PromptState(opp, None, False, advance_debug, anchor_node=opp)
        message = f"Correct: {san}. Continue along the line."
        if advance_debug:
            message = f"{message} {advance_debug}"
        self._show_prompt(self._prompt.node, message=message)

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
            fen(board),
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
            fen(board),
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )

    def _choose_random_prompt(self) -> PromptState:
        """Pick a random game and navigate to start_ply, then choose a prompt."""        
        for _ in range(len(self._games)):
            game = self._rng.choice(self._games)
            root = self._mainline_node_at_ply(game, self._session.options.start_ply)
            while not (prompt := self._choose_prompt(root)):
                pass
            if prompt.node is not None or prompt.board is not None:
                self._current_game = game
                self._tree_root = root
                return prompt
         
        # Fallback: try the first game
        game = self._games[0]
        root = self._mainline_node_at_ply(game, self._session.options.start_ply)
        while not (prompt := self._choose_prompt(root)):
                pass
        self._current_game = game
        self._tree_root = root
        return prompt

    def _choose_prompt_line_length(self, node: Any) -> int:
        remaining = self._session.options.end_ply - node.ply()
        if remaining <= 0:
            return 0
        # Choose a short line length for prompt sampling.
        # return min(max(1, self._rng.randint(1, 5)), remaining)
        return self._rng.randint(1, remaining)

    def _choose_prompt(self, node: Any) -> PromptState:
        """Simulate walking through a line. Returns False if a walk along a line failed."""
        selection_debug = ""
        line_length = self._choose_prompt_line_length(node)

        # we'll do line_length or  line_length-1 steps total
        for step in range(line_length - 2):
            next_node, _ = self._choose_move(node, maybe_off_book=False)
            if not isinstance(next_node, Node):
                return False
            node = next_node

        if node.turn() == self._side:
            next_node, _ = self._choose_move(node, maybe_off_book=False)
            if not isinstance(next_node, Node):
                return False
            node = next_node

        # Final step: potentially off_book move for opponent's move
        assert node.turn() != self._side, f"Prompt selection should end on our turn {line_length}"
        next_board, selection_debug = self._choose_move(node, maybe_off_book=True)
        if isinstance(next_board, chess.Board):
            return PromptState(None, next_board, True, selection_debug, anchor_node=node)
        else:
            return PromptState(next_board, next_board.board(), False, selection_debug, anchor_node=next_board)

    def _choose_move(
        self,
        parent: Node,
        *,
        maybe_off_book: bool = False,
        use_engine: bool = False,
    ) -> tuple[Union[chess.Board, Node], str]:
        """
        Chooses a move randomly to simulate a line. Returns the resulting node/board and a debug string.

        If a choice could not be made, returns (False, "").
        """
        off_book = maybe_off_book and self._rng.random() < self._cfg.non_file_move_frequency   
        
        children = self._session.variations(parent)
        if not children:
            # if there are no moves for us in the file but we are still here, that's impoper usage
            if parent.turn() == self._side:
                return False, ""
            # if there are no moves for them, we can try anyway
            off_book = True
            use_engine = True

        # a tiny optimization
        if parent.turn() == self._side and not self._session.options.check_alternatives:
            return children[0], "our move"
            
        if off_book:
            # Try to find an off-book move with probability non_file_move_frequency
            off_book_move, off_book_debug = self._find_off_book_move(parent)
            if off_book_move is not None:
                board = parent.board()
                board.push(off_book_move)
                return board, off_book_debug
            elif use_engine:
                engine_move = self._session.query(fen(parent), "q-eval").move
                if engine_move:
                    board = parent.board()
                    board.push(engine_move)
                    return board, f"engine-suggested off-book move {engine_move}"
                else:
                # should only happen if it's mate
                    return False, ""
            # Fall through to normal logic

        weights = self._child_weights(parent, children)
        choice = self._rng_choice(children, weights)
        return choice, self._format_rng_weights(children, weights)

    def _mainline_node_at_ply(self, game: Any, ply: int) -> Any:
        node = game
        while getattr(node, "variations", None) and node.ply() < ply:
            node = node.variations[0]
        return node

    def _find_off_book_move(self, node: Node) -> tuple[Optional[chess.Move], str]:
        """Find an off-book DB move with frequency >= 5% and score_rate <= 75%."""
        move_weights = self._get_move_weights(node)
        if not move_weights:
            return None, ""
        
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
            raise ValueError("No items to choose from")
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


    def _evaluate_move(self, board: Union[chess.Board, Node], move: Union[chess.Move, str]) -> float:
        return self._session.q_eval_move(board, move).eval


    def _close_session(self) -> None:
        if self._session is None:
            return
        self._session.close()
        self._session = None

    def give_up(self) -> None:
        self._ensure_active()
        if self._mode != "guess":
            raise RuntimeError("give_up is only available while guessing")
        if self._prompt is None:
            raise RuntimeError("No active prompt")
        if self._prompt.anchor_node is None:
            raise RuntimeError("Prompt has no anchor node for review")

        if self._prompt.node is not None:
            expected_moves = list(self._session.variations(self._prompt.node))
            if expected_moves:
                expected_sans = ", ".join(node_san(n) for n in expected_moves)
                message = f"Gave up. Expected: {expected_sans}. Browse the tree or click New."
            else:
                message = "Gave up. No moves in file here. Browse the tree or click New."
        else:
            message = "Gave up (off-file prompt). Browse the repertoire tree or click New."

        self._enter_review_mode(node=self._prompt.anchor_node, message=message)

    def goto_review_path(self, path: list[int]) -> None:
        self._ensure_active()
        if self._mode != "review":
            raise RuntimeError("Browsing is only available in review mode")

        end_ply = self._session.options.end_ply
        node = node_at_path(self._session, self._tree_root, path, end_ply=end_ply)

        self._review_path = list(path)
        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message="Browsing variations",
            allow_moves=False,
        )
        self._broadcast_ui_state()

    def _enter_review_mode(self, *, node: chess.pgn.GameNode, message: str) -> None:
        self._ensure_active()

        self._mode = "review"
        end_ply = self._session.options.end_ply
        self._review_path = path_from_root(self._session, self._tree_root, node)
        exported = export_pgn_subtree(
            self._session,
            self._tree_root,
            end_ply=end_ply,
            prefer_mainline_path=self._review_path,
        )
        if exported.skipped_illegal_moves:
            message = (
                f"{message} (skipped {exported.skipped_illegal_moves} illegal move(s) while exporting PGN"
                + (f"; first: {exported.first_illegal_move}" if exported.first_illegal_move else "")
                + ")"
            )
        self._review_payload = {
            "fen": exported.fen,
            "pgn": exported.pgn,
            "initialPly": exported.initial_ply,
            "orientation": self._orientation,
        }

        self._hub.set_from_node(
            node,
            orientation=self._orientation,
            message=message,
            allow_moves=False,
        )
        self._broadcast_ui_state()




def _san_from_board(board: chess.Board, move: chess.Move) -> str:
    try:
        return board.san(move)
    except Exception:
        return move.uci()


def _load_games(path: str) -> list[Any]:
    games: list[Any] = []
    with open(path, "r", encoding="utf-8") as f:
        while True:
            g = chess.pgn.read_game(f)
            if g is None:
                break
            games.append(g)
    return games

def truncate_to_even(number: int) -> int:
    return number - (number % 2)
