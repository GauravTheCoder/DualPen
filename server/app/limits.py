import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from starlette.responses import JSONResponse

_FALSE = ("0", "false", "no")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def max_body_bytes() -> int:
    return _env_int("COLLAB_EDITOR_MAX_BODY_BYTES", 10 * 1024 * 1024)


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: rejects HTTP requests whose body exceeds the cap
    with 413, checking Content-Length up front and counting streamed bytes
    (chunked / lying Content-Length). WebSockets pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = max_body_bytes()
        too_large = JSONResponse({"detail": "Request body too large"}, status_code=413)

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await too_large(scope, receive, send)
                        return
                except ValueError:
                    pass

        received = 0
        response_started = False
        exceeded = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # Stop feeding the app; it sees a disconnect.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal response_started
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)
        if exceeded and not response_started:
            await too_large(scope, receive, send)


# --- rate limiting (in-process sliding window, keyed by client IP) ---

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def reset_rate_limits() -> None:
    _hits.clear()


def _rate_limit(scope_name: str, env_name: str, default: int):
    async def dependency(request: Request) -> None:
        if os.environ.get("COLLAB_EDITOR_RATE_LIMIT_ENABLED", "true").strip().lower() in _FALSE:
            return
        limit = _env_int(env_name, default)
        window = _env_int("COLLAB_EDITOR_RATE_LIMIT_WINDOW_SECONDS", 60)
        if limit <= 0:
            return
        # request.client is the direct peer; behind a reverse proxy this is
        # the proxy's address unless uvicorn --proxy-headers rewrites it.
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = _hits[(scope_name, ip)]
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(window - (now - hits[0])) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)

    return dependency


login_rate_limit = _rate_limit("login", "COLLAB_EDITOR_LOGIN_RATE_LIMIT", 10)
admin_rate_limit = _rate_limit("admin", "COLLAB_EDITOR_ADMIN_RATE_LIMIT", 120)
