"""POST /filter (spec 022) against a real temporary Doorstop project."""

from pathlib import Path

import doorstop
import pytest

from .conftest import set_item_attributes

# Keep in sync with SIMPLE_EXAMPLE / COMPLEX_EXAMPLE in src/filterNotebook.ts.
SIMPLE_EXAMPLE = "# Simple: every item that has not been reviewed yet\nreviewed == false\n"
COMPLEX_EXAMPLE = """# Complex: active, normative items that still need work - not reviewed yet
# or without a ref - leaving out change-log entries, and only those that
# something links to (they have a child). The table shows chosen columns.
filters:
  and:
    - active == true
    - normative == true
    - or:
        - reviewed == false
        - ref.isEmpty()
    - not:
        - header.startsWith("Change log")
    - hasChild: active == true
order: [level, header, reviewed, links]
"""


def set_custom(project_root: Path, uid: str, name: str, value) -> None:
    tree = doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)
    tree.find_item(uid).set(name, value)


@pytest.fixture
def project(client, document, project_root):
    """REQ-001..003 and a child document TST whose items link to REQ.

    REQ-001 (1.1 "Login", reviewed)  <- TST-001 (approved), TST-003 (draft)
    REQ-002 (1.2 "Logout", ref)      <- TST-002 (draft)
    REQ-003 (2.1, no header)         <- (no children)
    """

    def add(prefix, **body):
        response = client.post(f"/documents/{prefix}/items", json=body)
        assert response.status_code == 200, response.text
        return response.json()["uid"]

    add("REQ", level="1.1", header="Login", text="Users log in")
    add("REQ", level="1.2", header="Logout", text="Users log out")
    add("REQ", level="2.1", text="Audit trail | keep\neverything")
    set_item_attributes(project_root, "REQ-002", ref="logout_impl")

    response = client.post(
        "/documents",
        json={
            "prefix": "TST",
            "path": str(project_root / "reqs" / "TST"),
            "parentPrefix": "REQ",
            "separator": "-",
        },
    )
    assert response.status_code == 200, response.text
    for level, parent, status in [("1.1", "REQ-001", "approved"), ("1.2", "REQ-002", "draft"), ("1.3", "REQ-001", "draft")]:
        uid = add("TST", level=level, text=f"test of {parent}")
        client.post(f"/items/{uid}/links", json={"parentUid": parent})
        set_custom(project_root, uid, "status", status)

    client.post("/review", json={"scope": "item", "target": "REQ-001"})
    return client


def run(client, query):
    return client.post("/filter", json={"query": query})


def uids(client, query):
    response = run(client, query)
    assert response.status_code == 200, response.text
    return [item["uid"] for item in response.json()["items"]]


def assert_invalid(client, query, *fragments):
    response = run(client, query)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_FILTER"
    for fragment in fragments:
        assert fragment in error["message"]


# --- US1: single conditions, rows, errors -----------------------------------


def test_document_filter_returns_rows_in_tree_order(project):
    response = run(project, 'document == "REQ"')

    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["uid"] for i in items] == ["REQ-001", "REQ-002", "REQ-003"]
    first = items[0]
    assert first["documentPrefix"] == "REQ"
    assert first["level"] == "1.1"
    assert first["header"] == "Login"
    assert first["text"] == "Users log in"
    assert first["path"].endswith("REQ-001.yml")
    assert items[2]["header"] is None


def test_boolean_attribute(project):
    assert "REQ-001" not in uids(project, "reviewed == false")
    assert uids(project, 'and:\n  - document == "REQ"\n  - reviewed == true\n') == ["REQ-001"]


def test_text_methods(project):
    assert uids(project, 'header.startsWith("Log")') == ["REQ-001", "REQ-002"]
    assert uids(project, 'text.contains("log out")') == ["REQ-002"]
    assert uids(project, 'text.contains("LOG OUT")') == []  # case-sensitive
    assert "REQ-002" not in uids(project, "ref.isEmpty()")
    assert "REQ-001" in uids(project, "ref.isEmpty()")


def test_level_uses_doorstop_order(project):
    assert uids(project, 'and:\n  - document == "REQ"\n  - level >= "1.2"\n') == ["REQ-002", "REQ-003"]
    assert uids(project, 'and:\n  - document == "REQ"\n  - level < "1.10"\n') == ["REQ-001", "REQ-002"]


def test_no_match_is_empty_list(project):
    assert uids(project, 'uid == "NOPE-999"') == []


def test_type_mismatch_does_not_match(project):
    assert uids(project, "header > 3") == []


def test_new_notebook_examples_run(project):
    assert "REQ-002" in uids(project, SIMPLE_EXAMPLE)

    result = body(project, COMPLEX_EXAMPLE)
    assert result["columns"] == ["level", "header", "reviewed", "links"]
    # REQ-001 is reviewed but has no ref, REQ-002 is unreviewed; both have children.
    assert [item["uid"] for item in result["items"]] == ["REQ-001", "REQ-002"]


def test_blank_query_is_rejected(project):
    assert_invalid(project, "   \n")


def test_yaml_syntax_error_names_line(project):
    assert_invalid(project, "and: [", "line")


@pytest.mark.parametrize(
    "query",
    ['__import__("os")', "uid.__class__ == 1", 'uid == "a" and uid == "b"', "uid == text", "!reviewed"],
)
def test_unsupported_expressions_are_rejected(project, query):
    assert_invalid(project, query)


# --- US3: nested groups ------------------------------------------------------


def test_or_group(project):
    assert uids(project, 'or:\n  - uid == "REQ-001"\n  - uid == "TST-002"\n') == ["REQ-001", "TST-002"]


def test_not_group_excludes_any_listed_match(project):
    query = 'and:\n  - document == "REQ"\n  - not:\n      - uid == "REQ-001"\n      - uid == "REQ-003"\n'
    assert uids(project, query) == ["REQ-002"]


def test_three_level_nesting(project):
    query = """
and:
  - or:
      - document == "REQ"
      - document == "TST"
  - not:
      - or:
          - reviewed == true
          - status == "draft"
"""
    assert uids(project, query) == ["REQ-002", "REQ-003", "TST-001"]


@pytest.mark.parametrize(
    "query",
    ['and:\n  - uid == "a"\nor:\n  - uid == "b"\n', "and: []", 'foo:\n  - uid == "a"\n', "and: [42]", "42"],
)
def test_structure_errors(project, query):
    assert_invalid(project, query)


# --- US4: custom attributes, links, related items ---------------------------


def test_custom_attribute(project):
    assert uids(project, 'status == "approved"') == ["TST-001"]
    # REQ items have no status: no match, no error.
    assert uids(project, 'and:\n  - document == "REQ"\n  - status != "x"\n') == []


def test_links_contains(project):
    assert uids(project, 'links.contains("REQ-001")') == ["TST-001", "TST-003"]


def test_has_child(project):
    query = 'and:\n  - document == "REQ"\n  - hasChild: status == "approved"\n'
    assert uids(project, query) == ["REQ-001"]


def test_has_parent(project):
    assert uids(project, 'hasParent: header == "Logout"') == ["TST-002"]
    assert uids(project, 'hasParent: document == "REQ"') == ["TST-001", "TST-002", "TST-003"]


def test_has_child_with_nested_group(project):
    query = 'hasChild:\n  and:\n    - status == "draft"\n    - text.contains("REQ-002")\n'
    assert uids(project, query) == ["REQ-002"]


def test_item_without_children(project):
    assert "REQ-003" not in uids(project, 'hasChild: uid != ""')
    query = 'and:\n  - document == "REQ"\n  - not:\n      - hasChild: uid != ""\n'
    assert uids(project, query) == ["REQ-003"]


def test_is_not_empty(project):
    assert uids(project, "ref.isNotEmpty()") == ["REQ-002"]
    assert uids(project, "status.isNotEmpty()") == ["TST-001", "TST-002", "TST-003"]


def test_dangling_parent_link_is_ignored(project, project_root):
    project.post("/items/TST-001/links", json={"parentUid": "REQ-003"})
    Path(next(project_root.rglob("REQ-003.yml"))).unlink()

    assert uids(project, 'hasParent: document == "REQ"') == ["TST-001", "TST-002", "TST-003"]


# --- US6: table columns via order: ------------------------------------------


def body(client, query):
    response = run(client, query)
    assert response.status_code == 200, response.text
    return response.json()


def values_of(result, uid):
    return next(item["values"] for item in result["items"] if item["uid"] == uid)


def test_order_selects_columns(project):
    result = body(project, 'filters: document == "TST"\norder: [status, header]\n')

    assert result["columns"] == ["status", "header"]
    assert values_of(result, "TST-001") == ["approved", ""]


def test_default_columns_fall_back_to_text_for_header(project):
    result = body(project, 'document == "REQ"')

    assert result["columns"] == ["document", "level", "header"]
    assert values_of(result, "REQ-001") == ["REQ", "1.1", "Login"]
    assert values_of(result, "REQ-003")[2].startswith("Audit trail")


def test_order_drops_uid(project):
    assert body(project, 'filters: uid == "TST-001"\norder: [uid, status]\n')["columns"] == ["status"]


def test_list_values_are_comma_separated(project):
    project.post("/items/TST-001/links", json={"parentUid": "REQ-002"})

    result = body(project, 'filters: uid == "TST-001"\norder: [links]\n')

    assert values_of(result, "TST-001") == ["REQ-001, REQ-002"]


def test_long_values_are_cut_to_80_characters(project, project_root):
    set_custom(project_root, "TST-001", "note", "y" * 100)

    result = body(project, 'filters: uid == "TST-001"\norder: [note]\n')

    assert values_of(result, "TST-001") == ["y" * 80]


@pytest.mark.parametrize(
    "query",
    [
        'filters: uid == "a"\norder: 5\n',
        'filters: uid == "a"\norder: [1]\n',
        'filters: uid == "a"\nsort: [uid]\n',
        "order: [status]\n",
    ],
)
def test_malformed_order_is_rejected(project, query):
    assert_invalid(project, query)


def test_hyphenated_custom_attribute(project, project_root):
    set_custom(project_root, "TST-001", "invented-by", "Claude")

    assert uids(project, 'invented-by == "Claude"') == ["TST-001"]
    assert uids(project, 'invented-by.contains("Cl")') == ["TST-001"]
    assert uids(project, "invented-by.isNotEmpty()") == ["TST-001"]
    assert uids(project, 'hasChild: invented-by == "Claude"') == ["REQ-001"]
    assert uids(project, 'uid == "Cl-aude"') == []  # hyphens in literals untouched
    result = body(project, 'filters: uid == "TST-001"\norder: [invented-by]\n')
    assert values_of(result, "TST-001") == ["Claude"]
