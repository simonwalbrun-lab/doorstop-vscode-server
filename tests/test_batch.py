"""POST /batch (spec 036) against a real temporary Doorstop project."""

import datetime
import os
import stat
import time
from pathlib import Path

import doorstop
import pytest

from .conftest import add_document, add_item


def build(project_root: Path):
    return doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)


def set_custom(project_root: Path, uid: str, **values) -> None:
    item = build(project_root).find_item(uid)
    for name, value in values.items():
        item.set(name, value)


def value(project_root: Path, uid: str, name: str):
    return build(project_root).find_item(uid).get(name)


def item_path(project_root: Path, uid: str) -> Path:
    return next(project_root.rglob(f"{uid}.yml"))


def batch(client, edit: str, uids, dry_run=False, filters='document == "TST"'):
    """``edit`` is the flow-style YAML of the edit: list, e.g. ``[{set: {a: 1}}]``."""
    query = f"filters: {filters}\nedit: {edit}\n"
    return client.post("/batch", json={"query": query, "uids": list(uids), "dryRun": dry_run})


def results(response) -> dict:
    assert response.status_code == 200, response.text
    return {result["uid"]: result for result in response.json()["results"]}


def statuses(response) -> dict:
    return {uid: result["status"] for uid, result in results(response).items()}


def assert_error(response, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.json()["error"]["code"] == code


def generate_description(client) -> list:
    response = batch(client, '[{generateChildren: {document: "TST", force: true}}]', ["REQ-099"], dry_run=True)
    return response.json()["operations"]


def snapshot(project_root: Path) -> dict:
    return {path: path.read_bytes() for path in project_root.rglob("*.yml")}


@pytest.fixture
def tst(client, document, project_root):
    """TST below REQ: TST-001 and TST-002 have ``testrun: TR-1``, TST-003 has no testrun."""
    add_document(client, project_root, "TST")
    for _ in range(3):
        add_item(client, "TST")
    set_custom(project_root, "TST-001", testrun="TR-1", owner="ann")
    set_custom(project_root, "TST-002", testrun="TR-1")
    return client


# --- US1: set, selection, validation, failures --------------------------------


# Spec 036 FR-002, FR-004, FR-011
def test_set_changes_ticked_items_that_have_the_field(tst, project_root):
    add_item(tst, "TST")
    set_custom(project_root, "TST-004", testrun="TR-1")
    unticked = item_path(project_root, "TST-004").read_bytes()

    response = batch(tst, '[{set: {testrun: "TR-2"}}]', ["TST-001", "TST-002", "TST-003"])

    found = results(response)
    assert [found[uid]["status"] for uid in ("TST-001", "TST-002", "TST-003")] == ["changed", "changed", "skipped"]
    assert found["TST-003"]["reason"].startswith("field missing")
    assert value(project_root, "TST-001", "testrun") == "TR-2"
    assert value(project_root, "TST-001", "owner") == "ann"
    assert item_path(project_root, "TST-004").read_bytes() == unticked


# Spec 036 FR-004, FR-011, FR-012
def test_set_standard_attributes(tst, project_root):
    response = batch(tst, '[{set: {level: "2.5", text: "new text", ref: "a.py", normative: false}}]', ["TST-001"])

    assert statuses(response) == {"TST-001": "changed"}
    item = build(project_root).find_item("TST-001")
    assert (str(item.level), str(item.text), item.ref, item.normative) == ("2.5", "new text", "a.py", False)
    assert item.get("testrun") == "TR-1"


# Spec 036 FR-004
def test_set_keeps_yaml_types(tst, project_root):
    set_custom(project_root, "TST-001", priority=1, flag=False, ratio=0.5, due=datetime.date(2026, 1, 1))

    statuses(batch(tst, "[{set: {priority: 3, flag: true, ratio: 1.5, due: 2026-11-30}}]", ["TST-001"]))

    item = build(project_root).find_item("TST-001")
    assert (item.get("priority"), item.get("flag"), item.get("ratio")) == (3, True, 1.5)
    assert isinstance(item.get("priority"), int) and isinstance(item.get("flag"), bool)
    assert isinstance(item.get("ratio"), float)
    assert item.get("due") == datetime.date(2026, 11, 30)


# Spec 036 FR-021
def test_text_values_must_be_quoted(tst, project_root):
    before = snapshot(project_root)

    for edit, named in (
        ("[{set: {testrun: TR-1}}]", "TR-1"),
        ("[{appendEntry: {runs: TR-1}}]", "TR-1"),
        ("[{set: {level: 1.10}}]", 'level: "1.10"'),  # as typed, not the number 1.1
        ("[{generateChildren: {document: TST}}]", "TST"),
        # YAML 1.1 would read these as false, 90 and 8; the filter part rejects them too.
        ("[{set: {result: no}}]", "no"),
        ("[{set: {duration: 1:30}}]", "1:30"),
        ("[{set: {build: 010}}]", "010"),
    ):
        response = batch(tst, edit, ["TST-001"])
        assert_error(response, 400, "INVALID_EDIT")
        assert named in response.json()["error"]["message"] and "quotes" in response.json()["error"]["message"]
    cell = 'filters: document == "TST"\nedit: [{set: {testrun: TR-1}}]\n'
    assert_error(tst.post("/filter", json={"query": cell}), 400, "INVALID_EDIT")
    assert snapshot(project_root) == before

    assert statuses(batch(tst, '[{set: {level: "1.10", testrun: "TR-1"}}]', ["TST-001"])) == {"TST-001": "changed"}
    assert str(build(project_root).find_item("TST-001").level) == "1.10"


# Spec 036 FR-004, FR-005
def test_set_and_add_take_an_empty_list(tst, project_root):
    set_custom(project_root, "TST-001", runs=["TR-1"])

    assert statuses(batch(tst, "[{add: {runs: []}}]", ["TST-001", "TST-002"])) == {
        "TST-001": "skipped",
        "TST-002": "changed",
    }
    assert (value(project_root, "TST-001", "runs"), value(project_root, "TST-002", "runs")) == (["TR-1"], [])
    assert statuses(batch(tst, "[{set: {runs: []}}]", ["TST-001"])) == {"TST-001": "changed"}
    assert value(project_root, "TST-001", "runs") == []
    for edit in ('[{set: {runs: ["a"]}}]', "[{appendEntry: {runs: []}}]", "[{set: {active: []}}]"):
        assert_error(batch(tst, edit, ["TST-001"]), 400, "INVALID_EDIT")


# Spec 036 FR-003
def test_operations_run_in_listed_order(tst, project_root):
    response = batch(tst, '[{add: {result: "open"}}, {set: {result: "done"}}, {set: {testrun: "TR-3"}}]', ["TST-001"])

    assert statuses(response) == {"TST-001": "changed"}
    assert (value(project_root, "TST-001", "result"), value(project_root, "TST-001", "testrun")) == ("done", "TR-3")


# Spec 036 FR-003
def test_invalid_edits_write_nothing(tst, project_root):
    before = snapshot(project_root)
    query = 'filters: document == "TST"\nedit: [{set: {testrun: "TR-2"}}]\n'

    for response in (
        batch(tst, '[{apendEntry: {runs: "TR-1"}}]', ["TST-001"]),
        batch(tst, '[{set: {testrun: ["a", "b"]}}]', ["TST-001"]),
        batch(tst, "[{set: {testrun: null}}]", ["TST-001"]),
        batch(tst, "[]", ["TST-001"]),
        batch(tst, '[{set: {9lives: "x"}}]', ["TST-001"]),
        tst.post("/batch", json={"query": 'filters: document == "TST"\n', "uids": ["TST-001"]}),
        tst.post("/batch", json={"query": query, "uids": []}),
    ):
        assert_error(response, 400, "INVALID_EDIT")
    assert snapshot(project_root) == before


# Spec 036 FR-012
def test_protected_fields_are_rejected(tst, project_root):
    before = snapshot(project_root)

    for edit in (
        '[{set: {uid: "X"}}]',
        '[{set: {links: "X"}}]',
        '[{set: {header: "X"}}]',
        '[{set: {reviewed: "X"}}]',
        '[{set: {save: "X"}}]',
        '[{set: {document: "X"}}]',
        '[{add: {level: "2"}}]',
        "[{remove: active}]",
        '[{set: {testrun: "TR-2"}}, {appendEntry: {text: "x"}}]',
    ):
        assert_error(batch(tst, edit, ["TST-001"]), 422, "PROTECTED_FIELD")
    assert snapshot(project_root) == before


# Spec 036 FR-010
def test_dry_run_reports_without_writing(tst, project_root):
    before = snapshot(project_root)
    edit, ticked = '[{set: {testrun: "TR-2"}}]', ["TST-001", "TST-003"]

    response = batch(tst, edit, ticked, dry_run=True)
    planned = statuses(response)

    assert snapshot(project_root) == before
    assert response.json()["operations"] == ["set testrun"]
    assert generate_description(tst) == ["generateChildren into TST (force)"]
    assert planned == statuses(batch(tst, edit, ticked)) == {"TST-001": "changed", "TST-003": "skipped"}


# Spec 036 FR-013
def test_missing_and_failing_items_do_not_stop_the_batch(tst, project_root):
    locked = item_path(project_root, "TST-002")
    os.chmod(locked, stat.S_IREAD)
    try:
        found = results(batch(tst, '[{set: {testrun: "TR-2"}}]', ["TST-099", "TST-002", "TST-001"]))
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)

    assert (found["TST-099"]["status"], found["TST-099"]["reason"]) == ("skipped", "item not found")
    assert found["TST-002"]["status"] == "failed" and found["TST-002"]["reason"]
    assert found["TST-001"]["status"] == "changed"
    assert value(project_root, "TST-002", "testrun") == "TR-1"
    # The next request rebuilds the tree from disk instead of serving the failed item from memory.
    tree = tst.get("/tree?fields=testrun").json()
    fields = {i["uid"]: i.get("fields") for d in tree["documents"] for i in d["items"]}
    assert fields["TST-001"] == {"testrun": "TR-2"} and fields["TST-002"] == {"testrun": "TR-1"}


# --- US2: add / remove ---------------------------------------------------------


# Spec 036 FR-005
def test_add_only_where_the_field_is_missing(tst, project_root):
    found = results(batch(tst, '[{add: {testrun: "TR-9"}}]', ["TST-001", "TST-003"]))

    assert (found["TST-001"]["status"], found["TST-001"]["reason"]) == ("skipped", "field exists: testrun")
    assert found["TST-003"]["status"] == "changed"
    assert value(project_root, "TST-001", "testrun") == "TR-1"
    assert value(project_root, "TST-003", "testrun") == "TR-9"


# Spec 036 FR-006
def test_remove_fields(tst, project_root):
    found = results(batch(tst, "[{remove: testrun}, {remove: [owner]}]", ["TST-001", "TST-003"]))

    assert found["TST-001"]["status"] == "changed"
    assert found["TST-003"]["status"] == "skipped" and "field missing: testrun" in found["TST-003"]["reason"]
    assert "testrun:" not in item_path(project_root, "TST-001").read_text(encoding="utf-8")
    assert value(project_root, "TST-001", "owner") is None


# --- US3: list entries -----------------------------------------------------------


# Spec 036 FR-007
def test_append_entry(tst, project_root):
    set_custom(project_root, "TST-001", runs=["TR-1"])
    set_custom(project_root, "TST-002", runs=["TR-1", "TR-2"])

    found = results(batch(tst, '[{appendEntry: {runs: "TR-2"}}, {appendEntry: {testrun: "TR-2"}}]',
                          ["TST-001", "TST-002", "TST-003"]))

    assert value(project_root, "TST-001", "runs") == ["TR-1", "TR-2"]
    assert value(project_root, "TST-002", "runs") == ["TR-1", "TR-2"]
    assert value(project_root, "TST-003", "runs") == ["TR-2"]
    assert "entry exists: runs" in found["TST-002"]["reason"]
    assert "not a list: testrun" in found["TST-001"]["reason"]
    assert value(project_root, "TST-001", "testrun") == "TR-1"


# Spec 036 FR-008
def test_remove_entry(tst, project_root):
    set_custom(project_root, "TST-001", runs=["A", "B", "A"])
    set_custom(project_root, "TST-002", runs=["A"])

    found = results(batch(tst, '[{removeEntry: {runs: "A"}}]', ["TST-001", "TST-002", "TST-003"]))

    assert value(project_root, "TST-001", "runs") == ["B"]
    assert value(project_root, "TST-002", "runs") == []
    assert (found["TST-003"]["status"], found["TST-003"]["reason"]) == ("skipped", "field missing: runs")
    for edit, reason in (('[{removeEntry: {runs: "Z"}}]', "entry missing: runs"),
                         ('[{removeEntry: {testrun: "TR-1"}}]', "not a list: testrun")):
        assert results(batch(tst, edit, ["TST-001"]))["TST-001"]["reason"] == reason


# --- US4: generate children ------------------------------------------------------


@pytest.fixture
def req(client, document):
    """REQ-001..REQ-003."""
    for _ in range(3):
        add_item(client)
    return client


def generate(client, uids, params="", dry_run=False):
    return batch(client, f"[{{generateChildren: {{{params}}}}}]", uids, dry_run, filters='document == "REQ"')


def linked_parents(project_root: Path, uid: str):
    return [str(link) for link in build(project_root).find_item(uid).links]


# Spec 036 FR-009
def test_generate_children_takes_the_parent_numbers(req, project_root):
    add_document(req, project_root, "TST", digits=4)
    build(project_root).find_document("TST").add_item(name="5")

    found = results(generate(req, ["REQ-001", "REQ-003"]))

    assert {uid: (r["status"], r["created"]) for uid, r in found.items()} == {
        "REQ-001": ("created", "TST-0001"),
        "REQ-003": ("created", "TST-0003"),
    }
    assert linked_parents(project_root, "TST-0001") == ["REQ-001"]
    assert linked_parents(project_root, "TST-0003") == ["REQ-003"]


# Spec 036 FR-009
def test_generate_children_target_document(req, project_root):
    add_document(req, project_root, "TST")
    add_document(req, project_root, "SYS")
    before = snapshot(project_root)

    assert_error(generate(req, ["REQ-001"]), 409, "CHILD_DOCUMENT_REQUIRED")
    assert_error(generate(req, ["REQ-001"], 'document: "REQ"'), 409, "NOT_A_CHILD_DOCUMENT")
    add_item(req, "TST")
    assert_error(generate(req, ["REQ-001", "TST-001"], 'document: "TST"'), 409, "MIXED_DOCUMENTS")
    assert_error(generate(req, ["TST-001"]), 409, "NO_CHILD_DOCUMENT")
    after_add = snapshot(project_root)
    assert set(after_add) - set(before) == {item_path(project_root, "TST-001")}

    assert results(generate(req, ["REQ-002"], 'document: "SYS"'))["REQ-002"]["created"] == "SYS-002"


# Spec 036 FR-003
def test_generate_children_cannot_be_combined(req, project_root):
    add_document(req, project_root, "TST")
    response = batch(req, '[{generateChildren: }, {set: {status: "x"}}]', ["REQ-001"], filters='document == "REQ"')
    assert_error(response, 400, "INVALID_EDIT")


# Spec 036 FR-018
def test_existing_child_is_skipped_without_force(req, project_root):
    add_document(req, project_root, "TST")
    add_item(req, "TST", text="keep me")
    before = item_path(project_root, "TST-001").read_bytes()

    found = results(generate(req, ["REQ-001", "REQ-002"]))

    assert (found["REQ-001"]["status"], found["REQ-001"]["reason"]) == ("skipped", "UID exists: TST-001")
    assert found["REQ-002"]["status"] == "created"
    assert item_path(project_root, "TST-001").read_bytes() == before


# Spec 036 FR-018, FR-010
def test_force_overwrites_existing_child(req, project_root):
    add_document(req, project_root, "TST")
    add_item(req, "TST", text="old text")
    before = snapshot(project_root)

    planned = statuses(generate(req, ["REQ-001"], "force: true", dry_run=True))
    assert planned == {"REQ-001": "overwritten"}
    assert snapshot(project_root) == before

    assert statuses(generate(req, ["REQ-001"], "force: true")) == {"REQ-001": "overwritten"}
    child = build(project_root).find_item("TST-001")
    assert str(child.text) == ""
    assert [str(link) for link in child.links] == ["REQ-001"]


# Spec 036 FR-019
def test_blank_counts_up_in_request_order(req, project_root):
    add_document(req, project_root, "TST")
    add_item(req, "TST")

    planned = results(generate(req, ["REQ-003", "REQ-001"], "blank: true, force: true", dry_run=True))
    found = results(generate(req, ["REQ-003", "REQ-001"], "blank: true, force: true"))

    assert {uid: r["created"] for uid, r in found.items()} == {"REQ-003": "TST-002", "REQ-001": "TST-003"}
    assert {uid: r["created"] for uid, r in planned.items()} == {"REQ-003": "TST-002", "REQ-001": "TST-003"}
    assert linked_parents(project_root, "TST-002") == ["REQ-003"]
    assert linked_parents(project_root, "TST-003") == ["REQ-001"]


# --- Performance -----------------------------------------------------------------


# Spec 036 SC-002
def test_five_hundred_items_in_under_ten_seconds(client, document, project_root):
    add_document(client, project_root, "TST")
    tst = build(project_root).find_document("TST")
    uids = []
    for _ in range(500):
        item = tst.add_item()
        item.set("testrun", "old")
        uids.append(str(item.uid))

    start = time.perf_counter()
    response = batch(client, '[{set: {testrun: "new"}}]', uids)
    elapsed = time.perf_counter() - start

    assert set(statuses(response).values()) == {"changed"}
    assert elapsed < 10, f"{elapsed:.1f} s"
