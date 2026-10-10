"""Cross-document links in published output (spec 028).

Sample project from the spec: REQ -> SYS -> TST, where TST001 links to SYS001,
SYS002 (multi-parent) and REQ002 (skips SYS). Plain Doorstop drops the
TST001 -> REQ002 link from the matrix and from REQ002's child links.
"""

import csv
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import doorstop
import pytest
from doorstop import settings
from doorstop.core import publisher
from doorstop.core.item import Item
from doorstop.core.tree import Tree

from doorstop_server.publish import publish_options

from .conftest import set_item_attributes

CROSS_ROW = ("REQ002", "", "TST001")


@pytest.fixture
def xlink(client, project_root: Path) -> Path:
    for prefix, parent in (("REQ", None), ("SYS", "REQ"), ("TST", "SYS")):
        body = {"prefix": prefix, "path": str(project_root / prefix.lower()), "separator": ""}
        if parent:
            body["parentPrefix"] = parent
        assert client.post("/documents", json=body).status_code == 200
    for prefix, count in (("REQ", 2), ("SYS", 2), ("TST", 1)):
        for _ in range(count):
            assert client.post(f"/documents/{prefix}/items", json={}).status_code == 200
    for child, parent in (
        ("SYS001", "REQ001"),
        ("SYS002", "REQ001"),
        ("TST001", "SYS001"),
        ("TST001", "SYS002"),
        ("TST001", "REQ002"),
    ):
        link(client, child, parent)
    return project_root


def link(client, child: str, parent: str) -> None:
    response = client.post(f"/items/{child}/links", json={"parentUid": parent})
    assert response.status_code == 200, response.text


def unlink(client, child: str, parent: str) -> None:
    assert client.delete(f"/items/{child}/links/{parent}").status_code == 204


def publish_all(client, out: Path, **options) -> Path:
    response = client.post("/publish", json={"format": "html", "destinationPath": str(out), **options})
    assert response.status_code == 200, response.text
    return out


def matrix_rows(out: Path) -> list:
    with open(out / "traceability.csv", newline="", encoding="utf-8") as handle:
        return [tuple(row) for row in list(csv.reader(handle))[1:]]


def doorstop_rows(project_root: Path) -> list:
    """Unpatched Doorstop's matrix, the reference."""
    tree = doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)
    return [tuple(str(item.uid) if item else "" for item in row) for row in tree.get_traceability()]


class _MatrixHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.in_body, self.cell = [], False, None

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self.in_body = True
        elif self.in_body and tag == "tr":
            self.rows.append([])
        elif self.in_body and tag == "td":
            self.cell = ""

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_body = False
        elif self.in_body and tag == "td":
            self.rows[-1].append(self.cell.strip())
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data


def html_matrix_rows(out: Path) -> list:
    parser = _MatrixHtml()
    parser.feed((out / "traceability.html").read_text(encoding="utf-8"))
    return [tuple(row) for row in parser.rows]


def page(out: Path, prefix: str) -> str:
    return (out / "documents" / f"{prefix}.html").read_text(encoding="utf-8")


def line_with(text: str, needle: str, after: str = "") -> str:
    """First line containing ``needle`` after the first occurrence of ``after``."""
    assert after in text
    section = text.split(after, 1)[1] if after else text
    return next(line for line in section.splitlines() if needle in line)


# --- US1: matrix -----------------------------------------------------------


# Spec 028 FR-001, FR-002
def test_skip_level_link_has_a_matrix_row(client, xlink, tmp_path):
    rows = matrix_rows(publish_all(client, tmp_path / "out"))
    assert CROSS_ROW in rows
    assert ("REQ001", "SYS001", "TST001") in rows
    assert ("REQ001", "SYS002", "TST001") in rows


# Spec 028 FR-003
def test_hierarchy_only_matrix_equals_doorstop(client, xlink, tmp_path):
    unlink(client, "TST001", "REQ002")
    assert matrix_rows(publish_all(client, tmp_path / "out")) == doorstop_rows(xlink)


# Spec 028 FR-004
def test_csv_and_html_matrix_have_the_same_rows(client, xlink, tmp_path):
    out = publish_all(client, tmp_path / "out")
    assert html_matrix_rows(out) == matrix_rows(out)


# Spec 028 FR-007
def test_cycles_and_same_document_links_terminate_without_duplicates(client, xlink, tmp_path):
    link(client, "SYS002", "SYS001")  # same document
    # Doorstop's link API refuses cycles, so write one directly.
    set_item_attributes(xlink, "REQ001", links=["SYS001"])  # cycle REQ001 <-> SYS001
    rows = matrix_rows(publish_all(client, tmp_path / "out"))
    assert len(rows) == len(set(rows))
    assert CROSS_ROW in rows


# Spec 028 FR-011
def test_doorstop_mode_matrix_is_doorstops_own(client, xlink, tmp_path):
    rows = matrix_rows(publish_all(client, tmp_path / "out", traceability="doorstop"))
    assert rows == doorstop_rows(xlink)
    assert ("REQ002", "", "") in rows


# Spec 028 FR-011
def test_unknown_traceability_mode_is_rejected(client, xlink, tmp_path):
    response = client.post(
        "/publish", json={"format": "html", "destinationPath": str(tmp_path / "out"), "traceability": "bogus"}
    )
    assert response.status_code == 422


# --- US2: item pages -------------------------------------------------------


# Spec 028 FR-005
def test_target_html_page_lists_cross_document_child(client, xlink, tmp_path):
    req = page(publish_all(client, tmp_path / "out"), "REQ")
    assert 'href="TST.html#TST001"' in req
    assert "TST001" in line_with(req, "Child links", after='id="REQ002"')


# Spec 028 FR-005
def test_target_markdown_page_lists_cross_document_child(client, xlink, tmp_path):
    destination = tmp_path / "REQ.md"
    response = client.post(
        "/documents/REQ/publish", json={"format": "markdown", "destinationPath": str(destination)}
    )
    assert response.status_code == 200, response.text
    assert "TST001" in line_with(destination.read_text(encoding="utf-8"), "Child links:", after="{#REQ002}")


# Spec 028 FR-005
def test_hierarchy_only_child_links_equal_doorstop(client, xlink, tmp_path):
    unlink(client, "TST001", "REQ002")
    ours = tmp_path / "ours.md"
    assert client.post("/documents/SYS/publish", json={"format": "markdown", "destinationPath": str(ours)}).status_code == 200
    tree = doorstop.build(cwd=str(xlink), root=str(xlink), request_next_number=None)
    theirs = tmp_path / "theirs.md"
    publisher.publish(tree.find_document("SYS"), str(theirs), ext=".md")

    def child_lines(path):
        return [line for line in path.read_text(encoding="utf-8").splitlines() if "Child links" in line]

    assert child_lines(ours) == child_lines(theirs)
    assert child_lines(ours)


# Spec 028 FR-006
def test_linking_page_lists_every_parent(client, xlink, tmp_path):
    parents = line_with(page(publish_all(client, tmp_path / "out"), "TST"), "Parent links")
    for uid in ("REQ002", "SYS001", "SYS002"):
        assert uid in parents


# Spec 028 FR-011, FR-005
def test_doorstop_mode_keeps_complete_child_links(client, xlink, tmp_path):
    req = page(publish_all(client, tmp_path / "out", traceability="doorstop"), "REQ")
    assert 'href="TST.html#TST001"' in req


# Spec 028 FR-012
def test_no_child_links_hides_child_links_only(client, xlink, tmp_path):
    full = publish_all(client, tmp_path / "full")
    out = publish_all(client, tmp_path / "out", childLinks=False)
    for html in (out / "documents").glob("*.html"):
        assert "Child links" not in html.read_text(encoding="utf-8")
    tst = page(out, "TST")
    assert "Links:" in tst
    assert "Parent links:" not in tst
    assert matrix_rows(out) == matrix_rows(full)


# --- US3: CI command and templates -----------------------------------------


# Spec 028 FR-009
@pytest.mark.parametrize(
    "flags, options",
    [
        ([], {}),
        (["--traceability", "doorstop"], {"traceability": "doorstop"}),
        (["--no-child-links"], {"childLinks": False}),
    ],
)
def test_cli_matches_endpoint(client, xlink, tmp_path, flags, options):
    # Doorstop's CLI looks for a VCS root above the documents.
    subprocess.run(["git", "init", "-q"], cwd=xlink, check=True)
    cli_out = tmp_path / "cli"
    result = subprocess.run(
        [sys.executable, "-m", "doorstop_server.publish", *flags, "all", str(cli_out), "--html"],
        cwd=xlink,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    api_out = publish_all(client, tmp_path / "api", **options)
    assert matrix_rows(cli_out) == matrix_rows(api_out)
    for prefix in ("REQ", "TST"):
        assert page(cli_out, prefix) == page(api_out, prefix)


# Spec 028 FR-008
def test_custom_template_keeps_complete_links(client, xlink, tmp_path):
    views = xlink / "req" / "template" / "views"
    views.mkdir(parents=True)
    bundled = Path(doorstop.__file__).parent / "views"
    shutil.copy(bundled / "base.tpl", views / "base.tpl")
    shutil.copy(bundled / "doorstop.tpl", views / "custom.tpl")
    out = publish_all(client, tmp_path / "out", template="custom")
    assert CROSS_ROW in matrix_rows(out)
    assert 'href="TST.html#TST001"' in page(out, "REQ")


# --- Polish -----------------------------------------------------------------


# Spec 028 FR-010
def test_publish_changes_no_item_file_and_restores_doorstop(client, xlink, tmp_path):
    before = {path: path.read_bytes() for path in xlink.rglob("*.yml")}
    publish_all(client, tmp_path / "out")
    assert {path: path.read_bytes() for path in xlink.rglob("*.yml")} == before

    originals = (Item.find_child_items_and_documents, Tree.get_traceability, settings.PUBLISH_CHILD_LINKS)
    with pytest.raises(RuntimeError):
        with publish_options(matrix="doorstop", child_links=not originals[2]):
            raise RuntimeError("boom")
    assert (Item.find_child_items_and_documents, Tree.get_traceability, settings.PUBLISH_CHILD_LINKS) == originals
