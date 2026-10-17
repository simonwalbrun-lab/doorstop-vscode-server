import os
from typing import Iterator

import doorstop
from fastapi import Request

from doorstop_server import timing
from doorstop_server.config import Settings
from doorstop_server.doorstop_tree import fingerprint, load_tree, refresh_tree


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _shared_tree(request: Request) -> doorstop.Tree:
    """The one tree every request shares, brought up to date with disk first.

    Only documents whose files changed are re-read (spec 032 FR-020); a full
    rebuild (~0.7 s for 1,500 items) happens only when the tree's shape may have
    changed or nothing is cached yet. Checking for changes only stats files.
    The fingerprint is taken before the refresh: an edit made meanwhile shows
    up as a change on the next request instead of being missed.

    A reload here deliberately does not cancel a running check: it is usually
    the very save the check already sees, and a change during the check is
    caught by the worker itself (spec 032 research R3, C2').
    """
    state = request.app.state
    settings = get_settings(request)
    with timing.timed("load"):
        current = fingerprint(settings.project_root)
        if state.tree_fingerprint is None:
            state.cached_tree, kind = load_tree(settings), "full"
        else:
            state.cached_tree, kind = refresh_tree(state.cached_tree, settings, state.tree_fingerprint, current)
        state.tree_fingerprint = current
        if kind != "none":
            state.tree_version += 1
    return state.cached_tree


def get_tree(request: Request) -> Iterator[doorstop.Tree]:
    """The shared tree, for requests that change the project.

    A running check is cancelled as ``changed`` once the request has changed
    project files (spec 032 R3, C2): its result would describe the state before.
    Requests that write nothing the check reads (publish, export) leave it
    running. If the request fails, Doorstop's in-memory objects may
    no longer match disk, so the next request rebuilds from scratch.

    If it succeeds, the files it wrote through the tree already match memory,
    so they are taken into the fingerprint instead of being re-read by the next
    request. A written ``.doorstop*`` file (new document, skip marker) changes
    the tree's shape, so it is left for the next request's full rebuild.
    ponytail: an outside edit landing during the request itself is absorbed
    too; per-path tracking of the request's own writes if that ever bites.
    """
    state = request.app.state
    tree = _shared_tree(request)
    state.tree_version += 1
    try:
        yield tree
    except Exception:
        state.tree_fingerprint = None
        state.validator.cancel("changed")  # it may have written part of its change
        raise
    before = state.tree_fingerprint
    after = fingerprint(get_settings(request).project_root)
    written = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    if written:
        state.validator.cancel("changed")
    if not any(os.path.basename(path).startswith(".doorstop") for path in written):
        state.tree_fingerprint = after


def get_tree_for_reading(request: Request) -> doorstop.Tree:
    """The shared tree, for read-only requests."""
    return _shared_tree(request)


def load_items(tree: doorstop.Tree) -> None:
    """Reads every item file (Doorstop otherwise loads them lazily); timed as `load`, not `work`."""
    with timing.timed("load"):
        tree.load()
