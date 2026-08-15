from __future__ import annotations

from uuid import UUID

import pytest

from onyx.db.mattermost_bot import mattermost_attachment_placement_proposal_identity
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.mutations import (
    MattermostMutationContext,
    MattermostMutationPermissionError,
    SeafileActionRequest,
)
from onyx.onyxbot.mattermost.placement import (
    APPROVED_ONEQODE_ROOTS,
    MattermostAttachmentPlacementInput,
    MattermostAttachmentPromotionError,
    MattermostPromotionPreflightEvidence,
    SeafileHierarchyEvidence,
    SeafilePlacementFileEvidence,
    promote_mattermost_attachment_proposal,
    propose_mattermost_attachment_placement,
)


class FakeMattermost:
    def __init__(self, roles: str) -> None:
        self.roles = roles
        self.calls: list[str] = []

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        self.calls.append(user_id)
        return MattermostUserInfo(
            id=user_id,
            username="reiss" if "system_admin" in self.roles else "ordinary",
            display_name="Untrusted Name",
            roles=self.roles,
        )


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[MattermostMutationContext, SeafileActionRequest]] = []

    def mutate(
        self,
        context: MattermostMutationContext,
        request: SeafileActionRequest,
    ) -> dict[str, str]:
        self.calls.append((context, request))
        return {"mutation_id": "mut-1"}


def _attachment_input(
    *,
    content_text: str | None = "quote for Project Apollo implementation",
    filename: str = " Apollo Quote FINAL!!.pdf ",
    sha256: str = "a" * 64,
) -> MattermostAttachmentPlacementInput:
    return MattermostAttachmentPlacementInput(
        attachment_id=42,
        mattermost_file_id="file-1",
        source_post_id="post-1",
        uploader_user_id="user-1",
        channel_id="channel-1",
        root_post_id="root-1",
        filename=filename,
        mime_type="application/pdf",
        size_bytes=1234,
        sha256=sha256,
        file_store_id="mattermost/instance/channel-1/file-1",
        user_file_id=UUID("00000000-0000-0000-0000-000000000123"),
        content_text=content_text,
    )


def _hierarchy(
    *files: SeafilePlacementFileEvidence,
) -> SeafileHierarchyEvidence:
    return SeafileHierarchyEvidence(
        library_id="oneqode-lib",
        library_name="OneQode",
        root_path="/",
        root_revision="root-rev-1",
        approved_roots=APPROVED_ONEQODE_ROOTS,
        files=files,
    )


def _event() -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="root-1",
        user_id="user-1",
        text="promote attachment",
        raw_event_type="posted",
        file_ids=("file-1",),
        dedupe_key="event_id:post-1",
    )


def _proposal():
    return propose_mattermost_attachment_placement(
        attachment=_attachment_input(),
        hierarchy=_hierarchy(),
        request_is_system_admin=False,
    )


def test_normal_member_gets_sourced_recommendation_without_seafile_transport() -> None:
    seafile_transport_calls: list[str] = []

    proposal = propose_mattermost_attachment_placement(
        attachment=_attachment_input(),
        hierarchy=_hierarchy(
            SeafilePlacementFileEvidence(
                path="/Projects/Apollo/Existing Quote.pdf",
                file_id="sf-existing-1",
                revision_id="rev-existing-1",
                size_bytes=999,
                sha256="b" * 64,
            )
        ),
        request_is_system_admin=False,
        seafile_transport=lambda *_args, **_kwargs: seafile_transport_calls.append(
            "called"
        ),
    )

    assert seafile_transport_calls == []
    assert proposal.should_remain_temporary is False
    assert proposal.proposed_root == "Projects"
    assert proposal.proposed_path.startswith("/Projects/")
    assert proposal.normalized_filename == "Apollo Quote FINAL.pdf"
    assert proposal.confidence > 0.5
    assert proposal.hierarchy_root_revision == "root-rev-1"
    assert proposal.identity.mattermost_file_id == "file-1"
    assert proposal.identity.source_post_id == "post-1"
    assert proposal.identity.uploader_user_id == "user-1"
    assert proposal.identity.sha256 == "a" * 64
    assert proposal.duplicate_conflict_evidence.same_name_paths == []
    assert "Project Apollo" in proposal.rationale


def test_low_confidence_unparseable_attachment_stays_temporary() -> None:
    proposal = propose_mattermost_attachment_placement(
        attachment=_attachment_input(content_text=None, filename="scan.bin"),
        hierarchy=_hierarchy(),
        request_is_system_admin=False,
    )

    assert proposal.should_remain_temporary is True
    assert proposal.proposed_root == "Inbox"
    assert proposal.confidence == 0
    assert "could not parse" in proposal.rationale


def test_same_name_and_byte_identical_evidence_are_distinguished() -> None:
    proposal = propose_mattermost_attachment_placement(
        attachment=_attachment_input(filename="brief.txt", sha256="c" * 64),
        hierarchy=_hierarchy(
            SeafilePlacementFileEvidence(
                path="/Projects/Apollo/brief.txt",
                file_id="sf-same-name",
                revision_id="rev-name",
                size_bytes=100,
                sha256="d" * 64,
            ),
            SeafilePlacementFileEvidence(
                path="/Reference/renamed-brief.txt",
                file_id="sf-same-bytes",
                revision_id="rev-bytes",
                size_bytes=1234,
                sha256="c" * 64,
            ),
        ),
        request_is_system_admin=False,
    )

    assert proposal.duplicate_conflict_evidence.same_name_paths == [
        "/Projects/Apollo/brief.txt"
    ]
    assert proposal.duplicate_conflict_evidence.byte_identical_paths == [
        "/Reference/renamed-brief.txt"
    ]
    assert proposal.should_remain_temporary is True
    assert "unresolved duplicate" in proposal.rationale


def test_proposal_identity_is_stable_for_replay_and_changes_with_hierarchy() -> None:
    proposal = _proposal()
    replay = _proposal()
    changed_hierarchy = propose_mattermost_attachment_placement(
        attachment=_attachment_input(),
        hierarchy=SeafileHierarchyEvidence(
            library_id="oneqode-lib",
            library_name="OneQode",
            root_path="/",
            root_revision="root-rev-2",
            approved_roots=APPROVED_ONEQODE_ROOTS,
            files=(),
        ),
        request_is_system_admin=False,
    )

    assert mattermost_attachment_placement_proposal_identity(proposal) == (
        mattermost_attachment_placement_proposal_identity(replay)
    )
    assert mattermost_attachment_placement_proposal_identity(proposal) != (
        mattermost_attachment_placement_proposal_identity(changed_hierarchy)
    )


@pytest.mark.asyncio
async def test_non_admin_promotion_is_denied_with_zero_seafile_transport() -> None:
    gateway = RecordingGateway()

    with pytest.raises(MattermostMutationPermissionError):
        await promote_mattermost_attachment_proposal(
            event=_event(),
            attachment=_attachment_input(),
            proposal=_proposal(),
            preflight=MattermostPromotionPreflightEvidence(
                hierarchy_root_revision="root-rev-1",
                destination_revision=None,
                attachment_sha256="a" * 64,
                conflict_paths=(),
            ),
            mattermost=FakeMattermost("system_user"),
            gateway=gateway,
            read_back=lambda: {
                "path": "/Projects/Apollo Quote FINAL.pdf",
                "file_id": "sf-1",
                "revision_id": "rev-2",
                "sha256": "a" * 64,
            },
            freshness_check=lambda: "fresh:index-attempt-1",
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_stale_confirmation_rechecks_checksum_before_gateway() -> None:
    gateway = RecordingGateway()

    with pytest.raises(MattermostAttachmentPromotionError, match="checksum"):
        await promote_mattermost_attachment_proposal(
            event=_event(),
            attachment=_attachment_input(),
            proposal=_proposal(),
            preflight=MattermostPromotionPreflightEvidence(
                hierarchy_root_revision="root-rev-1",
                destination_revision=None,
                attachment_sha256="b" * 64,
                conflict_paths=(),
            ),
            mattermost=FakeMattermost("system_admin"),
            gateway=gateway,
            read_back=lambda: {},
            freshness_check=lambda: "fresh:index-attempt-1",
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_system_admin_promotion_requires_readback_audit_rollback_and_freshness() -> (
    None
):
    gateway = RecordingGateway()

    receipt = await promote_mattermost_attachment_proposal(
        event=_event(),
        attachment=_attachment_input(),
        proposal=_proposal(),
        preflight=MattermostPromotionPreflightEvidence(
            hierarchy_root_revision="root-rev-1",
            destination_revision=None,
            attachment_sha256="a" * 64,
            conflict_paths=(),
        ),
        mattermost=FakeMattermost("system_user system_admin"),
        gateway=gateway,
        read_back=lambda: {
            "path": "/Projects/Apollo Quote FINAL.pdf",
            "file_id": "sf-1",
            "revision_id": "rev-2",
            "sha256": "a" * 64,
        },
        freshness_check=lambda: "fresh:index-attempt-1",
    )

    assert len(gateway.calls) == 1
    _, request = gateway.calls[0]
    assert request.confirmed is True
    assert request.path == "/Projects/Apollo Quote FINAL.pdf"
    assert request.scope_prefix == "/Projects"
    assert request.content == "mattermost/instance/channel-1/file-1"
    assert receipt.readback_file_id == "sf-1"
    assert receipt.readback_revision == "rev-2"
    assert receipt.audit_evidence["mattermost_file_id"] == "file-1"
    assert receipt.rollback_data == {
        "action": "delete_created_file",
        "path": "/Projects/Apollo Quote FINAL.pdf",
        "file_id": "sf-1",
        "revision_id": "rev-2",
    }
    assert receipt.ingestion_freshness_proof == "fresh:index-attempt-1"
