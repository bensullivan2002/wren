"""Wren's own, provider-neutral conversation types.

Every model provider (Anthropic, OpenAI, a local model...) has its own JSON
shape for messages. The rest of Wren never sees those shapes: it only works
with the small dataclasses in this file. Each provider translates to and from
them at its boundary. That translation is what makes the model "swappable".

The structure mirrors how modern chat APIs think about a conversation:

    conversation = list[Message]
    Message      = a role ("user" or "assistant") + a list of content blocks
    Block        = TextBlock | ToolUseBlock | ToolResultBlock

Tool blocks were defined in step 1, before anything used them, because they
decide the *shape* of a message (content is a list of blocks, not a string),
and shapes are the most expensive thing to change later. Step 2 put them to work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

Role = Literal["user", "assistant"]


# --- Content blocks -------------------------------------------------------


@dataclass
class TextBlock:
    """Plain text, written by either the user or the model."""

    text: str


@dataclass
class ToolUseBlock:
    """The model asking the harness to run a tool."""

    id: str  # unique id, so the result can be matched back to this request
    name: str  # which tool
    input: dict[str, Any]  # the tool's arguments, as JSON


@dataclass
class ToolResultBlock:
    """The harness's answer to a ToolUseBlock, sent back in the next *user* message."""

    tool_use_id: str
    content: str
    is_error: bool = False


Block = Union[TextBlock, ToolUseBlock, ToolResultBlock]


# --- Messages -------------------------------------------------------------


@dataclass
class Message:
    role: Role
    content: list[Block]

    @classmethod
    def user(cls, text: str) -> Message:
        return cls("user", [TextBlock(text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls("assistant", [TextBlock(text)])

    @property
    def text(self) -> str:
        """All the text in this message, ignoring non-text blocks."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    @property
    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.content if isinstance(b, ToolResultBlock)]


def history_problems(messages: list[Message]) -> list[str]:
    """Check a conversation against the rules a model API expects between turns.

    Returns a list of problems (empty means valid). The agent's job is to make
    sure this is always empty after a turn; the tests hold it to that.
    """
    problems = []
    if not messages:
        return problems
    if messages[0].role != "user":
        problems.append("the conversation must start with a user message")
    if messages[-1].role != "assistant":
        problems.append("between turns, the conversation must end with an assistant message")
    for i, message in enumerate(messages):
        if not message.content:
            problems.append(f"message {i} has no content")
        if i and message.role == messages[i - 1].role:
            problems.append(f"messages {i - 1} and {i} are both from the {message.role}")
        asked = [b.id for b in message.tool_uses]
        if asked:
            following = messages[i + 1] if i + 1 < len(messages) else None
            answered = [b.tool_use_id for b in following.tool_results] if following else []
            if sorted(asked) != sorted(answered):
                problems.append(f"message {i} asks for tools {asked} but the next message answers {answered}")
    return problems


# --- Describing a tool to the model ----------------------------------------


@dataclass
class ToolSpec:
    """Everything the model is told about a tool. It never sees our Python, only this."""

    name: str
    description: str
    input_schema: dict[str, Any]  # a JSON Schema describing the tool's arguments


# --- Why the model stopped, and what it cost ------------------------------

# "end_turn"   the model finished its reply normally
# "max_tokens" it ran out of room (our max_tokens setting) mid-reply
# "tool_use"   it wants us to run a tool and send back the result
# "refusal"    it declined to continue
# "other"      anything else a provider might report
StopReason = Literal["end_turn", "max_tokens", "tool_use", "stop_sequence", "refusal", "other"]


@dataclass
class Usage:
    """Token counts. Input tokens = everything we sent; output = what the model wrote."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


# --- Streaming events -----------------------------------------------------
#
# A provider doesn't return a reply all at once: it *yields* events while the
# model is still generating, so the terminal can show words as they arrive.
# The contract every provider follows: zero or more TextDelta, then exactly
# one Completed, which is always the last event.


@dataclass
class TextDelta:
    """A fragment of the reply, to print as soon as it arrives."""

    text: str


@dataclass
class Completed:
    """The final event: the whole assistant message, assembled, plus metadata."""

    message: Message
    stop_reason: StopReason
    usage: Usage


StreamEvent = Union[TextDelta, Completed]
