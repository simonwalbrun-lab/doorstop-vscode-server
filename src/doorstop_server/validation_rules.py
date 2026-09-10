"""Adapter from Doorstop's raw validation output to structured issue records.

Doorstop 3.2 yields issues as bare ``DoorstopError`` / ``DoorstopWarning`` /
``DoorstopInfo`` objects whose only payload is a message string -- no check id,
no item reference, no field. Anchoring a problem to a specific line therefore
requires recognising the message, and recognising Doorstop's own message
templates is Doorstop knowledge, so it lives here, server-side (Constitution
Principle I).

**Version coupling, stated plainly**: ``CHECK_TABLE`` is pinned to Doorstop 3.2's
wording and needs review on a Doorstop upgrade. An unmatched message is never
dropped -- it degrades to ``check="unknown"`` with ``field=None`` at Doorstop's
own severity, so an upgrade costs placement accuracy, never correctness or
completeness (FR-011, SC-007).

See specs/014-doorstop-validation-diagnostics/research.md sections 1-4.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from typing import Iterator, List, Optional, Pattern

from doorstop import settings
from doorstop.common import DoorstopError, DoorstopInfo, DoorstopWarning

#: Doorstop settings that make *validation* mutate the repository. Verified
#: empirically: a plain ``get_issues()`` pass over a copy of
#: ``testdata/regression`` rewrote 7 of 9 item files and silently stamped away
#: REQ-007's suspect link instead of reporting it (research.md section 1).
_READ_ONLY_SETTINGS = {
    "REFORMAT": False,
    "REORDER": False,
    "REVIEW_NEW_ITEMS": False,
    "STAMP_NEW_LINKS": False,
}


@contextmanager
def read_only_validation() -> Iterator[None]:
    """Runs the body with every repository-mutating validation setting disabled.

    Restores the previous values in a ``finally`` block, so an exception raised
    mid-validation cannot leave the process in a state where a later, unrelated
    request silently reformats files.
    """
    previous = {name: getattr(settings, name) for name in _READ_ONLY_SETTINGS}
    try:
        for name, value in _READ_ONLY_SETTINGS.items():
            setattr(settings, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


@dataclass
class ValidationRecord:
    """One classified issue, before it becomes a pydantic response model."""

    severity: str
    check: str
    message: str
    document_prefix: str
    uids: List[str] = dataclass_field(default_factory=list)
    related_uid: Optional[str] = None
    field: Optional[str] = None


@dataclass(frozen=True)
class CheckRule:
    """One row of the check catalogue (contracts/validation-api.md section 2).

    ``pattern`` is ``None`` for a *reserved* id: a check the spec asks for that
    Doorstop 3.2 never emits. Reserved rows exist so the id and its anchor are
    already agreed if a future Doorstop version starts reporting them; nothing
    can match them today.
    """

    check: str
    pattern: Optional[Pattern[str]]
    field: Optional[str] = None
    #: Regex group holding the *other* item named by the message, if any.
    related_uid_group: Optional[str] = None
    #: Regex group holding a parenthesised list of the items the issue concerns.
    uids_group: Optional[str] = None


_UID = r"[^\s:]+"

#: Ordered: the first matching rule wins. Patterns are anchored end to end so a
#: message can never be mistaken for a shorter one that happens to prefix it.
CHECK_TABLE: List[CheckRule] = [
    # --- Errors (contract section 2b) ---
    CheckRule(
        "invalid_uid_in_links",
        re.compile(rf"^invalid UID in links: (?P<related>{_UID})$"),
        field="link_entry",
        related_uid_group="related",
    ),
    CheckRule(
        "linked_to_unknown_item",
        re.compile(rf"^linked to unknown item: (?P<related>{_UID})$"),
        field="link_entry",
        related_uid_group="related",
    ),
    CheckRule(
        "external_reference_not_found",
        re.compile(r"^external reference not found: .+$"),
        field="ref",
    ),
    # --- Warnings (contract section 2a) ---
    CheckRule(
        "linked_to_non_normative",
        re.compile(rf"^linked to non-normative item: (?P<related>{_UID})$"),
        field="link_entry",
        related_uid_group="related",
    ),
    CheckRule(
        "suspect_link",
        re.compile(rf"^suspect link: (?P<related>{_UID})$"),
        field="link_entry",
        related_uid_group="related",
    ),
    CheckRule(
        "non_normative_has_links",
        re.compile(r"^non-normative, but has links$"),
        field="links",
    ),
    CheckRule(
        "no_links_from_child_document",
        re.compile(r"^no links from child document: .+$"),
        field="derived",
    ),
    CheckRule(
        "no_links_to_parent_document",
        re.compile(r"^no links to parent document: .+$"),
        field="derived",
    ),
    CheckRule("no_items", re.compile(r"^no items$"), field="document"),
    CheckRule("no_documents", re.compile(r"^no documents$"), field="document"),
    CheckRule("no_text", re.compile(r"^no text$"), field="text"),
    CheckRule("unreviewed_changes", re.compile(r"^unreviewed changes$"), field="reviewed"),
    # Document-level messages that name the items they concern: the concrete
    # fan-out case (FR-005). Every UID inside the parentheses is collected.
    CheckRule(
        "duplicate_level",
        re.compile(r"^duplicate level: \S+ \((?P<uids>[^)]*)\)$"),
        field="level",
        uids_group="uids",
    ),
    CheckRule(
        "skipped_level",
        re.compile(r"^skipped level: \S+ \([^)]*\)(?:, \S+ \([^)]*\))+$"),
        field="level",
        uids_group="*parenthesised*",
    ),
    # --- Info (contract section 2c) ---
    CheckRule("needs_initial_review", re.compile(r"^needs initial review$"), field="reviewed"),
    CheckRule(
        "prefix_differs_from_document",
        re.compile(r"^prefix differs from document \(.*\)$"),
        field=None,
    ),
    CheckRule(
        "unexpected_parent_prefix",
        re.compile(rf"^parent is '[^']*', but linked to: (?P<related>{_UID})$"),
        field="link_entry",
        related_uid_group="related",
    ),
    # --- Reserved: specified, but never emitted by Doorstop 3.2 (section 2d) ---
    # Do not give these patterns. See spec.md "Scope decision" for the empirical
    # reason each is unreachable; implementing them here would duplicate Doorstop
    # validation logic, which Constitution Principle II forbids.
    CheckRule("linked_to_self", None, field="link_entry"),
    CheckRule("link_cycle", None, field="links"),
    CheckRule("child_link_inactive", None, field="link_entry"),
]

#: Extracts every UID from a `(...)`-delimited group list, e.g.
#: "skipped level: 1.2 (REQ-002), 1.4 (REQ-003)".
_PARENTHESISED = re.compile(r"\(([^)]*)\)")


def classify_severity(issue: BaseException) -> str:
    """Doorstop's own class, mapped 1:1 and never overridden.

    Order matters: ``DoorstopInfo`` subclasses ``DoorstopWarning``, which
    subclasses ``DoorstopError``, so testing the base class first would classify
    everything as an error (data-model.md section 1).
    """
    if isinstance(issue, DoorstopInfo):
        return "info"
    if isinstance(issue, DoorstopWarning):
        return "warning"
    if isinstance(issue, DoorstopError):
        return "error"
    return "error"


def _split_uids(raw: str) -> List[str]:
    return [uid.strip() for uid in raw.split(",") if uid.strip()]


def classify(message: str, document_prefix: str, severity: str, owner_uid: Optional[str]) -> ValidationRecord:
    """Turns one Doorstop message into a structured record.

    ``owner_uid`` is the item the message was prefixed with, or ``None`` for a
    document-level message. A rule's ``uids_group`` overrides it, because a
    document-level message such as ``duplicate level`` names the items itself.
    """
    for rule in CHECK_TABLE:
        if rule.pattern is None:
            continue
        match = rule.pattern.match(message)
        if not match:
            continue

        uids = [owner_uid] if owner_uid else []
        if rule.uids_group == "*parenthesised*":
            uids = [
                uid
                for group in _PARENTHESISED.findall(message)
                for uid in _split_uids(group)
            ]
        elif rule.uids_group:
            uids = _split_uids(match.group(rule.uids_group))

        related = match.group(rule.related_uid_group) if rule.related_uid_group else None
        return ValidationRecord(
            severity=severity,
            check=rule.check,
            message=message,
            document_prefix=document_prefix,
            uids=uids,
            related_uid=related,
            field=rule.field,
        )

    # Nothing recognised the message. Report it anyway, at Doorstop's own
    # severity, with no placement claim (FR-011, SC-007).
    return ValidationRecord(
        severity=severity,
        check="unknown",
        message=message,
        document_prefix=document_prefix,
        uids=[owner_uid] if owner_uid else [],
        related_uid=None,
        field=None,
    )


def collect_issues(tree) -> Iterator[ValidationRecord]:
    """Every issue Doorstop reports for the whole tree, classified.

    Iterates documents rather than calling ``Tree.get_issues()`` so the document
    is known from the loop variable and only one prefix layer ever needs
    stripping. ``Document.get_issues()`` re-wraps item issues as
    ``"{uid}: {message}"``, and a message arriving *without* such a prefix is
    exactly the discriminator for a document-level issue (research.md section 3).
    """
    with read_only_validation():
        for document in tree:
            prefix = str(document.prefix)
            uids_in_document = {str(item.uid) for item in document}

            for issue in document.get_issues():
                severity = classify_severity(issue)
                message = str(issue)

                owner_uid = None
                head, separator, tail = message.partition(": ")
                if separator and head in uids_in_document:
                    owner_uid = head
                    message = tail

                yield classify(message, prefix, severity, owner_uid)
