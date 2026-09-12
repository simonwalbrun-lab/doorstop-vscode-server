from typing import List, Literal, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    projectRoot: str


class CreateDocumentRequest(BaseModel):
    prefix: str
    path: str
    parentPrefix: Optional[str] = None
    itemFormat: Optional[Literal["yaml", "markdown"]] = None
    digits: Optional[int] = None
    separator: Optional[str] = None


class DocumentResponse(BaseModel):
    prefix: str
    path: str


class AddItemRequest(BaseModel):
    level: Optional[str] = None
    # UID of an item of the same document to insert after (spec 019): the new
    # level is derived from it by Doorstop's own append rule. Exclusive with level.
    after: Optional[str] = None
    header: Optional[str] = None
    text: Optional[str] = None


class UpdateItemRequest(BaseModel):
    """Partial update of an item's editable prose (spec 019). At least one field."""

    header: Optional[str] = None
    text: Optional[str] = None


class ItemResponse(BaseModel):
    uid: str
    path: str
    level: str


class ReviewClearRequest(BaseModel):
    scope: Literal["item", "document", "all"]
    target: Optional[str] = None
    parents: Optional[List[str]] = None


class LinkRequest(BaseModel):
    parentUid: str


class LinkResponse(BaseModel):
    child: str
    parent: str


class ReorderRequest(BaseModel):
    mode: Literal["auto", "manual"]


class ReorderResponse(BaseModel):
    prefix: str
    mode: Literal["auto", "manual"]


class ReorderIndexResponse(BaseModel):
    prefix: str
    indexPath: str


class ImportRequest(BaseModel):
    sourcePath: str


class ExportRequest(BaseModel):
    format: Literal["yaml", "csv", "tsv", "xlsx"]
    destinationPath: str


class ExportResponse(BaseModel):
    path: str


class PublishRequest(BaseModel):
    format: Literal["markdown", "html", "latex"]
    destinationPath: str


class PublishResponse(BaseModel):
    path: str


class LinkInfo(BaseModel):
    uid: str
    suspect: bool


class ItemNode(BaseModel):
    uid: str
    path: str
    level: str
    header: Optional[str] = None
    text: Optional[str] = None
    ref: Optional[str] = None
    active: bool
    normative: bool
    derived: bool
    reviewed: bool
    cleared: bool
    links: List[LinkInfo]


class DocumentNode(BaseModel):
    prefix: str
    markerPath: str
    parentPrefix: Optional[str] = None
    digits: Optional[int] = None
    separator: Optional[str] = None
    itemFormat: Optional[str] = None
    items: List[ItemNode]


class TreeResponse(BaseModel):
    documents: List[DocumentNode]


FieldAnchor = Literal[
    "document", "level", "text", "reviewed", "links", "link_entry", "derived", "ref"
]


class ValidationIssue(BaseModel):
    """One problem Doorstop reports, classified and anchored.

    ``uids`` carries the fan-out: a problem naming several items (a duplicate
    level, say) is ONE record listing them all, not one record each.
    """

    severity: Literal["error", "warning", "info"]
    check: str
    message: str
    documentPrefix: str
    uids: List[str]
    relatedUid: Optional[str] = None
    field: Optional[FieldAnchor] = None


class ValidationResponse(BaseModel):
    issues: List[ValidationIssue]
