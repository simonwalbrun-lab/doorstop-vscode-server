"""Item hooks for the validation worker in tests (spec 032 T031).

The worker is a spawned process, which imports these by module name, so they
must be plain module-level functions. Doorstop calls the hook before checking
each item (``Document.get_issues(item_hook=...)``); returning ``[]`` adds no issue.
"""

import os
import time

from doorstop.common import DoorstopWarning


def busy(**_):
    """Keeps a CPU core busy for 50 ms, like a real check on a huge tree."""
    until = time.perf_counter() + 0.05
    while time.perf_counter() < until:
        pass
    return []


def slow(**_):
    """Makes each item take 0.2 s without using the CPU: a long check to act on."""
    time.sleep(0.2)
    return []


def future_message(**_):
    """Reports a message no Doorstop 3.2 template matches."""
    return [DoorstopWarning("a message from a future Doorstop")]


def pid(**_):
    """Writes the worker's process id to the file named by DOORSTOP_TEST_PID_FILE."""
    with open(os.environ["DOORSTOP_TEST_PID_FILE"], "w", encoding="utf-8") as file:
        file.write(str(os.getpid()))
    return []
