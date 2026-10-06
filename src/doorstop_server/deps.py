import doorstop
from fastapi import Request

from doorstop_server import timing
from doorstop_server.config import Settings
from doorstop_server.doorstop_tree import load_tree


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_tree(request: Request) -> doorstop.Tree:
    with timing.timed("load"):
        return load_tree(get_settings(request))


def load_items(tree: doorstop.Tree) -> None:
    """Reads every item file (Doorstop otherwise loads them lazily); timed as `load`, not `work`."""
    with timing.timed("load"):
        tree.load()
