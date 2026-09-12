from pathlib import Path

import doorstop

from .conftest import set_item_attributes, set_item_text, tree_items


def test_tree_reflects_documents_and_items(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.get("/tree")

    assert response.status_code == 200
    body = response.json()
    assert len(body["documents"]) == 1
    doc = body["documents"][0]
    assert doc["prefix"] == document["prefix"]
    assert doc["markerPath"].endswith(".doorstop.yml")
    assert len(doc["items"]) == 1
    assert doc["items"][0]["uid"] == item["uid"]
    assert doc["items"][0]["links"] == []
    assert doc["items"][0]["active"] is True
    assert doc["items"][0]["normative"] is True
    assert doc["items"][0]["derived"] is False


def test_tree_reports_document_parent_relationship(client, document, project_root):
    client.post(
        "/documents",
        json={
            "prefix": "SYS",
            "path": str(project_root / "reqs" / "SYS"),
            "parentPrefix": document["prefix"],
        },
    )

    body = client.get("/tree").json()

    sys_doc = next(d for d in body["documents"] if d["prefix"] == "SYS")
    assert sys_doc["parentPrefix"] == document["prefix"]
    req_doc = next(d for d in body["documents"] if d["prefix"] == document["prefix"])
    assert req_doc["parentPrefix"] is None


def test_tree_link_becomes_suspect_after_parent_content_changes(client, document, project_root):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})
    client.post("/clear", json={"scope": "item", "target": child["uid"]})

    cleared_tree = client.get("/tree").json()
    cleared_node = next(
        i for d in cleared_tree["documents"] for i in d["items"] if i["uid"] == child["uid"]
    )
    assert cleared_node["links"] == [{"uid": parent["uid"], "suspect": False}]

    set_item_text(project_root, parent["uid"], "changed after the link was cleared")

    updated_tree = client.get("/tree").json()
    updated_node = next(
        i for d in updated_tree["documents"] for i in d["items"] if i["uid"] == child["uid"]
    )
    assert updated_node["links"] == [{"uid": parent["uid"], "suspect": True}]


def test_tree_link_to_dangling_parent_is_suspect(client, document):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})
    client.post("/clear", json={"scope": "item", "target": child["uid"]})

    # Remove the parent item's file directly, leaving a dangling link.
    Path(parent["path"]).unlink()

    body = client.get("/tree").json()
    node = next(i for d in body["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert node["links"] == [{"uid": parent["uid"], "suspect": True}]

def test_tree_reports_item_ref(client, document, project_root):
    """`ref` is part of the item payload so the extension can show it without
    reading and parsing the requirement file itself."""
    created = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    tree = doorstop.build(
        cwd=str(project_root), root=str(project_root), request_next_number=None
    )
    tree.find_item(created["uid"]).ref = "src/module.py"

    response = client.get("/tree")

    assert response.status_code == 200
    items = response.json()["documents"][0]["items"]
    item = next(entry for entry in items if entry["uid"] == created["uid"])
    assert item["ref"] == "src/module.py"


def test_tree_reports_null_ref_when_unset(client, document):
    created = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.get("/tree")

    items = response.json()["documents"][0]["items"]
    item = next(entry for entry in items if entry["uid"] == created["uid"])
    assert item["ref"] is None


def test_tree_items_are_sorted_by_level(client, document, project_root):
    # spec 019: the document view relies on the server's order, not file-load order.
    prefix = document["prefix"]
    first = client.post(f"/documents/{prefix}/items", json={}).json()
    second = client.post(f"/documents/{prefix}/items", json={}).json()
    third = client.post(f"/documents/{prefix}/items", json={}).json()
    set_item_attributes(project_root, first["uid"], level="2.0")
    set_item_attributes(project_root, second["uid"], level="1.1")
    set_item_attributes(project_root, third["uid"], level="1.0")

    items = tree_items(client, prefix)

    assert [i["level"] for i in items] == ["1.0", "1.1", "2.0"]
    assert [i["uid"] for i in items] == [third["uid"], second["uid"], first["uid"]]
