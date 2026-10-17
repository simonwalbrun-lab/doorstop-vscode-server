from fastapi import FastAPI

from doorstop_server.config import Settings
from doorstop_server.doorstop_tree import use_libyaml
from doorstop_server.errors import register_exception_handlers
from doorstop_server.lock import SerializeRequestsMiddleware
from doorstop_server.validation_worker import Validator
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
    # The one shared tree (deps._shared_tree); None forces a full build.
    app.state.tree_fingerprint = None
    # +1 whenever the shared tree is reloaded or mutated; keys the /tree answer
    # cache. A mutation, not a read-side reload, also cancels a running check
    # (spec 032 research R3, C2, deps.get_tree).
    app.state.tree_version = 0
    # The check process, started on the first /validate (spec 032 research R2).
    app.state.validator = Validator(settings.project_root)
    # GET /tree answers per `fields` value, valid while tree_version is unchanged.
    app.state.tree_responses = {}
    use_libyaml()

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
