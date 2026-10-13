"""Signing in to a provider with an account, instead of an API key.

There's one so far: Sign in with ChatGPT, which lets the OpenAI provider pay
for requests from your ChatGPT plan's allowance (chatgpt.py). This is
*authentication*, not a provider: the model, the API and Wren's agent loop
are the same as with an API key. What changes is the token on each request,
who is billed, and a few rules the requests must follow
(providers/openai_plan.py).

    store.py      where the sign-in is kept: ~/.wren/auth/, owner-only
    chatgpt.py    the sign-in itself (OAuth with PKCE), refresh, sign-out
    callback.py   the one-request web server the browser returns to
    cli.py        `wren login`, `wren logout`, `wren auth-status`

Nothing here is imported unless you use it, so Wren with an API key runs
none of it.
"""
