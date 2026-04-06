from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from chess import Color, WHITE, BLACK


@dataclass(frozen=True)
class Arrow:
    orig: str
    dest: str
    color: str = "green"  # chessground brush: green|red|blue|yellow


@dataclass(frozen=True)
class Circle:
    square: str
    color: str = "green"


@dataclass
class BoardState:
    fen: str
    turn: str  # "white" | "black"
    orientation: str = "white"
    dests: dict[str, list[str]] = field(default_factory=dict)
    last_move: Optional[list[str]] = None  # ["e2", "e4"]
    arrows: list[Arrow] = field(default_factory=list)
    circles: list[Circle] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fen": self.fen,
            "turn": self.turn,
            "orientation": self.orientation,
            "dests": self.dests,
            "lastMove": self.last_move,
            "arrows": [a.__dict__ for a in self.arrows],
            "circles": [c.__dict__ for c in self.circles],
            "message": self.message,
        }


MoveCallback = Callable[[str, BoardState], None]


class WebBoard(ABC):
    """
    Abstract board contract (Python-level API).

    - Reads moves created in the browser (as UCI, e.g. "e2e4", "e7e8q").
    - Renders a position from FEN plus optional arrow/circle annotations.
    """

    @abstractmethod
    def get_state(self) -> BoardState: ...

    @abstractmethod
    def set_fen(
        self,
        fen: str,
        *,
        arrows: Optional[list[Arrow]] = None,
        circles: Optional[list[Circle]] = None,
        orientation: str = "white",
        message: str = "",
        allow_moves: bool = True,
    ) -> BoardState: ...

    @abstractmethod
    def set_from_node(
        self,
        node: Any,
        *,
        orientation: str = "white",
        message: str = "",
        allow_moves: bool = True,
    ) -> BoardState: ...

    @abstractmethod
    def apply_uci(self, uci: str) -> BoardState: ...
