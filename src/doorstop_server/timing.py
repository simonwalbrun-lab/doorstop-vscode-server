import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

# Per-request stage durations in ms, reported to the extension as a W3C
# Server-Timing header (spec 023). The middleware in lock.py sets a fresh dict
# per request; sync dependencies run in a thread pool with a copy of the
# context, which still points at the same dict, so their writes are seen.
stages: ContextVar[Optional[dict]] = ContextVar("timing_stages", default=None)

STAGE_NAMES = ("wait", "load", "work")


@contextmanager
def timed(stage: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        current = stages.get()
        if current is not None:
            current[stage] += (time.perf_counter() - started) * 1000


def header_value(current: dict, route: str) -> bytes:
    metrics = ", ".join(f"{name};dur={current.get(name, 0.0):.2f}" for name in STAGE_NAMES)
    escaped = route.replace("\\", "\\\\").replace('"', '\\"')
    return f'{metrics}, route;desc="{escaped}"'.encode("latin-1", "replace")
