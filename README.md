# doorstop-vscode-server

A small FastAPI server that wraps the [Doorstop](https://pypi.org/project/doorstop/) Python API directly (instead of shelling out to the `doorstop` CLI). It is designed to serve exactly one client — the Doorstop VS Code extension — and answers requests strictly one at a time; there is no concurrent request handling.

## Install (editable, for development)

```bash
pip install -e .[dev]
```

## Run

```bash
python -m doorstop_server --project <path-to-doorstop-project-root> --host 127.0.0.1 --port 7867
```

or, after installing:

```bash
doorstop-vscode-server --project <path-to-doorstop-project-root> --host 127.0.0.1 --port 7867
```

## API

See `src/doorstop_server/routers/` for the endpoint implementations. `GET /health` is the only route that is not serialized behind the request lock.

- `GET /tree` — every document with its items in Doorstop's own order (level, then UID); inactive items included (`routers/tree.py`)
- `POST /documents/{prefix}/items` — create an item; optional `level`, or `after` (UID of an item of the same document: the new item gets Doorstop's "append after" level and the followers are renumbered), plus `header` / `text` (`routers/documents.py`)
- `PATCH /items/{uid}` — set `header` and/or `text` through Doorstop's own setters, one file write (`routers/items.py`)
- `DELETE /items/{uid}` — remove the item and renumber the document like `doorstop remove` (`routers/items.py`)

## Tests

The test suite exercises the real FastAPI app against a temporary Doorstop project (no mocking of Doorstop itself), so it doubles as a pinned-down contract for the HTTP interface — every request/response shape, status code, and error format a test asserts on is something the extension can rely on.

```bash
pip install -e .[dev]
pytest
```

- `tests/conftest.py` — shared fixtures (`client`, `project_root`, `document`) and a `set_item_text` helper for mutating an item directly via the Doorstop API (bypassing the server) to simulate out-of-band edits.
- `tests/test_serialization.py` — proves the one-request-at-a-time guarantee black-box: fires concurrent `/items` calls and checks for zero UID collisions/gaps, rather than inspecting the lock directly.
- `tests/test_errors.py` — pins down the structured `{"error": {"code", "message"}}` shape for both expected (`DoorstopError`) and unexpected exceptions.

## Doorstop version coupling

`src/doorstop_server/validation_rules.py` recognises Doorstop's validation
output by matching its **message wording**, because Doorstop 3.2 yields issues
as bare `DoorstopError` / `DoorstopWarning` / `DoorstopInfo` objects carrying
nothing but a string - no check id, no item reference, no field.

`CHECK_TABLE` in that file is therefore pinned to Doorstop 3.2. **On a Doorstop
upgrade, re-check it**: run the test suite first (several tests assert exact
`check` ids against real Doorstop output and will fail loudly if the wording
moved), then compare against `contracts/validation-api.md` section 2 in the
extension repo.

Two things limit the damage of a missed change:

- An unmatched message is never dropped. It is returned as `check: "unknown"`
  with `field: null` at Doorstop's own severity, so the user still sees the
  problem - only its placement degrades to the item's first line.
- The coupling lives in exactly one named file.

`GET /validate` must also keep running under `read_only_validation()`. Doorstop's
validation **rewrites requirement files** with its shipped defaults: a plain
`get_issues()` pass over a 9-item project rewrote 7 files and silently stamped
away a suspect link instead of reporting it. `test_validate_writes_nothing`
is the regression test for this.
