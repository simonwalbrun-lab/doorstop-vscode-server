from pathlib import Path

from fastapi.testclient import TestClient

from doorstop_server.app import create_app
from doorstop_server.config import Settings
from tests.conftest import set_item_text, tree_items

# Read-only requests reuse one tree until a project file changes
# (deps.get_tree_for_reading). These checks run against real Doorstop projects.


def add_item(client) -> str:
    response = client.post("/documents/REQ/items", json={})
    assert response.status_code == 200, response.text
    return response.json()["uid"]


def test_unchanged_project_reuses_the_tree(client, document):
    add_item(client)
    client.get("/tree")
    cached = client.app.state.cached_tree

    client.get("/tree")
    client.get("/validate")
    client.post("/filter", json={"query": ""})

    assert client.app.state.cached_tree is cached


def test_hand_edit_is_picked_up(client, document, project_root: Path):
    uid = add_item(client)
    client.get("/tree")

    set_item_text(project_root, uid, "edited by hand outside the server")

    item = next(item for item in tree_items(client, "REQ") if item["uid"] == uid)
    assert item["text"] == "edited by hand outside the server"


def test_change_through_the_server_is_visible_on_the_next_read(client, document):
    first = add_item(client)
    assert [item["uid"] for item in tree_items(client, "REQ")] == [first]

    second = add_item(client)

    assert [item["uid"] for item in tree_items(client, "REQ")] == [first, second]


def test_read_only_requests_leave_the_cached_tree_as_a_fresh_build_would(client, document, project_root: Path):
    for _ in range(3):
        add_item(client)
    client.get("/tree")
    client.get("/validate")
    client.post("/filter", json={"query": ""})

    cached = client.get("/tree").json()
    fresh_client = TestClient(create_app(Settings(project_root=str(project_root), host="127.0.0.1", port=0)))

    assert cached == fresh_client.get("/tree").json()
    assert client.get("/validate").json() == fresh_client.get("/validate").json()
