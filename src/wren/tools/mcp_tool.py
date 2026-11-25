"""MCP tools: a tool from an MCP server, dressed as a Wren tool.

Every other tool in this folder was written for Wren: its arguments are a
Pydantic model, and it knows what it changes, so it can ask exactly the
right question. A tool from an MCP server is the opposite. Wren knows only
what the server says about it: a name, a description, and a JSON Schema for
its arguments. It could read a web page or delete a database. So:

  * Every call asks (D140). The server may label a tool "read-only", but
    the MCP spec itself says to treat those labels as untrusted, and a
    label can't be checked. "Always" covers one tool from one server, for
    this session. Nothing about an MCP call is ever `contained`
    (ARCHITECTURE invariant 25): the server runs outside the sandbox.
  * The model explains each call (D141). Approving `{"owner": "x", "repo":
    "y", "title": "..."}` means reading JSON (D83), so every MCP tool gets
    one extra argument, `wren_reason`: a plain-English sentence that leads
    the permission question. It's the model's account, shown as such, with
    the real arguments underneath. Wren removes it before calling the
    server, which never sees it.
  * Arguments are checked against the server's schema before you're asked
    (D147), as the registry does for Wren's own tools: nobody should be
    asked to approve a call that's bound to be refused.

A tool's name tells the model and you where it came from:
`mcp__github__create_issue` is `create_issue` from the server you named
`github` (D142). It's the convention Claude Code uses too.

What comes back is passed to the model as text. Like a file's contents, it
is untrusted: a web page fetched by a server can contain instructions aimed
at the model. That's why calls ask, and why the model's own account of a
call is never shown as fact.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Protocol

from jsonschema import exceptions as schema_errors
from jsonschema import validators
from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from mcp.types import Tool as McpToolInfo

from wren.mcp_servers import McpCallError
from wren.messages import ToolSpec
from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.permissions import PermissionRequest

PREFIX = "mcp__"
REASON = "wren_reason"  # the argument Wren adds to every MCP tool, and removes again

# The providers accept tool names of up to 64 of these characters.
_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOT_NAME = re.compile(r"[^A-Za-z0-9_-]")

_REASON_SCHEMA = {
    "type": "string",
    "description": (
        "One plain-English sentence for the user: what this call will do and why. "
        "It is shown when asking the user's permission, and is not sent to the tool."
    ),
}


class Caller(Protocol):
    """What an McpTool needs from the running servers (McpServers, or a test's fake)."""

    def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        should_stop: Any = ...,
        timeout: float = ...,
    ) -> CallToolResult: ...


class McpCall(ToolInput):
    """A checked MCP call: the model's reason, and the arguments for the server."""

    reason: str
    arguments: dict[str, Any]


def wren_name(server: str, tool: str) -> str:
    """The name the model knows a server's tool by. Characters the providers
    won't take (MCP allows dots, for example) become underscores."""
    return f"{PREFIX}{server}__{_NOT_NAME.sub('_', tool)}"


class McpTool(Tool):
    Input = McpCall

    def __init__(self, server: str, info: McpToolInfo, caller: Caller) -> None:
        self.server = server
        self.info = info
        self.caller = caller
        self.tool_name = info.name  # what the server calls it
        self.name = wren_name(server, info.name)  # pyright: ignore[reportAttributeAccessIssue] - per instance, unlike Wren's own tools
        self.description = (info.description or info.title or info.name).strip()  # pyright: ignore[reportAttributeAccessIssue]
        self._schema = info.input_schema if isinstance(info.input_schema, dict) else {}
        self._validator = _validator_for(self._schema)

    def spec(self) -> ToolSpec:
        """The server's schema, plus `wren_reason`. The server's own words are
        passed on unchanged: they're the best guide the model has."""
        schema = copy.deepcopy(self._schema) or {"type": "object"}
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict) and REASON not in properties:
            properties[REASON] = _REASON_SCHEMA
            required = schema.get("required")
            schema["required"] = (
                [*required, REASON] if isinstance(required, list) else [REASON]
            )
        description = f"{self.description}\n\n(From the MCP server '{self.server}'.)"
        return ToolSpec(self.name, description, schema)

    def parse(self, args: Any) -> McpCall:
        if not isinstance(args, dict):
            raise ToolError(
                f"Tool arguments must be a JSON object, not {type(args).__name__}."
            )
        arguments = dict(args)
        reason = arguments.pop(REASON, None)
        if not isinstance(reason, str) or not reason.strip():
            raise ToolError(
                f"Missing required argument '{REASON}': one plain-English sentence saying what this call is for."
            )
        if self._validator is not None:
            problems = [
                _describe(error) for error in self._validator.iter_errors(arguments)
            ]
            if problems:
                raise ToolError(f"Invalid arguments: {'; '.join(problems[:5])}.")
        return McpCall(reason=reason.strip(), arguments=arguments)

    def permission(self, args: McpCall, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            title=f"Use the tool {self.tool_name} from the MCP server {self.server}",
            explanation=args.reason,
            detail=json.dumps(args.arguments, indent=2, ensure_ascii=False)
            if args.arguments
            else "(no arguments)",
            scope=f"mcp:{self.server}:{self.tool_name}",
            scope_label=f"the {self.tool_name} tool from the MCP server {self.server}",
        )

    def run(self, args: McpCall, ctx: ToolContext) -> ToolResult:
        try:
            result = self.caller.call(
                self.server, self.tool_name, args.arguments, ctx.should_stop
            )
        except McpCallError as e:
            raise ToolError(str(e)) from None
        text = render(result)
        if result.is_error:
            return ToolResult(text, summary=_first_line(text), is_error=True)
        return ToolResult(text, summary=_size(text))


def tools_for(
    server: str, infos: list[McpToolInfo], caller: Caller, taken: set[str]
) -> tuple[list[McpTool], list[str]]:
    """Wrap a server's tools, skipping any whose name the providers would
    refuse or that clashes with one already taken. Returns the tools, and
    a line for each one skipped."""
    tools: list[McpTool] = []
    skipped: list[str] = []
    for info in infos:
        name = wren_name(server, info.name)
        if not _NAME_OK.match(name):
            skipped.append(
                f"{server}: tool {info.name!r} skipped, its name is too long for the providers"
            )
        elif name in taken:
            skipped.append(
                f"{server}: tool {info.name!r} skipped, another tool is already called {name}"
            )
        else:
            taken.add(name)
            tools.append(McpTool(server, info, caller))
    return tools, skipped


# --- From an MCP result to text for the model ------------------------------


def render(result: CallToolResult) -> str:
    """A tool result as the text the model will read.

    MCP results are a list of content blocks: text, images, audio, links to
    resources, or resources themselves. Wren's messages carry text only
    (journal 08), so anything else is described rather than dropped
    silently, and the model knows something was there.
    """
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, (ImageContent, AudioContent)):
            kind = "image" if isinstance(block, ImageContent) else "audio"
            size = (
                len(block.data) * 3 // 4 // 1024
            )  # base64 is 4 characters per 3 bytes
            parts.append(
                f"[{kind}, {block.mime_type}, about {size} KB, not shown: Wren passes on text only]"
            )
        elif isinstance(block, ResourceLink):
            parts.append(f"[resource link: {block.name} {block.uri}]")
        elif isinstance(block, EmbeddedResource):
            resource = block.resource
            if isinstance(resource, TextResourceContents):
                parts.append(resource.text)
            else:
                parts.append(
                    f"[resource {resource.uri}, binary, not shown: Wren passes on text only]"
                )
    if not parts and result.structured_content is not None:
        # The spec says a tool returning structured content SHOULD repeat it
        # as text; for one that doesn't, the JSON is the text.
        parts.append(
            json.dumps(result.structured_content, indent=2, ensure_ascii=False)
        )
    return "\n".join(parts) if parts else "(The tool returned nothing.)"


def _validator_for(schema: dict[str, Any]) -> Any:
    """A JSON Schema validator for the server's schema, or None if the schema
    itself is broken (then the server is left to check its own arguments).
    The 2026-07-28 spec says schemas are JSON Schema 2020-12 unless they say
    otherwise with `$schema`, which `validator_for` honours."""
    if not schema:
        return None
    cls = validators.validator_for(schema, default=validators.Draft202012Validator)
    try:
        cls.check_schema(schema)
    except schema_errors.SchemaError:
        return None
    return cls(schema)


def _describe(error: Any) -> str:
    where = ".".join(str(part) for part in error.absolute_path)
    return f"'{where}': {error.message}" if where else error.message


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "error"
    return line if len(line) <= 100 else line[:99] + "…"


def _size(text: str) -> str:
    count = len(text.splitlines())
    return "no output" if count == 0 else "1 line" if count == 1 else f"{count} lines"
