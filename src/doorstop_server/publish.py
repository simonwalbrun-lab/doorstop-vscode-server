"""Publishing with cross-document links (spec 028).

Doorstop only looks for an item's children in the documents directly below
its own, so a link that skips a level or crosses branches is missing from the
target's child links and from the traceability matrix. Doorstop itself is
never modified: ``publish_options`` overrides two of its public methods, and
the ``PUBLISH_CHILD_LINKS`` setting, for the duration of one publish.

Run as ``python -m doorstop_server.publish [--traceability doorstop] <doorstop
publish arguments>`` to get the same output in CI.
"""

import argparse
from contextlib import contextmanager
from unittest.mock import patch

from doorstop import settings
from doorstop.core.item import Item, UnknownItem
from doorstop.core.tree import Tree


def _complete_traceability(self):
    """Doorstop 3.2's ``Tree.get_traceability``/``_iter_rows`` plus one guard.

    A neighbour whose document column is already filled in the current row is
    not followed: with children found tree-wide, upward, sideways and
    same-document links would otherwise overwrite a column or recurse forever.
    In a hierarchy-only tree the guard never triggers, so rows stay Doorstop's.
    """

    def by_uid(row):
        return ["0" + str(item.uid) if item else "1" for item in row]

    mapping = {document.prefix: index for index, document in enumerate(self.documents)}

    class Row(list):
        def __init__(self, *args, parent=False, child=False):
            super().__init__(*args)
            self.parent = parent
            self.child = child

    def unfilled(items, row):
        # `normative` first: UnknownItem has no real document.
        return [i for i in items if not (i.normative and row[mapping[i.document.prefix]] is not None)]

    def iter_rows(item, parent=True, child=True, row=None):
        if not item.normative:
            return
        row = Row([None] * len(mapping)) if row is None else Row(row, parent=row.parent, child=row.child)
        row[mapping[item.document.prefix]] = item
        if parent:
            items = unfilled(item.parent_items, row)
            for item2 in items:
                yield from iter_rows(item2, child=False, row=row)
            if not items:
                row.parent = True
        if child:
            items = unfilled(item.child_items, row)
            for item2 in items:
                yield from iter_rows(item2, parent=False, row=row)
            if not items:
                row.child = True
        if row.parent and row.child:
            yield tuple(row)

    rows = set()
    for document in self.documents:
        for item in document:
            if item.active:
                rows.update(iter_rows(item))
    return sorted(rows, key=by_uid)


@contextmanager
def publish_options(matrix: str = "complete", child_links: bool = True):
    """Child links tree-wide; matrix "complete" or Doorstop's own; Doorstop's --no-child-links.

    Class-level patches: safe because the server handles one request at a time.
    """
    original_children = Item.find_child_items_and_documents
    original_matrix = Tree.get_traceability
    index = {}
    built = False

    def tree_wide_children(self, document=None, tree=None, find_all=True):
        nonlocal built
        _, documents = original_children(self, document=document, tree=tree, find_all=False)
        # ponytail: index built from the first tree seen; one publish = one tree
        if not built:
            for document2 in tree or self.tree:
                for item2 in document2:
                    for uid in item2.links:
                        index.setdefault(str(uid), []).append(item2 if item2.active else UnknownItem(item2.uid))
            built = True
        return list(index.get(str(self.uid), [])), documents

    def doorstop_traceability(self):
        # Doorstop's own matrix must not see the tree-wide child lookup.
        with patch.object(Item, "find_child_items_and_documents", original_children):
            return original_matrix(self)

    with patch.object(Item, "find_child_items_and_documents", tree_wide_children), patch.object(
        Tree, "get_traceability", _complete_traceability if matrix == "complete" else doorstop_traceability
    ), patch.object(settings, "PUBLISH_CHILD_LINKS", child_links):
        yield


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m doorstop_server.publish",
        description="doorstop publish with complete cross-document links (spec 028); other arguments go to doorstop publish",
    )
    parser.add_argument("--traceability", choices=["complete", "doorstop"], default="complete")
    args, rest = parser.parse_known_args(argv)
    from doorstop.cli.main import main as doorstop_main

    # --no-child-links is applied by Doorstop's own CLI, inside this context.
    with publish_options(matrix=args.traceability):
        doorstop_main(["publish", *rest])


if __name__ == "__main__":
    main()
