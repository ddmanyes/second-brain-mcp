"""Hardened HTTP reads for local model services in multi-user mode."""

from __future__ import annotations

import urllib.error
import urllib.request
from contextlib import suppress
from urllib.parse import urlsplit

from .visibility import multiuser_enabled

DEFAULT_RESPONSE_BYTES = 1024**2
MAX_RESPONSE_BYTES = 128 * 1024**2
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LocalModelRequestError(RuntimeError):
    """A fixed-message local model transport or response failure."""


class LocalModelHTTPError(LocalModelRequestError):
    """Bounded HTTP status/body detail for internal retry decisions only."""

    def __init__(self, code: int, body: bytes):
        super().__init__("local model request failed")
        self.code = code
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


def _validate_request(request) -> None:
    try:
        parsed = urlsplit(request.full_url)
        valid = (
            parsed.scheme.lower() == "http"
            and parsed.hostname is not None
            and parsed.hostname.lower() in _LOOPBACK_HOSTS
            and parsed.username is None
            and parsed.password is None
        )
        # Accessing port also rejects malformed/out-of-range values.
        _ = parsed.port
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise LocalModelRequestError("local model request refused")


def request_bytes(
    request,
    *,
    timeout: float,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
) -> bytes:
    """Open one request and return a bounded body in multi-user mode.

    Single-user services retain their historical ``urllib.request.urlopen``
    behavior, including proxy and redirect handling and an unbounded read.
    """
    if not multiuser_enabled():
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES
    ):
        raise ValueError("invalid local model response limit")
    _validate_request(request)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        body = b""
        with suppress(Exception):
            body = error.read(max_response_bytes)
        raise LocalModelHTTPError(int(error.code), body) from None
    except (OSError, urllib.error.URLError, ValueError):
        raise LocalModelRequestError("local model request failed") from None
    if len(body) > max_response_bytes:
        raise LocalModelRequestError("local model response unavailable")
    return body
