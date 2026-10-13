"""Tests for signing in with ChatGPT (wren.auth). No network: OpenAI's sign-in
service is a fake that hands out ID tokens signed with a key made here, and
publishes that key, so the real signature checks run against it. The one
real connection is the browser's return visit, to Wren's own server on
127.0.0.1."""

import base64
import hashlib
import hmac
import io
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from wren.auth import cli as auth_cli
from wren.auth.callback import CallbackServer
from wren.auth.chatgpt import (
    DISCOVERY_URL,
    ISSUER,
    REGISTER,
    RESOURCE,
    TOKEN_URL,
    AuthUnavailable,
    ChatGPTSession,
    HttpResponse,
    LoginAttempt,
    PlanUsageDisabled,
    SignInFailed,
    SignInRequired,
    UrllibHttp,
    challenge_for,
    finish_login,
)
from wren.auth.store import Credentials, CredentialStore

CLIENT = "oaiapp_wren_test"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
REVOKE_URL = f"{ISSUER}/oauth/revoke"
FULL_SCOPE = (
    "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
)
NO_PLAN_SCOPE = "email offline_access openid profile resource.invoke"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path / "auth")


def id_token(key, kid="k1", algorithm="RS256", **claims):
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "user-1",
        "email": "ben@example.com",
        "nonce": "nonce-1",
        "iat": now,
        "exp": now + 3600,
    }
    body.update(claims)
    body = {name: value for name, value in body.items() if value is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


def token_answer(key, scope=FULL_SCOPE, access="at-1", refresh="rt-1", **claims):
    return HttpResponse(
        200,
        {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token(key, **claims),
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope,
        },
    )


class FakeHttp:
    """OpenAI's sign-in service, played back: discovery, keys, and queued
    answers for the token and revocation endpoints. Records what was sent."""

    def __init__(self, key, tokens=(), revocations=(), delay=0.0):
        public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        self.jwks = {"keys": [{**public, "kid": "k1", "alg": "RS256", "use": "sig"}]}
        self.tokens = list(tokens)
        self.revocations = list(revocations)
        self.delay = delay
        self.posts = []

    def get(self, url):
        if url == DISCOVERY_URL:
            return HttpResponse(
                200,
                {
                    "issuer": ISSUER,
                    "jwks_uri": JWKS_URL,
                    "revocation_endpoint": REVOKE_URL,
                },
            )
        assert url == JWKS_URL
        return HttpResponse(200, self.jwks)

    def post_form(self, url, form):
        self.posts.append((url, dict(form)))
        time.sleep(self.delay)
        answer = (self.tokens if url == TOKEN_URL else self.revocations).pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def attempt(returning=None):
    return LoginAttempt(
        "http://127.0.0.1:1455/auth/callback",
        "urn:uuid:host-1",
        returning,
        state="state-1",
        nonce="nonce-1",
    )


def answer(**extra):
    return {"state": "state-1", "code": "code-1", "client_id": CLIENT, **extra}


def saved(store, scopes=FULL_SCOPE, saved_at=NOW, **changes):
    credentials = Credentials(
        issuer=ISSUER,
        subject="user-1",
        client_id=CLIENT,
        ext_agent_host_id="urn:uuid:host-1",
        email="ben@example.com",
        id_token="old-id-token",
        access_token="at-1",
        refresh_token="rt-1",
        expires_in=3600,
        scopes=scopes.split(),
        saved_at=saved_at.isoformat(),
    )
    for name, value in changes.items():
        setattr(credentials, name, value)
    store.save(credentials)
    return credentials


def test_the_real_http_client_only_ever_uses_https():
    # urllib would happily open these; the sign-in must not.
    for address in (
        "http://auth.openai.com/x",
        "file:///etc/passwd",
        "ftp://example.com/",
    ):
        with pytest.raises(AuthUnavailable, match="without https"):
            UrllibHttp().get(address)
        with pytest.raises(AuthUnavailable, match="without https"):
            UrllibHttp().post_form(address, {"token": "rt-1"})


# --- The request to sign in ----------------------------------------------------------


def test_the_pkce_challenge_matches_rfc_7636s_worked_example():
    # RFC 7636, appendix B: this verifier must give exactly this challenge.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_every_attempt_has_fresh_random_values():
    one, two = LoginAttempt("r", "h", None), LoginAttempt("r", "h", None)
    assert one.state != two.state and one.nonce != two.nonce
    assert one.verifier != two.verifier and challenge_for(one.verifier) == one.challenge


def query(url):
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


def test_a_first_sign_in_asks_to_register_wren_with_this_computer():
    url = attempt().authorize_url()
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    sent = query(url)
    assert sent["client_id"] == REGISTER and sent["agent_name_hint"] == "Wren"
    assert sent["ext_agent_host_id"] == "urn:uuid:host-1"
    assert sent["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert sent["scope"].split() == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "resource.invoke",
        "chatgpt.tokens.use.direct",
    ]
    assert sent["resource"] == RESOURCE and sent["response_type"] == "code"
    assert sent["code_challenge_method"] == "S256"
    assert (sent["state"], sent["nonce"]) == ("state-1", "nonce-1")
    assert "id_token_hint" not in sent and "prompt" not in sent
    assert "%20" in url and "+" not in url  # each value percent-encoded


def test_a_returning_sign_in_reuses_its_client_and_hints_at_the_account(store):
    sent = query(attempt(saved(store)).authorize_url())
    assert sent["client_id"] == CLIENT and "agent_name_hint" not in sent
    assert sent["id_token_hint"] == "old-id-token"
    assert sent["login_hint"] == "ben@example.com"
    assert sent["ext_agent_host_id"] == "urn:uuid:host-1"
    assert "prompt" not in sent  # an ordinary sign-in doesn't force consent


def test_after_declining_plan_usage_signing_in_again_asks_for_consent(store):
    sent = query(attempt(saved(store, scopes=NO_PLAN_SCOPE)).authorize_url())
    assert sent["prompt"] == "consent"
    assert "chatgpt.tokens.use.direct" in sent["scope"]


# --- The browser's answer ------------------------------------------------------------


def test_a_first_sign_in_is_verified_and_saved_owner_only(key, store):
    http = FakeHttp(key, tokens=[token_answer(key)])
    credentials = finish_login(
        attempt(), answer(scope=FULL_SCOPE), store=store, http=http, clock=lambda: NOW
    )

    url, form = http.posts[0]
    assert url == TOKEN_URL
    assert form.pop("code_verifier")  # its value: the next test
    assert form == {
        "grant_type": "authorization_code",
        "client_id": CLIENT,  # the issued ID, not dynamic_agent_client
        "code": "code-1",
        "redirect_uri": "http://127.0.0.1:1455/auth/callback",  # exactly as sent before
        "resource": RESOURCE,
    }
    assert (credentials.subject, credentials.email, credentials.client_id) == (
        "user-1",
        "ben@example.com",
        CLIENT,
    )
    assert credentials.plan_usage_allowed and credentials.saved_at == NOW.isoformat()
    assert store.load() == credentials
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert "at-1" not in repr(credentials) and "rt-1" not in repr(credentials)
    assert not list(store.root.glob("*.tmp"))  # the side file was renamed into place


def test_the_code_exchange_sends_this_attempts_own_verifier(key, store):
    this = attempt()
    http = FakeHttp(key, tokens=[token_answer(key)])
    finish_login(this, answer(), store=store, http=http)
    assert http.posts[0][1]["code_verifier"] == this.verifier
    assert challenge_for(this.verifier) == query(this.authorize_url())["code_challenge"]


def test_an_answer_with_the_wrong_state_is_ignored_before_anything_else(key, store):
    http = FakeHttp(key, tokens=[token_answer(key)])
    for wrong in (answer(state="forged"), {"code": "code-1"}):
        with pytest.raises(SignInFailed, match="state"):
            finish_login(attempt(), wrong, store=store, http=http)
    # Even an error in it isn't believed: the state is checked first.
    with pytest.raises(SignInFailed, match="state"):
        finish_login(
            attempt(),
            {"state": "forged", "error": "access_denied"},
            store=store,
            http=http,
        )
    assert http.posts == [] and store.load() is None


def test_declining_changes_nothing(key, store):
    http = FakeHttp(key)
    with pytest.raises(SignInFailed, match="didn't allow"):
        finish_login(
            attempt(),
            {"state": "state-1", "error": "access_denied"},
            store=store,
            http=http,
        )
    assert http.posts == [] and store.load() is None


def test_a_first_sign_in_without_an_issued_client_id_is_incomplete(key, store):
    http = FakeHttp(key, tokens=[token_answer(key)])
    for client in (None, REGISTER):
        reply = (
            answer(client_id=client) if client else {"state": "state-1", "code": "c"}
        )
        with pytest.raises(SignInFailed, match="client ID"):
            finish_login(attempt(), reply, store=store, http=http)
    assert http.posts == [] and store.load() is None


def test_a_returning_sign_in_refuses_a_different_registration(key, store):
    before = saved(store)
    with pytest.raises(SignInFailed, match="different registration"):
        finish_login(
            attempt(before),
            answer(client_id="oaiapp_other"),
            store=store,
            http=FakeHttp(key),
        )
    assert store.load() == before


def test_a_returning_sign_in_may_leave_the_client_id_out(key, store):
    before = saved(store, access_token=None, refresh_token=None, id_token=None)
    http = FakeHttp(key, tokens=[token_answer(key, access="at-2")])
    reply = {"state": "state-1", "code": "code-1"}
    after = finish_login(attempt(before), reply, store=store, http=http)
    assert http.posts[0][1]["client_id"] == CLIENT and after.access_token == "at-2"


def test_a_refused_code_is_thrown_away(key, store):
    http = FakeHttp(key, tokens=[HttpResponse(400, {"error": "invalid_grant"})])
    with pytest.raises(SignInFailed, match="invalid_grant"):
        finish_login(attempt(), answer(), store=store, http=http)
    assert store.load() is None


@pytest.mark.parametrize(
    ("claims", "complaint"),
    [
        ({"iss": "https://evil.example"}, "wasn't issued by OpenAI"),
        ({"aud": "oaiapp_someone_else"}, "different app"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expired"),
        ({"nonce": "replayed"}, "nonce"),
        ({"nonce": None}, "nonce"),
        ({"sub": None}, "checks"),
    ],
)
def test_an_id_token_that_fails_a_check_saves_nothing(key, store, claims, complaint):
    http = FakeHttp(key, tokens=[token_answer(key, **claims)])
    with pytest.raises(SignInFailed, match=complaint):
        finish_login(attempt(), answer(), store=store, http=http)
    assert store.load() is None


def test_an_id_token_signed_with_another_key_is_refused(key, store):
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = token_answer(key)
    forged.body["id_token"] = id_token(impostor)  # same kid, wrong key
    with pytest.raises(SignInFailed, match="InvalidSignatureError"):
        finish_login(
            attempt(), answer(), store=store, http=FakeHttp(key, tokens=[forged])
        )
    assert store.load() is None


def test_an_id_token_signed_with_a_shared_secret_is_refused(key, store):
    # The classic forgery: sign with HS256 (a shared-secret algorithm),
    # using OpenAI's PUBLIC key, which anyone has, as the "secret". A
    # verifier that let the token pick its algorithm would accept it. Only
    # public-key algorithms are accepted here, so it fails.
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    forged = token_answer(key)
    claims = jwt.decode(forged.body["id_token"], options={"verify_signature": False})
    forged.body["id_token"] = hs256_by_hand(claims, public_pem)
    with pytest.raises(SignInFailed, match="InvalidAlgorithmError"):
        finish_login(
            attempt(), answer(), store=store, http=FakeHttp(key, tokens=[forged])
        )
    assert store.load() is None


def hs256_by_hand(claims, secret):
    # PyJWT refuses to use a PEM key as an HMAC secret (rightly), so the
    # token is put together by hand, as an attacker would.
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = part(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = part(json.dumps(claims).encode())
    signature = hmac.new(
        secret, f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{part(signature)}"


def test_signing_in_as_a_different_account_replaces_nothing(key, store):
    before = saved(store)
    http = FakeHttp(key, tokens=[token_answer(key, sub="someone-else")])
    with pytest.raises(SignInFailed, match="different ChatGPT account"):
        finish_login(attempt(before), answer(), store=store, http=http)
    assert store.load() == before


def test_without_plan_consent_the_sign_in_is_kept_but_never_used(key, store):
    http = FakeHttp(key, tokens=[token_answer(key, scope=NO_PLAN_SCOPE)])
    credentials = finish_login(attempt(), answer(), store=store, http=http)
    assert credentials.signed_in and not credentials.plan_usage_allowed
    session = ChatGPTSession(store, http=http)
    with pytest.raises(PlanUsageDisabled, match="wren login"):
        session.access_token()


def test_a_token_answer_that_doesnt_list_scopes_grants_no_plan_usage(key, store):
    reply = token_answer(key)
    del reply.body["scope"]
    credentials = finish_login(
        attempt(), answer(), store=store, http=FakeHttp(key, tokens=[reply])
    )
    assert credentials.scopes == [] and not credentials.plan_usage_allowed


# --- Using the sign-in: refreshing --------------------------------------------------


def test_with_no_sign_in_every_request_is_told_how_to_sign_in(store):
    with pytest.raises(SignInRequired, match="wren login"):
        ChatGPTSession(store).access_token()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")
    with pytest.raises(SignInRequired, match="isn't valid JSON"):
        ChatGPTSession(store).access_token()


def test_a_fresh_token_is_used_without_refreshing(key, store):
    saved(store)
    http = FakeHttp(key)
    session = ChatGPTSession(
        store, http=http, clock=lambda: NOW + timedelta(minutes=30)
    )
    assert session.access_token() == "at-1" and http.posts == []


def test_a_token_near_its_end_is_refreshed_and_everything_replaced_at_once(key, store):
    saved(store)
    later = NOW + timedelta(minutes=58)
    renewed = HttpResponse(
        200,
        {
            "access_token": "at-2",
            "refresh_token": "rt-2",
            "expires_in": 3600,
            "scope": FULL_SCOPE,
            "earliest_refresh_at": 1,
        },
    )
    http = FakeHttp(key, tokens=[renewed])
    assert (
        ChatGPTSession(store, http=http, clock=lambda: later).access_token() == "at-2"
    )

    url, form = http.posts[0]
    assert url == TOKEN_URL and form == {
        "grant_type": "refresh_token",
        "client_id": CLIENT,
        "refresh_token": "rt-1",
        "resource": RESOURCE,
    }  # no scope: the grant is kept as it was
    after = store.load()
    assert (after.access_token, after.refresh_token) == ("at-2", "rt-2")
    assert after.saved_at == later.isoformat() and after.id_token == "old-id-token"


def test_a_token_is_kept_until_openai_says_it_may_be_refreshed(key, store):
    wait_until = (NOW + timedelta(minutes=59)).timestamp()
    saved(store, earliest_refresh_at=wait_until)
    http = FakeHttp(key)
    session = ChatGPTSession(
        store, http=http, clock=lambda: NOW + timedelta(minutes=57)
    )
    assert session.access_token() == "at-1" and http.posts == []


def test_a_dead_refresh_token_signs_you_out_but_keeps_the_registration(key, store):
    saved(store)
    http = FakeHttp(key, tokens=[HttpResponse(400, {"error": "refresh_token_expired"})])
    session = ChatGPTSession(store, http=http, clock=lambda: NOW + timedelta(hours=2))
    with pytest.raises(SignInRequired, match="refresh_token_expired"):
        session.access_token()
    after = store.load()
    assert not after.signed_in and after.id_token is None
    assert (after.client_id, after.subject, after.email) == (
        CLIENT,
        "user-1",
        "ben@example.com",
    )


@pytest.mark.parametrize(
    "failure",
    [
        HttpResponse(503, None),
        HttpResponse(500, {"error": "server_error"}),
        AuthUnavailable("offline"),
    ],
)
def test_a_temporary_failure_keeps_the_sign_in(key, store, failure):
    before = saved(store)
    session = ChatGPTSession(
        store,
        http=FakeHttp(key, tokens=[failure]),
        clock=lambda: NOW + timedelta(hours=2),
    )
    with pytest.raises(AuthUnavailable):
        session.access_token()
    assert store.load() == before


def test_concurrent_refreshes_spend_the_refresh_token_once(key, store):
    # Two Wrens (here, five threads with sessions of their own) find the
    # token stale at once. Only one may refresh: the others wait for the
    # lock, re-read, and use what it saved.
    saved(store)
    renewed = HttpResponse(
        200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}
    )
    http = FakeHttp(key, tokens=[renewed], delay=0.2)
    clock = lambda: NOW + timedelta(minutes=58)
    got = []

    def use():
        got.append(ChatGPTSession(store, http=http, clock=clock).access_token())

    threads = [threading.Thread(target=use) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(http.posts) == 1 and got == ["at-2"] * 5


# --- Signing out -------------------------------------------------------------------


def test_signing_out_ends_the_session_then_forgets_the_tokens(key, store):
    saved(store)
    host = store.host_id()
    http = FakeHttp(key, revocations=[HttpResponse(200, None)])
    result = ChatGPTSession(store, http=http).sign_out(sleep=lambda s: None)
    assert result.had_tokens and result.revoked
    assert http.posts == [
        (
            REVOKE_URL,
            {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": CLIENT},
        )
    ]
    after = store.load()
    assert not after.signed_in and after.client_id == CLIENT
    assert store.host_id() == host  # the computer's ID outlives signing out


def test_an_unconfirmed_sign_out_still_forgets_the_tokens_and_says_so(key, store):
    saved(store)
    waits = []
    http = FakeHttp(
        key,
        revocations=[
            AuthUnavailable("offline"),
            HttpResponse(503, None),
            HttpResponse(502, None),
        ],
    )
    result = ChatGPTSession(store, http=http).sign_out(sleep=waits.append)
    assert result.had_tokens and not result.revoked and "502" in result.problem
    assert waits == [0, 1, 3] and not store.load().signed_in


def test_signing_out_when_signed_out_does_nothing(key, store):
    http = FakeHttp(key)
    assert not ChatGPTSession(store, http=http).sign_out().had_tokens
    assert http.posts == []


# --- The whole thing, through the browser's return visit ------------------------------


def visit(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def browser(answer_with):
    """A pretend browser: reads the sign-in address Wren opened, and comes
    back to Wren's server the way OpenAI would send it."""
    statuses = []

    def open_browser(url):
        sent = query(url)
        back = sent["redirect_uri"]

        def come_back():
            statuses.append(visit(back.replace("/auth/callback", "/favicon.ico")))
            statuses.append(visit(f"{back}?{urlencode(answer_with(sent))}"))

        threading.Thread(target=come_back, daemon=True).start()
        return True

    open_browser.statuses = statuses
    return open_browser


def test_wren_login_end_to_end(key, store, monkeypatch, capsys):
    monkeypatch.setattr("wren.auth.chatgpt.PREFERRED_PORT", 0)  # any free port
    http = FakeHttp(key)

    def reply(sent):
        # Wren chose the nonce; the pretend OpenAI puts it in the ID token.
        http.tokens.append(token_answer(key, nonce=sent["nonce"]))
        return {"code": "code-1", "state": sent["state"], "client_id": CLIENT}

    out = io.StringIO()
    open_browser = browser(reply)
    assert (
        auth_cli.login(out=out, store=store, http=http, open_browser=open_browser) == 0
    )

    shown = out.getvalue()
    assert "Signed in with ChatGPT as ben@example.com" in shown
    assert (
        "shared with ChatGPT" in shown and "https://chatgpt.com/settings/usage" in shown
    )
    assert "--auth chatgpt" in shown
    assert open_browser.statuses == [404, 200]  # the favicon wasn't taken as the answer
    assert store.load().plan_usage_allowed
    assert "code-1" not in capsys.readouterr().err  # the server logged nothing


def test_wren_login_reports_a_refusal_and_saves_nothing(key, store, monkeypatch):
    monkeypatch.setattr("wren.auth.chatgpt.PREFERRED_PORT", 0)
    out = io.StringIO()
    open_browser = browser(
        lambda sent: {"state": sent["state"], "error": "access_denied"}
    )
    assert (
        auth_cli.login(
            out=out, store=store, http=FakeHttp(key), open_browser=open_browser
        )
        == 1
    )
    assert "didn't allow" in out.getvalue() and store.load() is None


def test_ctrl_c_cancels_wren_login(key, store, monkeypatch):
    monkeypatch.setattr("wren.auth.chatgpt.PREFERRED_PORT", 0)

    def interrupted(url):
        raise KeyboardInterrupt

    out = io.StringIO()
    assert (
        auth_cli.login(
            out=out, store=store, http=FakeHttp(key), open_browser=interrupted
        )
        == 130
    )
    assert "cancelled" in out.getvalue() and store.load() is None


def test_the_callback_server_gives_up_and_moves_off_a_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        with CallbackServer(taken) as server:
            assert str(taken) not in server.redirect_uri
            assert server.redirect_uri.startswith("http://127.0.0.1:")
            assert server.wait(timeout=0.2) is None


# --- What `wren auth-status` and `wren logout` say ------------------------------------


def test_status_shows_the_account_and_never_a_token(store):
    saved(store)
    out = io.StringIO()
    assert (
        auth_cli.status(out=out, store=store, clock=lambda: NOW + timedelta(minutes=20))
        == 0
    )
    shown = out.getvalue()
    assert "ben@example.com" in shown and "valid for 40 more minutes" in shown
    assert "allowed (chatgpt.tokens.use.direct)" in shown and CLIENT in shown
    assert "at-1" not in shown and "rt-1" not in shown and "old-id-token" not in shown


def test_status_says_when_plan_usage_isnt_allowed_or_youre_signed_out(store):
    saved(store, scopes=NO_PLAN_SCOPE)
    out = io.StringIO()
    auth_cli.status(out=out, store=store)
    assert "NOT allowed" in out.getvalue()
    saved(store, access_token=None, refresh_token=None)
    out = io.StringIO()
    auth_cli.status(out=out, store=store)
    assert "signed out" in out.getvalue()


def test_logout_says_whether_openai_confirmed_it(key, store):
    saved(store)
    out = io.StringIO()
    http = FakeHttp(key, revocations=[HttpResponse(400, {"error": "invalid_request"})])
    auth_cli.logout(out=out, session=ChatGPTSession(store, http=http))
    assert "didn't confirm" in out.getvalue() and "settings" in out.getvalue()
