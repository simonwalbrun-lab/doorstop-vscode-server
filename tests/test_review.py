def test_review_single_item(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.post("/review", json={"scope": "item", "target": item["uid"]})

    assert response.status_code == 204
    tree = client.get("/tree").json()
    node = next(i for d in tree["documents"] for i in d["items"] if i["uid"] == item["uid"])
    assert node["reviewed"] is True


def test_review_missing_target_returns_422(client, document):
    response = client.post("/review", json={"scope": "item"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARGET_REQUIRED"


def test_review_all_scope_does_not_require_target(client, document):
    client.post(f"/documents/{document['prefix']}/items", json={})

    response = client.post("/review", json={"scope": "all"})

    assert response.status_code == 204


def test_review_document_scope_reviews_every_item(client, document):
    first = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    second = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.post("/review", json={"scope": "document", "target": document["prefix"]})

    assert response.status_code == 204
    tree = client.get("/tree").json()
    nodes = {i["uid"]: i for d in tree["documents"] for i in d["items"]}
    assert nodes[first["uid"]]["reviewed"] is True
    assert nodes[second["uid"]]["reviewed"] is True


def test_review_unknown_target_returns_400(client, document):
    response = client.post("/review", json={"scope": "item", "target": "REQ-999"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"


def test_clear_item_scope(client, document):
    parent = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent["uid"]})

    # A freshly created link has no stamp yet, so it starts out suspect/uncleared.
    before = client.get("/tree").json()
    node_before = next(i for d in before["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert node_before["cleared"] is False

    response = client.post("/clear", json={"scope": "item", "target": child["uid"]})

    assert response.status_code == 204
    after = client.get("/tree").json()
    node_after = next(i for d in after["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert node_after["cleared"] is True


def test_clear_with_unknown_parent_filter_returns_400(client, document):
    item = client.post(f"/documents/{document['prefix']}/items", json={}).json()

    response = client.post(
        "/clear", json={"scope": "item", "target": item["uid"], "parents": ["REQ-999"]}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOORSTOP_ERROR"


def test_clear_with_parents_filter_clears_only_the_named_link(client, document):
    """The per-link "Clear the Suspicion" CodeLens depends on this: `parents`
    must re-stamp only the links it names, leaving the item's other links
    suspect. Doorstop implements the filter in Item.clear(parents=...); this
    test pins the behaviour the extension relies on."""
    parent_a = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    parent_b = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    child = client.post(f"/documents/{document['prefix']}/items", json={}).json()
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent_a["uid"]})
    client.post(f"/items/{child['uid']}/links", json={"parentUid": parent_b["uid"]})

    before = client.get("/tree").json()
    node_before = next(i for d in before["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert {link["uid"]: link["suspect"] for link in node_before["links"]} == {
        parent_a["uid"]: True,
        parent_b["uid"]: True,
    }

    response = client.post(
        "/clear",
        json={"scope": "item", "target": child["uid"], "parents": [parent_a["uid"]]},
    )

    assert response.status_code == 204
    after = client.get("/tree").json()
    node_after = next(i for d in after["documents"] for i in d["items"] if i["uid"] == child["uid"])
    assert {link["uid"]: link["suspect"] for link in node_after["links"]} == {
        parent_a["uid"]: False,
        parent_b["uid"]: True,
    }, "only the named parent's link may be cleared"
