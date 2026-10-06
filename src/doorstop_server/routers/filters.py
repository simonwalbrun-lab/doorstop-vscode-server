"""POST /filter (spec 022): evaluate an Obsidian-Bases-shaped filter over the tree.

A filter is YAML: one expression string, or a single-key mapping -
``and`` / ``or`` / ``not`` (a list of filters) or ``hasChild`` / ``hasParent``
(one filter). A cell may also wrap it Bases-style as ``filters:`` plus an
optional ``order:`` list naming the table's columns. Expressions are parsed with ``ast`` and checked against a small
whitelist (one comparison, or one ``contains`` / ``startsWith`` / ``isEmpty``
call) - nothing is ever ``eval``'d. The whole filter is compiled before any
item is looked at, so a malformed filter fails even on an empty project.

Grammar and semantics: specs/022-filter-notebooks/contracts/filter-syntax.md.
"""

import ast
import operator
import re
from collections import defaultdict
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import yaml
from doorstop.core.types import Level
from fastapi import APIRouter, Depends

from doorstop_server.deps import get_tree_for_reading, load_items
from doorstop_server.errors import DoorstopApiError
from doorstop_server.schemas import FilterItem, FilterRequest, FilterResponse

router = APIRouter()


class _Index(NamedTuple):
    items: Dict[str, object]
    children: Dict[str, List[object]]


Predicate = Callable[[object, _Index], bool]

_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_METHOD_ARITY = {"contains": 1, "startsWith": 1, "isEmpty": 0, "isNotEmpty": 0}
# Bases/JS spelling of the literals, next to Python's True/False/None.
_LITERAL_NAMES = {"true": True, "false": False, "null": None}
_HINT = (
    "Use `attribute <op> value` (==, !=, <, <=, >, >=) or "
    "`attribute.contains(...)` / `.startsWith(...)` / `.isEmpty()` / `.isNotEmpty()`; "
    "combine and negate with and: / or: / not: groups (no !, &&, ||)."
)


def _text(value) -> str:
    return str(value) if value else ""


_STANDARD = {
    "uid": lambda item: str(item.uid),
    "document": lambda item: str(item.document.prefix),
    "level": lambda item: item.level,
    "header": lambda item: _text(item.header),
    "text": lambda item: _text(item.text),
    "ref": lambda item: _text(item.ref),
    "active": lambda item: item.active,
    "normative": lambda item: item.normative,
    "derived": lambda item: item.derived,
    "reviewed": lambda item: item.reviewed,
    "links": lambda item: [str(uid) for uid in item.links],
}


def _invalid(message: str) -> DoorstopApiError:
    return DoorstopApiError(400, "INVALID_FILTER", message)


def _value(item, name: str):
    getter = _STANDARD.get(name)
    if getter:
        return getter(item)
    # Custom attribute. Item.get falls back to real attributes, so a name like
    # `save` would hand back a bound method - that is not data.
    value = item.get(name)
    return None if callable(value) else value


def _compare(op, value, literal) -> bool:
    if value is None:
        return False
    try:
        if isinstance(value, Level):
            literal = Level(str(literal))
        return op(value, literal)
    except (TypeError, ValueError):
        return False


# Custom attribute names may be kebab-case (`invented-by`), which ast would read
# as a subtraction: the leading name is swapped for a placeholder before parsing.
_LEADING_NAME = re.compile(r"\s*([A-Za-z_][\w-]*)")
_PLACEHOLDER = "__attr__"

DEFAULT_COLUMNS = ["document", "level", "header"]
_MAX_CELL = 80


def _call(method: str, value, literal) -> bool:
    if method == "isEmpty":
        return value is None or value == "" or value == []
    if method == "isNotEmpty":
        return not _call("isEmpty", value, literal)
    try:
        if method == "contains":
            return isinstance(value, (str, list)) and literal in value
        return isinstance(value, str) and value.startswith(literal)
    except TypeError:
        return False


def _literal(node: ast.AST, expr: str):
    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, (str, int, float, bool))
    ):
        return node.value
    if isinstance(node, ast.Name) and node.id in _LITERAL_NAMES:
        return _LITERAL_NAMES[node.id]
    raise _invalid(f"Expected a literal value in `{expr}`. {_HINT}")


def _compile_expression(expr: str) -> Predicate:
    source, hyphenated = expr.strip(), None
    match = _LEADING_NAME.match(source)
    if match and "-" in match.group(1):
        hyphenated, source = match.group(1), _PLACEHOLDER + source[match.end():]

    def attribute(name: str) -> str:
        return hyphenated if hyphenated and name == _PLACEHOLDER else name

    try:
        node = ast.parse(source, mode="eval").body
    except SyntaxError:
        raise _invalid(f"Cannot parse `{expr}`. {_HINT}") from None

    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and type(node.ops[0]) in _COMPARE
        and isinstance(node.left, ast.Name)
    ):
        name, op = attribute(node.left.id), _COMPARE[type(node.ops[0])]
        literal = _literal(node.comparators[0], expr)
        return lambda item, _index: _compare(op, _value(item, name), literal)

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.attr in _METHOD_ARITY
        and len(node.args) == _METHOD_ARITY[node.func.attr]
        and not node.keywords
    ):
        name, method = attribute(node.func.value.id), node.func.attr
        literal = _literal(node.args[0], expr) if node.args else None
        return lambda item, _index: _call(method, _value(item, name), literal)

    raise _invalid(f"Unsupported expression `{expr}`. {_HINT}")


def _compile(node) -> Predicate:
    if isinstance(node, str):
        return _compile_expression(node)
    if not isinstance(node, dict):
        raise _invalid(f"Not a filter: {node!r}. {_HINT}")
    if len(node) != 1:
        raise _invalid(
            f"A filter mapping needs exactly one key, got {', '.join(map(str, node))}. "
            "Wrap several conditions in and: / or:."
        )
    ((key, value),) = node.items()

    if key in ("and", "or", "not"):
        if not isinstance(value, list) or not value:
            raise _invalid(f"`{key}:` needs a non-empty list of filters.")
        parts = [_compile(part) for part in value]
        if key == "and":
            return lambda item, index: all(p(item, index) for p in parts)
        if key == "or":
            return lambda item, index: any(p(item, index) for p in parts)
        return lambda item, index: not any(p(item, index) for p in parts)

    if key == "hasChild":
        inner = _compile(value)
        return lambda item, index: any(
            inner(child, index) for child in index.children.get(str(item.uid), ())
        )

    if key == "hasParent":
        inner = _compile(value)
        # Links to unknown UIDs are skipped, not an error.
        return lambda item, index: any(
            inner(index.items[str(uid)], index) for uid in item.links if str(uid) in index.items
        )

    raise _invalid(f"Unknown key `{key}:`. Use and, or, not, hasChild or hasParent.")


def _cell(item, name: str, default_columns: bool) -> str:
    value = _value(item, name)
    if name == "header" and default_columns and not value:
        value = _text(item.text)
    if value is None:
        return ""
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return text[:_MAX_CELL]


def compile_filter(query: str) -> Tuple[Predicate, Optional[List[str]]]:
    """The cell's predicate and its `order:` columns (after UID), or None without `order:`."""
    try:
        node = yaml.safe_load(query) if query.strip() else None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise _invalid(f"Invalid YAML{where}: {problem}") from None
    if node is None:
        raise _invalid("Filter is empty.")
    if isinstance(node, dict) and set(node) in ({"filters"}, {"filters", "order"}):
        order = node.get("order", [])
        if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
            raise _invalid("`order:` must be a list of attribute names, e.g. order: [status, header].")
        columns = [name for name in order if name != "uid"] if "order" in node else None
        return _compile(node["filters"]), columns
    return _compile(node), None


@router.post("/filter", response_model=FilterResponse)
async def filter_items(body: FilterRequest, tree=Depends(get_tree_for_reading)) -> FilterResponse:
    predicate, columns = compile_filter(body.query)
    default_columns = columns is None
    columns = DEFAULT_COLUMNS if columns is None else columns
    load_items(tree)
    # Same order as GET /tree (Doorstop's Item.__lt__: level, then UID);
    # inactive items are included.
    ordered = [item for document in tree for item in sorted(document)]
    children: Dict[str, List[object]] = defaultdict(list)
    for item in ordered:
        for uid in item.links:
            children[str(uid)].append(item)
    index = _Index({str(item.uid): item for item in ordered}, children)

    return FilterResponse(
        columns=columns,
        items=[
            FilterItem(
                uid=str(item.uid),
                documentPrefix=str(item.document.prefix),
                level=str(item.level),
                header=str(item.header) if item.header else None,
                text=str(item.text) if item.text else None,
                path=item.path,
                values=[_cell(item, name, default_columns) for name in columns],
            )
            for item in ordered
            if predicate(item, index)
        ]
    )
