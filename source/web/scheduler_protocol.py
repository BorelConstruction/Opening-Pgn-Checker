"""
Describes the high-level structure of a spaced repetition manager.
Components:
 - Scheduler: chooses the next abstract unit to practice
 - RepetitionEngine: builds prompts, interprets responses, and updates learning state
 - SessionLog: keeps the history of completed prompts
 - RepetitionController: orchestrates

Core ideas:
 - Scheduler operates on SpecId (abstract "what to practice")
 - RepetitionEngine produces concrete PromptId / prompt instances
 - Learning happens inside RepetitionEngine, where generation and user feedback meet
 - SessionLog is passive (records, does not decide)

The workflow:
 - Scheduler -SpecId-> RepetitionController -SpecId-> RepetitionEngine
 - On user response:
    - RepetitionController -response-> RepetitionEngine
    - RepetitionEngine updates move learning state
    - RepetitionEngine continues or terminates the prompt
 - On prompt end:
    - RepetitionEngine -PromptId-> SessionLog
    - RepetitionEngine -Feedback-> SessionLog
    - RepetitionEngine -Feedback-> Scheduler
"""

from typing import Any, Hashable, Protocol, TypeAlias


"""Abstract identifier of a schedulable unit (no chess knowledge)."""
SpecId: TypeAlias = Hashable

PromptId: TypeAlias = Hashable


class Prompt:
    ...


class Feedback:
    """
    Coarse-grained feedback for the scheduler (aggregated over a prompt).
    """

    def __init__(self, quality: float):
        self.quality = quality


class Scheduler(Protocol):
    """Chooses what to practice next (policy only)."""

    def next(self) -> SpecId:
        """Select next spec."""
        ...

    def feedback(self, spec_id: SpecId, feedback: Feedback) -> None:
        """Update scheduling policy based on completed prompt."""
        ...


class RepetitionEngine(Protocol):
    """
    Owns prompt generation, response interpretation, and learning state.
    """

    def start_prompt(self, spec_id: SpecId) -> Prompt:
        """Start generating a new prompt of given type."""
        ...

    def start_prompt_by_id(self, prompt_id: PromptId, spec_id: SpecId) -> Prompt:
        """Start a specific prompt identified outside the engine."""
        ...

    def on_response(self, response: Any) -> Prompt:
        """
        Process user response, update internal state, and advance or terminate the prompt.
        """
        ...

    def accept_pending_alternative(self) -> Prompt:
        """Accept the latest hidden alternative response and continue the prompt."""
        ...

    def summarize(self) -> Feedback:
        """Aggregate prompt-level feedback for the scheduler."""
        ...

    def is_finished(self) -> bool:
        """Whether current prompt is complete."""
        ...

    def current_prompt_id(self) -> PromptId:
        """Identifier of the current prompt."""
        ...

    def current_spec_id(self) -> SpecId:
        """Spec that produced the current prompt."""
        ...


class SessionLog(Protocol):
    """Passive storage of session history."""

    def record_prompt(self, prompt_id: PromptId, spec_id: SpecId) -> None:
        ...

    def record_feedback(self, prompt_id: PromptId, feedback: Feedback) -> None:
        ...


class RepetitionController:
    """
    Orchestrates the spaced repetition loop and routes events.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        engine: RepetitionEngine,
        session_log: SessionLog,
    ):
        self.scheduler = scheduler
        self.engine = engine
        self.session_log = session_log
        self.current_spec_id = None

    def get_prompt_view(self) -> Prompt:
        return self._prompt

    def start_next_prompt(self) -> None:
        spec_id = self.scheduler.next()
        self.current_spec_id = spec_id
        self._prompt = self.engine.start_prompt(spec_id)

    def start_prompt_by_id(self, prompt_id: PromptId, spec_id: SpecId | None = None) -> None:
        spec_id = spec_id or "by id"
        self.current_spec_id = spec_id
        self._prompt = self.engine.start_prompt_by_id(prompt_id, spec_id)

    def finalize_current_prompt(self) -> None:
        prompt_id = self.engine.current_prompt_id()
        spec_id = self.engine.current_spec_id()
        feedback = self.engine.summarize()

        self.session_log.record_prompt(prompt_id, spec_id)
        self.session_log.record_feedback(prompt_id, feedback)
        self.scheduler.feedback(spec_id, feedback)

    def on_user_response(self, response: Any) -> bool:
        """
        Process user response.
        Returns True if the prompt should continue, False if it should terminate.
        """
        self._prompt = self.engine.on_response(response)

        if not self.engine.is_finished():
            return True

        self.finalize_current_prompt()
        return False

    def accept_pending_alternative(self) -> bool:
        self._prompt = self.engine.accept_pending_alternative()

        if not self.engine.is_finished():
            return True

        self.finalize_current_prompt()
        return False
