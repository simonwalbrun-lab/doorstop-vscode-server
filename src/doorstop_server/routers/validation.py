from fastapi import APIRouter, Depends

from doorstop_server.deps import get_tree
from doorstop_server.schemas import ValidationIssue, ValidationResponse
from doorstop_server.validation_rules import collect_issues

router = APIRouter()


@router.get("/validate", response_model=ValidationResponse)
async def validate_tree(tree=Depends(get_tree)) -> ValidationResponse:
    """Every issue Doorstop reports for the whole tree.

    Read-only: ``collect_issues`` runs under a settings scope that disables the
    four Doorstop settings which would otherwise make validation rewrite
    requirement files. Whole-tree by nature -- cycles, cross-document links and
    duplicate levels cannot be judged from a single file, so there is no
    per-file variant of this call.
    """
    tree.load()
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
        for record in collect_issues(tree)
    ]
    return ValidationResponse(issues=issues)
