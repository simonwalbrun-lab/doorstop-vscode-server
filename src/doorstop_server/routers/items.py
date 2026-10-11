from fastapi import APIRouter, Depends, Response

from doorstop_server.deps import get_tree
from doorstop_server.errors import DoorstopApiError
from doorstop_server.schemas import ItemResponse, LinkRequest, LinkResponse, UpdateItemRequest

router = APIRouter(prefix="/items")


# Attributes a document view never edits as fields (spec 030): identity, level,
# the flags and review state have their own commands, header/text their own lines.
PROTECTED_FIELDS = frozenset(
    {"uid", "level", "active", "normative", "derived", "reviewed", "cleared", "links", "header", "text"}
)


def is_protected_field(item, name: str) -> bool:
    """True for names that are not plain header fields of ``item``.

    ``Item.set`` assigns to real attributes, so a name like ``save`` or
    ``document`` would overwrite a method or property. Only ``ref`` is an
    attribute that is an editable field.
    """
    return name in PROTECTED_FIELDS or (name != "ref" and hasattr(item, name))


def apply_prose(item, header, text, fields=None) -> None:
    """Set header, text and/or extended fields through Doorstop's own setters with a single save.

    ``auto`` is switched off so several assignments do not rewrite the file
    repeatedly; Doorstop's ``save()`` handles both the YAML and the markdown
    item format.
    """
    item.auto = False
    if header is not None:
        item.header = header
    if text is not None:
        item.text = text
    for name, value in (fields or {}).items():
        item.set(name, value)
    item.save()


@router.patch("/{uid}", response_model=ItemResponse)
async def update_item(uid: str, body: UpdateItemRequest, tree=Depends(get_tree)) -> ItemResponse:
    if body.header is None and body.text is None and not body.fields:
        raise DoorstopApiError(422, "INVALID_REQUEST", "header, text or fields is required")
    item = tree.find_item(uid)
    for name in body.fields or {}:
        if is_protected_field(item, name):
            raise DoorstopApiError(422, "PROTECTED_FIELD", f"{name} cannot be edited here")
    apply_prose(item, body.header, body.text, body.fields)
    return ItemResponse(uid=str(item.uid), path=item.path, level=str(item.level))


@router.delete("/{uid}", status_code=204)
async def delete_item(uid: str, tree=Depends(get_tree)) -> Response:
    item = tree.find_item(uid)
    # Doorstop's default reorder=True: the same renumbering `doorstop remove` does.
    item.document.remove_item(item.uid)
    return Response(status_code=204)


@router.post("/{child_uid}/links", response_model=LinkResponse)
async def link_item(child_uid: str, body: LinkRequest, tree=Depends(get_tree)) -> LinkResponse:
    child, parent = tree.link_items(child_uid, body.parentUid)
    return LinkResponse(child=str(child.uid), parent=str(parent.uid))


@router.delete("/{child_uid}/links/{parent_uid}", status_code=204)
async def unlink_item(child_uid: str, parent_uid: str, tree=Depends(get_tree)) -> Response:
    tree.unlink_items(child_uid, parent_uid)
    return Response(status_code=204)
