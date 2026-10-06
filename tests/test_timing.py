import asyncio
import time
from pathlib import Path

import httpx

from doorstop_server import deps
from doorstop_server.app import create_app
from doorstop_server.config import Settings


def parse_server_timing(header: str) -> dict:
    """{"wait": 0.1, "load": 80.0, "work": 12.3, "route": "GET /tree"} from a Server-Timing header."""
    result = {}
    for metric in header.split(","):
        name, *params = [part.strip() for part in metric.split(";")]
        for param in params:
            key, _, value = param.partition("=")
            if key == "dur":
                result[name] = float(value)
            elif key == "desc":
                result[name] = value.strip('"')
    return result


def test_tree_reports_wait_load_and_work(client, document):
    timing = parse_server_timing(client.get("/tree").headers["server-timing"])

    for stage in ("wait", "load", "work"):
        assert timing[stage] >= 0, timing
    assert timing["load"] > 0, "GET /tree builds the Doorstop tree, so load must be measured"
    assert timing["route"] == "GET /tree"


def test_route_is_the_template_not_the_concrete_path(client, document):
    response = client.post("/documents/REQ/items", json={})

    assert response.status_code == 200
    assert parse_server_timing(response.headers["server-timing"])["route"] == "POST /documents/{prefix}/items"


def test_error_responses_still_carry_the_header(client, document):
    response = client.delete("/items/NOPE-999")

    assert response.status_code >= 400
    timing = parse_server_timing(response.headers["server-timing"])
    assert timing["route"] == "DELETE /items/{uid}"
    assert "work" in timing


def test_health_never_waits(client):
    assert parse_server_timing(client.get("/health").headers["server-timing"])["wait"] == 0


def test_a_queued_request_reports_its_wait(tmp_path: Path, monkeypatch):
    """Slows the real tree load (Doorstop itself is not mocked) so the second of two
    concurrent requests deterministically queues behind the first (see lock.py)."""
    real_load_tree = deps.load_tree

    def slow_load_tree(settings):
        time.sleep(0.05)
        return real_load_tree(settings)

    settings = Settings(project_root=str(tmp_path), host="127.0.0.1", port=0)
    app = create_app(settings)

    async def run() -> list[dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            created = await async_client.post(
                "/documents", json={"prefix": "REQ", "path": str(tmp_path / "reqs" / "REQ")}
            )
            assert created.status_code == 200
            monkeypatch.setattr(deps, "load_tree", slow_load_tree)
            responses = await asyncio.gather(
                *[async_client.post("/documents/REQ/items", json={}) for _ in range(2)]
            )
            return [parse_server_timing(response.headers["server-timing"]) for response in responses]

    timings = asyncio.run(run())

    assert max(timing["wait"] for timing in timings) >= 40, timings
