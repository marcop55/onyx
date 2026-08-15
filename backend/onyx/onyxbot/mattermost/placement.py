"""Mattermost attachment placement proposals for controlled Seafile promotion."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from onyx.onyxbot.mattermost.models import NormalizedMattermostEvent
from onyx.onyxbot.mattermost.mutations import (
    ControlledSeafileMutationGateway,
    MattermostIdentityLookup,
    MattermostMutationAdapter,
    SeafileActionOrigin,
    SeafileActionRequest,
    SeafileActionType,
)

APPROVED_ONEQODE_ROOTS: tuple[str, ...] = (
    "Company",
    "Projects",
    "Customers",
    "Vendors",
    "Facilities",
    "Reference",
    "Inbox",
    "Archive",
)

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-]+")
_SPACING = re.compile(r"\s+")


@dataclass(frozen=True)
class MattermostAttachmentPlacementInput:
    attachment_id: int
    mattermost_file_id: str
    source_post_id: str
    uploader_user_id: str
    channel_id: str
    root_post_id: str | None
    filename: str
    mime_type: str
    size_bytes: int | None
    sha256: str | None
    file_store_id: str | None
    user_file_id: UUID | None
    content_text: str | None = None


@dataclass(frozen=True)
class SeafilePlacementFileEvidence:
    path: str
    file_id: str
    revision_id: str | None
    size_bytes: int | None
    sha256: str | None = None


@dataclass(frozen=True)
class SeafileHierarchyEvidence:
    library_id: str
    library_name: str
    root_path: str
    root_revision: str
    approved_roots: Sequence[str]
    files: Sequence[SeafilePlacementFileEvidence]


@dataclass(frozen=True)
class MattermostAttachmentProposalIdentity:
    attachment_id: int
    mattermost_file_id: str
    source_post_id: str
    uploader_user_id: str
    channel_id: str
    root_post_id: str | None
    sha256: str | None


@dataclass(frozen=True)
class DuplicateConflictEvidence:
    same_name_paths: list[str]
    byte_identical_paths: list[str]
    same_name_revisions: dict[str, str]
    byte_identical_revisions: dict[str, str]

    @property
    def has_unresolved_evidence(self) -> bool:
        return bool(self.same_name_paths or self.byte_identical_paths)


@dataclass(frozen=True)
class MattermostAttachmentPlacementProposal:
    identity: MattermostAttachmentProposalIdentity
    library_id: str
    proposed_root: str
    proposed_path: str
    normalized_filename: str
    rationale: str
    confidence: float
    should_remain_temporary: bool
    hierarchy_root_revision: str
    duplicate_conflict_evidence: DuplicateConflictEvidence


class MattermostAttachmentPromotionError(RuntimeError):
    """Promotion precondition, read-back, audit, or freshness proof failed."""


@dataclass(frozen=True)
class MattermostPromotionPreflightEvidence:
    hierarchy_root_revision: str
    destination_revision: str | None
    attachment_sha256: str | None
    conflict_paths: Sequence[str]


@dataclass(frozen=True)
class MattermostAttachmentPromotionConfirmation:
    confirmed: bool
    confirmer_user_id: str
    mattermost_file_id: str
    source_post_id: str
    uploader_user_id: str
    channel_id: str
    root_post_id: str | None
    proposed_path: str
    hierarchy_root_revision: str
    attachment_sha256: str | None
    destination_revision: str | None
    conflict_paths: Sequence[str]


@dataclass(frozen=True)
class MattermostAttachmentPromotionReceipt:
    readback_file_id: str
    readback_revision: str
    audit_evidence: dict[str, str | None]
    rollback_data: dict[str, str]
    ingestion_freshness_proof: str


def propose_mattermost_attachment_placement(
    *,
    attachment: MattermostAttachmentPlacementInput,
    hierarchy: SeafileHierarchyEvidence,
    request_is_system_admin: bool,
    seafile_transport: Callable[..., object] | None = None,
) -> MattermostAttachmentPlacementProposal:
    """Return a sourced recommendation without performing Seafile transport."""

    if type(request_is_system_admin) is not bool:
        raise ValueError("Mattermost requester admin state must be explicit")
    if seafile_transport is not None and not callable(seafile_transport):
        raise ValueError("Seafile transport probe must be callable")
    _validate_hierarchy(hierarchy)
    normalized_filename = _normalize_filename(attachment.filename)
    identity = MattermostAttachmentProposalIdentity(
        attachment_id=attachment.attachment_id,
        mattermost_file_id=attachment.mattermost_file_id,
        source_post_id=attachment.source_post_id,
        uploader_user_id=attachment.uploader_user_id,
        channel_id=attachment.channel_id,
        root_post_id=attachment.root_post_id,
        sha256=attachment.sha256,
    )
    duplicate_conflict_evidence = _duplicate_conflict_evidence(
        normalized_filename=normalized_filename,
        sha256=attachment.sha256,
        files=hierarchy.files,
    )
    root, confidence, rationale = _classify_root(attachment.content_text)
    should_remain_temporary = (
        confidence == 0 or duplicate_conflict_evidence.has_unresolved_evidence
    )
    if duplicate_conflict_evidence.has_unresolved_evidence:
        rationale = f"{rationale}; unresolved duplicate or conflict evidence requires admin review"
    proposed_path = _join_path(root, normalized_filename)
    return MattermostAttachmentPlacementProposal(
        identity=identity,
        library_id=hierarchy.library_id,
        proposed_root=root,
        proposed_path=proposed_path,
        normalized_filename=normalized_filename,
        rationale=rationale,
        confidence=confidence,
        should_remain_temporary=should_remain_temporary,
        hierarchy_root_revision=hierarchy.root_revision,
        duplicate_conflict_evidence=duplicate_conflict_evidence,
    )


async def promote_mattermost_attachment_proposal(
    *,
    event: NormalizedMattermostEvent,
    attachment: MattermostAttachmentPlacementInput,
    proposal: MattermostAttachmentPlacementProposal,
    preflight: MattermostPromotionPreflightEvidence,
    confirmation: MattermostAttachmentPromotionConfirmation,
    mattermost: MattermostIdentityLookup,
    gateway: ControlledSeafileMutationGateway,
    read_back: Callable[[], Mapping[str, str | None]],
    freshness_check: Callable[[], str],
    membership_check: Callable[[str, str], Awaitable[bool]] | None = None,
    bot_user_id: str | None = None,
) -> MattermostAttachmentPromotionReceipt:
    """Execute one confirmed, guarded attachment promotion through typed APIs."""

    _validate_promotion_preconditions(
        attachment=attachment,
        proposal=proposal,
        preflight=preflight,
        confirmation=confirmation,
    )
    if event.user_id != confirmation.confirmer_user_id:
        raise MattermostAttachmentPromotionError(
            "Mattermost confirmation user changed before promotion"
        )
    if membership_check is not None:
        if bot_user_id is None:
            raise MattermostAttachmentPromotionError(
                "Bot identity is required for promotion membership checks"
            )
        if not await membership_check(event.channel_id, bot_user_id):
            raise MattermostAttachmentPromotionError(
                "Mattermost bot membership changed before promotion"
            )
        if not await membership_check(event.channel_id, attachment.uploader_user_id):
            raise MattermostAttachmentPromotionError(
                "Mattermost sender membership changed before promotion"
            )
    request = SeafileActionRequest(
        action=SeafileActionType.CREATE,
        repo_id=proposal.library_id,
        path=proposal.proposed_path,
        requesting_user="<unverified>",
        origin=SeafileActionOrigin.ATTACHMENT_PROMOTION,
        expected_revision=None,
        content=attachment.file_store_id,
        destination_path=None,
        confirmed=confirmation.confirmed,
        scope_prefix=f"/{proposal.proposed_root}",
    )
    await MattermostMutationAdapter(mattermost, gateway).route(event, request)

    readback = read_back()
    readback_path = readback.get("path")
    readback_file_id = readback.get("file_id")
    readback_revision = readback.get("revision_id")
    readback_sha256 = readback.get("sha256")
    if (
        readback_path != proposal.proposed_path
        or not readback_file_id
        or not readback_revision
        or readback_sha256 != attachment.sha256
    ):
        raise MattermostAttachmentPromotionError("Seafile destination read-back failed")
    freshness_proof = freshness_check()
    if not freshness_proof:
        raise MattermostAttachmentPromotionError(
            "Seafile ingestion freshness proof is required"
        )
    return MattermostAttachmentPromotionReceipt(
        readback_file_id=readback_file_id,
        readback_revision=readback_revision,
        audit_evidence={
            "mattermost_file_id": proposal.identity.mattermost_file_id,
            "source_post_id": proposal.identity.source_post_id,
            "uploader_user_id": proposal.identity.uploader_user_id,
            "channel_id": proposal.identity.channel_id,
            "root_post_id": proposal.identity.root_post_id,
            "sha256": proposal.identity.sha256,
            "seafile_path": proposal.proposed_path,
            "seafile_file_id": readback_file_id,
            "seafile_revision": readback_revision,
        },
        rollback_data={
            "action": "delete_created_file",
            "path": proposal.proposed_path,
            "file_id": readback_file_id,
            "revision_id": readback_revision,
        },
        ingestion_freshness_proof=freshness_proof,
    )


def _validate_promotion_preconditions(
    *,
    attachment: MattermostAttachmentPlacementInput,
    proposal: MattermostAttachmentPlacementProposal,
    preflight: MattermostPromotionPreflightEvidence,
    confirmation: MattermostAttachmentPromotionConfirmation,
) -> None:
    if not confirmation.confirmed:
        raise MattermostAttachmentPromotionError(
            "Fresh signed Mattermost confirmation is required for promotion"
        )
    if proposal.should_remain_temporary:
        raise MattermostAttachmentPromotionError(
            "Temporary or conflicted attachments cannot be promoted"
        )
    if attachment.file_store_id is None:
        raise MattermostAttachmentPromotionError(
            "Stored Mattermost attachment bytes are required for promotion"
        )
    if proposal.hierarchy_root_revision != preflight.hierarchy_root_revision:
        raise MattermostAttachmentPromotionError("Seafile hierarchy revision is stale")
    if tuple(preflight.conflict_paths):
        raise MattermostAttachmentPromotionError(
            "Destination conflict evidence must be resolved before promotion"
        )
    if preflight.destination_revision is not None:
        raise MattermostAttachmentPromotionError(
            "Destination already exists and cannot be silently overwritten"
        )
    if attachment.sha256 != proposal.identity.sha256:
        raise MattermostAttachmentPromotionError(
            "Attachment proposal checksum is stale"
        )
    if attachment.sha256 != preflight.attachment_sha256:
        raise MattermostAttachmentPromotionError(
            "Attachment checksum changed before promotion"
        )
    expected_identity = MattermostAttachmentProposalIdentity(
        attachment_id=attachment.attachment_id,
        mattermost_file_id=attachment.mattermost_file_id,
        source_post_id=attachment.source_post_id,
        uploader_user_id=attachment.uploader_user_id,
        channel_id=attachment.channel_id,
        root_post_id=attachment.root_post_id,
        sha256=attachment.sha256,
    )
    if proposal.identity != expected_identity:
        raise MattermostAttachmentPromotionError(
            "Mattermost attachment identity changed before promotion"
        )
    if (
        confirmation.mattermost_file_id != proposal.identity.mattermost_file_id
        or confirmation.source_post_id != proposal.identity.source_post_id
        or confirmation.uploader_user_id != proposal.identity.uploader_user_id
        or confirmation.channel_id != proposal.identity.channel_id
        or confirmation.root_post_id != proposal.identity.root_post_id
        or confirmation.proposed_path != proposal.proposed_path
        or confirmation.hierarchy_root_revision != proposal.hierarchy_root_revision
        or confirmation.attachment_sha256 != attachment.sha256
        or confirmation.destination_revision != preflight.destination_revision
        or tuple(confirmation.conflict_paths) != tuple(preflight.conflict_paths)
    ):
        raise MattermostAttachmentPromotionError(
            "Mattermost confirmation does not match the current proposal evidence"
        )


def _validate_hierarchy(hierarchy: SeafileHierarchyEvidence) -> None:
    if tuple(hierarchy.approved_roots) != APPROVED_ONEQODE_ROOTS:
        raise ValueError(
            "Seafile placement hierarchy must use the approved OneQode roots"
        )
    if hierarchy.root_path != "/" or not hierarchy.root_revision:
        raise ValueError("Current Seafile root hierarchy evidence is required")
    for file in hierarchy.files:
        root = _root_from_path(file.path)
        if root not in APPROVED_ONEQODE_ROOTS:
            raise ValueError(
                "Seafile hierarchy evidence escaped the approved OneQode root"
            )


def _classify_root(content_text: str | None) -> tuple[str, float, str]:
    if content_text is None or not content_text.strip():
        return "Inbox", 0, "Attachment content could not parse; keeping it temporary"
    lowered = content_text.lower()
    if any(term in lowered for term in ("project", "implementation", "sow")):
        return (
            "Projects",
            0.72,
            "Project Apollo content indicates this belongs under Projects",
        )
    if any(term in lowered for term in ("customer", "client")):
        return (
            "Customers",
            0.68,
            "Customer-facing content indicates this belongs under Customers",
        )
    if "vendor" in lowered or "supplier" in lowered:
        return "Vendors", 0.66, "Vendor content indicates this belongs under Vendors"
    if any(term in lowered for term in ("policy", "handbook", "company")):
        return (
            "Company",
            0.64,
            "Company reference content indicates this belongs under Company",
        )
    return (
        "Inbox",
        0.4,
        "Low-confidence classification; Inbox is the controlled fallback",
    )


def _duplicate_conflict_evidence(
    *,
    normalized_filename: str,
    sha256: str | None,
    files: Sequence[SeafilePlacementFileEvidence],
) -> DuplicateConflictEvidence:
    same_name_paths: list[str] = []
    byte_identical_paths: list[str] = []
    same_name_revisions: dict[str, str] = {}
    byte_identical_revisions: dict[str, str] = {}
    lowered_name = normalized_filename.lower()
    for file in files:
        if PurePosixPath(file.path).name.lower() == lowered_name:
            same_name_paths.append(file.path)
            if file.revision_id:
                same_name_revisions[file.path] = file.revision_id
        if sha256 and file.sha256 == sha256:
            byte_identical_paths.append(file.path)
            if file.revision_id:
                byte_identical_revisions[file.path] = file.revision_id
    return DuplicateConflictEvidence(
        same_name_paths=same_name_paths,
        byte_identical_paths=byte_identical_paths,
        same_name_revisions=same_name_revisions,
        byte_identical_revisions=byte_identical_revisions,
    )


def _normalize_filename(filename: str) -> str:
    basename = PurePosixPath(filename.strip().replace("\\", "/")).name
    cleaned = _SPACING.sub(" ", _FILENAME_UNSAFE.sub("", basename)).strip(" .")
    if not cleaned:
        return "mattermost-attachment"
    stem, dot, extension = cleaned.rpartition(".")
    if dot:
        stem = stem.rstrip(" ._-") or "mattermost-attachment"
        extension = extension.lower().strip(" .")
        return f"{stem}.{extension}" if extension else stem
    return cleaned


def _root_from_path(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] != "/":
        return None
    return parts[1]


def _join_path(root: str, filename: str) -> str:
    if root not in APPROVED_ONEQODE_ROOTS:
        raise ValueError("Proposed destination escaped approved OneQode roots")
    return f"/{root}/{filename}"
