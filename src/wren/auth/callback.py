"""The tiny web server your browser comes back to after signing in.

OAuth for a program on your own computer works like this (RFC 8252): Wren
starts a web server on 127.0.0.1, opens your browser at OpenAI's sign-in
page, and tells OpenAI to send the browser back to that server when you're
done. The browser's return visit carries the answer in its address:

    http://127.0.0.1:1455/auth/callback?code=...&state=...&client_id=...

So this server only ever needs to take one request. It listens on 127.0.0.1
only (never on your network), answers the browser with a short page, and
hands the address's parameters to chatgpt.py, which decides whether to
trust them. Nothing is checked here: a request with the right path is
simply the answer, and validating it is the next step's job.

Two details:

  * The port. OpenAI's docs use 1455, and later sign-ins may use any free
    port, so if 1455 is taken we let the system choose one. Only the port
    may vary: the scheme, host and path must match exactly, and must be the
    same in the sign-in request and the code exchange.
  * The address contains a one-time authorization code, so the server's
    usual habit of logging every request to the terminal is switched off.
"""

from __future__ import annotations

import errno
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"  # not "localhost": the docs require the address itself
PREFERRED_PORT = 1455
CALLBACK_PATH = "/auth/callback"

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Wren</title>
<body style="font-family: system-ui, sans-serif; max-width: 34em; margin: 4em auto">
<h1>Back to Wren</h1>
<p>Wren has your browser's answer. Return to your terminal to see whether
signing in worked. You can close this tab.</p>
</body>
"""


class CallbackServer:
    def __init__(self, port: int = PREFERRED_PORT) -> None:
        try:
            self._server = _Server((HOST, port), _Handler)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            self._server = _Server((HOST, 0), _Handler)  # 0: any free port
        self._server.timeout = 0.5  # how long each wait lasts, so we can give up

    @property
    def redirect_uri(self) -> str:
        return f"http://{HOST}:{self._server.server_port}{CALLBACK_PATH}"

    def wait(self, timeout: float) -> dict[str, str] | None:
        """The parameters of the browser's return visit, or None if it
        didn't come within `timeout` seconds. Ctrl-C stops the wait."""
        deadline = time.monotonic() + timeout
        while self._server.answer is None and time.monotonic() < deadline:
            self._server.handle_request()  # one request, or none in 0.5s
        return self._server.answer

    def close(self) -> None:
        self._server.server_close()

    def __enter__(self) -> CallbackServer:  # noqa: PYI034 - typing.Self needs Python 3.11; Wren supports 3.10
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Server(HTTPServer):
    answer: dict[str, str] | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # Only _Server installs this handler. Keep the base class's mutable
        # `server` type intact, and narrow it where we use our extra field.
        server = cast(_Server, self.server)
        address = urlsplit(self.path)
        if address.path != CALLBACK_PATH:
            # A browser also asks for /favicon.ico, say. Not the answer.
            self.send_error(404)
            return
        if server.answer is None:  # the first answer is the one used
            # parse_qs gives each name a list of values; one is all we need.
            params = parse_qs(address.query, keep_blank_values=True)
            server.answer = {name: values[0] for name, values in params.items()}
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Normally every request is printed, address and all. The address
        # holds the authorization code, so nothing is printed.
        pass
