"""PATCH /items/{uid} with extended header fields (spec 030)."""

from .conftest import tree_items


def _fields(client, prefix, uid, names):
    items = tree_items(client, prefix) if not names else next(
        d for d in client.get(f"/tree?fields={','.join(names)}").json()["documents"] if d["prefix"] == prefix
    )["items"]
    return next(i for i in items if i["uid"] == uid).get("fields")


def test_patch_fields_persists(client, document):
    # spec 030 FR-015
    uid = client.post(f"/documents/{document['prefix']}/items", json={}).json()["uid"]

    response = client.patch(f"/items/{uid}", json={"fields": {"status": "approved", "ref": "a.py"}})

    assert response.status_code == 200, response.text
    assert _fields(client, document["prefix"], uid, ["status", "ref"]) == {"status": "approved", "ref": "a.py"}


def test_patch_fields_together_with_header_and_text(client, document):
    # spec 030 FR-015
    uid = client.post(f"/documents/{document['prefix']}/items", json={}).json()["uid"]

    client.patch(f"/items/{uid}", json={"header": "H", "text": "T", "fields": {"status": "x"}})

    node = next(i for i in tree_items(client, document["prefix"]) if i["uid"] == uid)
    assert (node["header"], node["text"]) == ("H", "T")
    assert _fields(client, document["prefix"], uid, ["status"]) == {"status": "x"}


def test_patch_protected_field_is_rejected_and_writes_nothing(client, document):
    # spec 030 FR-016
    uid = client.post(f"/documents/{document['prefix']}/items", json={"header": "Keep"}).json()["uid"]

    for name in ("level", "links", "reviewed", "save", "document"):
        response = client.patch(f"/items/{uid}", json={"header": "Changed", "fields": {name: "x"}})

        assert response.status_code == 422, name
        assert response.json()["error"]["code"] == "PROTECTED_FIELD"
    node = next(i for i in tree_items(client, document["prefix"]) if i["uid"] == uid)
    assert node["header"] == "Keep"


def test_patch_without_header_text_or_fields_is_rejected(client, document):
    uid = client.post(f"/documents/{document['prefix']}/items", json={}).json()["uid"]

    response = client.patch(f"/items/{uid}", json={})

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "header, text or fields is required"


def test_fields_round_trip_in_a_markdown_format_document(client, project_root):
    # spec 030 FR-015: the Markdown item format keeps extended attributes too
    created = client.post(
        "/documents",
        json={"prefix": "MD", "path": str(project_root / "md"), "separator": "-", "itemFormat": "markdown"},
    )
    assert created.status_code == 200, created.text
    uid = client.post("/documents/MD/items", json={"text": "Body"}).json()["uid"]

    response = client.patch(f"/items/{uid}", json={"fields": {"status": "approved", "ref": "a.py"}})

    assert response.status_code == 200, response.text
    assert next(project_root.glob("md/*.md")).exists()
    assert _fields(client, "MD", uid, ["status", "ref"]) == {"status": "approved", "ref": "a.py"}
