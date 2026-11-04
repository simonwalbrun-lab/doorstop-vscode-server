import importlib.util
from pathlib import Path

from doorstop import settings
from doorstop.core.item import Item
from doorstop.core.tree import Tree

from tests.conftest import add_item, link


def _publish(client, prefix, destination, **extra):
    return client.post(
        f"/documents/{prefix}/publish",
        json={"format": "markdown", "destinationPath": str(destination), **extra},
    )


# Spec 028 FR-001
def test_publish_leaves_doorstop_unpatched(client, document, tmp_path):
    methods = (Tree.get_traceability, Item.find_child_items_and_documents)
    add_item(client)
    assert _publish(client, document["prefix"], tmp_path / "a.md").status_code == 200
    assert (Tree.get_traceability, Item.find_child_items_and_documents) == methods
    for source in Path(__file__).parents[1].joinpath("src").rglob("*.py"):
        assert "unittest.mock" not in source.read_text(encoding="utf8"), source


# Spec 028 FR-003
def test_publish_ignores_removed_traceability_field(client, document, tmp_path):
    add_item(client)
    response = _publish(client, document["prefix"], tmp_path / "a.md", traceability="doorstop")
    assert response.status_code == 200


# Spec 028 FR-004
def test_no_child_links_applies_and_restores_setting(client, document, child_document, tmp_path):
    parent, child = add_item(client), add_item(client, child_document["prefix"])
    link(client, child["uid"], parent["uid"])
    with_links = tmp_path / "with.md"
    without = tmp_path / "without.md"
    assert _publish(client, document["prefix"], with_links).status_code == 200
    assert _publish(client, document["prefix"], without, childLinks=False).status_code == 200
    assert "Child links" in with_links.read_text(encoding="utf8")
    assert "Child links" not in without.read_text(encoding="utf8")
    assert settings.PUBLISH_CHILD_LINKS is True


# Spec 028 FR-005
def test_ci_wrapper_module_is_gone():
    assert importlib.util.find_spec("doorstop_server.publish") is None


# Spec 035 FR-012
def test_published_html_resolves_attachment_links(client, document, project_root, tmp_path):
    """An image pasted into item text lives in the document's `assets` folder and is
    linked as `assets/<name>`; that link must still work in the published HTML."""
    assets = project_root / "reqs" / "REQ" / "assets"
    assets.mkdir()
    (assets / "shot.png").write_bytes(b"PNG")
    add_item(client, text="![shot.png](assets/shot.png)")
    out = tmp_path / "out"
    response = client.post(
        f"/documents/{document['prefix']}/publish",
        json={"format": "html", "destinationPath": str(out)},
    )
    assert response.status_code == 200, response.text
    pages = [page for page in out.rglob("*.html") if 'src="assets/shot.png"' in page.read_text(encoding="utf8")]
    assert pages, sorted(str(path.relative_to(out)) for path in out.rglob("*"))
    for page in pages:
        assert (page.parent / "assets" / "shot.png").read_bytes() == b"PNG", page
