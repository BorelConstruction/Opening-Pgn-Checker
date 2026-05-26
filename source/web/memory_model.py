from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from inspect import isabstract
import math
import time
from typing import Any, ClassVar, TypeVar


SECONDS_PER_DAY = 24.0 * 60.0 * 60.0
DELAY_BEFORE_GUESSED_MEANS_REMEMBERS = 60.0
MemoryModelT = TypeVar("MemoryModelT", bound="MemoryModel")


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
    def from_json(cls, payload: Any) -> PerformanceRecord:
        if not isinstance(payload, dict):
            raise TypeError("Performance record payload must be a dict")
        return cls(
            success=bool(payload["success"]),
            attempt_time=float(payload["attemptTime"]),
        )


class MemoryModel(ABC):
    _registry: ClassVar[dict[str, type[MemoryModel]]] = {}
    MODEL_TYPE: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if isabstract(cls):
            return

        model_type = getattr(cls, "MODEL_TYPE", None)
        if not isinstance(model_type, str) or not model_type:
            raise TypeError(f"{cls.__name__} must define a non-empty MODEL_TYPE string")
        if model_type in MemoryModel._registry:
            raise ValueError(f"Duplicate memory model type: {model_type!r}")
        MemoryModel._registry[model_type] = cls

    @abstractmethod
    def predict_success(self, past_performance: list[PerformanceRecord]) -> float:
        """Estimate recall probability after the given elapsed time."""

    @abstractmethod
    def update(self, remembered: bool, past_performance: list[PerformanceRecord]) -> None:
        """Update model parameters from a recall outcome.
        elapsed_seconds time since the last update. None if there isn't one.
        """

    def to_json(self) -> dict[str, Any]:
        payload = self.to_payload()
        if not isinstance(payload, dict):
            raise TypeError(f"{type(self).__name__}.to_payload() must return a dict")
        return {
            "type": type(self).MODEL_TYPE,
            **payload,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> MemoryModel:
        if not isinstance(payload, dict):
            raise TypeError("Memory model payload must be a dict")

        model_type = payload["type"]
        if not isinstance(model_type, str) or not model_type:
            raise TypeError("Memory model type must be a non-empty string")

        try:
            model_cls = cls._registry[model_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported memory model type: {model_type!r}") from exc

        model_payload = dict(payload)
        del model_payload["type"]
        return model_cls.from_payload(model_payload)

    @abstractmethod
    def to_payload(self) -> dict[str, Any]:
        """Serialize model-specific state without the type envelope."""

    @classmethod
    @abstractmethod
    def from_payload(cls: type[MemoryModelT], payload: dict[str, Any]) -> MemoryModelT:
        """Reconstruct model-specific state from payload data."""

    def debug_payload(self) -> dict[str, Any]:
        return self.to_json()


@dataclass
class NaiveMemoryModel(MemoryModel):
    """
    Let memory strength be exp(-lambda t), where lambda~Gamma(a, b) is updated bayesianly.
    Then P(success after t seconds) = (b/(b+t))^a.
    We'll set a=1.

    The default rate is normalized so the predicted success after one day is 0.5.
    """

    FAILURE_SCALE = 0.5
    DEFAULT_SUCCESS_PROBABILITY = 0.5
    MODEL_TYPE: ClassVar[str] = "naive_exponential"

    b: float = SECONDS_PER_DAY
    once_remembered: bool = False

    @staticmethod
    def _latest_success(past_performance: list[PerformanceRecord]) -> PerformanceRecord | None:
        return next((record for record in reversed(past_performance) if record.success), None)

    def predict_success(self, past_performance: list[PerformanceRecord]) -> float:
        # Derive remembered state from history so deserialized models still render
        # the evaluated probability instead of falling back to the default 0.5.
        previous_success = self._latest_success(past_performance)
        if previous_success is None:
            return self.DEFAULT_SUCCESS_PROBABILITY
        elapsed_seconds = time.time() - previous_success.attempt_time
        return (self.b/(self.b+elapsed_seconds))

    def update(self, remembered: bool, past_performance: list[PerformanceRecord]) -> None:
        elapsed_seconds = 0
        if remembered:
            previous_success = self._latest_success(past_performance)
            if previous_success:
                elapsed_seconds = time.time() - previous_success.attempt_time
            self.once_remembered = True
            self.b += elapsed_seconds
            return
        self.b *= self.FAILURE_SCALE

    def to_payload(self) -> dict[str, Any]:
        return {"b": self.b}

    @classmethod
    def from_payload(cls: type[MemoryModelT], payload: dict[str, Any]) -> MemoryModelT:
        return cls(b=float(payload["b"]))
