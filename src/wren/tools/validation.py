"""Checking the model's tool arguments against the tool's JSON Schema.

Why check at all? The model *usually* sends arguments that match the schema,
but not always: it can leave out a required field, misspell a name, send a
string where a number belongs, or get cut off mid-request. A clear error
message lets it notice and try again, instead of the tool crashing on a
KeyError.

This is a deliberately tiny, hand-written subset of JSON Schema: just enough
for Wren's own schemas (an object of simple typed fields, with required
fields, defaults and minimums). It exists so you can see exactly what
validation involves. Step 3 replaces this whole file with Pydantic models,
which generate the schema *and* do the checking from one definition.
"""

from __future__ import annotations

from typing import Any

from wren.tools.base import ToolError

_PYTHON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(args: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Return the arguments with defaults filled in, or raise ToolError listing every problem."""
    if not isinstance(args, dict):
        raise ToolError(f"Tool arguments must be a JSON object, not {_json_type(args)}.")

    properties: dict[str, Any] = schema.get("properties", {})
    problems = []

    for name in schema.get("required", []):
        if name not in args:
            problems.append(f"missing required argument '{name}'")

    for name, value in args.items():
        if name not in properties:
            problems.append(f"unknown argument '{name}'")
            continue
        rules = properties[name]
        expected = rules.get("type")
        if expected and not _is_type(value, expected):
            problems.append(f"'{name}' must be {_a(expected)}, not {_json_type(value)}")
        elif "minimum" in rules and value < rules["minimum"]:
            problems.append(f"'{name}' must be at least {rules['minimum']}")

    if problems:
        accepted = ", ".join(properties) or "none"
        raise ToolError(f"Invalid arguments: {'; '.join(problems)}. (Accepted arguments: {accepted}.)")

    with_defaults = {name: rules["default"] for name, rules in properties.items() if "default" in rules}
    with_defaults.update(args)
    return with_defaults


def _is_type(value: Any, expected: str) -> bool:
    # A Python quirk: True and False are also ints (True == 1). Without this
    # check, {"limit": true} would pass as an integer.
    if isinstance(value, bool):
        return expected == "boolean"
    return isinstance(value, _PYTHON_TYPES[expected])


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    for name, python_type in _PYTHON_TYPES.items():
        if isinstance(value, python_type):
            return _a(name)
    return type(value).__name__


def _a(type_name: str) -> str:
    return f"an {type_name}" if type_name[0] in "aeiou" else f"a {type_name}"
