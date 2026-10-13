"""The ChatGPT plan route: OpenAI's models, paid for by your ChatGPT plan.

Signing in with ChatGPT (auth/chatgpt.py) gives Wren an access token that
can be spent against your plan's allowance instead of an API account. It's
the same model behind the same Responses API, so the translation in
openai.py is shared. What changes is a set of rules this route sets, from
OpenAI's "preview limitations" (checked 13 October 2026):

    API key route                         ChatGPT plan route
    ---------------------------------     ------------------------------------------
    key from OPENAI_API_KEY               a fresh OAuth access token, every request
    billed to your API account            counted against your ChatGPT plan's limits
    tools: a flat list of functions       tools: grouped in a namespace (refused flat)
    max_output_tokens sent                max_output_tokens refused, so not sent
    the SDK's stream helper               the raw events: done means response.completed
    the SDK retries 429s and 5xx          no retries: a limit stops the request
    models.list(): {"data": [...]}        GET /models: {"models": [...]}, own order

Both keep the history themselves (`store: false`) and send all of it each
time, which this route requires anyway.

The one rule that matters most isn't in the table: a failure here STOPS the
request. Nothing retries it with your API key, another provider, or bought
credits. The error says what happened, in words, with the code, HTTP status
and request ID OpenAI sent, so it can be looked up.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import Any

import openai

from wren.messages import Message, TextDelta, ToolSpec

BASE_URL = "https://api.openai.com/v1"  # the public API; never ChatGPT's own backend
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"
# This route takes function tools only inside a namespace. One namespace
# holds them all: a tool call comes back as (namespace "wren", name
# "read_file"), and must be sent back in the history the same way.
NAMESPACE = "wren"
NAMESPACE_DESCRIPTION = "Wren's tools, for working in the user's project."


def make_client(access_token: str) -> openai.OpenAI:
    """An SDK client for one request, carrying the access token.

    The SDK sends `api_key` as "Authorization: Bearer ...", which is what
    an OAuth access token needs. `base_url` is given, not taken from
    OPENAI_BASE_URL, because this route works only on the public API. And
    max_retries=0: the SDK would otherwise retry a 429 by itself, but a
    usage limit here means stop."""
    return openai.OpenAI(api_key=access_token, base_url=BASE_URL, max_retries=0)


def build_request(
    *,
    model: str,
    effort: str | None,
    system: str,
    messages: list[Message],
    tools: Sequence[ToolSpec],
) -> dict[str, Any]:
    from wren.providers.openai import to_api_tools, to_input_items

    request: dict[str, Any] = {
        "model": model,
        "input": [
            # A tool call in the history names its namespace, as it came.
            {**item, "namespace": NAMESPACE}
            if item.get("type") == "function_call"
            else item
            for item in to_input_items(messages)
        ],
        # Both required on this route, for every request.
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        # No max_output_tokens: this route refuses it. The model's own
        # limit applies, and Wren's --max-tokens doesn't.
    }
    if system:
        request["instructions"] = system
    if tools:
        request["tools"] = [
            {
                "type": "namespace",
                "name": NAMESPACE,
                "description": NAMESPACE_DESCRIPTION,
                "tools": to_api_tools(tools),
            }
        ]
    if effort:
        request["reasoning"] = {"effort": effort}
    return request


def read_stream(events: Any) -> Generator[TextDelta, None, Any]:
    """Yield the reply's text as it arrives; return the finished Response.

    A stream ends in exactly one of these, and only the first two are a
    reply:

        response.completed    finished
        response.incomplete   stopped early (it says why): a partial reply
        response.failed       failed: a usage limit can arrive this way,
                              after text has already streamed
        error                 failed
        (nothing)             the connection ended part-way

    The SDK raises some failures itself, as openai.APIError: see `explain`.
    """
    final = None
    for event in events:
        kind = getattr(event, "type", None)
        if kind == "response.output_text.delta":
            yield TextDelta(event.delta)
        elif kind in ("response.completed", "response.incomplete"):
            final = event.response
        elif kind == "response.failed":
            error = getattr(event.response, "error", None)
            raise PlanRequestError.from_code(
                code=getattr(error, "code", None),
                message=getattr(error, "message", None),
            )
        elif kind == "error":
            raise PlanRequestError.from_code(
                code=getattr(event, "code", None),
                message=getattr(event, "message", None),
                param=getattr(event, "param", None),
            )
    if final is None:
        raise PlanRequestError(
            "The reply stopped before it finished: the stream ended without saying it was "
            "complete. Nothing was retried; send your message again to try again."
        )
    return final


def list_models(client: Any) -> list[str]:
    """The models your ChatGPT account may use, in OpenAI's order.

    This route's model list isn't the usual one. Each entry has a `slug`
    (what to send as `model`) and a `visibility`; only "list" ones are
    meant to be offered."""
    try:
        data = client.get("/models", cast_to=object)  # the JSON, as it came
    except openai.APIError as e:
        raise explain(e) from e
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise PlanRequestError(
            "OpenAI's list of models for your ChatGPT account wasn't in the expected form. "
            "You can still choose one by name: /model NAME"
        )
    return [
        m["slug"]
        for m in models
        if isinstance(m, dict)
        and m.get("visibility") == "list"
        and isinstance(m.get("slug"), str)
    ]


# --- When it goes wrong ------------------------------------------------------------

# What each documented error code means, in words, and what to do. From
# OpenAI's "Errors and recovery" page, checked 13 October 2026. Not a full
# list: anything else is shown with OpenAI's own message.
ADVICE = {
    "subscription_sharing_user_not_eligible": (
        "Your ChatGPT account or workspace can't use its plan in other apps. "
        "Trying again won't change that."
    ),
    "subscription_sharing_usage_limit_exceeded": (
        "You've reached a usage limit for using your ChatGPT plan in apps: your plan's, "
        "or one set for Wren. Wait for it to reset, or check your limits at "
        f"{USAGE_SETTINGS_URL}"
    ),
    "subscription_sharing_usage_unavailable": (
        "ChatGPT couldn't check your remaining usage just now. Your sign-in is kept; "
        "try again in a little while."
    ),
    "subscription_sharing_unsupported_capability": (
        "This request used something the ChatGPT plan route doesn't support{param}. "
        "Sending it again unchanged won't help."
    ),
    "subscription_sharing_route_not_supported": (
        "The ChatGPT plan route doesn't accept this kind of request. This is a bug in Wren."
    ),
    "subscription_sharing_invalid_user": (
        "ChatGPT couldn't validate your account for this request. "
        "Run `wren auth-status`, and `wren login` to sign in again."
    ),
    "chatpass_v2_scope_not_authorized": (
        "Your sign-in doesn't permit this. Run `wren auth-status` to check plan usage is "
        "allowed, and `wren login` to allow it."
    ),
    "chatpass_v2_invalid_authorization_context": (
        "Your sign-in doesn't permit this. Run `wren auth-status` to check plan usage is "
        "allowed, and `wren login` to allow it."
    ),
    "subscription_sharing_user_unavailable": (
        "ChatGPT couldn't look up your account just now. Your sign-in is kept; "
        "try again in a little while."
    ),
}

# Before a request is even admitted, the route can refuse it with only a
# status and a {"detail": "..."} body: no code. What those statuses mean.
ADMISSION_ADVICE = {
    401: (
        "ChatGPT didn't accept your sign-in for plan usage. "
        "Run `wren auth-status`, and `wren login` to sign in again."
    ),
    403: (
        "ChatGPT plan usage was refused by a policy or permission check, such as "
        "the region you're in."
    ),
    503: (
        "ChatGPT plan usage isn't available right now. Your sign-in is kept; "
        "try again in a little while."
    ),
}

NO_FALLBACK = "Wren didn't switch to an API key or anything else: the request stopped."


class PlanRequestError(Exception):
    """A request on the ChatGPT plan route failed, and stopped. The first
    line says what happened, for the conversation's error note; the second
    gives OpenAI's own facts, for looking it up."""

    def __init__(
        self,
        advice: str,
        *,
        status: int | None = None,
        code: str | None = None,
        param: str | None = None,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.status, self.code, self.param, self.request_id = (
            status,
            code,
            param,
            request_id,
        )
        facts = [
            f"HTTP {status}" if status else "",
            code or "",
            f"param {param}" if param else "",
            f"request {request_id}" if request_id else "",
        ]
        said = "; ".join(fact for fact in facts if fact)
        lines = [advice]
        if detail:
            lines.append(f"OpenAI said: {detail}")
        lines.append((f"({said}) " if said else "") + NO_FALLBACK)
        super().__init__("\n".join(lines))

    @classmethod
    def from_code(
        cls,
        *,
        code: str | None,
        message: str | None = None,
        param: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> PlanRequestError:
        if code in ADVICE:
            advice = ADVICE[code].format(param=f" ({param})" if param else "")
            detail = None
        elif code is None and status in ADMISSION_ADVICE:
            advice, detail = ADMISSION_ADVICE[status], message
        else:
            advice = "The ChatGPT plan route refused this request."
            detail = message
        return cls(
            advice,
            status=status,
            code=code,
            param=param,
            request_id=request_id,
            detail=detail,
        )


def explain(error: openai.APIError) -> PlanRequestError:
    """An SDK error, as a PlanRequestError that says what to do."""
    if isinstance(error, openai.APIConnectionError):
        return PlanRequestError(
            f"Couldn't reach OpenAI ({error.message}). Nothing was retried."
        )
    body = error.body if isinstance(error.body, dict) else {}
    # The admission check's {"detail": "..."} is text to show, not a code.
    detail = body.get("detail") if isinstance(body.get("detail"), str) else None
    message = detail or (
        body.get("message") if isinstance(body.get("message"), str) else None
    )
    return PlanRequestError.from_code(
        code=error.code,
        message=message,
        param=error.param,
        status=getattr(error, "status_code", None),
        request_id=getattr(error, "request_id", None),
    )
