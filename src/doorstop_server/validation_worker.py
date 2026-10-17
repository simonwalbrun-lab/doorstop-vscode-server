"""The problem check as background work in a separate process (spec 032).

Constitution Principle I v1.6.0: validation must not influence how fast the
server answers. In CPython a thread shares the interpreter lock with request
handling, so the check runs in its own long-lived worker process (research R2).
The worker keeps its own Doorstop tree, the "check copy", brings it up to date
before every job with the same partial reload the server uses, and only reads.
``GET /validate`` takes no request lock: it hands a job number to the worker
and waits on a pipe.

Cancellation (research R3): ``wanted`` holds the job whose result is still
wanted; anything else stops the running job before its next item, through
Doorstop's own ``item_hook``.

- C1 ``superseded``: a newer /validate started (``Validator.check``).
- C2 ``changed``: a mutation through the server (``deps.get_tree``).
- C2' ``changed``: the files changed on disk while the check ran (worker).
- C3 ``disconnected``: the client went away (user cancel, mode switched Off).
- C4 shutdown: the pipe breaks and the worker exits; it is a daemon process.

ponytail: after a switch to Off the idle worker keeps its tree in memory until
the server stops; stop it on Off if memory ever matters.
"""

import asyncio
import multiprocessing
import os

from starlette.concurrency import run_in_threadpool

from doorstop_server.config import Settings
from doorstop_server.doorstop_tree import fingerprint, load_tree, refresh_tree, use_libyaml
from doorstop_server.errors import DoorstopApiError
from doorstop_server.validation_rules import collect_issues


class _Cancelled(Exception):
    pass


def _lower_priority() -> None:
    """Lets the OS schedule the worker behind the server and VS Code when CPU gets short."""
    try:
        if os.name == "nt":
            import ctypes  # pylint: disable=import-outside-toplevel

            kernel32 = ctypes.windll.kernel32  # pylint: disable=no-member  # Windows only, see the os.name check
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)  # pylint: disable=no-member  # POSIX only, this is the else branch
    except OSError:
        pass


def run(conn, wanted, root: str, item_hook) -> None:
    """The worker process: answers each job number with ``(job, kind, reload_kind, payload)``."""
    _lower_priority()
    use_libyaml()
    settings = Settings(project_root=root, host="", port=0)
    parent = multiprocessing.parent_process()
    tree = fp = None
    while True:
        try:
            job = conn.recv()
        except EOFError:
            return  # the server went away (C4)
        if wanted.value != job:
            continue  # cancelled before it started

        def hook(**kwargs):
            # A dead server (C4) closes the pipe; the next recv() then ends the worker.
            if wanted.value != job or not parent.is_alive():
                raise _Cancelled
            return item_hook(**kwargs) if item_hook else []

        try:
            new = fingerprint(root)
            if fp is None:
                tree, kind = load_tree(settings), "full"
            else:
                tree, kind = refresh_tree(tree, settings, fp, new)
            fp = new
            tree.load()
            records = list(collect_issues(tree, item_hook=hook))
            if fingerprint(root) != new:
                conn.send((job, "changed", kind, None))  # C2'
            else:
                conn.send((job, "done", kind, records))
        except _Cancelled:
            continue  # the server already answered 409
        except Exception as error:  # pylint: disable=broad-except
            fp = None  # the next job loads the whole tree again
            conn.send((job, "error", None, str(error)))


class Validator:  # pylint: disable=too-many-instance-attributes  # data-model.md "Validator"
    """The server's side of the worker: starts it on the first check, sends jobs, cancels them."""

    def __init__(self, root: str, item_hook=None) -> None:
        self.root = root
        self.item_hook = item_hook  # tests only: a module-level function, spawn imports it by name
        self.ctx = multiprocessing.get_context("spawn")
        self.wanted = self.ctx.Value("i", 0)
        self.job = 0
        self.reason = None
        self.process = None
        self.conn = None
        self.lock = None  # created in check(), so it belongs to the serving event loop
        self.last_reload = None

    def cancel(self, reason: str) -> None:
        """Stops the running check, if any; its /validate answers 409 ``reason``."""
        if self.job and self.wanted.value == self.job:
            self.reason = reason
            self.wanted.value = 0

    async def check(self, is_disconnected) -> list:
        """The issue records of a fresh check, or ``DoorstopApiError`` 409 CANCELLED / 500."""
        self.cancel("superseded")
        if self.lock is None:
            self.lock = asyncio.Lock()
        # Only /validate takes this lock; the older check gives it up within one poll.
        async with self.lock:
            if self.process is None or not self.process.is_alive():
                await run_in_threadpool(self._start)
            self.job += 1
            job = self.job
            self.reason = None
            self.wanted.value = job
            try:
                self.conn.send(job)
            except OSError:
                self._ended()
            while True:
                ready = await run_in_threadpool(self._poll)
                if self.wanted.value != job:
                    raise DoorstopApiError(409, "CANCELLED", self.reason or "superseded")
                if await is_disconnected():
                    self.cancel("disconnected")
                    raise DoorstopApiError(409, "CANCELLED", "disconnected")
                if not ready:
                    if not self.process.is_alive():
                        self._ended()
                    continue
                try:
                    reply_job, kind, reload_kind, payload = self.conn.recv()
                except (EOFError, OSError):
                    self._ended()
                if reply_job != job:
                    continue  # a late reply to a cancelled job
                self.wanted.value = 0
                self.last_reload = reload_kind
                if kind == "done":
                    return payload
                if kind == "changed":
                    raise DoorstopApiError(409, "CANCELLED", "changed")
                raise DoorstopApiError(500, "VALIDATION_FAILED", payload)

    def close(self) -> None:
        """Stops the worker; safe to call more than once."""
        self.wanted.value = 0
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.process is not None:
            self.process.join(1)
            if self.process.is_alive():
                self.process.terminate()
            self.process = None

    def _start(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn, child = self.ctx.Pipe()
        self.process = self.ctx.Process(
            target=run, args=(child, self.wanted, self.root, self.item_hook), daemon=True, name="doorstop-validation"
        )
        self.process.start()
        child.close()  # only the worker holds its end, so its death reads as EOF here

    def _poll(self) -> bool:
        try:
            return self.conn.poll(0.2)
        except (EOFError, OSError):
            return True  # recv() reports it

    def _ended(self):
        self.process = None
        self.wanted.value = 0
        raise DoorstopApiError(500, "VALIDATION_FAILED", "the check process ended")
