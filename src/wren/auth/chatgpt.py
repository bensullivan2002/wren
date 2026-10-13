"""Sign in with ChatGPT: let Wren pay for OpenAI requests from your ChatGPT plan.

This is OAuth 2.0, the standard way for one program to act for you at a
service without ever seeing your password. The shape of it:

    wren login
      1. start a web server on 127.0.0.1 (callback.py)
      2. open your browser at OpenAI, with a request saying who's asking
         (Wren), what for (the scopes), and where to come back to
      3. you sign in and approve, at OpenAI; Wren never sees your password
      4. the browser comes back to our server with a one-time CODE
      5. Wren swaps the code for TOKENS (a direct request to OpenAI)
      6. Wren checks the ID token's signature, then saves everything (store.py)

    every request, afterwards
      the ACCESS TOKEN goes in the Authorization header. It lasts an hour;
      the REFRESH TOKEN gets a new one (and a new refresh token) when needed.

    wren logout
      ask OpenAI to end the session (revoke the refresh token), then forget
      the tokens

Each defence below exists because of a specific attack:

  * PKCE (the verifier and challenge). The code passes through the browser,
    so something else on your computer could see it. To swap it for tokens
    you also need the verifier, which never left Wren: OpenAI only ever saw
    its SHA-256 hash.
  * STATE. Stops a web page from sending your browser to our server with a
    code of its own (signing you in to someone else's account). The answer
    must carry the random state we sent, or it's ignored.
  * NONCE, and checking the ID TOKEN. The ID token says who signed in, signed
    by OpenAI. We check that signature against OpenAI's published keys, that
    it was issued by OpenAI, for Wren's client ID, hasn't expired, and
    carries the nonce we sent: so it can't be forged or replayed.
  * SCOPES. Signing in proves who you are. Using your plan is a separate
    permission, `chatgpt.tokens.use.direct`, and you can decline it. Without
    it, Wren keeps the sign-in but makes no plan-usage requests.

The registration. The first sign-in REGISTERS Wren with your account: it
asks as "dynamic_agent_client", and OpenAI issues a client ID of Wren's own
(oaiapp_...), which every later sign-in, refresh and sign-out uses. The
host ID (store.py) says which computer is asking.

Refreshing is serialised across processes (store.locked) because each
refresh token works once. And nothing here ever falls back to anything:
when the sign-in can't be used, the request stops and says why.

The protocol details (endpoints, parameters, error codes) are from OpenAI's
"Sign in with ChatGPT" docs for open-source apps, checked 13 October 2026:
https://developers.openai.com/siwc/token-sharing-open-source
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

import jwt

from wren.auth.callback import PREFERRED_PORT, CallbackServer
from wren.auth.store import (
    PLAN_SCOPE,
    Credentials,
    CredentialsError,
    CredentialStore,
    now_utc,
)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"  # what the tokens are for
REGISTER = "dynamic_agent_client"  # the client ID for a FIRST sign-in only
AGENT_NAME = "Wren"  # shown to you at OpenAI; you can rename it there
SCOPES = (
    # Who you are:
    "openid",
    "profile",
    "email",
    # Using your plan: a refresh token, the API, and the plan itself.
    "offline_access",
    "resource.invoke",
    PLAN_SCOPE,
)
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"

# Refresh this long before the access token runs out, so a request that
# starts just before expiry doesn't fail half-way.
REFRESH_MARGIN = timedelta(minutes=5)
# How far apart our clock and OpenAI's may be when checking token times.
CLOCK_LEEWAY = 60  # seconds
# Signature algorithms accepted on an ID token. Only public-key ones: an
# "HS256" token is signed with a shared secret, which a public client like
# Wren doesn't have, and accepting one is a known way to forge tokens.
SIGNATURE_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384")
# Refresh errors that mean the refresh token will never work again.
REFRESH_TOKEN_DEAD = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


# --- What can go wrong --------------------------------------------------------
#
# Every message is written for you, the person at the terminal, and says what
# to do next. None of them contains a token or a code.


class ChatGPTAuthError(Exception):
    """The ChatGPT sign-in stopped something. The message says why, and what to do."""


class SignInRequired(ChatGPTAuthError):
    """There's no usable sign-in: `wren login` is the way on."""


class PlanUsageDisabled(ChatGPTAuthError):
    """Signed in, but without permission to use the ChatGPT plan."""


class SignInFailed(ChatGPTAuthError):
    """A `wren login` attempt didn't work. Nothing was saved or replaced."""


class AuthUnavailable(ChatGPTAuthError):
    """OpenAI couldn't be reached, or had a temporary fault. The sign-in is kept."""


NOT_SIGNED_IN = (
    "You're not signed in with ChatGPT. Run `wren login`, "
    "or use your API key instead: --auth api-key."
)
PLAN_NOT_ALLOWED = (
    "You're signed in with ChatGPT, but you haven't allowed Wren to use your ChatGPT plan, "
    "so Wren won't send requests with it. Run `wren login` to allow it, or use your API key: --auth api-key."
)


# --- Talking to OpenAI's sign-in service ---------------------------------------
#
# A handful of small JSON requests, made with the standard library rather
# than an HTTP package. `Http` is the seam the tests replace.


@dataclass
class HttpResponse:
    status: int
    body: Any  # the parsed JSON, or None if it wasn't JSON


class Http(Protocol):
    def get(self, url: str) -> HttpResponse: ...
    def post_form(self, url: str, form: dict[str, str]) -> HttpResponse: ...


class UrllibHttp:
    timeout = 30  # seconds

    def get(self, url: str) -> HttpResponse:
        return self._send(url, None, _ACCEPT_JSON)

    def post_form(self, url: str, form: dict[str, str]) -> HttpResponse:
        # Form-encoded, as OAuth's token and revocation endpoints expect.
        # Tokens go in the body, never in the address.
        data = urlencode(form).encode()
        headers = {**_ACCEPT_JSON, "Content-Type": "application/x-www-form-urlencoded"}
        return self._send(url, data, headers)

    def _send(
        self, url: str, data: bytes | None, headers: dict[str, str]
    ) -> HttpResponse:
        host = urlsplit(url).hostname
        # urllib also opens file: and other addresses. Only https is ever
        # meant here, so anything else is refused before a request exists.
        if urlsplit(url).scheme != "https":
            raise AuthUnavailable(
                f"refusing to send a sign-in request without https: {host}"
            )
        request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - https only, checked above
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https only, checked above
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:  # an answer, just not a 2xx one
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise AuthUnavailable(f"couldn't reach {host}: {reason}") from None
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return HttpResponse(status, body)


_ACCEPT_JSON = {"Accept": "application/json", "User-Agent": "wren"}


def _error_code(body: Any) -> str | None:
    """The machine-readable code in an OAuth error, in either common shape:
    {"error": "invalid_grant"} or {"error": {"code": "invalid_grant"}}."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    return None


def _describe(response: HttpResponse) -> str:
    code = _error_code(response.body)
    return f"HTTP {response.status}" + (f", {code}" if code else "")


def _discovery(http: Http) -> dict[str, Any]:
    """OpenAI's OpenID configuration: where its keys and endpoints are."""
    response = http.get(DISCOVERY_URL)
    if response.status != 200 or not isinstance(response.body, dict):
        raise AuthUnavailable(
            f"couldn't read OpenAI's sign-in configuration ({_describe(response)})"
        )
    if response.body.get("issuer") != ISSUER:
        raise SignInFailed(
            "OpenAI's sign-in configuration named an unexpected issuer, so it wasn't trusted."
        )
    return response.body


# --- Signing in -----------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier (kept) and its challenge (sent): RFC 7636, method S256."""
    verifier = secrets.token_urlsafe(64)  # 86 characters; 43 to 128 are allowed
    return verifier, challenge_for(verifier)


def challenge_for(verifier: str) -> str:
    """base64url(SHA-256(verifier)), without the "=" padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class LoginAttempt:
    """One go at signing in: everything that has to match when the browser
    comes back. Made fresh every time, and never saved."""

    redirect_uri: str
    host_id: str
    returning: Credentials | None  # the saved registration, if there is one
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = ""
    challenge: str = ""

    def __post_init__(self) -> None:
        if not self.verifier:
            self.verifier, self.challenge = pkce_pair()

    @property
    def client_id(self) -> str:
        return self.returning.client_id if self.returning else REGISTER

    @property
    def asks_consent_again(self) -> bool:
        # You declined plan usage before, and running `wren login` is you
        # asking again: so ask OpenAI to show the permission screen, which
        # it otherwise skips for a registration it knows.
        return self.returning is not None and not self.returning.plan_usage_allowed

    def authorize_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "ext_agent_host_id": self.host_id,
        }
        if self.returning is None:
            params["agent_name_hint"] = AGENT_NAME  # first registration only
        else:
            # Hints, so OpenAI can skip asking which account. The ID token
            # is gone after signing out; the email stays.
            if self.returning.id_token:
                params["id_token_hint"] = self.returning.id_token
            if self.returning.email:
                params["login_hint"] = self.returning.email
        if self.asks_consent_again:
            params["prompt"] = "consent"
        # quote, not urlencode's default quote_plus: spaces become %20.
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def finish_login(
    attempt: LoginAttempt,
    answer: dict[str, str],
    *,
    store: CredentialStore,
    http: Http,
    clock: Callable[[], datetime] = now_utc,
) -> Credentials:
    """Check the browser's answer, swap its code for tokens, check those,
    and save them. Raises SignInFailed (nothing saved) if anything is off."""
    # 1. Is this the answer to OUR request? Checked before anything else in
    #    it is believed, the error included. compare_digest takes the same
    #    time however much matches, so timing can't reveal the state.
    if not secrets.compare_digest(
        answer.get("state", "").encode(), attempt.state.encode()
    ):
        raise SignInFailed(
            "The browser's answer didn't belong to this sign-in (its state didn't match), "
            "so it was ignored and nothing was saved. Run `wren login` to try again."
        )
    if "error" in answer:
        if answer["error"] == "access_denied":
            raise SignInFailed(
                "You didn't allow Wren to sign in, so nothing changed. "
                "Run `wren login` if you change your mind."
            )
        detail = answer.get("error_description", "")
        raise SignInFailed(
            f"OpenAI's sign-in returned an error: {answer['error']}"
            + (f" ({detail})" if detail else "")
            + ". Nothing was saved."
        )

    # 2. Which client ID? A first sign-in is told its new one here; a later
    #    one must not be switched to a different registration.
    issued = answer.get("client_id")
    if attempt.returning is None:
        if not issued or issued == REGISTER:
            raise SignInFailed(
                "OpenAI didn't issue Wren a client ID, so registering didn't finish. "
                "Nothing was saved. Run `wren login` to try again."
            )
        client_id = issued
    else:
        if issued and issued != attempt.client_id:
            raise SignInFailed(
                "The browser's answer was for a different registration from the one saved, "
                "so it was refused. Nothing was replaced."
            )
        client_id = attempt.client_id
    code = answer.get("code")
    if not code:
        raise SignInFailed(
            "The browser's answer had no authorization code. Nothing was saved."
        )

    # 3. Swap the code for tokens. The verifier proves we started this.
    response = http.post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": attempt.verifier,
            "redirect_uri": attempt.redirect_uri,
            "resource": RESOURCE,
        },
    )
    received_at = clock()
    tokens = response.body
    if response.status != 200 or not isinstance(tokens, dict):
        if _error_code(tokens) == "invalid_grant":
            raise SignInFailed(
                "OpenAI refused the sign-in code (invalid_grant), so it was thrown away. "
                "Run `wren login` to start again."
            )
        raise SignInFailed(
            f"Swapping the sign-in code for tokens failed ({_describe(response)}). Nothing was saved."
        )
    if not tokens.get("access_token"):
        raise SignInFailed("OpenAI's answer had no access token. Nothing was saved.")

    # 4. Who signed in? Only a verified ID token is believed.
    claims = verify_id_token(
        tokens.get("id_token"), client_id=client_id, nonce=attempt.nonce, http=http
    )
    if attempt.returning is not None and claims["sub"] != attempt.returning.subject:
        raise SignInFailed(
            "You signed in as a different ChatGPT account from the one Wren has saved, "
            "so nothing was replaced. Wren keeps one account for now."
        )

    credentials = Credentials(
        issuer=ISSUER,
        subject=claims["sub"],
        client_id=client_id,
        ext_agent_host_id=attempt.host_id,
        email=claims.get("email") or "",
        id_token=tokens.get("id_token"),
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        token_type=tokens.get("token_type") or "Bearer",
        expires_in=int(tokens.get("expires_in") or 0),
        # What was GRANTED, which may be less than what was asked for. If
        # the answer doesn't say, nothing is assumed: no plan usage.
        scopes=_scopes(tokens.get("scope")),
        saved_at=received_at.isoformat(),
        earliest_refresh_at=tokens.get("earliest_refresh_at"),
    )
    # Saved under the refresh lock, so a refresh in another Wren can't
    # write its older tokens over these afterwards.
    with store.locked():
        store.save(credentials)
    return credentials


def _scopes(scope: Any) -> list[str]:
    return sorted(scope.split()) if isinstance(scope, str) else []


def verify_id_token(
    id_token: Any, *, client_id: str, nonce: str, http: Http
) -> dict[str, Any]:
    """The ID token's claims, if it really is from OpenAI, for Wren, recent,
    and from this sign-in. Raises SignInFailed otherwise.

    A JWT is three base64 parts, header.payload.signature. Anyone can read
    the first two; only the signature, checked against OpenAI's public key,
    shows OpenAI wrote them. PyJWT does that check, and the time, issuer and
    audience checks; the nonce is ours to compare."""
    if not isinstance(id_token, str) or not id_token:
        raise SignInFailed("OpenAI's answer had no ID token. Nothing was saved.")
    jwks_uri = _discovery(http).get("jwks_uri")
    if not isinstance(jwks_uri, str) or urlsplit(jwks_uri).scheme != "https":
        raise SignInFailed("OpenAI's sign-in configuration had no usable key address.")
    keys = http.get(jwks_uri)
    if keys.status != 200:
        raise AuthUnavailable(
            f"couldn't fetch OpenAI's signing keys ({_describe(keys)}). Nothing was saved."
        )
    try:
        key_set = jwt.PyJWKSet.from_dict(keys.body)
        key_id = jwt.get_unverified_header(id_token).get("kid")
        key = next(k for k in key_set.keys if key_id is None or k.key_id == key_id)
    except (jwt.PyJWTError, StopIteration, TypeError, AttributeError):
        raise SignInFailed(
            "The ID token couldn't be matched to one of OpenAI's published keys, "
            "so it wasn't trusted. Nothing was saved."
        ) from None
    if key.algorithm_name not in SIGNATURE_ALGORITHMS:
        raise SignInFailed(
            f"The ID token's key uses {key.algorithm_name}, which Wren doesn't accept. Nothing was saved."
        )
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=[key.algorithm_name],
            audience=client_id,
            issuer=ISSUER,
            leeway=CLOCK_LEEWAY,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise SignInFailed("The ID token had expired. Nothing was saved.") from None
    except jwt.InvalidAudienceError:
        raise SignInFailed(
            "The ID token was issued for a different app. Nothing was saved."
        ) from None
    except jwt.InvalidIssuerError:
        raise SignInFailed(
            "The ID token wasn't issued by OpenAI. Nothing was saved."
        ) from None
    except jwt.PyJWTError as e:
        # A bad signature, a missing claim, a malformed token...
        raise SignInFailed(
            f"The ID token didn't pass its checks ({type(e).__name__}). Nothing was saved."
        ) from None
    if not secrets.compare_digest(
        str(claims.get("nonce", "")).encode(), nonce.encode()
    ):
        raise SignInFailed(
            "The ID token wasn't from this sign-in (its nonce didn't match). Nothing was saved."
        )
    return claims


def sign_in(
    *,
    store: CredentialStore,
    http: Http,
    open_browser: Callable[[str], bool],
    say: Callable[[str], None],
    port: int | None = None,
    timeout: float = 600,
) -> Credentials:
    """The whole of `wren login`, from opening the browser to saving. The
    server listens on PREFERRED_PORT unless given another `port`."""
    port = PREFERRED_PORT if port is None else port
    try:
        saved = store.load()
    except CredentialsError as e:
        raise SignInFailed(
            f"{e}. Move it out of the way, then run `wren login` again."
        ) from None
    host_id = store.host_id()
    with CallbackServer(port) as server:
        attempt = LoginAttempt(server.redirect_uri, host_id, saved)
        url = attempt.authorize_url()
        say("Opening your browser to sign in with ChatGPT…")
        if not open_browser(url):
            say(f"Couldn't open a browser. Open this address yourself:\n\n  {url}\n")
        say(
            f"Waiting for the browser (up to {int(timeout // 60)} minutes; Ctrl-C to cancel)…"
        )
        answer = server.wait(timeout)
    if answer is None:
        raise SignInFailed(
            "No answer came back from the browser, so signing in was abandoned. "
            "Nothing was saved. Run `wren login` to try again."
        )
    return finish_login(attempt, answer, store=store, http=http)


# --- Using the sign-in ------------------------------------------------------------


@dataclass
class SignOut:
    had_tokens: bool  # was there anything to sign out of?
    revoked: bool  # did OpenAI confirm the session is over?
    problem: str = ""  # why it didn't, if it didn't


class ChatGPTSession:
    """A saved ChatGPT sign-in, as the OpenAI provider uses it.

    Every request asks `access_token()` for a token, and gets one that's
    good for at least REFRESH_MARGIN more: refreshed first if need be. It
    reads the record from disk each time, so a refresh or sign-out in
    another Wren is seen straight away."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        http: Http | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = store if store is not None else CredentialStore()
        self.http = http if http is not None else UrllibHttp()
        self.clock = clock

    def credentials(self) -> Credentials:
        """The saved sign-in. Raises SignInRequired if there's none to use."""
        try:
            saved = self.store.load()
        except CredentialsError as e:
            raise SignInRequired(f"{e}. Run `wren login` to sign in again.") from None
        if saved is None or not saved.signed_in:
            raise SignInRequired(NOT_SIGNED_IN)
        return saved

    def check(self) -> Credentials:
        """The saved sign-in, if it may be used for plan usage; else raises."""
        credentials = self.credentials()
        if not credentials.plan_usage_allowed:
            raise PlanUsageDisabled(PLAN_NOT_ALLOWED)
        return credentials

    def access_token(self) -> str:
        credentials = self.check()
        if not self._needs_refresh(credentials):
            return credentials.access_token or ""
        with self.store.locked():
            # Another Wren may have refreshed while we waited for the lock:
            # its new tokens are on disk, and the refresh token we read
            # before may already be spent. So read again before deciding.
            credentials = self.check()
            if self._needs_refresh(credentials):
                credentials = self._refresh(credentials)
        return credentials.access_token or ""

    def _needs_refresh(self, credentials: Credentials) -> bool:
        expires = credentials.expires_at()
        if not credentials.access_token or expires is None:
            return True
        now = self.clock()
        if now < expires - REFRESH_MARGIN:
            return False
        # Near the end, but OpenAI may ask us to wait before refreshing. A
        # token that's still valid is used until then.
        not_before = credentials.refresh_not_before()
        return not (not_before is not None and now < not_before and now < expires)

    def _refresh(self, credentials: Credentials) -> Credentials:
        """Trade the refresh token for new tokens. Call with the lock held."""
        if not credentials.refresh_token:
            self.store.save(credentials.signed_out())
            raise SignInRequired(
                "Your ChatGPT sign-in has run out. Run `wren login` to sign in again."
            )
        response = self.http.post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": credentials.client_id,  # the issued one, never REGISTER
                "refresh_token": credentials.refresh_token,
                "resource": RESOURCE,
                # No scope: leaving it out keeps what was granted.
            },
        )
        body = response.body
        if (
            response.status == 200
            and isinstance(body, dict)
            and body.get("access_token")
        ):
            renewed = replace(
                credentials,
                access_token=body["access_token"],
                # The replacement refresh token: the old one is now spent.
                refresh_token=body.get("refresh_token") or credentials.refresh_token,
                token_type=body.get("token_type") or credentials.token_type,
                expires_in=int(body.get("expires_in") or 0),
                # Leaving scope out of a refresh answer means "unchanged".
                scopes=(
                    _scopes(body["scope"]) if "scope" in body else credentials.scopes
                ),
                saved_at=self.clock().isoformat(),
                earliest_refresh_at=body.get("earliest_refresh_at"),
                # The ID token is kept as it was: it's only ever a hint for
                # the next sign-in, and the one we have was verified.
            )
            # All replaced together, in one write.
            self.store.save(renewed)
            if not renewed.plan_usage_allowed:
                raise PlanUsageDisabled(PLAN_NOT_ALLOWED)
            return renewed

        code = _error_code(body)
        if code in REFRESH_TOKEN_DEAD:
            # This refresh token will never work again: forget the tokens,
            # keep the registration, so `wren login` can reuse it.
            self.store.save(credentials.signed_out())
            raise SignInRequired(
                f"Your ChatGPT sign-in has ended ({code}). "
                "Run `wren login` to sign in again."
            )
        if code == "invalid_client":
            raise ChatGPTAuthError(
                "OpenAI no longer recognises Wren's registration (invalid_client). "
                "If you disconnected Wren in ChatGPT's settings, delete "
                f"{self.store.path} and run `wren login` to register again."
            )
        if response.status >= 500 or response.status in (408, 429):
            # Temporary, probably. The sign-in is kept: try again shortly.
            raise AuthUnavailable(
                f"Couldn't renew the ChatGPT sign-in just now ({_describe(response)}). "
                "Your sign-in is kept; try again in a moment."
            )
        raise ChatGPTAuthError(
            f"Renewing the ChatGPT sign-in failed ({_describe(response)}). Your sign-in is kept."
        )

    def sign_out(self, sleep: Callable[[float], None] = time.sleep) -> SignOut:
        """End the session at OpenAI if possible, then forget the tokens.

        The tokens are forgotten either way: you asked to sign out. Whether
        OpenAI confirmed it is reported, so you know whether to disconnect
        Wren in ChatGPT's settings too."""
        with self.store.locked():  # not while a refresh is half-done
            try:
                saved = self.store.load()
            except CredentialsError as e:
                raise SignInRequired(f"{e}. Nothing was changed.") from None
            if saved is None or not saved.signed_in:
                return SignOut(had_tokens=False, revoked=False)
            revoked, problem = (
                self._revoke(saved, sleep)
                if saved.refresh_token
                else (False, "there was no refresh token to revoke")
            )
            self.store.save(saved.signed_out())
            return SignOut(had_tokens=True, revoked=revoked, problem=problem)

    def _revoke(
        self, saved: Credentials, sleep: Callable[[float], None]
    ) -> tuple[bool, str]:
        try:
            endpoint = _discovery(self.http).get("revocation_endpoint")
        except ChatGPTAuthError as e:
            return False, str(e)
        if not isinstance(endpoint, str) or urlsplit(endpoint).scheme != "https":
            return False, "OpenAI's sign-in configuration had no revocation address"
        problem = ""
        # A network fault or a 5xx is worth retrying, a little; a refusal isn't.
        for delay in (0, 1, 3):
            sleep(delay)
            try:
                response = self.http.post_form(
                    endpoint,
                    {
                        "token": saved.refresh_token or "",
                        "token_type_hint": "refresh_token",
                        "client_id": saved.client_id,
                    },
                )
            except AuthUnavailable as e:
                problem = str(e)
                continue
            if response.status == 200:  # also the answer for an already-dead token
                return True, ""
            problem = f"OpenAI answered {_describe(response)}"
            if response.status < 500:
                break
        return False, problem
