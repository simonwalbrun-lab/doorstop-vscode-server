"""GET /validate as background work in a separate process (spec 032 US1).

The check runs in the validation worker process with its own tree
(Constitution Principle I v1.6.0, research R2/R3). The hooks in
``worker_hooks`` make a check slow or CPU-bound, so these tests can act while
it runs. Real Doorstop projects, no mocks of Doorstop itself (Principle V).
"""

import asyncio
import os
import statistics
import time
from pathlib import Path

import doorstop
import httpx
import pytest
from fastapi.testclient import TestClient

from doorstop_server.app import create_app
from doorstop_server.config import Settings
from doorstop_server.validation_rules import collect_issues
from doorstop_server.validation_worker import Validator

from tests import worker_hooks
from tests.conftest import add_document, add_item, build_sample_project, link
from tests.test_timing import parse_server_timing
ITEMS_PER_DOCUMENT = 5  # with worker_hooks.slow a full check takes 10 x 0.2 s = 2 s


@pytest.fixture
def two_documents(client: TestClient, document: dict, project_root: Path) -> list:
    """REQ plus a child TST, five linked items each; returns the REQ item UIDs."""
    add_document(client, project_root, "TST")
    uids = []
    for _ in range(ITEMS_PER_DOCUMENT):
        uid = add_item(client, text="parent")["uid"]
        child = add_item(client, "TST", text="child")["uid"]
        link(client, child, uid)
        uids.append(uid)
    return uids


@pytest.fixture
def use_hook(client: TestClient, project_root: Path):
    """Gives the app a validator whose worker calls `hook` before each item."""
    validators = []

    def use(hook):
        validator = Validator(str(project_root), item_hook=hook)
        client.app.state.validator = validator
        validators.append(validator)
        return validator

    yield use
    for validator in validators:
        validator.close()


def run(app, scenario):
    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as async_client:
            return await scenario(async_client)

    return asyncio.run(main())


async def timed_get(async_client, url):
    started = time.perf_counter()
    response = await async_client.get(url)
    assert response.status_code == 200, response.text
    return time.perf_counter() - started


# Spec 032 FR-001
def test_the_check_runs_in_another_process(client, two_documents, use_hook, tmp_path, monkeypatch):
    pid_file = tmp_path / "worker.pid"
    monkeypatch.setenv("DOORSTOP_TEST_PID_FILE", str(pid_file))
    use_hook(worker_hooks.pid)

    assert client.get("/validate").status_code == 200

    assert int(pid_file.read_text()) != os.getpid()


# Spec 032 FR-001, SC-001
def test_read_requests_keep_their_speed_during_a_cpu_bound_check(client, two_documents, use_hook):
    use_hook(worker_hooks.busy)
    assert client.get("/validate").status_code == 200  # worker started, its tree loaded

    async def scenario(async_client):
        await async_client.get("/tree")  # the server's own tree loaded
        # A new field name each time, so the /tree answer cache never answers.
        without = [await timed_get(async_client, f"/tree?fields=a{n}") for n in range(15)]
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.1)  # the worker is busy checking now
        during = [await timed_get(async_client, f"/tree?fields=b{n}") for n in range(15)]
        assert not check.done(), "the check ended before the measurement did"
        await check
        return statistics.median(without), statistics.median(during)

    without, during = run(client.app, scenario)

    assert during <= 1.2 * without + 0.010, f"median {during * 1000:.1f} ms during vs {without * 1000:.1f} ms without"


# Spec 032 FR-001
def test_a_request_during_a_check_does_not_wait_for_it(client, two_documents, use_hook):
    use_hook(worker_hooks.slow)

    async def scenario(async_client):
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)
        tree = await async_client.get("/tree")
        assert not check.done()
        return tree, await check

    tree, check = run(client.app, scenario)

    assert tree.status_code == 200
    assert parse_server_timing(tree.headers["server-timing"])["wait"] < 50
    assert check.status_code == 200, check.text


# Spec 032 FR-002, FR-003
def test_a_newer_check_supersedes_the_running_one(client, two_documents, use_hook):
    use_hook(worker_hooks.slow)

    async def scenario(async_client):
        first = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)
        second = asyncio.create_task(async_client.get("/validate"))
        started = time.perf_counter()
        first = await first
        return first, time.perf_counter() - started, await second

    first, cancel_time, second = run(client.app, scenario)

    assert first.status_code == 409
    assert first.json() == {"error": {"code": "CANCELLED", "message": "superseded"}}
    assert cancel_time < 1
    assert second.status_code == 200, second.text


# Spec 032 FR-003, FR-005
def test_an_edit_during_a_check_cancels_it_and_is_applied_once(client, two_documents, use_hook, project_root):
    use_hook(worker_hooks.slow)
    uid = two_documents[0]

    async def scenario(async_client):
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)
        started = time.perf_counter()
        patch = await async_client.patch(f"/items/{uid}", json={"text": "edited meanwhile"})
        patch_time = time.perf_counter() - started
        check = await check
        return patch, patch_time, check, time.perf_counter() - started

    patch, patch_time, check, cancel_time = run(client.app, scenario)

    assert patch.status_code == 200, patch.text
    assert patch_time < 1, "the edit waited for the check"
    assert check.status_code == 409
    assert check.json()["error"] == {"code": "CANCELLED", "message": "changed"}
    assert cancel_time < 1
    tree = doorstop.build(cwd=str(project_root), root=str(project_root), request_next_number=None)
    assert tree.find_item(uid).text == "edited meanwhile"


# Spec 032 FR-003
def test_a_publish_during_a_check_does_not_cancel_it(client, two_documents, use_hook, tmp_path):
    use_hook(worker_hooks.slow)

    async def scenario(async_client):
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)
        published = await async_client.post(
            "/publish", json={"format": "html", "destinationPath": str(tmp_path / "published")}
        )
        return published, await check

    published, check = run(client.app, scenario)

    assert published.status_code == 200, published.text
    assert check.status_code == 200, check.text  # nothing the check reads changed


# Spec 032 FR-003
def test_an_outside_edit_during_a_check_cancels_it(client, two_documents, use_hook, project_root):
    use_hook(worker_hooks.slow)
    assert client.get("/validate").status_code == 200  # worker warm: the next check starts at once
    item_file = Path(doorstop.build(cwd=str(project_root), root=str(project_root)).find_item(two_documents[0]).path)

    async def scenario(async_client):
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)  # the worker has fingerprinted the files by now
        item_file.write_text(item_file.read_text(encoding="utf-8") + "\nnote: edited outside\n", encoding="utf-8")
        return await check

    check = run(client.app, scenario)

    assert check.status_code == 409
    assert check.json()["error"] == {"code": "CANCELLED", "message": "changed"}


# Spec 032 SC-002
def test_a_tree_reload_of_a_save_the_check_already_sees_does_not_cancel_it(
    client, two_documents, use_hook, project_root
):
    use_hook(worker_hooks.slow)
    client.get("/tree")  # the server's tree is loaded before the save
    item_file = Path(doorstop.build(cwd=str(project_root), root=str(project_root)).find_item(two_documents[0]).path)
    item_file.write_text(item_file.read_text(encoding="utf-8") + "\nnote: saved in the editor\n", encoding="utf-8")

    async def scenario(async_client):
        check = asyncio.create_task(async_client.get("/validate"))
        await asyncio.sleep(0.3)
        tree = await async_client.get("/tree")  # reloads the saved file
        return tree, await check

    tree, check = run(client.app, scenario)

    assert tree.status_code == 200
    assert check.status_code == 200, check.text


# Spec 032 FR-003
def test_a_disconnected_client_cancels_the_check(client, two_documents, use_hook, monkeypatch):
    use_hook(worker_hooks.slow)

    async def gone(_request):
        return True

    monkeypatch.setattr("starlette.requests.Request.is_disconnected", gone)

    started = time.perf_counter()
    response = client.get("/validate")

    assert response.status_code == 409
    assert response.json()["error"] == {"code": "CANCELLED", "message": "disconnected"}
    assert time.perf_counter() - started < 1


# Spec 032 FR-020
def test_the_check_copy_stays_loaded_and_reloads_only_changed_documents(client, two_documents, monkeypatch):
    validator = client.app.state.validator
    assert client.get("/validate").status_code == 200
    assert validator.last_reload == "full"

    async def gone(_request):
        return True

    with monkeypatch.context() as patched:
        patched.setattr("starlette.requests.Request.is_disconnected", gone)
        assert client.get("/validate").status_code == 409
    assert client.get("/validate").status_code == 200
    assert validator.last_reload == "none", "the cancelled check restarted the worker"

    client.patch(f"/items/{two_documents[0]}", json={"text": "edited"})
    assert client.get("/validate").status_code == 200
    assert validator.last_reload == "partial"


def test_a_crashed_worker_is_replaced_by_the_next_check(client, two_documents):
    validator = client.app.state.validator
    assert client.get("/validate").status_code == 200

    validator.process.kill()
    validator.process.join(5)

    response = client.get("/validate")
    assert response.status_code == 200, response.text
    assert validator.last_reload == "full"


# Spec 032 FR-006
def test_background_results_equal_the_whole_tree_validation(tmp_path):
    root = build_sample_project(tmp_path)
    app = create_app(Settings(project_root=str(root), host="127.0.0.1", port=0))
    try:
        response = TestClient(app).get("/validate")
    finally:
        app.state.validator.close()

    tree = doorstop.build(cwd=str(root), root=str(root), request_next_number=None)
    expected = [
        {
            "severity": r.severity,
            "check": r.check,
            "message": r.message,
            "documentPrefix": r.document_prefix,
            "uids": r.uids,
            "relatedUid": r.related_uid,
            "field": r.field,
        }
        for r in collect_issues(tree)
    ]
    assert response.status_code == 200, response.text
    assert response.json()["issues"] == expected
    assert {issue["check"] for issue in expected} >= {"suspect_link", "linked_to_unknown_item", "no_text"}, (
        "the sample project must report problems for this comparison to mean anything"
    )
