import io
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcp_second_brain import late_chunking, local_model_http, reranker, vault_db
from mcp_second_brain.local_model_http import (
    DEFAULT_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    LocalModelHTTPError,
    LocalModelRequestError,
    request_bytes,
)


class Response:
    def __init__(self, body=b"{}"):
        self.body = body
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_multiuser_rejects_external_and_userinfo_before_any_io(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    opened = []
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: opened.append(True),
    )
    for url in (
        "http://example.org/model",
        "https://localhost/model",
        "http://user:password@localhost/model",
    ):
        with pytest.raises(LocalModelRequestError, match="^local model request refused$"):
            request_bytes(urllib.request.Request(url), timeout=1)
    assert opened == []


def test_multiuser_opener_has_empty_proxy_map_and_no_redirect_handler(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    response = Response(b'{"ok": true}')
    observed = {}

    class Opener:
        def open(self, request, *, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            return response

    def build(*handlers):
        observed["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    body = request_bytes(
        urllib.request.Request("http://127.0.0.1:8080/model"),
        timeout=0.5,
        max_response_bytes=32,
    )
    assert body == b'{"ok": true}'
    proxy = next(
        handler
        for handler in observed["handlers"]
        if isinstance(handler, urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(
        isinstance(handler, urllib.request.HTTPRedirectHandler)
        for handler in observed["handlers"]
    )
    assert response.read_sizes == [33]


def test_success_and_http_error_bodies_are_bounded_and_errors_are_fixed(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")

    class Opener:
        def __init__(self, result):
            self.result = result

        def open(self, *_args, **_kwargs):
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    too_large = Response(b"x" * 20)
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *_handlers: Opener(too_large)
    )
    with pytest.raises(LocalModelRequestError) as oversized:
        request_bytes(
            urllib.request.Request("http://localhost/model"),
            timeout=1,
            max_response_bytes=8,
        )
    assert str(oversized.value) == "local model response unavailable"
    assert too_large.read_sizes == [9]

    error = urllib.error.HTTPError(
        "http://localhost/private-path",
        500,
        "secret reason",
        {},
        io.BytesIO(b"too large and secret"),
    )
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *_handlers: Opener(error)
    )
    with pytest.raises(LocalModelHTTPError) as caught:
        request_bytes(
            urllib.request.Request("http://localhost/model"),
            timeout=1,
            max_response_bytes=8,
        )
    assert caught.value.code == 500
    assert caught.value.body == b"too larg"
    assert str(caught.value) == "local model request failed"
    assert "private-path" not in str(caught.value)


def test_single_user_keeps_legacy_urlopen_path(monkeypatch):
    monkeypatch.delenv("SB_MULTIUSER", raising=False)
    response = Response(b"legacy")
    calls = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: calls.append((request, timeout)) or response,
    )
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("single-user path built a hardened opener"),
    )
    request = urllib.request.Request("https://example.org/legacy")
    assert request_bytes(request, timeout=2, max_response_bytes=1) == b"legacy"
    assert calls == [(request, 2)]
    assert response.read_sizes == [-1]


def test_model_clients_route_reads_through_bounded_helper(monkeypatch):
    responses = iter(
        (
            b'{"data":[{"embedding":[0.25]}]}',
            b'{"tokens":[1,2]}',
            b'{"results":[{"index":0,"relevance_score":0.75}]}',
        )
    )
    limits = []

    def fake_request_bytes(
        request,
        *,
        timeout,
        max_response_bytes=DEFAULT_RESPONSE_BYTES,
    ):
        assert isinstance(request, urllib.request.Request)
        assert timeout > 0
        limits.append(max_response_bytes)
        return next(responses)

    monkeypatch.setattr(local_model_http, "request_bytes", fake_request_bytes)

    assert vault_db._call_embed_api("private query") == [0.25]
    assert late_chunking._post_json("/tokenize", {"content": "private text"}) == {
        "tokens": [1, 2]
    }
    assert reranker.rerank("private query", ["private document"]) == [0.75]
    assert limits == [DEFAULT_RESPONSE_BYTES, MAX_RESPONSE_BYTES, DEFAULT_RESPONSE_BYTES]


def test_model_clients_do_not_surface_hardened_transport_details(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise LocalModelRequestError("local model request failed")

    monkeypatch.setattr(local_model_http, "request_bytes", fail)

    assert vault_db._call_embed_api("private query") is None
    with pytest.raises(late_chunking.LateChunkingUnavailable) as late_error:
        late_chunking._post_json("/private-path", {"content": "private text"})
    assert str(late_error.value) == "local model request failed"
    assert reranker.rerank("private query", ["private document"]) is None
    assert capsys.readouterr().err == "[reranker] unavailable, skipping rerank\n"


def test_real_local_redirect_is_not_followed(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    hits = {"redirect": 0, "target": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                hits["redirect"] += 1
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
            else:
                hits["target"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"target")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect"
        )
        with pytest.raises(LocalModelHTTPError) as error:
            request_bytes(request, timeout=1)
        assert error.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert hits == {"redirect": 1, "target": 0}
