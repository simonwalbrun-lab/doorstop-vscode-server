"""Spec 031: skipping a document through its ``.doorstop.skip`` marker."""

from pathlib import Path


def marker_of(client, prefix: str) -> str:
    return next(d for d in client.get("/tree").json()["documents"] if d["prefix"] == prefix)["markerPath"]


def toggle(client, marker: str, skip: bool):
    return client.post("/documents/skip", json={"markerPath": marker, "skip": skip})


def add_child(client, document, project_root) -> str:
    client.post(
        "/documents",
        json={"prefix": "SYS", "path": str(project_root / "reqs" / "SYS"), "parentPrefix": document["prefix"]},
    )
    return marker_of(client, "SYS")


# Spec 031 FR-002, FR-005, FR-009
def test_skip_creates_marker_and_hides_document_from_tree_and_validation(client, document, project_root):
    client.post(f"/documents/{document['prefix']}/items", json={})
    sys_marker = add_child(client, document, project_root)

    response = toggle(client, sys_marker, True)  # the leaf; the parent stays untouched

    assert response.status_code == 200
    assert response.json() == {"markerPath": sys_marker, "skipped": True, "affected": [sys_marker]}
    assert (Path(sys_marker).parent / ".doorstop.skip").is_file()
    body = client.get("/tree").json()
    assert [d["prefix"] for d in body["documents"]] == [document["prefix"]]
    assert body["skippedDocuments"] == [{"prefix": "SYS", "markerPath": sys_marker}]
    assert all(i["documentPrefix"] != "SYS" for i in client.get("/validate").json()["issues"])


# Spec 031 FR-009
def test_skip_cascades_to_descendants_and_enable_does_not(client, document, project_root):
    sys_marker = add_child(client, document, project_root)
    client.post(
        "/documents",
        json={"prefix": "SUB", "path": str(project_root / "reqs" / "SUB"), "parentPrefix": "SYS"},
    )
    sub_marker = marker_of(client, "SUB")
    req_marker = marker_of(client, document["prefix"])

    skipped = toggle(client, req_marker, True)

    assert skipped.status_code == 200
    assert sorted(skipped.json()["affected"]) == sorted([req_marker, sys_marker, sub_marker])
    for marker in (req_marker, sys_marker, sub_marker):
        assert (Path(marker).parent / ".doorstop.skip").is_file()
    body = client.get("/tree").json()
    assert body["documents"] == []
    assert sorted(d["prefix"] for d in body["skippedDocuments"]) == ["REQ", "SUB", "SYS"]

    refused = toggle(client, sys_marker, False)  # the parent is still disabled
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "PARENT_DISABLED"

    enabled = toggle(client, req_marker, False)
    assert enabled.json()["affected"] == [req_marker]  # the children stay disabled
    assert (Path(sys_marker).parent / ".doorstop.skip").is_file()
    assert (Path(sub_marker).parent / ".doorstop.skip").is_file()


# Spec 031 FR-009 (a branch member that was already disabled is left as it is)
def test_cascade_leaves_already_disabled_descendants_alone(client, document, project_root):
    sys_marker = add_child(client, document, project_root)
    req_marker = marker_of(client, document["prefix"])
    toggle(client, sys_marker, True)

    skipped = toggle(client, req_marker, True)

    assert skipped.json()["affected"] == [req_marker]


# Spec 031 FR-003, FR-005
def test_enable_removes_marker_and_restores_document(client, document):
    marker = marker_of(client, document["prefix"])
    toggle(client, marker, True)

    response = toggle(client, marker, False)

    assert response.json()["skipped"] is False
    assert not (Path(marker).parent / ".doorstop.skip").exists()
    body = client.get("/tree").json()
    assert [d["prefix"] for d in body["documents"]] == [document["prefix"]]
    assert body["skippedDocuments"] == []


# Spec 031 FR-002, FR-003 (rapid repeated clicks)
def test_toggle_is_idempotent(client, document):
    marker = marker_of(client, document["prefix"])
    assert toggle(client, marker, True).status_code == 200
    assert toggle(client, marker, True).status_code == 200
    assert toggle(client, marker, False).status_code == 200
    assert toggle(client, marker, False).status_code == 200
    assert client.get("/tree").json()["skippedDocuments"] == []


# Spec 031 FR-008
def test_unknown_marker_path_is_a_typed_404(client, document, project_root):
    response = toggle(client, str(project_root / "nowhere" / ".doorstop.yml"), True)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# Spec 031 FR-007
def test_marker_changed_by_hand_is_picked_up(client, document):
    marker = marker_of(client, document["prefix"])
    skip_file = Path(marker).parent / ".doorstop.skip"

    skip_file.touch()
    assert [d["prefix"] for d in client.get("/tree").json()["skippedDocuments"]] == [document["prefix"]]

    skip_file.unlink()
    body = client.get("/tree").json()
    assert body["skippedDocuments"] == []
    assert [d["prefix"] for d in body["documents"]] == [document["prefix"]]


# Spec 031 FR-006
def test_editing_an_item_of_a_disabled_document_says_the_document_is_disabled(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    toggle(client, marker_of(client, document["prefix"]), True)

    response = client.patch(f"/items/{item['uid']}", json={"text": "changed"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_DISABLED"
    assert document["prefix"] in response.json()["error"]["message"]
