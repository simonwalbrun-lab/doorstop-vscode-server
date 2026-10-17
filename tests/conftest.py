from pathlib import Path
from typing import Iterator

import doorstop
import pytest
from fastapi.testclient import TestClient

from doorstop_server.app import create_app
from doorstop_server.config import Settings


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def client(project_root: Path) -> Iterator[TestClient]:
    settings = Settings(project_root=str(project_root), host="127.0.0.1", port=0)
    app = create_app(settings)
    yield TestClient(app)
    app.state.validator.close()  # stops the validation worker, if a test started one


# --- Setup helpers: each asserts success and returns the response body. Tests
# whose subject is one of these requests send it themselves.


def add_document(client: TestClient, project_root: Path, prefix: str, parent="REQ", **fields) -> dict:
    """A document in ``reqs/<prefix>`` below ``parent`` (``None`` for the root).

    Doorstop's default separator is empty (UIDs like "REQ001"); "-" makes test
    UIDs read as "REQ-001", like typical real-world projects. ``fields`` are
    further request fields, e.g. ``separator=""`` or ``itemFormat="markdown"``.
    """
    body = {"prefix": prefix, "path": str(project_root / "reqs" / prefix), "separator": "-", **fields}
    if parent:
        body["parentPrefix"] = parent
    response = client.post("/documents", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def add_item(client: TestClient, prefix: str = "REQ", **fields) -> dict:
    """A new item in ``prefix``; ``fields`` are request fields such as ``text`` or ``after``."""
    response = client.post(f"/documents/{prefix}/items", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def link(client: TestClient, child: str, parent: str) -> None:
    response = client.post(f"/items/{child}/links", json={"parentUid": parent})
    assert response.status_code == 200, response.text


# --- Fixtures: the root document REQ, a child document SYS, a linked pair.


@pytest.fixture
def document(client: TestClient, project_root: Path) -> dict:
    return add_document(client, project_root, "REQ", parent=None)


@pytest.fixture
def child_document(client: TestClient, project_root: Path, document: dict) -> dict:
    return add_document(client, project_root, "SYS")


@pytest.fixture
def linked_items(client: TestClient, document: dict) -> tuple:
    """(parent, child) in REQ; the child's link to the parent is unstamped, so suspect."""
    parent, child = add_item(client), add_item(client)
    link(client, child["uid"], parent["uid"])
    return parent, child


def build_sample_project(root: Path) -> Path:
    """A small Doorstop project with the usual problems, built with Doorstop itself.

    REQ has a reviewed item, an unreviewed one with a heading and unicode,
    multi-line text, and one without text; TST below it has a suspect link and a
    link to a missing item. Self-contained, so the server repository's CI needs
    no fixture from outside it.
    """
    tree = doorstop.build(cwd=str(root), root=str(root), request_next_number=None)
    req = tree.create_document(str(root / "reqs" / "REQ"), "REQ", sep="-")
    tst = tree.create_document(str(root / "reqs" / "TST"), "TST", parent="REQ", sep="-")
    reviewed = req.add_item()
    reviewed.text = "The system shall log every event."
    reviewed.review()
    unreviewed = req.add_item()
    unreviewed.header = "Ünïcödé heading"
    unreviewed.text = "A long line " + "x" * 120 + "\n\nand a second paragraph."
    req.add_item()  # no text
    suspect = tst.add_item()
    suspect.text = "Logging is tested."
    suspect.link(reviewed.uid)  # an unstamped link is suspect
    broken = tst.add_item()
    broken.text = "Links to an item that does not exist."
    broken.link("REQ-999")
    return root


def set_item_text(project_root: Path, uid: str, text: str) -> None:
    """Mutates an item's text directly via the Doorstop API, bypassing the server -
    used to simulate a parent item changing after a link was created, without
    assuming anything about the on-disk YAML/Markdown format."""
    tree = doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)
    item = tree.find_item(uid)
    item.text = text


def set_item_attributes(project_root: Path, uid: str, **attributes) -> None:
    """Sets arbitrary item attributes (level, normative, ...) via the Doorstop API,
    bypassing the server - for shaping a document before a request is made."""
    tree = doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)
    item = tree.find_item(uid)
    for key, value in attributes.items():
        setattr(item, key, value)


def tree_items(client, prefix: str) -> list:
    """The items of one document as GET /tree reports them."""
    tree = client.get("/tree").json()
    return next(d for d in tree["documents"] if d["prefix"] == prefix)["items"]
