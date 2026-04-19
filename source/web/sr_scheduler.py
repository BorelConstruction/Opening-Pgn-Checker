"""
Describes the high-level structure of a spaced repetition manager.
Components:
 - Scheduler: chooses the type of the next prompt, doesn't know chess
 - Generator: generates propmpts of the chosen type
 - SessionLog: keeps the history of comptleted prompts
 - Grader: process feedback and prompt response quality
 - LearningSessionController: orchestrates

Core ideas:
 - Scheduler operates on SpecId (abstract "what to practice")
 - Generator produces concrete PromptId / prompt instances
 - Learning happens at the level of transitions (inside Generator / its state)
 - SessionLog is passive (records, does not decide)
 - Grader is pure (produces signals, no side effects)

 The workflow:
 - Scheduler -SpecId-> SessionController -SpecId-> Generator
 - Generator.start_generating() -SessionController-> 
 - On user response: 
    - SessionController -response-> Generator
    - SessionController -response-> Grader
    - Grader -Signal-> Generator
    - Generator updates move probabilities
    - Generator continues or terminates the prompt
 - On prompt end:
    - Generator -PromptId-> SessionLog
    - Grader -Feedback-> SessionLog
    - Grader -Feedback-> Scheduler
"""

from abc import ABC, abstractmethod
from typing import Protocol, TypeAlias, Hashable, Any


"""Abstract identifier of a schedulable unit (no chess knowledge)."""
SpecId: TypeAlias = Hashable

PromptId: TypeAlias = Hashable

class Scheduler(Protocol):
    """Chooses what to practice next (policy only)."""

    def next(self) -> SpecId:
        """Select next spec."""
        ...
    
    def feedback(self) -> None:
        """Update scheduling policy based on completed prompt."""
        ...
    
class Feedback:
    """
    Coarse-grained feedback for the scheduler (aggregated over a prompt).
    """
    def __init__(self, quality: float):
        self.quality = quality

class Signal(Protocol):
    """Fine-grained signal produced per user action."""
    ...

class Grader(Protocol):
    """Pure component that assesses user responses.

    We don't call it "Evaluator" to avoid confusion with engine eval.

    Note that it does not only evaluate "quality" of the response --
    it also can return information useful for the generator.
    TODO: think of better design... It feels like "Grader" should not return
    Node objects, but who returns them then?
    """

    def evaluate(self, response: Any) -> Signal:
        ...

    def summarize(self) -> Feedback:
        """Aggregate signals over the prompt."""

class Generator(Protocol):
    """
    Produces and manages prompts (domain-specific, e.g. chess).

    Owns:
    - step-wise generation
    - internal learning state (e.g. edge weights)
    - prompt lifecycle
    """

    def start_prompt(self, spec_id: SpecId) -> None:
        """Start generating a new prompt of given type."""
        ...
    def on_response(self, response: Any) -> None:
        """
        Process user response.
        Should:
        - update internal state (e.g. edge stats)
        - advance or terminate prompt
        """
        ...

    def is_finished(self) -> bool:
        """Whether current prompt is complete."""
        ...

    def current_prompt_id(self) -> PromptId:
        """Identifier of the current prompt."""
        ...

    def get_spec_id(self) -> SpecId:
        """Spec that produced current prompt."""
        ...



class SessionLog(Protocol):
    """Passive storage of session history."""

    def record_prompt(self, prompt_id: PromptId, spec_id: SpecId) -> None:
        ...

    def record_feedback(self, prompt_id: PromptId, feedback: Feedback) -> None:
        ...


class Controller:
    """
    Orchestrates the spaced repetition loop, routes events.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        generator: Generator,
        evaluator: Grader,
        session_log: SessionLog,
    ):
        self.scheduler = scheduler
        self.generator = generator
        self.evaluator = evaluator
        self.session_log = session_log
        self.current_spec_id = None

    def start_next_prompt(self) -> None:
        spec_id = self.scheduler.next()
        self.generator.start_prompt(spec_id)

    def on_user_response(self, response: Any) -> None:
        signal = self.evaluator.evaluate(response)

        self.generator.on_response(response, signal)

        if self.generator.is_finished():
            prompt_id = self.generator.current_prompt_id()
            spec_id = self.generator.get_spec_id()

            feedback = self.evaluator.summarize()

            self.session_log.record_prompt(prompt_id, spec_id)
            self.session_log.record_feedback(prompt_id, feedback)

            self.scheduler.feedback(spec_id, feedback)
