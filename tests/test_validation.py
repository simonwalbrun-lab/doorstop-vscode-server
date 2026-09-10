"""Tests for `GET /validate` and the message classification behind it.

The behavioural guarantees of contracts/validation-api.md section 1 are each a
test here; `test_validate_writes_nothing` is the important one, and it fails
outright without the read-only settings scope in `validation_rules.py`.
"""

import hashlib
from pathlib import Path

import doorstop
import pytest

from doorstop_server.validation_rules import classify, classify_severity


def issues_for(client) -> list:
    response = client.get("/validate")
    assert response.status_code == 200, response.text
    return response.json()["issues"]


def find(issues: list, check: str) -> list:
    return [issue for issue in issues if issue["check"] == check]


def only(issues: list, check: str) -> dict:
    matching = find(issues, check)
    assert len(matching) == 1, f"expected exactly one {check}, got {matching}"
    return matching[0]


def hash_tree(root: Path) -> dict:
    """Every file under `root`, by content hash - the no-write guarantee's evidence."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def build_tree(project_root: Path):
    return doorstop.build(
        cwd=str(project_root), root=str(project_root), request_next_number=None
    )


# --------------------------------------------------------------------------
# Guarantee 1: no writes  (T009)
# --------------------------------------------------------------------------


def test_validate_writes_nothing(client, document, project_root):
    """Doorstop's validation rewrites files with its shipped defaults.

    With REFORMAT / REVIEW_NEW_ITEMS / STAMP_NEW_LINKS left at their defaults
    this call stamps `reviewed:` on every unreviewed item and stamps link
    entries - and silently "fixes" a suspect link instead of reporting it.
    The project below deliberately contains both an unreviewed item and an
    unstamped link, so removing the read-only scope makes this test fail.
    """
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    before = hash_tree(project_root)
    assert before, "the fixture project must contain files to compare"

    issues_for(client)

    after = hash_tree(project_root)
    changed = sorted(name for name in before if before[name] != after.get(name))
    assert changed == [], f"GET /validate must not modify the repository, but changed {changed}"
    assert sorted(before) == sorted(after), "no file may be added or removed either"


def test_validate_leaves_a_suspect_link_suspect(client, document, project_root):
    """The sharp edge of the same defect: STAMP_NEW_LINKS would clear the
    suspicion during validation, so the problem would disappear by being
    reported."""
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    issues_for(client)

    tree = client.get("/tree").json()
    node = next(i for d in tree["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert node["links"][0]["suspect"] is True


# --------------------------------------------------------------------------
# Guarantees 3 and 4: nothing dropped, prefix stripped  (T010)
# --------------------------------------------------------------------------


def test_unrecognised_message_is_reported_as_unknown(client, document, monkeypatch):
    """A message matching no template must still reach the user, at Doorstop's
    own severity, with no placement claim (FR-011, SC-007)."""
    from doorstop.common import DoorstopWarning
    from doorstop.core.document import Document

    monkeypatch.setattr(
        Document,
        "get_issues",
        lambda self, **kwargs: iter([DoorstopWarning("a message from a future Doorstop")]),
    )

    issues = issues_for(client)

    unknown = only(issues, "unknown")
    assert unknown["severity"] == "warning", "Doorstop's own severity is kept"
    assert unknown["field"] is None, "nothing is known about placement"
    assert unknown["message"] == "a message from a future Doorstop"


def test_no_message_retains_its_uid_prefix(client, document):
    client.post(f"/documents/{document['prefix']}/items", json={})
    tree = client.get("/tree").json()
    uids = [i["uid"] for d in tree["documents"] for i in d["items"]]

    for issue in issues_for(client):
        for uid in uids:
            assert not issue["message"].startswith(f"{uid}: "), (
                f"the UID prefix belongs in uids, not in the message: {issue}"
            )


def test_classify_severity_orders_subclasses_correctly():
    """DoorstopInfo subclasses DoorstopWarning subclasses DoorstopError, so a
    naive isinstance order would report everything as an error."""
    from doorstop.common import DoorstopError, DoorstopInfo, DoorstopWarning

    assert classify_severity(DoorstopInfo("x")) == "info"
    assert classify_severity(DoorstopWarning("x")) == "warning"
    assert classify_severity(DoorstopError("x")) == "error"


@pytest.mark.parametrize(
    "check",
    ["linked_to_self", "link_cycle", "child_link_inactive"],
)
def test_reserved_checks_are_never_emitted(check):
    """Reserved ids hold their anchor mapping but have no pattern: Doorstop 3.2
    never reports them, and implementing them ourselves would duplicate Doorstop
    validation logic (spec.md "Scope decision")."""
    from doorstop_server.validation_rules import CHECK_TABLE

    rule = next(entry for entry in CHECK_TABLE if entry.check == check)
    assert rule.pattern is None, f"{check} must stay reserved, not implemented"


# --------------------------------------------------------------------------
# User Story 1 - errors  (T017, T018)
# --------------------------------------------------------------------------


def test_linked_to_unknown_item_is_an_error(client, document, project_root):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    tree = build_tree(project_root)
    tree.find_item(item["uid"]).link("REQ-999")

    issue = only(issues_for(client), "linked_to_unknown_item")

    assert issue["severity"] == "error"
    assert issue["field"] == "link_entry"
    assert issue["relatedUid"] == "REQ-999"
    assert issue["uids"] == [item["uid"]]


def test_inactive_parent_link_is_reported_as_unknown_item_error(
    client, document, project_root
):
    """The spec asks for "parent link is an inactive item ⇒ ERROR". Doorstop 3.2
    has no such check, but `tree.find_item()` skips inactive items, so the link
    surfaces as `linked to unknown item` - already at error severity, satisfying
    the spec's intent with no severity override of our own (research.md 2b)."""
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    tree = build_tree(project_root)
    tree.find_item(parent["uid"]).active = False

    issue = only(issues_for(client), "linked_to_unknown_item")

    assert issue["severity"] == "error"
    assert issue["relatedUid"] == parent["uid"]
    assert issue["uids"] == [child["uid"]]


def test_external_reference_not_found_is_an_error(client, document, project_root):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    tree = build_tree(project_root)
    tree.find_item(item["uid"]).ref = "no/such/file.py"

    issue = only(issues_for(client), "external_reference_not_found")

    assert issue["severity"] == "error"
    assert issue["field"] == "ref"
    assert issue["uids"] == [item["uid"]]


# --------------------------------------------------------------------------
# User Story 2 - link-health warnings  (T024, T025)
# --------------------------------------------------------------------------


def test_suspect_link_is_a_warning_on_the_link_entry(client, document, project_root):
    """Fails without the read-only settings scope: STAMP_NEW_LINKS stamps the
    link during validation instead of reporting it (research.md 1)."""
    from .conftest import set_item_text

    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})
    client.post("/review", json={"scope": "all"})
    set_item_text(project_root, parent["uid"], "the parent changed after linking")

    issue = only(issues_for(client), "suspect_link")

    assert issue["severity"] == "warning"
    assert issue["field"] == "link_entry"
    assert issue["relatedUid"] == parent["uid"]
    assert issue["uids"] == [child["uid"]]


def test_linked_to_non_normative_is_a_warning(client, document, project_root):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    tree = build_tree(project_root)
    tree.find_item(parent["uid"]).normative = False

    issue = only(issues_for(client), "linked_to_non_normative")

    assert issue["severity"] == "warning"
    assert issue["field"] == "link_entry"
    assert issue["relatedUid"] == parent["uid"]


def test_non_normative_item_with_links_is_a_warning(client, document, project_root):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    tree = build_tree(project_root)
    tree.find_item(child["uid"]).normative = False

    issue = only(issues_for(client), "non_normative_has_links")

    assert issue["severity"] == "warning"
    assert issue["field"] == "links"
    assert issue["uids"] == [child["uid"]]


def test_missing_parent_and_child_links_anchor_to_derived(client, document, project_root):
    """A parent document with an unlinked child document produces both halves of
    the pair: the child item lacks a parent link, the parent item lacks a child
    link. Both anchor to `derived:`."""
    client.post(f"/documents/{document['prefix']}/items", json={})
    child_path = project_root / "reqs" / "CHILD"
    created = client.post(
        "/documents",
        json={
            "prefix": "CHILD",
            "path": str(child_path),
            "separator": "-",
            "parentPrefix": document["prefix"],
        },
    )
    assert created.status_code == 200, created.text
    client.post("/documents/CHILD/items", json={})

    issues = issues_for(client)

    for check in ("no_links_to_parent_document", "no_links_from_child_document"):
        issue = find(issues, check)
        assert issue, f"expected a {check} warning"
        assert issue[0]["severity"] == "warning"
        assert issue[0]["field"] == "derived"


# --------------------------------------------------------------------------
# User Story 3 - content and review warnings  (T030, T031)
# --------------------------------------------------------------------------


def test_empty_text_and_unreviewed_changes(client, document, project_root):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    issues = issues_for(client)

    no_text = only(issues, "no_text")
    assert no_text["field"] == "text"
    assert no_text["uids"] == [item["uid"]]

    # A brand-new item has never been reviewed, which Doorstop reports as Info.
    review_issue = only(issues, "needs_initial_review")
    assert review_issue["field"] == "reviewed"
    assert review_issue["severity"] == "info"


def test_unreviewed_changes_after_edit(client, document, project_root):
    from .conftest import set_item_text

    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post("/review", json={"scope": "all"})
    set_item_text(project_root, item["uid"], "changed after the review")

    issue = only(issues_for(client), "unreviewed_changes")

    assert issue["severity"] == "warning"
    assert issue["field"] == "reviewed"
    assert issue["uids"] == [item["uid"]]


def test_duplicate_level_is_one_record_naming_both_items(client, document, project_root):
    """Guarantee 5, fan-out preserved: one Doorstop message naming two items
    becomes ONE record with two uids, not two records.

    The levels are assigned directly rather than through `POST /items`, because
    Doorstop renumbers on add specifically to keep levels unique - a duplicate
    can only arise from an edit, which is exactly when the user needs to see it.
    """
    first = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    second = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    tree = build_tree(project_root)
    tree.find_item(first["uid"]).level = "1.0"
    tree.find_item(second["uid"]).level = "1.0"

    issue = only(issues_for(client), "duplicate_level")

    assert issue["field"] == "level"
    assert sorted(issue["uids"]) == sorted([first["uid"], second["uid"]])
    assert issue["relatedUid"] is None


def test_skipped_level_names_every_item_it_mentions(client, document, project_root):
    """`skipped level: 1.0 (REQ-002), 1.5 (REQ-003)` names two items in two
    separate parenthesised groups - the other fan-out shape."""
    first = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    second = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    tree = build_tree(project_root)
    tree.find_item(first["uid"]).level = "1.0"
    tree.find_item(second["uid"]).level = "1.5"

    issue = only(issues_for(client), "skipped_level")

    assert issue["field"] == "level"
    assert issue["severity"] == "info"
    assert sorted(issue["uids"]) == sorted([first["uid"], second["uid"]])


# --------------------------------------------------------------------------
# User Story 4 - document-level problems  (T037, T038)
# --------------------------------------------------------------------------


def test_empty_document_yields_a_document_level_issue(client, document, project_root):
    # Give REQ an item, so the only "no items" left is EMPTY's own.
    client.post(f"/documents/{document['prefix']}/items", json={})
    empty_path = project_root / "reqs" / "EMPTY"
    created = client.post(
        "/documents",
        json={
            "prefix": "EMPTY",
            "path": str(empty_path),
            "separator": "-",
            "parentPrefix": document["prefix"],
        },
    )
    assert created.status_code == 200, created.text

    issue = only(issues_for(client), "no_items")

    assert issue["field"] == "document"
    assert issue["uids"] == [], "a document-level issue names no item"
    assert issue["documentPrefix"] == "EMPTY"


def test_issues_are_returned_for_every_document(client, document, project_root):
    """Guarantee 2, whole tree: no editor is open on anything here, and the
    second document is empty, yet both are represented (FR-015)."""
    client.post(f"/documents/{document['prefix']}/items", json={})
    other_path = project_root / "reqs" / "OTHER"
    client.post(
        "/documents",
        json={
            "prefix": "OTHER",
            "path": str(other_path),
            "separator": "-",
            "parentPrefix": document["prefix"],
        },
    )

    prefixes = {issue["documentPrefix"] for issue in issues_for(client)}

    assert document["prefix"] in prefixes
    assert "OTHER" in prefixes


# --------------------------------------------------------------------------
# Classification unit tests - no HTTP, no Doorstop project
# --------------------------------------------------------------------------


def test_classify_extracts_every_uid_of_a_skipped_level():
    record = classify(
        "skipped level: 1.2 (REQ-002), 1.4 (REQ-003)", "REQ", "info", None
    )

    assert record.check == "skipped_level"
    assert record.field == "level"
    assert record.uids == ["REQ-002", "REQ-003"]


def test_classify_recognises_the_unexpected_parent_prefix_info():
    """The check that reports a link crossing to a non-parent document - the
    same condition the derive quick pick labels "sibling" / "nephew" / "cousin".
    """
    record = classify("parent is 'REQ', but linked to: ARCH-001", "MD", "info", "MD-002")

    assert record.check == "unexpected_parent_prefix"
    assert record.field == "link_entry"
    assert record.related_uid == "ARCH-001"
    assert record.uids == ["MD-002"]


def test_classify_falls_back_to_unknown_with_no_field():
    record = classify("something Doorstop has never said", "REQ", "warning", "REQ-001")

    assert record.check == "unknown"
    assert record.field is None
    assert record.uids == ["REQ-001"], "the item attribution is still kept"
