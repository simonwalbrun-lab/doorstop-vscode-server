"""Partial reload of the shared tree (spec 032 US2).

Real Doorstop projects. Spies count full builds (``load_tree``) and which
documents re-read their items (``Document._iter(reload=True)``), so each test
can say exactly how much was reloaded.
"""

import os
import shutil
from pathlib import Path

import pytest
from doorstop.core.document import Document
from fastapi.testclient import TestClient

from doorstop_server import deps, doorstop_tree
from doorstop_server.app import create_app
from doorstop_server.config import Settings
from doorstop_server.routers import tree as tree_router
from tests.conftest import add_document, add_item, set_item_text


@pytest.fixture
def project(client: TestClient, document: dict, project_root: Path) -> dict:
    """REQ with a child TST beside it and a child NST nested inside REQ's folder; one item each."""
    add_document(client, project_root, "TST")
    add_document(client, project_root, "NST", path=str(project_root / "reqs" / "REQ" / "NST"))
    uids = {prefix: add_item(client, prefix)["uid"] for prefix in ("REQ", "TST", "NST")}
    client.get("/tree")  # warm the shared tree
    return uids


@pytest.fixture
def spy(monkeypatch) -> dict:
    seen = {"builds": 0, "reloaded": []}
    real_load_tree, real_iter = doorstop_tree.load_tree, Document._iter  # pylint: disable=protected-access

    def load_tree(settings):
        seen["builds"] += 1
        return real_load_tree(settings)

    def _iter(self, reload=False):
        if reload:
            seen["reloaded"].append(str(self.prefix))
        return real_iter(self, reload=reload)

    monkeypatch.setattr(doorstop_tree, "load_tree", load_tree)
    monkeypatch.setattr(deps, "load_tree", load_tree)
    monkeypatch.setattr(Document, "_iter", _iter)
    return seen


def item_file(project_root: Path, uid: str) -> Path:
    return next(project_root.rglob(f"{uid}.yml"))


def assert_same_as_fresh(client: TestClient, project_root: Path) -> None:
    # Spec 032 FR-022: whatever was reloaded, the answers equal a brand-new server's.
    fresh = TestClient(create_app(Settings(project_root=str(project_root), host="127.0.0.1", port=0)))
    assert client.get("/tree").json() == fresh.get("/tree").json()
    assert client.get("/validate").json() == fresh.get("/validate").json()


# Spec 032 FR-020, FR-022
def test_editing_one_item_reloads_only_its_document(client, project, project_root, spy):
    tst_items = list(client.app.state.cached_tree.find_document("TST"))

    set_item_text(project_root, project["REQ"], "edited by hand")
    tree = client.get("/tree").json()

    assert spy == {"builds": 0, "reloaded": ["REQ"]}
    assert list(client.app.state.cached_tree.find_document("TST")) == tst_items
    req = next(d for d in tree["documents"] if d["prefix"] == "REQ")
    assert req["items"][0]["text"] == "edited by hand"
    assert_same_as_fresh(client, project_root)


# Spec 032 FR-020
def test_a_nested_document_is_reloaded_on_its_own(client, project, project_root, spy):
    set_item_text(project_root, project["NST"], "nested edit")
    client.get("/tree")

    assert spy == {"builds": 0, "reloaded": ["NST"]}


# Spec 032 FR-020, FR-022
def test_added_and_deleted_item_files_reload_only_their_document(client, project, project_root, spy):
    source = item_file(project_root, project["REQ"])
    shutil.copy(source, source.with_name("REQ-099.yml"))
    item_file(project_root, project["TST"]).unlink()

    tree = client.get("/tree").json()

    assert spy["builds"] == 0
    assert sorted(spy["reloaded"]) == ["REQ", "TST"]
    uids = {d["prefix"]: [i["uid"] for i in d["items"]] for d in tree["documents"]}
    assert "REQ-099" in uids["REQ"]
    assert uids["TST"] == []
    assert_same_as_fresh(client, project_root)


# Spec 032 FR-021, FR-022
@pytest.mark.parametrize("change", ["config", "skip"])
def test_a_doorstop_marker_change_rebuilds_the_whole_tree(client, project, project_root, spy, change):
    if change == "config":
        config = project_root / "reqs" / "REQ" / ".doorstop.yml"
        config.write_text(config.read_text() + "\n# touched\n")
    else:
        (project_root / "reqs" / "TST" / ".doorstop.skip").write_text("")

    client.get("/tree")

    assert spy["builds"] == 1
    assert_same_as_fresh(client, project_root)


# Spec 032 FR-023
def test_nothing_is_reloaded_when_nothing_changed(client, project, project_root, spy):
    client.get("/tree")
    client.get("/validate")
    (project_root / "README.md").write_text("outside every document")
    client.get("/tree")

    assert spy == {"builds": 0, "reloaded": []}


# Spec 032 FR-023
def test_a_mutation_on_an_unchanged_tree_reloads_nothing_first(client, project, spy):
    response = client.patch(f"/items/{project['REQ']}", json={"text": "via the server"})

    assert response.status_code == 200, response.text
    assert spy == {"builds": 0, "reloaded": []}


# Spec 032 FR-020
def test_a_failed_mutation_makes_the_next_request_rebuild(client, project, spy):
    response = client.patch("/items/REQ-404", json={"text": "nobody"})
    assert response.status_code >= 400

    client.get("/tree")

    assert spy["builds"] == 1


# Spec 032 FR-020, SC-006: refilled, not emptied - a miss scans every document item by item.
def test_the_item_cache_is_complete_after_a_partial_reload(client, project, project_root, monkeypatch):
    set_item_text(project_root, project["REQ"], "edited by hand")
    scans = []
    real_find_item = Document.find_item
    monkeypatch.setattr(
        Document, "find_item", lambda self, *a, **k: scans.append(self) or real_find_item(self, *a, **k)
    )

    client.get("/tree")

    assert not scans, "every link lookup is answered from the item cache"
    cache = client.app.state.cached_tree._item_cache  # pylint: disable=protected-access
    assert {str(uid) for uid in cache} >= set(project.values())


# Spec 032 FR-023: the server's own writes already match the tree in memory.
def test_files_written_by_a_mutation_are_not_reloaded(client, project, spy):
    client.patch(f"/items/{project['REQ']}", json={"text": "via the server"})
    tree = client.get("/tree").json()

    assert spy == {"builds": 0, "reloaded": []}
    req = next(d for d in tree["documents"] if d["prefix"] == "REQ")
    assert req["items"][0]["text"] == "via the server"


# Spec 032 FR-021: a marker the server wrote itself still changes the tree's shape.
def test_a_marker_written_by_a_mutation_still_rebuilds(client, project, project_root, spy):
    marker = project_root / "reqs" / "TST" / ".doorstop.yml"
    response = client.post("/documents/skip", json={"markerPath": str(marker), "skip": True})
    assert response.status_code == 200, response.text

    tree = client.get("/tree").json()

    assert spy["builds"] == 1
    assert "TST" not in [d["prefix"] for d in tree["documents"]]


# Spec 032 FR-023: the os.scandir walk sees exactly what the old os.walk/os.stat one did.
def test_fingerprint_matches_a_plain_walk(project_root, project):
    (project_root / ".venv").mkdir()
    (project_root / ".venv" / "ignored.yml").write_text("x: 1")
    (project_root / "notes.txt").write_text("not a Doorstop file")
    expected = {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [name for name in dirnames if name not in {".git", ".tox", ".venv", "venv"}]
        for name in filenames:
            if name.startswith(".doorstop") or name.endswith((".yml", ".yaml", ".md")):
                stat = os.stat(os.path.join(dirpath, name))
                expected[os.path.join(dirpath, name)] = (stat.st_mtime_ns, stat.st_size)

    assert doorstop_tree.fingerprint(str(project_root)) == expected


# Spec 032 FR-022, FR-023: an unchanged tree answers from the last response, a change rebuilds it.
def test_an_unchanged_tree_reuses_its_answer(client, project, monkeypatch):
    built = []
    real_links = tree_router._item_links  # pylint: disable=protected-access
    monkeypatch.setattr(tree_router, "_item_links", lambda item, tree: built.append(item) or real_links(item, tree))

    first = client.get("/tree").json()
    assert not built, "the fixture's warm-up request is reused"
    client.get("/tree?fields=status")
    with_fields = len(built)
    assert with_fields > 0, "other fields are their own answer"

    client.patch(f"/items/{project['REQ']}", json={"text": "changed"})
    changed = client.get("/tree").json()

    assert len(built) > with_fields
    assert changed != first
