"""POST /batch (spec 036): apply a filter cell's ``edit:`` section to ticked items.

The ``edit:`` value is a list of single-key operations; ``compile_edit`` checks
it completely before any item is touched, so an invalid edit never writes
(FR-003). Field operations are worked out by the pure ``plan_item`` on a copy
of the item's data; a dry run stops there, a real run writes the result with
one ``save()`` per item. Syntax: specs/036-batch-edit-items/contracts/batch-edit.md.
"""

import datetime
import re
from typing import Dict, List, Optional, Tuple

import yaml
from doorstop.core.item import Item
from doorstop.core.types import UID
from fastapi import APIRouter, Depends, Request

from doorstop_server.deps import get_tree, load_items
from doorstop_server.errors import DoorstopApiError
from doorstop_server.routers.items import is_protected_field
from doorstop_server.schemas import BatchItemResult, BatchRequest, BatchResponse

router = APIRouter()

FIELD_OPERATIONS = ("set", "add", "remove", "appendEntry", "removeEntry")
GENERATE = "generateChildren"
# Standard attributes a batch may change, with `set` only (FR-012).
SETTABLE_STANDARD = frozenset({"active", "normative", "derived", "level", "text", "ref"})
_FIELD_NAME = re.compile(r"[A-Za-z_][\w-]*")
# YAML scalars as safe_load returns them; a date stays a date, as in a hand-written item.
_SCALARS = (str, int, float, bool, datetime.date)

Operation = Tuple[str, object]


class Unquoted(str):
    """Text written without quotes in the cell; edit values must quote text like filters do (FR-021)."""


class CellLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # PyYAML's own hierarchy
    """safe_load that remembers which text was written without quotes."""

    def construct_yaml_str(self, node):
        text = super().construct_yaml_str(node)
        return Unquoted(text) if node.style is None else text

    def construct_mapping(self, node, deep=False):
        # A level is text: an unquoted 1.10 stays "1.10" for the message instead of the number 1.1.
        for key, value in node.value:
            if key.value == "level" and isinstance(value, yaml.ScalarNode) and value.style is None:
                value.tag = "tag:yaml.org,2002:str"
        return super().construct_mapping(node, deep)


CellLoader.add_constructor("tag:yaml.org,2002:str", CellLoader.construct_yaml_str)

# Unquoted, only these are numbers or true/false, as in filters; YAML 1.1 would also
# read `no` as false, `1:30` as 90 and `010` as 8 (FR-021).
_PLAIN_LITERAL = re.compile(r"(?i:true|false)|-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?")


def _literal_or_unquoted(construct):
    def wrapped(loader, node):
        if node.style is None and not _PLAIN_LITERAL.fullmatch(node.value):
            return Unquoted(node.value)
        return construct(loader, node)

    return wrapped


for _tag, _construct in (
    ("bool", yaml.SafeLoader.construct_yaml_bool),
    ("int", yaml.SafeLoader.construct_yaml_int),
    ("float", yaml.SafeLoader.construct_yaml_float),
):
    CellLoader.add_constructor(f"tag:yaml.org,2002:{_tag}", _literal_or_unquoted(_construct))


def load_cell(query: str):
    """A filter cell's YAML, like ``yaml.safe_load``; unquoted text comes back as :class:`Unquoted`."""
    return yaml.load(query, Loader=CellLoader)  # nosec B506 - CellLoader is a SafeLoader


def _invalid(message: str) -> DoorstopApiError:
    return DoorstopApiError(400, "INVALID_EDIT", message)


def _protected(name: str, why: str) -> DoorstopApiError:
    return DoorstopApiError(422, "PROTECTED_FIELD", f"{name} {why}")


def _check_name(operation: str, name) -> str:
    if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
        raise _invalid(f"`{operation}`: {name!r} is not a field name.")
    if name in SETTABLE_STANDARD:
        if operation != "set":
            raise _protected(name, "can only be changed with set")
    elif hasattr(Item, name):  # uid, links, header, reviewed, methods; instance attributes: _apply_fields
        raise _protected(name, "cannot be changed by a batch edit")
    return str(name)  # a plain str: Doorstop's C YAML dumper rejects str subclasses


def _check_value(operation: str, name: str, value):
    if value == [] and operation in ("set", "add") and name not in SETTABLE_STANDARD:
        return []  # an empty list field to fill with appendEntry (FR-004, FR-005)
    if name == "level" and (not isinstance(value, str) or isinstance(value, Unquoted)):
        # Unquoted, YAML reads 1.10 as the number 1.1.
        raise _invalid(f'`{operation}`: write levels in double quotes, e.g. level: "{value}".')
    if isinstance(value, Unquoted):
        raise _invalid(f'`{operation}`: put the text {value} in double quotes, e.g. {name}: "{value}".')
    if not isinstance(value, _SCALARS):
        raise _invalid(
            f"`{operation}`: the value of {name} must be text in double quotes, a number, true/false or a date."
        )
    return value


def compile_edit(node) -> List[Operation]:
    """The checked operations of an ``edit:`` section, in order; raises on anything invalid."""
    if not isinstance(node, list) or not node:
        raise _invalid('`edit:` needs a non-empty list of operations, e.g. - set: {status: "done"}.')
    operations: List[Operation] = []
    for entry in node:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise _invalid(f"Each edit entry is one operation, got {entry!r}.")
        ((operation, params),) = entry.items()
        if operation == GENERATE:
            operations.append((operation, _compile_generate(params)))
        elif operation == "remove":
            names = params if isinstance(params, list) else [params]
            operations.append((operation, [_check_name(operation, name) for name in names]))
        elif operation in FIELD_OPERATIONS:
            if not isinstance(params, dict) or not params:
                raise _invalid(f'`{operation}` needs field: value pairs, e.g. {operation}: {{status: "done"}}.')
            operations.append(
                (operation, {_check_name(operation, k): _check_value(operation, k, v) for k, v in params.items()})
            )
        else:
            raise _invalid(
                f"Unknown edit operation `{operation}`. Use {', '.join(FIELD_OPERATIONS)} or {GENERATE}."
            )
    if len(operations) > 1 and any(operation == GENERATE for operation, _ in operations):
        raise _invalid(f"`{GENERATE}` cannot be combined with other operations; run them separately.")
    return operations


def _compile_generate(params) -> dict:
    params = {} if params is None else params
    if not isinstance(params, dict) or not set(params) <= {"document", "force", "blank"}:
        raise _invalid(f"`{GENERATE}` takes only document, force and blank.")
    document = params.get("document", "")
    if isinstance(document, Unquoted):
        raise _invalid(f'`{GENERATE}`: put the prefix {document} in double quotes, e.g. document: "{document}".')
    if not isinstance(document, str):
        raise _invalid(f'`{GENERATE}`: document must be a document prefix in double quotes, e.g. document: "TST".')
    for flag in ("force", "blank"):
        if not isinstance(params.get(flag, False), bool):
            raise _invalid(f"`{GENERATE}`: {flag} must be true or false.")
    return {
        "document": params.get("document"),
        "force": params.get("force", False),
        "blank": params.get("blank", False),
    }


def plan_item(data: dict, operations: List[Operation]) -> Tuple[dict, set, List[str]]:
    """(values to set, names to remove, skip reasons) for one item; ``data`` is not changed.

    Later operations see the result of earlier ones. Standard attributes are
    always present.
    """
    data = dict(data)
    changed, removed, reasons = set(), set(), []
    for operation, params in operations:
        if operation == "remove":
            for name in params:
                if name in data:
                    del data[name]
                    changed.discard(name)
                    removed.add(name)
                else:
                    reasons.append(f"field missing: {name}")
            continue
        for name, value in params.items():
            present = name in data or name in SETTABLE_STANDARD
            current = data.get(name) if present else []
            reason = _skip_reason(operation, present, current, value)
            if reason:
                reasons.append(f"{reason}: {name}")
                continue
            if operation == "appendEntry":
                value = list(current) + [value]
            elif operation == "removeEntry":
                value = [entry for entry in current if entry != value]
            data[name] = value
            changed.add(name)
            removed.discard(name)
    return {name: data[name] for name in changed}, removed, reasons


def _skip_reason(operation: str, present: bool, current, value) -> Optional[str]:
    """Why a field operation does not apply to an item, or None (FR-004..FR-008)."""
    if operation == "set":
        return None if present else "field missing"
    if operation == "add":
        return "field exists" if present else None
    if not present:
        return None if operation == "appendEntry" else "field missing"
    if not isinstance(current, list):
        return "not a list"
    if operation == "appendEntry":
        return "entry exists" if value in current else None
    return None if value in current else "entry missing"


def _apply_fields(items: Dict[str, Optional[Item]], operations, dry_run: bool) -> List[BatchItemResult]:
    # Names that are real attributes of an item instance (path, document, ...) would be
    # overwritten by Item.set; checked on the items themselves before anything is written.
    for item in items.values():
        if item is None:
            continue
        for _, params in operations:
            for name in params:
                if name not in SETTABLE_STANDARD and is_protected_field(item, name):
                    raise _protected(name, "cannot be changed by a batch edit")
        break
    results = []
    for uid, item in items.items():
        if item is None:
            results.append(BatchItemResult(uid=uid, status="skipped", reason="item not found"))
            continue
        try:
            changes, removed, reasons = plan_item(item.data, operations)
            if not changes and not removed:
                results.append(BatchItemResult(uid=uid, status="skipped", reason="; ".join(reasons)))
                continue
            if not dry_run:
                _write(item, changes, removed)
            results.append(BatchItemResult(uid=uid, status="changed", reason="; ".join(reasons) or None))
        except Exception as error:  # pylint: disable=broad-except  # FR-013: one item never stops the batch
            results.append(BatchItemResult(uid=uid, status="failed", reason=str(error)))
    return results


def _write(item: Item, changes: dict, removed: set) -> None:
    """One save through Doorstop's own setters (as ``items.apply_prose``)."""
    auto = item.auto
    item.auto = False
    try:
        for name in removed:
            # ponytail: Doorstop 3.2 has no public way to delete an extended attribute; switch if Item gains one.
            item._data.pop(name, None)  # pylint: disable=protected-access
        for name, value in changes.items():
            item.set(name, value)
        item.save()
    finally:
        item.auto = auto


def _child_document(tree, items: Dict[str, Optional[Item]], requested: Optional[str]):
    """The target document of generateChildren; raises when it cannot be chosen (nothing written)."""
    sources = sorted({str(item.document.prefix) for item in items.values() if item is not None})
    if len(sources) > 1:
        raise DoorstopApiError(
            409, "MIXED_DOCUMENTS", f"Children can only be generated from one document, got {', '.join(sources)}."
        )
    source = sources[0]
    children = [document for document in tree if str(document.parent or "") == source]
    if requested is not None:
        match = next((d for d in children if str(d.prefix) == requested), None)
        if match is None:
            raise DoorstopApiError(409, "NOT_A_CHILD_DOCUMENT", f"{requested} is not a child document of {source}.")
        return match
    if not children:
        raise DoorstopApiError(409, "NO_CHILD_DOCUMENT", f"{source} has no child document.")
    if len(children) > 1:
        names = ", ".join(sorted(str(d.prefix) for d in children))
        raise DoorstopApiError(
            409, "CHILD_DOCUMENT_REQUIRED", f"{source} has several child documents ({names}); name one with document:."
        )
    return children[0]


def _generate_children(tree, items: Dict[str, Optional[Item]], params: dict, dry_run: bool) -> List[BatchItemResult]:
    if all(item is None for item in items.values()):
        return [BatchItemResult(uid=uid, status="skipped", reason="item not found") for uid in items]
    target = _child_document(tree, items, params["document"])
    # blank: the next free numbers, counted here so a dry run plans the same UIDs (FR-019).
    next_number = target.next_number
    results = []
    for uid, parent in items.items():
        if parent is None:
            results.append(BatchItemResult(uid=uid, status="skipped", reason="item not found"))
            continue
        number = next_number if params["blank"] else parent.uid.number
        try:
            results.append(_generate_child(target, parent, number, params["force"], dry_run))
        except Exception as error:  # pylint: disable=broad-except  # FR-013
            results.append(BatchItemResult(uid=uid, status="failed", reason=str(error)))
        next_number += 1
    return results


def _generate_child(target, parent: Item, number: int, force: bool, dry_run: bool) -> BatchItemResult:
    uid, child_uid = str(parent.uid), str(UID(target.prefix, target.sep, number, target.digits))
    # Inactive items count too: their file still holds the UID.
    existing = next((item for item in target if str(item.uid) == child_uid), None)
    if existing is not None and not force:
        return BatchItemResult(uid=uid, status="skipped", reason=f"UID exists: {child_uid}")
    if not dry_run:
        if existing is not None:
            existing.delete()
        # name=, not number=: add_item takes max(number, next_number) and could never fill a gap.
        child = target.add_item(name=str(number))
        _link(child, parent)
    return BatchItemResult(uid=uid, status="overwritten" if existing is not None else "created", created=child_uid)


def _link(child: Item, parent: Item) -> None:
    child.link(parent.uid)
    child.save()  # a new item is created with auto save off


@router.post("/batch", response_model=BatchResponse)
async def batch_edit(body: BatchRequest, request: Request, tree=Depends(get_tree)) -> BatchResponse:
    operations = _edit_of(body.query)
    if not body.uids:
        raise _invalid("Nothing selected.")
    load_items(tree)
    # Inactive items are filter results too, and Tree.find_item skips them.
    known = {str(item.uid): item for document in tree for item in document}
    items = {uid: known.get(uid) for uid in body.uids}
    if operations[0][0] == GENERATE:
        results = _generate_children(tree, items, operations[0][1], body.dryRun)
    else:
        results = _apply_fields(items, operations, body.dryRun)
    if any(result.status == "failed" for result in results):
        # get_tree only rebuilds after an exception; a failed item may differ from its file.
        request.app.state.tree_fingerprint = None
    return BatchResponse(results=results, operations=[_describe(*operation) for operation in operations])


def _describe(operation: str, params) -> str:
    """``set testrun, owner`` / ``generateChildren into TST (force)``."""
    if operation != GENERATE:
        return f"{operation} {', '.join(params)}"
    flags = [flag for flag in ("force", "blank") if params[flag]]
    target = f" into {params['document']}" if params["document"] else ""
    return f"{operation}{target}" + (f" ({', '.join(flags)})" if flags else "")


def _edit_of(query: str) -> List[Operation]:
    """The compiled ``edit:`` of a cell; the filter itself was checked when the cell ran."""
    try:
        node = load_cell(query)
    except yaml.YAMLError:
        node = None
    if not isinstance(node, dict) or "edit" not in node:
        raise _invalid("The cell has no edit: section.")
    return compile_edit(node["edit"])
