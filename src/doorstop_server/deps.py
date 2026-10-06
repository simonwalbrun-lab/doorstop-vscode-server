import doorstop
from fastapi import Request

from doorstop_server import timing
from doorstop_server.config import Settings
from doorstop_server.doorstop_tree import fingerprint, load_tree


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_tree(request: Request) -> doorstop.Tree:
    """A fresh tree, for requests that change the project."""
    with timing.timed("load"):
        return load_tree(get_settings(request))


def get_tree_for_reading(request: Request) -> doorstop.Tree:
    """A tree shared between read-only requests, rebuilt whenever a project file changed.

    Rebuilding reads every item file (~0.7 s for 1,500 items); checking for
    changes only stats them. Requests that change the project never get this
    tree (they use get_tree), so its in-memory state only ever reflects disk.
    The fingerprint is taken before the build: an edit made while building
    shows up as a change on the next request instead of being missed.
    """
    state = request.app.state
    with timing.timed("load"):
        current = fingerprint(get_settings(request).project_root)
        if current != state.tree_fingerprint:
            state.cached_tree = load_tree(get_settings(request))
            state.tree_fingerprint = current
    return state.cached_tree


def load_items(tree: doorstop.Tree) -> None:
    """Reads every item file (Doorstop otherwise loads them lazily); timed as `load`, not `work`."""
    with timing.timed("load"):
        tree.load()
