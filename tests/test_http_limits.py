import asyncio

import pytest

from mcp_second_brain.http_limits import HTTPBodyLimitMiddleware


def exercise(messages, *, headers=(), timeout=0.1, path="/mcp", method="POST"):
    sent, received = [], []

    async def app(scope, receive, send):
        received.append(await receive())
        await send({"type": "http.response.start", "status": 200})

    async def receive():
        if not messages:
            await asyncio.sleep(1)
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    middleware = HTTPBodyLimitMiddleware(
        app, max_body_bytes=4, timeout=timeout, upload_prefix="/uploads/"
    )
    asyncio.run(
        middleware(
            {"type": "http", "method": method, "path": path, "headers": headers},
            receive,
            send,
        )
    )
    return sent, received


def test_chunked_overflow_refused_before_parser():
    sent, received = exercise(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de"},
        ]
    )
    assert sent[0]["status"] == 413
    assert received == []


@pytest.mark.parametrize(
    "headers,status",
    [
        ([(b"content-length", b"5")], 413),
        ([(b"content-length", b"x")], 400),
        ([(b"content-length", b"2"), (b"content-length", b"2")], 400),
    ],
)
def test_invalid_declared_body_never_consumed(headers, status):
    sent, received = exercise([], headers=headers)
    assert sent[0]["status"] == status and not received


def test_slow_body_times_out_before_parser():
    sent, received = exercise([], timeout=0.01)
    assert sent[0]["status"] == 408 and not received


def test_valid_body_replayed_once_and_disconnect_not_processed():
    sent, received = exercise(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd"},
        ],
        headers=[(b"content-length", b"4")],
    )
    assert sent[0]["status"] == 200
    assert received == [{"type": "http.request", "body": b"abcd", "more_body": False}]
    assert exercise([{"type": "http.disconnect"}]) == ([], [])


def test_upload_keeps_existing_streaming_ticket_boundary():
    sent, received = exercise(
        [{"type": "http.request", "body": b"123456"}],
        path="/uploads/owned",
        method="PUT",
    )
    assert sent[0]["status"] == 200 and received[0]["body"] == b"123456"
