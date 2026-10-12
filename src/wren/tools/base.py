"""What a tool is, and the small pieces every tool shares.

A tool has two halves:

  * what the MODEL sees: a name, a description and a JSON Schema for its
    arguments (`spec()`). That's all the model ever knows about it, so the
    description is effectively a prompt: it decides when and how the model
    uses the tool.
  * what WREN runs: the `run()` method, ordinary Python.

Both halves need to agree on the arguments, so each tool declares them ONCE,
as a small Pydantic model called `Input`. From that one definition we get:

  * the JSON Schema sent to the model       (`Tool.spec`)
  * the checking of what the model sends    (`Tool.parse`)
  * a typed object for `run()` to work with (`args.path`, not `args["path"]`)

The model asks for a tool by name with some JSON arguments; the registry
parses them, calls `run()`, and sends the result back. See registry.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.json_schema import GenerateJsonSchema

from wren.messages import ToolSpec
from wren.tools.permissions import FileChange, PermissionRequest


class ToolError(Exception):
    """Raise this from a tool to report a problem to the model.

    The message is sent back as the tool's result (marked as an error), so
    write it for the model: say what went wrong and what to try instead.
    """


@dataclass
class ToolResult:
    content: str  # what the model sees
    summary: str = ""  # one short line for the person watching the terminal
    is_error: bool = False
    change: FileChange | None = None  # set by tools that changed a file, so a UI can show the diff


@dataclass
class ToolContext:
    """What a tool is allowed to know about its surroundings."""

    root: Path  # the working directory, fully resolved; tools must stay inside it

    # "Has the user asked us to stop?" A tool that can run for a long time
    # (the shell) checks this now and then and gives up early if so. The
    # default never says stop; a front end that can cancel replaces it.
    should_stop: Callable[[], bool] = lambda: False


class ToolInput(BaseModel):
    """Base class for every tool's arguments. It sets two rules for all of them.

    extra="forbid": an argument the tool doesn't have is an error, not
        something to ignore quietly. If the model sends `file` instead of
        `path`, it should hear about it.

    strict=True: no helpful conversions. Left to itself, Pydantic would turn
        "10" into 10 and True into 1. For a web form that's friendly. Here,
        a wrong type means the model has misread the schema, and it's better
        to say so than to guess what it meant.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class Tool:
    """Base class for tools. Subclasses set `name` and `description`, declare
    an `Input` model (unless they take no arguments), and implement run().
    Tools that change things also implement permission()."""

    name: ClassVar[str]
    description: ClassVar[str]
    Input: ClassVar[type[ToolInput]] = ToolInput  # the default: no arguments at all

    def spec(self) -> ToolSpec:
        """What the model is told about this tool."""
        return ToolSpec(self.name, self.description, schema_for(self.Input))

    def parse(self, args: Any) -> ToolInput:
        """Turn the model's raw JSON arguments into a checked `Input` object,
        with defaults filled in. Raises ToolError listing every problem at once."""
        if not isinstance(args, dict):
            raise ToolError(f"Tool arguments must be a JSON object, not {type(args).__name__}.")
        try:
            return self.Input.model_validate(args)
        except ValidationError as e:
            problems = "; ".join(_describe_problem(error) for error in e.errors())
            accepted = ", ".join(self.Input.model_fields) or "none"
            raise ToolError(f"Invalid arguments: {problems}. (Accepted arguments: {accepted}.)") from None

    def permission(self, args: Any, ctx: ToolContext) -> PermissionRequest | None:
        """Say what this call is about to change, so the user can be asked first.

        The default, None, means "nothing: this tool only reads". A tool that
        edits files or runs commands overrides this. It can also raise
        ToolError: if the call is bound to fail, there's no point asking the
        user to approve it.
        """
        return None

    def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        """Do the work. `args` is this tool's `Input`, already checked, and
        the user has already approved it if `permission()` asked for that.
        Raise ToolError for problems the model should hear about."""
        raise NotImplementedError


# --- From an Input model to a JSON Schema ----------------------------------


class _NoTitles(GenerateJsonSchema):
    """Pydantic adds a "title" to every field ("path" gets "Path"). The model
    already has the field's name, so that's just tokens we'd pay for on every
    request. This hook is Pydantic's own way to switch them off."""

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema the model sees for a tool's arguments."""
    schema = model.model_json_schema(schema_generator=_NoTitles)
    # The class's own name and docstring describe our Python, not the tool:
    # the tool's name and description are sent separately.
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


# --- From a Pydantic error to a sentence for the model ---------------------


def _describe_problem(error: Any) -> str:
    """One entry from `ValidationError.errors()`, reworded for the model.

    Each entry is a dict: `type` is a stable code ("missing", "int_type"...),
    `loc` is the path to the field, `msg` is Pydantic's wording, and `input`
    is the offending value.
    """
    name = ".".join(str(part) for part in error["loc"])
    if error["type"] == "missing":
        return f"missing required argument '{name}'"
    if error["type"] == "extra_forbidden":
        return f"unknown argument '{name}'"
    # Pydantic's messages all start "Input should be...", which reads well
    # after the argument's name. Showing the value helps the model spot
    # what it actually sent.
    value = repr(error["input"])
    if len(value) > 40:
        value = value[:39] + "…"
    return f"'{name}': {error['msg']} (got {value})"
