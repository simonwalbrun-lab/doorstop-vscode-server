from fastapi import APIRouter, Request

from doorstop_server.schemas import ValidationIssue, ValidationResponse

router = APIRouter()


@router.get("/validate", response_model=ValidationResponse)
async def validate_tree(request: Request) -> ValidationResponse:
    """Every issue Doorstop reports for the whole tree.

    Background validation (Constitution Principle I v1.6.0, spec 032 R2): the
    check runs in the validation worker process on its own read-only copy of
    the project. This route takes no request lock and never touches the shared
    tree, so other requests are served at their normal speed meanwhile. Whole-tree
    by nature -- cycles, cross-document links and duplicate levels cannot be
    judged from a single file.

    It answers 409 CANCELLED only for the reasons Principle I allows (R3):
    C1 ``superseded`` (a newer /validate), C2/C2' ``changed`` (a mutation, or the
    files changed while it ran), C3 ``disconnected`` (user cancel, mode Off),
    C4 server shutdown.
    """
    records = await request.app.state.validator.check(request.is_disconnected)
    issues = [
        ValidationIssue(
            severity=record.severity,
            check=record.check,
            message=record.message,
            documentPrefix=record.document_prefix,
            uids=record.uids,
            relatedUid=record.related_uid,
            field=record.field,
        )
        for record in records
    ]
    return ValidationResponse(issues=issues)
