import asyncio
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from doorstop_server import timing

_UNLOCKED_PATHS = {"/health"}


class SerializeRequestsMiddleware:
    """Ensures the app answers exactly one request at a time (except /health).

    Implemented as a bare ASGI middleware -- not `@app.middleware("http")`
    (Starlette's BaseHTTPMiddleware) -- because BaseHTTPMiddleware runs the
    downstream app inside a separate anyio task/stream that our
    @app.exception_handler() registrations cannot see through: an unexpected
    exception raised in a route would bypass the structured {"error": ...}
    response entirely and blow past this middleware as a raw exception. A plain
    ASGI middleware just awaits the inner app directly, so by the time control
    returns here any exception has already been turned into a normal response.

    The lock is created per middleware instance (i.e. per app / per
    create_app() call), not as a module-level global, so it's always bound to
    whatever event loop first serves that app.

    It also reports how long the request waited for the lock, loaded the tree
    and worked, as a Server-Timing header (spec 023, timing.py).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        current = dict.fromkeys(timing.STAGE_NAMES, 0.0)
        timing.stages.set(current)
        acquired = time.perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                current["work"] = max(0.0, (time.perf_counter() - acquired) * 1000 - current["load"])
                route = getattr(scope.get("route"), "path", scope["path"])
                header = (b"server-timing", timing.header_value(current, f'{scope["method"]} {route}'))
                message = {**message, "headers": [*message.get("headers", []), header]}
            await send(message)

        if scope["path"] in _UNLOCKED_PATHS:
            await self.app(scope, receive, send_with_timing)
            return
        async with self.lock:
            # send_with_timing reads `acquired` when it runs, so work starts here.
            current["wait"] = (time.perf_counter() - acquired) * 1000
            acquired = time.perf_counter()
            await self.app(scope, receive, send_with_timing)
