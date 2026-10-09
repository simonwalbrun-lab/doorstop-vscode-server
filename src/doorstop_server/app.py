from fastapi import FastAPI

from doorstop_server.config import Settings
from doorstop_server.errors import register_exception_handlers
from doorstop_server.lock import SerializeRequestsMiddleware
from doorstop_server.routers import (
    documents,
    filters,
    health,
    items,
    review,
    tree,
    validation,
)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="doorstop-vscode-server")
    app.state.settings = settings
    # Shared by read-only requests (deps.get_tree_for_reading).
    app.state.tree_fingerprint = None

    app.add_middleware(SerializeRequestsMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(documents.publish_all_router)
    app.include_router(items.router)
    app.include_router(review.router)
    app.include_router(tree.router)
    app.include_router(validation.router)
    app.include_router(filters.router)

    return app
