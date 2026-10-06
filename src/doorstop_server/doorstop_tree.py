import os

import doorstop

from doorstop_server.config import Settings

# The same folders doorstop.core.builder.build() skips when it looks for documents.
_EXCLUDED_DIRS = {".git", ".tox", ".venv", "venv"}


def load_tree(settings: Settings) -> doorstop.Tree:
    """Build a fresh Tree from disk.

    Requests that change the project always use a fresh tree, and read-only
    requests reuse one only while ``fingerprint()`` shows no file changed, so
    hand edits made in VS Code between two server calls are never missed or
    clobbered. ``request_next_number=None`` is the ``--force`` equivalent the
    CLI uses to skip Doorstop's own numbering server -- safe here because
    this server is the only process ever mutating the project and every
    request is fully serialized.
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
    """
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS]
        for name in filenames:
            if name.startswith(".doorstop") or name.endswith((".yml", ".yaml", ".md")):
                path = os.path.join(dirpath, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue  # deleted while scanning; the next scan sees that as a change
                files[path] = (stat.st_mtime_ns, stat.st_size)
    return files
