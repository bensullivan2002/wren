"""`wren login`, `wren logout` and `wren auth-status`: the ChatGPT sign-in,
from the terminal.

They're commands you run in a terminal rather than slash commands inside
Wren, like `codex login`: signing in opens a browser and waits for it, and
doing that from a plain command needs no screen of its own. Each returns
the exit status for `wren` to exit with.
"""

from __future__ import annotations

import sys
import webbrowser
from collections.abc import Callable
from datetime import datetime
from typing import TextIO

from wren.auth.chatgpt import (
    USAGE_SETTINGS_URL,
    ChatGPTAuthError,
    ChatGPTSession,
    Http,
    UrllibHttp,
    sign_in,
)
from wren.auth.store import (
    PLAN_SCOPE,
    Credentials,
    CredentialsError,
    CredentialStore,
    now_utc,
)

HOW_TO_USE = "Use it with:  wren --provider openai --auth chatgpt   (or set WREN_OPENAI_AUTH=chatgpt)"
SHARED_LIMITS = (
    "Requests count against your ChatGPT plan's usage limits, which are shared with ChatGPT "
    "and any other apps you've allowed: it isn't a separate allowance. "
    f"See and limit it at {USAGE_SETTINGS_URL}"
)


def login(
    out: TextIO | None = None,
    store: CredentialStore | None = None,
    http: Http | None = None,
    open_browser: Callable[[str], bool] = webbrowser.open,
) -> int:
    # sys.stdout looked up now, not when the module loaded, so whatever
    # stdout is at the time (a pipe, a test's capture) gets the output.
    out = out or sys.stdout
    store = store if store is not None else CredentialStore()

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        credentials = sign_in(
            store=store,
            http=http if http is not None else UrllibHttp(),
            open_browser=open_browser,
            say=say,
        )
    except KeyboardInterrupt:
        say("\nSigning in was cancelled. Nothing was saved.")
        return 130  # the usual exit status after Ctrl-C
    except ChatGPTAuthError as e:
        say(f"wren: {e}")
        return 1
    who = credentials.email or "your ChatGPT account"
    if credentials.plan_usage_allowed:
        say(f"Signed in with ChatGPT as {who}. Wren may use your ChatGPT plan.")
        say(SHARED_LIMITS)
        say(HOW_TO_USE)
    else:
        say(
            f"Signed in with ChatGPT as {who}, but without permission to use your ChatGPT plan, "
            "so Wren won't send requests with it. Run `wren login` again to allow it, "
            "or keep using an API key (--auth api-key)."
        )
    return 0


def logout(out: TextIO | None = None, session: ChatGPTSession | None = None) -> int:
    out = out or sys.stdout
    session = session if session is not None else ChatGPTSession()
    try:
        result = session.sign_out()
    except ChatGPTAuthError as e:
        print(f"wren: {e}", file=out)
        return 1
    if not result.had_tokens:
        print(
            "You weren't signed in with ChatGPT, so there was nothing to do.", file=out
        )
    elif result.revoked:
        print(
            "Signed out. OpenAI confirmed the session is over, and Wren has deleted its tokens.",
            file=out,
        )
    else:
        print(
            "Signed out on this computer: Wren has deleted its tokens. But OpenAI didn't "
            f"confirm the session is over ({result.problem}). To be sure, disconnect Wren "
            f"in ChatGPT's settings: {USAGE_SETTINGS_URL}",
            file=out,
        )
    return 0


def status(
    out: TextIO | None = None,
    store: CredentialStore | None = None,
    clock: Callable[[], datetime] = now_utc,
) -> int:
    """What's saved, read from disk. Nothing is sent: OpenAI's own view is
    checked by the next request."""
    out = out or sys.stdout
    store = store if store is not None else CredentialStore()
    try:
        saved = store.load()
    except CredentialsError as e:
        print(f"wren: {e}", file=out)
        return 1
    print(f"ChatGPT sign-in, kept in {store.path}", file=out)
    if saved is None:
        print("  Not signed in. Run `wren login` to sign in.", file=out)
        return 0
    for label, value in describe(saved, clock()):
        print(f"  {label + ':':<16}{value}", file=out)
    print(
        "Wren uses it only when started with --auth chatgpt. This is what's saved "
        "here; OpenAI's own view is checked by the next request.",
        file=out,
    )
    return 0


def describe(saved: Credentials, now: datetime) -> list[tuple[str, str]]:
    rows = [("Account", saved.email or "(no email given)")]
    if not saved.signed_in:
        rows.append(("Signed in", "no: signed out. `wren login` signs in again"))
    else:
        expires = saved.expires_at()
        if expires is None or expires <= now:
            validity = "access token expired; renewed on the next request"
        else:
            minutes = int((expires - now).total_seconds() // 60)
            validity = f"access token valid for {minutes} more minutes, then renewed automatically"
        rows.append(("Signed in", f"yes; {validity}"))
        rows.append(
            (
                "Plan usage",
                f"allowed ({PLAN_SCOPE})"
                if saved.plan_usage_allowed
                else "NOT allowed: `wren login` to allow it",
            )
        )
    rows.append(
        ("Registration", f"{saved.client_id} (issued to Wren for this account)")
    )
    rows.append(("This computer", saved.ext_agent_host_id))
    rows.append(("Usage & limits", USAGE_SETTINGS_URL))
    return rows
