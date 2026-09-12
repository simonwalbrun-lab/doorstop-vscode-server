from fastapi import APIRouter, Depends, Response

from doorstop_server.deps import get_tree
from doorstop_server.errors import DoorstopApiError
from doorstop_server.schemas import ItemResponse, LinkRequest, LinkResponse, UpdateItemRequest

router = APIRouter(prefix="/items")


def apply_prose(item, header, text) -> None:
    """Set header and/or text through Doorstop's own setters with a single save.

    ``auto`` is switched off so two assignments do not rewrite the file twice;
    Doorstop's ``save()`` handles both the YAML and the markdown item format.
    """
    item.auto = False
    if header is not None:
        item.header = header
    if text is not None:
        item.text = text
    item.save()


@router.patch("/{uid}", response_model=ItemResponse)
async def update_item(uid: str, body: UpdateItemRequest, tree=Depends(get_tree)) -> ItemResponse:
    if body.header is None and body.text is None:
        raise DoorstopApiError(422, "INVALID_REQUEST", "header or text is required")
    item = tree.find_item(uid)
    apply_prose(item, body.header, body.text)
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
