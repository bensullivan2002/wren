"""Where Wren keeps a ChatGPT sign-in, and how it keeps it safe.

    ~/.wren/auth/          only you can open it (0700)
        host.json          this computer's host ID: an identifier, not a secret
        chatgpt.json       the signed-in account and its tokens (0600)
        chatgpt.lock       held while a token is refreshed
        host.lock          held while the host ID is first made

Three things in this file matter more than they look:

  * Tokens are as good as a password while they last, so the record is
    written owner-only, and atomically: to a new file, then renamed into
    place. A crash half-way never leaves a damaged record, and there's
    never a moment when the file exists with looser permissions.

  * The refresh token ROTATES: each refresh returns a new one and the old
    one stops working. If two Wrens (two terminals) refreshed at once, one
    would spend the token the other was about to use, and that one would be
    signed out. So a refresh happens only while holding `chatgpt.lock`
    (see `locked`), and re-reads the record once it has it.

  * The record's `repr` leaves the tokens out, so an error message, a
    traceback or a debugging print can't show them.

Outside the repository by design: none of this can be committed by
accident. Wren's sandbox also stops commands reading this folder
(tools/sandbox.py), since whatever a command prints goes to the model.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLAN_SCOPE = "chatgpt.tokens.use.direct"  # the permission to use the ChatGPT plan


class CredentialsError(Exception):
    """The saved sign-in can't be read. It's left as it is, for you to look at."""


@dataclass
class Credentials:
    """One signed-in ChatGPT account, in the shape OpenAI's docs suggest.

    Two halves with different lifetimes:

      * the REGISTRATION: who you are (`subject`, checked from a signed ID
        token) and the `client_id` OpenAI issued when you first allowed
        Wren. Kept after signing out, so signing in again reuses it.
      * the TOKENS: what each request is sent with. Cleared on sign-out.
    """

    issuer: str
    subject: str  # the account's stable id, from the validated ID token
    client_id: str  # issued for Wren on this account; never "dynamic_agent_client"
    ext_agent_host_id: str
    email: str = ""  # for showing, and as a hint when signing in again; not an identity
    # field(repr=False): never in an error message or a traceback.
    id_token: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "Bearer"  # noqa: S105 - the name of a kind of token, not a secret
    expires_in: int = 0  # seconds the access token lasts, from saved_at
    scopes: list[str] = field(default_factory=list)
    saved_at: str = ""  # UTC, ISO 8601: when the token response arrived
    # From the token response. Its format isn't documented; a number is
    # read as Unix seconds (like the tokens' own times), anything else is
    # kept but not acted on.
    earliest_refresh_at: int | float | str | None = None

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    @property
    def plan_usage_allowed(self) -> bool:
        """Whether you said yes to Wren using your ChatGPT plan. A valid
        sign-in alone doesn't mean that: the permission is a separate scope."""
        return PLAN_SCOPE in self.scopes

    def expires_at(self) -> datetime | None:
        if not self.saved_at:
            return None
        return datetime.fromisoformat(self.saved_at) + timedelta(
            seconds=self.expires_in
        )

    def refresh_not_before(self) -> datetime | None:
        if isinstance(self.earliest_refresh_at, (int, float)):
            return datetime.fromtimestamp(self.earliest_refresh_at, timezone.utc)
        return None

    def signed_out(self) -> Credentials:
        """The registration without the tokens: what's kept after signing out."""
        return Credentials(
            issuer=self.issuer,
            subject=self.subject,
            client_id=self.client_id,
            ext_agent_host_id=self.ext_agent_host_id,
            email=self.email,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Credentials:
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as e:  # a required field is missing
            raise CredentialsError(f"the saved sign-in is incomplete: {e}") from None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CredentialStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else Path.home() / ".wren" / "auth"
        self.path = self.root / "chatgpt.json"
        self._host_path = self.root / "host.json"
        self._lock_path = self.root / "chatgpt.lock"

    def load(self) -> Credentials | None:
        """The saved sign-in, or None if there isn't one. Raises
        CredentialsError if the file is there but can't be used."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise CredentialsError(f"couldn't read {self.path}: {e}") from None
        try:
            data = json.loads(text)
        except ValueError:
            raise CredentialsError(f"{self.path} isn't valid JSON") from None
        if not isinstance(data, dict):
            raise CredentialsError(f"{self.path} isn't a sign-in record")
        return Credentials.from_dict(data)

    def save(self, credentials: Credentials) -> None:
        self._write_private(self.path, credentials.to_dict())

    def host_id(self) -> str:
        """This computer's `ext_agent_host_id`, made the first time it's asked for.

        OpenAI uses it to tell apart the places one registration runs (a
        laptop, a VM), so it must stay the same for this computer: signing
        in again or signing out doesn't change it. It's random, so it says
        nothing about you. The docs accept a UUID in this form."""
        # Two first runs at once must agree on one ID. A lock of its own, so
        # asking for the ID while holding the refresh lock can't deadlock.
        with self._locked(self.root / "host.lock"):
            try:
                saved = json.loads(self._host_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and isinstance(
                    saved.get("ext_agent_host_id"), str
                ):
                    return saved["ext_agent_host_id"]
            except (OSError, ValueError):
                pass
            host_id = f"urn:uuid:{uuid.uuid4()}"
            self._write_private(self._host_path, {"ext_agent_host_id": host_id})
            return host_id

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the lock that makes refreshing (and signing out) one at a time.

        `flock` is an advisory lock the operating system keeps on an open
        file: a second process asking for it waits until the first lets go,
        and it's let go automatically if that process dies. Each call opens
        the file afresh, so two threads in one process wait for each other
        too. (Unix only, like the rest of Wren's sandboxing.)

        Not re-entrant: taking it again while holding it waits for ever."""
        with self._locked(self._lock_path):
            yield

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        self._make_root()
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the file releases the lock

    def _make_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)  # mkdir's mode is trimmed by your umask; this isn't

    def _write_private(self, path: Path, data: dict) -> None:
        self._make_root()
        # mkstemp creates the file owner-only (0600) from the start, with a
        # name no other process will pick, so two Wrens can't share it.
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())  # on the disk before it replaces the old one
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
