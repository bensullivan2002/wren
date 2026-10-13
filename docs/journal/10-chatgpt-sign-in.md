# 10 · Paying with a ChatGPT plan: signing in with OAuth

*Step 10 · October 2026 · a spike, from [issue #19](https://github.com/bensullivan2002/wren/issues/19)*

## What we built

Wren can now pay for OpenAI's models from your ChatGPT plan instead of an API key:

```text
$ wren login
Opening your browser to sign in with ChatGPT…
Waiting for the browser (up to 10 minutes; Ctrl-C to cancel)…
Signed in with ChatGPT as ben@example.com. Wren may use your ChatGPT plan.
Requests count against your ChatGPT plan's usage limits, which are shared with ChatGPT and any
other apps you've allowed: it isn't a separate allowance. See and limit it at https://chatgpt.com/settings/usage
Use it with:  wren --provider openai --auth chatgpt   (or set WREN_OPENAI_AUTH=chatgpt)

$ wren --provider openai --auth chatgpt
```

The footer says who's paying, next to the provider: `provider: openai (ChatGPT plan)`, or `(API key)`. `/usage` says it in a sentence, with the account. `wren auth-status` shows what's saved and `wren logout` ends it.

Everything else is unchanged: the same model, the same Responses API, the same translation in `providers/openai.py`, and above all **the same agent loop**. Wren still holds the conversation, runs the tools and asks the permission questions. All the sign-in gives Wren is a token to send with each request, a different bill, and a few rules the requests must follow.

| | |
| --- | --- |
| `auth/store.py` | where the sign-in is kept: `~/.wren/auth/`, owner-only |
| `auth/chatgpt.py` | the sign-in (OAuth with PKCE), checking the ID token, refreshing, signing out |
| `auth/callback.py` | the one-request web server your browser comes back to |
| `auth/cli.py` | `wren login`, `wren logout`, `wren auth-status` |
| `providers/openai_plan.py` | the plan route's request rules, model list and error messages |

## Idea 1: a way to pay is not a provider

The tempting design was a third provider, `chatgpt`, beside `anthropic` and `openai`. It would have been less code. But it would have tied two separate questions together: *which model answers* and *who pays for it*. The issue asked for them to stay separate, and the code shows why: on the plan route, `OpenAIProvider` sends the same items, reads the same events and builds the same `Completed`. So the route is a setting, `--auth api-key|chatgpt`, and the provider takes a `plan`:

```python
if plan is None:
    self.billing = "API key"
    self.client = client if client is not None else openai.OpenAI()
else:
    self.billing = "ChatGPT plan"
    # No API-key client at all on this route, so nothing in this
    # provider can quietly pay with one.
    self.client = None
```

That `self.client = None` is the most important line in the step. The issue's firmest rule is: **never silently fall back** to an API key, another provider or bought credits. The easiest way to keep a promise like that is to make breaking it impossible. On the plan route, the provider has no client that could spend from your API account.

The same rule decides where the route lives. It's chosen when Wren starts, by the flag or `WREN_OPENAI_AUTH`, and **only** there: not in `settings.json` with your saved model, and not by `/provider`. Switching provider has to rebuild OpenAI somehow, so the agent carries the route (`agent.auth`), and `switch_provider` passes it on:

```python
provider = make_provider(name, model, effort, auth=agent.auth)
```

If you started on the plan route and aren't signed in, `/provider openai` fails with "Run `wren login`", even with `OPENAI_API_KEY` set. A test checks exactly that.

## Idea 2: OAuth, or how a program acts for you without your password

You don't give Wren your ChatGPT password. You give it to OpenAI, in your own browser, and OpenAI gives Wren a **token**: a string that says "the holder may do *these things* for *this person* until *this time*". That's OAuth 2.0. For a program on your own computer it goes like this (RFC 8252):

1. Wren starts a tiny web server on `127.0.0.1` (`callback.py`).
2. Wren opens your browser at OpenAI with a request: who's asking (a client ID), what for (the **scopes**), and where to send you back (that server).
3. You sign in and approve, at OpenAI.
4. Your browser comes back to Wren's server, carrying a one-time **code**.
5. Wren swaps the code for tokens, in a direct request to OpenAI.

Why the detour through a code, rather than putting the tokens in the browser's address? Addresses leak: into browser history, other programs, logs. A code is useless on its own. That's also why the callback server switches off its usual habit of printing every request:

```python
def log_message(self, format: str, *args: object) -> None:  # noqa: A002
    # Normally every request is printed, address and all. The address
    # holds the authorization code, so nothing is printed.
    pass
```

## Idea 3: three random values, three attacks

Each `wren login` makes three random values (`LoginAttempt`), and each one defeats a specific attack:

| Value | Sent as | Stops |
| --- | --- | --- |
| PKCE verifier | only its SHA-256 hash, the *challenge* | another program that saw the code from swapping it for tokens: it doesn't have the verifier |
| state | a parameter that must come back unchanged | a web page sending your browser to Wren's server with a code of its own, signing you in as someone else |
| nonce | a parameter OpenAI copies into the ID token | an old ID token being replayed |

The state is checked before anything else in the answer is believed, the error included, and with `secrets.compare_digest`:

```python
if not secrets.compare_digest(
    answer.get("state", "").encode(), attempt.state.encode()
):
    raise SignInFailed("The browser's answer didn't belong to this sign-in ...")
```

`compare_digest` takes the same time however many characters match. An ordinary `==` stops at the first difference, and with enough attempts that timing can reveal a secret one character at a time.

PKCE has a worked example in its RFC, so the test checks Wren's arithmetic against it:

```python
verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
```

## Idea 4: believing an ID token

The token answer includes an **ID token**, which says who signed in. It's a JWT: three base64 parts, `header.payload.signature`. Anyone can decode the first two (try it at a Python prompt). Only the signature shows OpenAI wrote them. So `verify_id_token` fetches OpenAI's **published public keys**, picks the one the token names (`kid`), and has PyJWT check:

- the **signature**, against that key;
- the **issuer** is `https://auth.openai.com`;
- the **audience** is *Wren's* client ID (a token issued to another app doesn't count);
- it hasn't **expired**;

and then Wren compares the **nonce** itself.

One check deserves its own paragraph. A JWT's header names its signature algorithm, and a careless verifier lets it choose. An attacker then writes `"alg": "HS256"`, which is a *shared-secret* algorithm, and signs with OpenAI's public key as the "secret". Everyone has the public key, so anyone can make that token, and a verifier that obeys the header accepts it. Wren never lets the token choose:

```python
claims = jwt.decode(
    id_token,
    key=key,
    algorithms=[key.algorithm_name],   # the KEY's algorithm, from OpenAI's key set
    audience=client_id,
    issuer=ISSUER,
    ...
)
```

The test builds exactly that forgery by hand, and checks that it's refused.

## Idea 5: who you are is not what you allowed

Signing in proves who you are. Using your plan is a separate permission, the scope `chatgpt.tokens.use.direct`, and the consent screen lets you decline it. So a valid sign-in isn't enough. Wren reads the scopes OpenAI actually **granted** (which can be fewer than it asked for) and, if that one is missing, keeps the sign-in but makes no plan-usage requests:

```python
@property
def plan_usage_allowed(self) -> bool:
    return PLAN_SCOPE in self.scopes
```

If the answer doesn't list scopes at all, Wren assumes none were granted. A permission should fail closed.

## Idea 6: a token that works once

An access token lasts an hour. With it comes a **refresh token** (it lasts 30 days) that gets a new one. Each refresh returns a *replacement* refresh token and spends the old one: it **rotates**. That's good for security, since a stolen refresh token is soon useless. But it creates a race. If two Wrens (two terminals) both notice the token is stale and both refresh, one spends the token the other is about to use, and that one is signed out.

So refreshing happens one at a time, under a lock the operating system keeps on a file:

```python
with self.store.locked():
    # Another Wren may have refreshed while we waited for the lock:
    # its new tokens are on disk, and the refresh token we read
    # before may already be spent. So read again before deciding.
    credentials = self.check()
    if self._needs_refresh(credentials):
        credentials = self._refresh(credentials)
```

The lock is half of it. The other half is **reading again after getting it**. Without that, the second Wren would wait politely and then spend a dead token anyway. This pattern (check, lock, check again) is called double-checked locking. The test starts five threads at once and checks that exactly one refresh request is made, and that all five get the new token.

What a failed refresh means depends on why it failed. A dead refresh token (`invalid_grant`, `refresh_token_expired`, …) won't ever work, so Wren forgets the tokens and says to run `wren login`. A network fault or a 5xx is probably temporary: Wren keeps everything and says to try again. Forgetting a good sign-in because the Wi-Fi dropped would be its own kind of bug.

## Idea 7: keeping a secret on disk

The tokens are as good as a password while they last. `store.py` does four things about that:

- **Owner-only from the first byte.** `tempfile.mkstemp` creates the file with mode `0600`, and the folder is `0700`. Writing a file and *then* `chmod`-ing it leaves a moment when anyone could read it.
- **Atomic.** Write a side file, flush it to disk, then `os.replace` it over the old one. A rename is all or nothing, so a crash leaves the old record or the new one, never half of each. That matters more than usual here, because a refresh replaces the access token, the expiry, the scopes and the rotated refresh token together.
- **Never printed.** `field(repr=False)` on each token, so `print(credentials)`, a traceback or a test failure shows everything except them.
- **Out of the sandbox's reach.** `.wren/auth` joins `CREDENTIALS` in `tools/sandbox.py`. Whatever a command prints goes to the model, and the model sends it to the provider.

## Idea 8: the plan route's rules

The plan route accepts a narrower set of requests than the API-key route ("preview limitations" in OpenAI's docs), so `openai_plan.build_request` differs in four ways:

- **No `max_output_tokens`.** This route refuses it, so `--max-tokens` doesn't apply here.
- **Tools in a namespace.** A flat list of function tools is refused. They go inside one namespace, `wren`, and a tool call in the history must say so when it's sent back:

  ```python
  {**item, "namespace": NAMESPACE} if item.get("type") == "function_call" else item
  ```
- **`stream: true`, and the raw events.** On the API-key route the SDK's stream helper assembles the reply. Here the docs are explicit that success means a `response.completed` event, and that a usage limit can arrive as `response.failed` *after text has already streamed*. So `read_stream` reads the events itself and ends in one of five ways: completed, incomplete (a partial reply, kept), failed or error (raised), or nothing at all (the connection ended part-way: raised).
- **No retries.** The SDK quietly retries a 429 twice by default. Here a 429 means "you've hit your plan's limit": stop. `max_retries=0`.

`read_stream` is a generator that also *returns* a value, which `yield from` hands back:

```python
with client.responses.create(**request) as events:
    final = yield from openai_plan.read_stream(events)
```

Every `TextDelta` passes straight through to the agent as it arrives, and `final` is the finished `Response`, the generator's return value.

## Idea 9: errors that say what to do

OpenAI documents what each plan-usage error means, and Wren turns each into a sentence and a next step, keeping OpenAI's own facts underneath for looking up:

```text
error: You've reached a usage limit for using your ChatGPT plan in apps: your plan's, or one set
for Wren. Wait for it to reset, or check your limits at https://chatgpt.com/settings/usage
(HTTP 429; subscription_sharing_usage_limit_exceeded; request req_123) Wren didn't switch to an
API key or anything else: the request stopped.
```

The first line goes into the conversation's error note too (the agent keeps the first line), so the model knows why the turn stopped. A refusal before the request is even admitted can come with no code at all, just `{"detail": "..."}`. Wren shows that text as it is, under advice chosen by the HTTP status.

## Python worth knowing from this step

- **`dataclasses.field(repr=False)`** leaves a field out of the generated `__repr__`. Here it keeps tokens out of anything printed.
- **`@contextlib.contextmanager`** turns a generator into a `with` block: the code before `yield` is the setup, and the `finally` around it is the teardown (`CredentialStore.locked`).
- **`fcntl.flock`** is an advisory lock held on an open file. The OS releases it when the file is closed, or when the process dies, so a crash can't leave it stuck.
- **`tempfile.mkstemp` + `os.replace`** is the standard atomic write.
- **`secrets`** is for anything an attacker mustn't guess (`token_urlsafe`) or time (`compare_digest`). `random` is not.
- **`http.server`** is in the standard library. `handle_request()` serves one request, or returns after `timeout`, which makes a server you can stop with Ctrl-C.
- **A default argument is evaluated once, when the function is defined.** `def status(out=sys.stdout)` captured the stdout that existed at import time, so pytest's capture never saw the output. The fix is `out=None`, then `out = out or sys.stdout` inside the function. It's the same trap as the famous `def f(items=[])`.

## Known limits (on the list for later)

- **Untried with a real account.** Everything is tested against fakes built from OpenAI's docs and the SDK's types. Real sign-in, a streamed turn and a tool round trip need your browser and your ChatGPT account (see "Things to try").
- **One account.** No account picker; signing in as someone else is refused. To start over, delete `~/.wren/auth/chatgpt.json`.
- **No automatic retry.** A temporary failure (503, `…_unavailable`) stops the turn, and you send the message again. The docs suggest bounded backoff.
- **The sign-in is in a file, not the macOS Keychain.**
- **The model picker shows slugs** (`gpt-6-luna`), not the list's `display_name`.
- **`--max-tokens` does nothing on this route**, and the footer's cost shows `$?`: plan usage has no per-token price.
- **The loopback callback needs a browser on the same machine**, so it won't work over SSH.
- **`/login` inside the app** isn't there yet: `wren login` is a terminal command.
- **The `earliest_refresh_at` format isn't documented.** Wren honours it when it's a number (Unix seconds) and otherwise ignores it.

## Things to try

1. `wren login`, then `wren auth-status`. Check the account, "Plan usage: allowed", and that no token is printed.
2. `wren --provider openai --auth chatgpt`, then a short message. Check the footer says `(ChatGPT plan)` and `/usage` names your account.
3. Ask for something that needs a tool ("list the Python files"). This checks the namespace round trip, the part most likely to be wrong: a tool call that arrives as `wren.list_files`, or one refused on the second request, would show up here.
4. `/model`: the list should be your account's models, in OpenAI's order.
5. Two Wrens at once for over an hour (or edit `saved_at` in the file to make the token stale): both should keep working, with one refresh between them.
6. `wren logout`: it should say OpenAI confirmed it. Then `wren --auth chatgpt …` should refuse to start, and `wren login` should come back without the consent screen.
7. Decline the plan permission on the consent screen: `wren login` should say so, and `wren auth-status` should show "NOT allowed". Then `wren login` again: the consent screen should come back.
8. `cat ~/.wren/auth/chatgpt.json` in Wren (a sandboxed command): it should be refused.

## What's next

Once you've tried it, the follow-ups above can become issues. Then step 11: extensions (MCP, subagents, hooks).
