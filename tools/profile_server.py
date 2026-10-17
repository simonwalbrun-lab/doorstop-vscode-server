"""Profiles the server in-process with cProfile: 10 scenarios against a generated tree.

    python tools/profile_server.py [--docs 5] [--items 300] [--out profiles] [--top 15]

Writes profiles/NN_<scenario>.prof (open with `snakeviz` or `python -m pstats`)
and prints each scenario's top functions by cumulative time. Scenarios run in
order on one server, like a real session: cold load first, then reads, then
changes.

Everything runs on the profiler's thread: cProfile keeps one call stack, so work
on other threads (TestClient's app thread, FastAPI's thread pool) would pile up
under "waiting" and show as unsplit full-width bars in snakeviz.
"""

import argparse
import asyncio
import cProfile
import pstats
import tempfile
import time
from pathlib import Path

import anyio.to_thread
import doorstop
import httpx

from doorstop_server.app import create_app
from doorstop_server.config import Settings
from doorstop_server.validation_rules import collect_issues

PREFIXES = ["REQ", "SYS", "ARC", "TST", "VER"]


async def _inline(func, *args, **_kwargs):
    """Stands in for anyio's thread pool (FastAPI's sync dependencies)."""
    return func(*args)


anyio.to_thread.run_sync = _inline


async def call(app, method: str, path: str, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://profile") as client:
        return await client.request(method, path, json=body)


def generate(root: Path, docs: int, items: int) -> None:
    """A chain of documents REQ <- SYS <- ARC ...; every child item links to its parent's twin."""
    tree = doorstop.build(cwd=str(root), root=str(root), request_next_number=None)
    parent = None
    for prefix in PREFIXES[:docs]:
        document = tree.create_document(str(root / prefix), prefix, parent=parent, sep="-")
        for number in range(1, items + 1):
            item = document.add_item()
            item.text = f"{prefix} requirement {number}: the system shall log {number} events."
            if parent:
                item.link(f"{parent}-{number:03}")
        parent = prefix


def scenarios(out: Path, docs: int) -> list:
    """(name, method, path, json) in execution order; each covers a different server path."""
    leaf, child = PREFIXES[docs - 1], PREFIXES[min(1, docs - 1)]
    return [
        ("cold_tree", "GET", "/tree", None),  # full build from disk
        ("warm_tree", "GET", "/tree", None),  # fingerprint only, shared tree
        # /validate only waits on the worker process, so profile the check itself, in-process.
        ("validate", "CHECK", None, None),
        ("filter", "POST", "/filter", {"query": 'text.contains("log 7")'}),
        ("add_item", "POST", f"/documents/{leaf}/items", {}),
        ("edit_item", "PATCH", "/items/REQ-001", {"text": "edited while profiling"}),
        ("partial_reload", "GET", "/tree", None),  # reloads only REQ after the edit
        ("link", "POST", f"/items/{leaf}-002/links", {"parentUid": "REQ-002"}),
        ("review_document", "POST", "/review", {"scope": "document", "target": child}),
        ("publish_all_html", "POST", "/publish", {"format": "html", "destinationPath": str(out / "html")}),
    ]


def main() -> None:  # pylint: disable=too-many-locals  # one linear script
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", type=int, default=5, choices=range(1, len(PREFIXES) + 1))
    parser.add_argument("--items", type=int, default=300, help="items per document")
    parser.add_argument("--out", type=Path, default=Path("profiles"))
    parser.add_argument("--top", type=int, default=15, help="functions printed per scenario")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        print(f"Generating {args.docs} x {args.items} items in {root} ...")
        generate(root, args.docs, args.items)
        app = create_app(Settings(project_root=str(root), host="127.0.0.1", port=0))

        timings = []
        for number, (name, method, path, body) in enumerate(scenarios(args.out, args.docs), start=1):
            profile = cProfile.Profile()
            started = time.perf_counter()
            profile.enable()
            if method == "CHECK":
                response = httpx.Response(200, json=len(list(collect_issues(app.state.cached_tree))))
            else:
                response = asyncio.run(call(app, method, path, body))
            profile.disable()
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code >= 400:
                raise SystemExit(f"{name}: {method} {path} -> {response.status_code} {response.text}")
            target = args.out / f"{number:02}_{name}.prof"
            profile.dump_stats(target)
            timings.append((name, elapsed, response.headers.get("server-timing", "")))
            print(f"\n=== {number:02} {name}: {method} {path} {elapsed:.0f} ms -> {target}")
            # Only server and Doorstop frames: the test client's own frames would top every list.
            frames = r"doorstop_server|site-packages.doorstop"
            pstats.Stats(profile).sort_stats("cumulative").print_stats(frames, args.top)

    print("\nScenario            ms      Server-Timing")
    for name, elapsed, server_timing in timings:
        print(f"{name:<18}{elapsed:>8.0f}  {server_timing}")


if __name__ == "__main__":
    main()
