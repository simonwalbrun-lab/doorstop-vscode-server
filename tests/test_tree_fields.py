"""GET /tree?fields=... (spec 030): header fields for the document view."""

from .conftest import add_item, set_item_attributes


def _item_with(client, document, **attributes):
    created = add_item(client)
    set_item_attributes(client.app.state.settings.project_root, created["uid"], **attributes)
    return created["uid"]


def _node(client, prefix, uid, query=""):
    items = client.get(f"/tree{query}").json()["documents"]
    return next(i for d in items if d["prefix"] == prefix for i in d["items"] if i["uid"] == uid)


def test_tree_returns_only_requested_fields_the_item_has(client, document):
    # spec 030 FR-013 FR-015
    uid = _item_with(client, document, ref="main.py")
    other = add_item(client)["uid"]
    client.patch(f"/items/{uid}", json={"fields": {"status": "draft"}})

    node = _node(client, document["prefix"], uid, "?fields=ref,status,missing")
    empty = _node(client, document["prefix"], other, "?fields=ref,status,missing")

    assert node["fields"] == {"ref": "main.py", "status": "draft"}
    assert empty["fields"] == {}


def test_tree_without_fields_parameter_has_no_fields(client, document):
    # spec 030 FR-014
    uid = _item_with(client, document, ref="x")

    assert _node(client, document["prefix"], uid).get("fields") is None


def test_tree_never_returns_protected_fields(client, document):
    # spec 030 FR-016
    uid = _item_with(client, document)

    node = _node(client, document["prefix"], uid, "?fields=level,links,reviewed,text,save,document")

    assert node["fields"] == {}
