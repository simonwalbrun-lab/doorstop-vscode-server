from typing import Optional

from doorstop.common import DoorstopError
from fastapi import APIRouter, Depends, Query, Request

from doorstop_server.deps import get_settings, get_tree_for_reading, load_items
from doorstop_server.doorstop_tree import find_skipped_documents
from doorstop_server.routers.items import is_protected_field
from doorstop_server.schemas import DocumentNode, ItemNode, LinkInfo, SkippedDocument, TreeResponse

router = APIRouter()


def _item_links(item, tree) -> list[LinkInfo]:
    links = []
    for uid in item.links:
        try:
            parent = tree.find_item(uid)
            suspect = uid.stamp != parent.stamp()
        except DoorstopError:
            # Dangling/renamed parent UID - treat as suspect rather than failing the request.
            suspect = True
        links.append(LinkInfo(uid=str(uid), suspect=suspect))
    return links


def _item_fields(item, names: list[str]) -> Optional[dict[str, str]]:
    """The requested header fields the item has as a non-empty scalar (spec 030)."""
    if not names:
        return None
    fields = {}
    for name in names:
        if is_protected_field(item, name):
            continue
        value = item.get(name)
        if isinstance(value, (str, int, float, bool)) and str(value) != "":
            fields[name] = str(value)
    return fields


@router.get("/tree", response_model=TreeResponse)
async def get_tree_structure(
    request: Request,
    fields: Optional[str] = Query(default=None, description="Comma-separated header fields to include"),
    tree=Depends(get_tree_for_reading),
) -> TreeResponse:
    # Unchanged tree, same fields: the last answer is still exact (tree_version
    # moves on every reload and every mutation), and building it costs ~0.3 s
    # on 1,500 items. Views ask for the tree often (spec 032 profiling).
    state = request.app.state
    cached = state.tree_responses.get(fields or "")
    if cached and cached[0] == state.tree_version:
        return cached[1]
    load_items(tree)
    names = [name.strip() for name in (fields or "").split(",") if name.strip()]
    documents = [
        DocumentNode(
            prefix=str(document.prefix),
            markerPath=document.config,
            parentPrefix=document.parent or None,
            digits=document.digits,
            separator=document.sep,
            itemFormat=str(document.itemformat) if document.itemformat else None,
            items=[
                ItemNode(
                    uid=str(item.uid),
                    path=item.path,
                    level=str(item.level),
                    header=str(item.header) if item.header else None,
                    text=str(item.text) if item.text else None,
                    ref=str(item.ref) if item.ref else None,
                    active=item.active,
                    normative=item.normative,
                    derived=item.derived,
                    reviewed=item.reviewed,
                    cleared=item.cleared,
                    links=_item_links(item, tree),
                    fields=_item_fields(item, names),
                )
                # Doorstop's own order (Item.__lt__: level, then UID) rather than
                # file-load order; `document.items` would also drop inactive items.
                for item in sorted(document)
            ],
        )
        for document in tree
    ]
    skipped = [
        SkippedDocument(prefix=prefix, markerPath=marker)
        for prefix, marker in find_skipped_documents(get_settings(request).project_root)
    ]
    response = TreeResponse(documents=documents, skippedDocuments=skipped)
    state.tree_responses[fields or ""] = (state.tree_version, response)
    return response
