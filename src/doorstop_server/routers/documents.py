from contextlib import contextmanager
from pathlib import Path

from doorstop.common import copy_dir_contents, delete
from doorstop.core import exporter, importer, publisher
from fastapi import APIRouter, Depends

from doorstop_server.deps import get_tree
from doorstop_server.errors import DoorstopApiError
from doorstop_server.routers.items import apply_prose
from doorstop_server.schemas import (
    AddItemRequest,
    CreateDocumentRequest,
    DocumentResponse,
    ExportRequest,
    ExportResponse,
    ImportRequest,
    ItemResponse,
    PublishRequest,
    PublishResponse,
    ReorderIndexResponse,
    ReorderRequest,
    ReorderResponse,
)

router = APIRouter(prefix="/documents")
publish_all_router = APIRouter()

_EXPORT_EXTENSIONS = {"yaml": ".yml", "csv": ".csv", "tsv": ".tsv", "xlsx": ".xlsx"}
_PUBLISH_EXTENSIONS = {"markdown": ".md", "html": ".html", "latex": ".tex"}


@router.post("", response_model=DocumentResponse)
async def create_document(body: CreateDocumentRequest, tree=Depends(get_tree)) -> DocumentResponse:
    document = tree.create_document(
        body.path,
        body.prefix,
        sep=body.separator,
        digits=body.digits,
        parent=body.parentPrefix,
        itemformat=body.itemFormat,
    )
    return DocumentResponse(prefix=str(document.prefix), path=document.path)


def _level_after(anchor):
    """The level Doorstop itself would give an item appended after ``anchor``.

    Mirrors ``Document.add_item``'s rule for the last item: a heading (``1.0``)
    gets its first child (``1.1``), anything else its next sibling (``1.3`` after
    ``1.2``). ``add_item(level=...)`` then shifts the existing occupants.
    """
    if anchor.level.heading:
        level = anchor.level >> 1
        level.heading = False
        return level
    return anchor.level + 1


@router.post("/{prefix}/items", response_model=ItemResponse)
async def add_item(prefix: str, body: AddItemRequest, tree=Depends(get_tree)) -> ItemResponse:
    document = tree.find_document(prefix)
    positions = [name for name, value in (("after", body.after), ("level", body.level), ("first", body.first)) if value]
    if len(positions) > 1:
        raise DoorstopApiError(
            422, "INVALID_REQUEST", f"{' and '.join(positions)} are mutually exclusive"
        )
    level = body.level
    if body.after is not None:
        anchor = tree.find_item(body.after)
        if str(anchor.document.prefix) != str(document.prefix):
            raise DoorstopApiError(
                400, "DOORSTOP_ERROR", f"{body.after} is not an item of document {prefix}"
            )
        level = _level_after(anchor)
    elif body.first:
        # The current first item's own level: add_item's reorder(keep=item)
        # then shifts that item and everything after it down, so the new item
        # really is first in reading order. An empty document needs no level.
        existing = document.items
        level = existing[0].level.copy() if existing else None
    item = document.add_item(level=level)
    if body.header is not None or body.text is not None:
        apply_prose(item, body.header, body.text)
    # The level after Doorstop's reorder, not the requested one.
    return ItemResponse(uid=str(item.uid), path=item.path, level=str(item.level))


@router.post("/{prefix}/reorder/index", response_model=ReorderIndexResponse)
async def ensure_reorder_index(prefix: str, tree=Depends(get_tree)) -> ReorderIndexResponse:
    document = tree.find_document(prefix)
    if not document.index:
        document.index = True
    return ReorderIndexResponse(prefix=prefix, indexPath=document.index)


@router.delete("/{prefix}/reorder/index", status_code=204)
async def discard_reorder_index(prefix: str, tree=Depends(get_tree)) -> None:
    document = tree.find_document(prefix)
    del document.index


@router.post("/{prefix}/reorder", response_model=ReorderResponse)
async def reorder_document(prefix: str, body: ReorderRequest, tree=Depends(get_tree)) -> ReorderResponse:
    document = tree.find_document(prefix)
    if body.mode == "auto":
        document.reorder(manual=False)
    else:
        if not document.index:
            raise DoorstopApiError(
                409,
                "NO_REORDER_INDEX",
                "No index file to reorder from -- call POST /documents/{prefix}/reorder/index first.",
            )
        document.reorder(manual=True, automatic=False)
    return ReorderResponse(prefix=prefix, mode=body.mode)


@router.post("/{prefix}/import", response_model=DocumentResponse)
async def import_into_document(prefix: str, body: ImportRequest, tree=Depends(get_tree)) -> DocumentResponse:
    document = tree.find_document(prefix)
    ext = Path(body.sourcePath).suffix
    importer.import_file(body.sourcePath, document, ext)
    return DocumentResponse(prefix=str(document.prefix), path=document.path)


def _resolve_written_path(reported: str | None, requested: str) -> str:
    """The path the renderer really wrote.

    Doorstop's ``publish``/``export`` return the destination they were *asked*
    for, which is not always where the output lands: publishing HTML, for
    instance, nests the document under a ``documents/`` directory next to the
    requested file (and drops a ``template/`` directory beside it). Reporting the
    requested path in that case sends the user to a file that does not exist,
    which is exactly what spec 004 FR-008 exists to prevent.

    Falls back to the reported path when nothing can be confirmed on disk, so a
    renderer this does not know about is never made worse.
    """
    candidate = reported or requested
    if Path(candidate).exists():
        return candidate

    nested = Path(candidate).parent / "documents" / Path(candidate).name
    if nested.exists():
        return str(nested)
    return candidate


@router.post("/{prefix}/export", response_model=ExportResponse)
async def export_document(prefix: str, body: ExportRequest, tree=Depends(get_tree)) -> ExportResponse:
    document = tree.find_document(prefix)
    ext = _EXPORT_EXTENSIONS[body.format]
    path = exporter.export(document, body.destinationPath, ext=ext)
    return ExportResponse(path=_resolve_written_path(path, body.destinationPath))


@contextmanager
def _borrowed_template(tree, document, body: PublishRequest):
    """Gives ``document`` another document's ``template`` folder for one publish.

    Doorstop only looks for ``<document>/template``, so a template kept next to
    one document is invisible to the others (spec 024). The copy is removed
    again whatever happens, and a folder that was already there is never touched.
    """
    needed = body.sharedTemplate and body.template and body.format != "markdown" and document.template is None
    owner = next((d for d in tree.documents if d.template), None) if needed else None
    if owner is None:
        yield
        return
    target = Path(document.path) / "template"
    target.mkdir()
    try:
        copy_dir_contents(owner.template, str(target))
        yield
    finally:
        delete(str(target))


@router.post("/{prefix}/publish", response_model=PublishResponse)
async def publish_document(prefix: str, body: PublishRequest, tree=Depends(get_tree)) -> PublishResponse:
    document = tree.find_document(prefix)
    ext = _PUBLISH_EXTENSIONS[body.format]
    with _borrowed_template(tree, document, body):
        path = publisher.publish(document, body.destinationPath, ext=ext, template=body.template)
    return PublishResponse(path=_resolve_written_path(path, body.destinationPath))


@publish_all_router.post("/publish", response_model=PublishResponse)
async def publish_tree(body: PublishRequest, tree=Depends(get_tree)) -> PublishResponse:
    """Doorstop's own tree publish: every document in one run (spec 024 US2)."""
    ext = _PUBLISH_EXTENSIONS[body.format]
    path = publisher.publish(tree, body.destinationPath, ext=ext, template=body.template)
    index = Path(path) / "index.html"
    return PublishResponse(path=str(index) if index.exists() else path)
