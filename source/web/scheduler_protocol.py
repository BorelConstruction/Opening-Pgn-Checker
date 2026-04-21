"""
Describes the high-level structure of a spaced repetition manager.
Components:
 - Scheduler: chooses the type of the next prompt, doesn't know chess
 - Generator: generates propmpts of the chosen type
 - SessionLog: keeps the history of comptleted prompts
 - Interpreter: process feedback and prompt response quality
 - RepetitionController: orchestrates

Core ideas:
 - Scheduler operates on SpecId (abstract "what to practice")
 - Generator produces concrete PromptId / prompt instances
 - Learning happens at the level of transitions (inside Generator / its state)
 - SessionLog is passive (records, does not decide)
 - Interpreter is pure (produces signals, no side effects)

 The workflow:
 - Scheduler -SpecId-> RepetitionController -SpecId-> Generator
 - Generator.start_generating() -RepetitionController-> 
 - On user response: 
    - RepetitionController -response-> Generator
    - RepetitionController -response-> Interpreter
    - Interpreter -Signal-> Generator
    - Generator updates move probabilities
    - Generator continues or terminates the prompt
 - On prompt end:
    - Generator -PromptId-> SessionLog
    - Interpreter -Feedback-> SessionLog
    - Interpreter -Feedback-> Scheduler
"""

from abc import ABC, abstractmethod
from typing import Callable, Protocol, TypeAlias, Hashable, Any


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

class Signal(Protocol):
    """Fine-grained signal produced per user action."""
    ...

class Scheduler(Protocol):
    """Chooses what to practice next (policy only)."""

    def next(self) -> SpecId:
        """Select next spec."""
        ...
    
    def feedback(self, spec_id: SpecId, feedback: Feedback) -> None:
        """Update scheduling policy based on completed prompt."""
        ...


class Interpreter(Protocol):
    """Pure component that assesses user responses.

    We don't call it "Evaluator" to avoid confusion with engine eval.

    Note that it does not only evaluate "quality" of the response --
    it also can return information useful for the generator.
    TODO: think of better design... It feels like "Interpreter" should not return
    Node objects, but who returns them then?
    """

    def interpret(self, response: Any) -> Signal:
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

    def start_prompt(self, spec_id: SpecId) -> Prompt:
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


class RepetitionController():
    """
    Orchestrates the spaced repetition loop, routes events.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        generator: Generator,
        interpreter: Interpreter,
        session_log: SessionLog,
        show_prompt: Callable[[Prompt], None],
    ):
        self.scheduler = scheduler
        self.generator = generator
        self.interpreter = interpreter
        self.session_log = session_log
        self.current_spec_id = None
        self.show_prompt = show_prompt

    def start_next_prompt(self) -> None:
        spec_id = self.scheduler.next()
        prompt = self.generator.start_prompt(spec_id)
        self.show_prompt(prompt)

    def on_user_response(self, response: Any) -> bool:
        """
        Process user response.
        Returns True if the prompt should continue, False if it should terminate."""
        signal = self.interpreter.interpret(response)

        prompt = self.generator.on_response(response, signal)

        if self.generator.is_finished():
            prompt_id = self.generator.current_prompt_id()
            spec_id = self.generator.current_spec_id()

            feedback = self.interpreter.summarize()

            self.session_log.record_prompt(prompt_id, spec_id)
            self.session_log.record_feedback(prompt_id, feedback)

            self.scheduler.feedback(spec_id, feedback)

            return False
        else:
            self.show_prompt(prompt)
            return True
