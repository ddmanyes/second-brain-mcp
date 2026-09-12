"""Bound authenticated JSON request bodies before MCP parses them."""

from __future__ import annotations

import asyncio


class HTTPBodyLimitMiddleware:
    def __init__(
        self, app, *, max_body_bytes=8 * 1024**2, timeout=10.0, upload_prefix=None
    ):
        if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= 8 * 1024**2:
            raise ValueError("invalid request body limit")
        if not 0 < timeout <= 10:
            raise ValueError("invalid request body deadline")
        self.app = app
        self.maximum = max_body_bytes
        self.timeout = timeout
        self.upload_prefix = upload_prefix

    async def __call__(self, scope, receive, send):
        # Upload PUT has its own owner ticket, streaming byte limit and deadline.
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "PUT", "PATCH"}
            or (
                self.upload_prefix
                and scope.get("method") == "PUT"
                and scope.get("path", "").startswith(self.upload_prefix)
            )
        ):
            return await self.app(scope, receive, send)

        async def reject(status):
            body = b'{"error":"request body rejected"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(lengths) > 1 or (
            lengths and (not lengths[0].isdigit() or len(lengths[0]) > 10)
        ):
            return await reject(400)
        if lengths and int(lengths[0]) > self.maximum:
            return await reject(413)
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        return await reject(400)
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.maximum:
                        return await reject(413)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408)
        if lengths and len(body) != int(lengths[0]):
            return await reject(400)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)
