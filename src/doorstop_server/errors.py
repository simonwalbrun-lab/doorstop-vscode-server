import re
from pathlib import Path

from doorstop.common import DoorstopError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from doorstop_server.doorstop_tree import find_skipped_documents


class DoorstopApiError(Exception):
    """A request-level error raised by a route handler (bad input, not found, ...)."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _disabled_document_of(request: Request, message: str) -> str | None:
    """Prefix of the disabled document an unknown-UID error is really about (spec 031 FR-006).

    Doorstop does not load a skipped document, so its items are "not found";
    say that the document is disabled instead.
    """
    match = re.fullmatch(r"no item with UID: (\S+)", message)
    if not match:
        return None
    for prefix, marker in find_skipped_documents(request.app.state.settings.project_root):
        folder = Path(marker).parent
        if any((folder / f"{match[1]}{ext}").is_file() for ext in (".yml", ".md")):
            return prefix
    return None


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DoorstopApiError)
    async def handle_api_error(_request: Request, exc: DoorstopApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(DoorstopError)
    async def handle_doorstop_error(request: Request, exc: DoorstopError) -> JSONResponse:
        disabled = _disabled_document_of(request, str(exc))
        if disabled:
            return _error_response(409, "DOCUMENT_DISABLED", f"Document {disabled} is disabled; enable it first")
        return _error_response(400, "DOORSTOP_ERROR", str(exc))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(500, "INTERNAL_ERROR", str(exc))
