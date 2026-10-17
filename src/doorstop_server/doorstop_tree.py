import os

import doorstop
import yaml
from doorstop import common
from doorstop.common import DoorstopError
from doorstop.core.base import BaseFileObject
from doorstop.core.document import Document
from doorstop.core.types import _Literal

from doorstop_server.config import Settings

# The same folders doorstop.core.builder.build() skips when it looks for documents.
_EXCLUDED_DIRS = {".git", ".tox", ".venv", "venv"}


def load_tree(settings: Settings) -> doorstop.Tree:
    """Build a fresh Tree from disk.

    Every request shares one tree that ``refresh_tree()`` brings up to date
    from ``fingerprint()`` first, so hand edits made in VS Code between two
    server calls are never missed or clobbered. ``request_next_number=None`` is
    the ``--force`` equivalent the CLI uses to skip Doorstop's own numbering
    server -- safe here because this server is the only process ever mutating
    the project and every request is fully serialized.
    """
    return doorstop.build(
        cwd=settings.project_root,
        root=settings.project_root,
        request_next_number=None,
    )


def fingerprint(root: str) -> dict:
    """Modified time and size of every file Doorstop could read to build the tree.

    Deliberately over-inclusive (any .yml/.yaml/.md, every .doorstop* marker): an
    unrelated change only costs one extra rebuild, a missed one would serve stale
    data. ponytail: a same-size edit within the filesystem's timestamp resolution
    goes unnoticed; fine on NTFS/ext4/APFS, hash the files if a coarse-timestamp
    filesystem (FAT, some network shares) ever matters.

    ``os.scandir``: on Windows ``DirEntry.stat()`` comes from the directory
    listing, no extra system call per file (~20x faster than ``os.stat`` on
    1,500 files). It reflects a file once its writer closed it, as editors do.
    """
    files = {}
    pending = [root]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue  # removed while scanning; the next scan sees that as a change
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _EXCLUDED_DIRS:
                            pending.append(entry.path)
                    elif entry.name.startswith(".doorstop") or entry.name.endswith((".yml", ".yaml", ".md")):
                        stat = entry.stat()
                        files[entry.path] = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    continue
    return files


def refresh_tree(tree: doorstop.Tree, settings: Settings, old: dict, new: dict) -> tuple[doorstop.Tree, str]:
    """Brings ``tree`` up to date with the files on disk: ``"none"``, ``"partial"`` or ``"full"``.

    Spec 032 FR-020..023. A changed ``.doorstop*`` file (config, skip marker,
    new or removed document) can change the tree's shape, so it rebuilds it.
    Otherwise each changed file belongs to the deepest document folder holding
    it, and only those documents re-read their items; files outside every
    document cannot be part of the tree and need nothing.

    ponytail: ``Document._iter(reload=True)`` and ``Tree._item_cache`` are
    private Doorstop 3.2 API (pinned like ``CHECK_TABLE``); switch to a public
    per-document reload if Doorstop ever gains one. Any failure falls back to a
    full rebuild.
    """
    changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
    if not changed:
        return tree, "none"
    if any(os.path.basename(path).startswith(".doorstop") for path in changed):
        return load_tree(settings), "full"

    documents = sorted(tree, key=lambda document: len(document.path), reverse=True)
    folders = [(document, os.path.normcase(os.path.abspath(document.path)) + os.sep) for document in documents]
    affected = []
    for path in changed:
        normalized = os.path.normcase(os.path.abspath(path))
        owner = next((document for document, folder in folders if normalized.startswith(folder)), None)
        if owner is not None and owner not in affected:
            affected.append(owner)
    if not affected:
        return tree, "none"
    try:
        for document in affected:
            list(document._iter(reload=True))  # pylint: disable=protected-access
        # Refilled, not just cleared: every miss in Tree.find_item scans the
        # documents item by item, which made the next GET /tree ~10x slower.
        cache = tree._item_cache  # pylint: disable=protected-access
        cache.clear()
        for document in tree:
            for item in document:
                cache[item.uid] = item
    except Exception:  # pylint: disable=broad-except
        return load_tree(settings), "full"
    return tree, "partial"


def use_libyaml() -> None:
    """Makes Doorstop parse and write YAML with libyaml's C code instead of PyYAML's Python.

    Same data and byte-identical text, 5-9x faster (spec 032 profiling).
    Doorstop calls ``load_yaml`` without a loader and registers its literal-block
    representer only for the Python dumper, so both are wired up here. Skipped
    when PyYAML was built without libyaml.
    """
    if not yaml.__with_libyaml__:
        return
    common.load_yaml.__defaults__ = (yaml.CSafeLoader,)
    # The C emitter accepts only exact str, and _Literal is a str subclass.
    yaml.add_representer(_Literal, lambda dumper, data: _Literal.representer(dumper, str(data)), Dumper=yaml.CDumper)
    BaseFileObject._dump = staticmethod(  # pylint: disable=protected-access
        lambda data: yaml.dump(data, Dumper=yaml.CDumper, default_flow_style=False, allow_unicode=True)
    )


def find_skipped_documents(root: str) -> list[tuple[str, str]]:
    """(prefix, marker path) of every document Doorstop skips because of its ``.doorstop.skip``.

    ``doorstop.build`` drops those documents, so the tree cannot list them; the
    extension needs them to offer "enable".
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS]
        if Document.SKIP in filenames:
            try:
                document = Document(dirpath, root, tree=None)
            except DoorstopError:
                continue  # a stray skip file without a document
            found.append((str(document.prefix), document.config))
    return sorted(found)
