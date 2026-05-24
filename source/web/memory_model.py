from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from typing import Any


SECONDS_PER_DAY = 24.0 * 60.0 * 60.0
INITIAL_ELAPSED_SECONDS = SECONDS_PER_DAY
DELAY_BEFORE_GUESSED_MEANS_REMEMBERS = 60.0


@dataclass(frozen=True)
class PerformanceRecord:
    success: bool
    attempt_time: float

    def to_json(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "attemptTime": self.attempt_time,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "PerformanceRecord":
        if not isinstance(payload, dict):
            raise TypeError("Performance record payload must be a dict")
        return cls(
            success=bool(payload["success"]),
            attempt_time=float(payload["attemptTime"]),
        )


class MemoryModel(ABC):
    @abstractmethod
    def predict_success(self, elapsed_seconds: float) -> float:
        """Estimate recall probability after the given elapsed time."""

    @abstractmethod
    def update(self, elapsed_seconds: float, remembered: bool) -> None:
        """Update model parameters from a recall outcome."""

    @abstractmethod
    def to_json(self) -> dict[str, Any]:
        """Serialize the model state."""

    @property
    @abstractmethod
    def model_type(self) -> str:
        """Stable identifier used for persistence."""

    def debug_payload(self) -> dict[str, Any]:
        return {"type": self.model_type}


@dataclass
class NaiveMemoryModel(MemoryModel):
    """
    P(success after t seconds) = exp(-a * t).

    The default rate is normalized so the predicted success after one day is 0.5.
    """

    FAILURE_SCALE = 0.5

    a: float = math.log(2.0) / SECONDS_PER_DAY

    @property
    def model_type(self) -> str:
        return "naive_exponential"

    def predict_success(self, elapsed_seconds: float) -> float:
        if elapsed_seconds < 0.0:
            raise ValueError("Elapsed time must be non-negative")
        return math.exp(-self.a * elapsed_seconds)

    def update(self, elapsed_seconds: float, remembered: bool) -> None:
        if elapsed_seconds < 0.0:
            raise ValueError("Elapsed time must be non-negative")
        if remembered:
            self.a += elapsed_seconds
            return
        self.a *= self.FAILURE_SCALE

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.model_type,
            "a": self.a,
        }

    def debug_payload(self) -> dict[str, Any]:
        return {
            "type": self.model_type,
            "a": self.a,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "NaiveMemoryModel":
        if not isinstance(payload, dict):
            raise TypeError("Naive memory model payload must be a dict")
        return cls(a=float(payload.get("a", math.log(2.0) / SECONDS_PER_DAY)))


def memory_model_from_json(payload: Any) -> MemoryModel:
    if payload is None:
        return NaiveMemoryModel()
    if not isinstance(payload, dict):
        raise TypeError("Memory model payload must be a dict")

    model_type = payload.get("type", "naive_exponential")
    if model_type == "naive_exponential":
        return NaiveMemoryModel.from_json(payload)
    raise ValueError(f"Unsupported memory model type: {model_type!r}")
