from pathlib import Path

from .conftest import tree_items


def test_link_and_unlink_items(client, document):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    link_response = client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})
    assert link_response.status_code == 200
    assert link_response.json() == {"child": child["uid"], "parent": parent["uid"]}

    tree = client.get("/tree").json()
    child_node = next(i for d in tree["documents"] for i in d["items"] if i["uid"] == child["uid"])
    # A freshly created link has no stamp yet (only /clear sets one), so it starts suspect.
    assert child_node["links"] == [{"uid": parent["uid"], "suspect": True}]

    unlink_response = client.delete(f"/items/{child['uid']}/links/{parent['uid']}")
    assert unlink_response.status_code == 204

    tree_after = client.get("/tree").json()
    child_node_after = next(
        i for d in tree_after["documents"] for i in d["items"] if i["uid"] == child["uid"]
    )
    assert child_node_after["links"] == []


def test_self_link_is_rejected(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.post(f"/items/{item['uid']}/links", json={"parentUid": item["uid"]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"


def test_link_to_unknown_parent_returns_400(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.post(f"/items/{item['uid']}/links", json={"parentUid": "REQ-999"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"


# --- spec 019: PATCH /items/{uid} and DELETE /items/{uid} ---


def _item(client, prefix, uid):
    return next(i for i in tree_items(client, prefix) if i["uid"] == uid)


def test_update_item_header_and_text(client, document):
    created = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.patch(f"/items/{created['uid']}", json={"header": "H", "text": "T\nU"})

    assert response.status_code == 200, response.text
    assert response.json() == {"uid": created["uid"], "path": created["path"], "level": "1.0"}
    node = _item(client, document["prefix"], created["uid"])
    assert node["header"] == "H"
    assert node["text"] == "T\nU"
    assert node["level"] == "1.0"
    assert node["links"] == []
    assert node["active"] is True


def test_update_item_text_only_leaves_header(client, document):
    created = client.post(f"/documents/{document['prefix']}/items", json={"header": "Keep"}).json()

    client.patch(f"/items/{created['uid']}", json={"text": "new text"})

    node = _item(client, document["prefix"], created["uid"])
    assert node["header"] == "Keep"
    assert node["text"] == "new text"


def test_update_markdown_item(client, document, project_root):
    md_path = project_root / "reqs" / "MD"
    client.post(
        "/documents",
        json={
            "prefix": "MD",
            "path": str(md_path),
            "parentPrefix": document["prefix"],
            "separator": "-",
            "itemFormat": "markdown",
        },
    )
    created = client.post("/documents/MD/items", json={}).json()

    response = client.patch(f"/items/{created['uid']}", json={"header": "Title", "text": "Body line"})

    assert response.status_code == 200, response.text
    node = _item(client, "MD", created["uid"])
    assert node["header"] == "Title"
    assert node["text"] == "Body line"
    assert created["path"].endswith(".md")
    body = Path(created["path"]).read_text(encoding="utf-8").split("---")[-1]
    assert "# Title" in body
    assert "Body line" in body


def test_update_item_empty_body_returns_422(client, document):
    created = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.patch(f"/items/{created['uid']}", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_update_unknown_item_returns_400(client, document):
    response = client.patch("/items/REQ-999", json={"text": "x"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"


def test_delete_item_removes_file_and_renumbers(client, document):
    prefix = document["prefix"]
    first = client.post(f"/documents/{prefix}/items", json={"level": "1.1"}).json()
    middle = client.post(f"/documents/{prefix}/items", json={"level": "1.2"}).json()
    last = client.post(f"/documents/{prefix}/items", json={"level": "1.3"}).json()

    response = client.delete(f"/items/{middle['uid']}")

    assert response.status_code == 204
    assert not Path(middle["path"]).exists()
    levels = {i["uid"]: i["level"] for i in tree_items(client, prefix)}
    assert middle["uid"] not in levels
    assert levels[first["uid"]] == "1.1"
    assert levels[last["uid"]] == "1.2"


def test_delete_unknown_item_returns_400(client, document):
    response = client.delete("/items/REQ-999")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"
