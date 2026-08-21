import datetime
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from onyx.db.chat import create_chat_session, get_or_create_root_message
from onyx.db.feedback import create_chat_message_feedback
from onyx.db.models import (
    AllowedAnswerFilters,
    ChannelConfig,
    ChatSession,
    MattermostAttachment,
    MattermostAttachmentPlacementProposal,
    MattermostBot,
    MattermostChannelConfig,
    MattermostEventState,
    MattermostSlashCommandConfig,
    MattermostThreadMapping,
)
from onyx.onyxbot.mattermost.models import (
    MattermostDeliveryTerminalOutcome,
    MattermostListenerConfig,
    MattermostResponseDeliveryMode,
)
from onyx.onyxbot.mattermost.placement import (
    MattermostAttachmentPlacementProposal as MattermostAttachmentPlacementProposalDTO,
)
from onyx.onyxbot.mattermost.placement import (
    MattermostAttachmentPromotionReceipt,
)

DEFAULT_MATTERMOST_TEAM_ID = "global"
MATTERMOST_CONTEXT_POST_ID_PREFIX = "context_post:"


def insert_mattermost_bot(
    db_session: Session,
    *,
    name: str,
    url: str,
    enabled: bool,
    token: str,
    bot_user_id: str,
    bot_username: str,
    health_status: str = "unknown",
    health_error: str | None = None,
) -> MattermostBot:
    mattermost_bot = MattermostBot(
        name=name,
        url=url,
        enabled=enabled,
        token=token,
        bot_user_id=bot_user_id,
        bot_username=bot_username,
        health_status=health_status,
        health_error=health_error,
    )
    db_session.add(mattermost_bot)
    db_session.commit()
    return mattermost_bot


def update_mattermost_bot(
    db_session: Session,
    *,
    mattermost_bot_id: int,
    name: str,
    url: str,
    enabled: bool,
    token: str | None,
    bot_user_id: str,
    bot_username: str,
    health_status: str,
    health_error: str | None,
) -> MattermostBot:
    mattermost_bot = fetch_mattermost_bot(db_session, mattermost_bot_id)
    mattermost_bot.name = name
    mattermost_bot.url = url
    mattermost_bot.enabled = enabled
    if token is not None:
        mattermost_bot.token = token  # ty: ignore[invalid-assignment]
    mattermost_bot.bot_user_id = bot_user_id
    mattermost_bot.bot_username = bot_username
    mattermost_bot.health_status = health_status
    mattermost_bot.health_error = health_error
    db_session.commit()
    return mattermost_bot


def _default_mattermost_channel_config(
    *,
    channel_name: str | None,
    respond_tag_only: bool = True,
    response_style: str = "orka_concise",
    response_type: str = "citations",
    include_source_previews: bool = False,
    answer_filters: list[AllowedAnswerFilters] | None = None,
    standard_answer_category_ids: list[int] | None = None,
    follow_up_tags: list[str] | None = None,
    disabled: bool = False,
) -> ChannelConfig:
    return {
        "channel_name": channel_name,
        "respond_tag_only": respond_tag_only,
        "response_style": response_style,
        "response_type": response_type,
        "include_source_previews": include_source_previews,
        "answer_filters": answer_filters or [],
        "standard_answer_category_ids": standard_answer_category_ids or [],
        "follow_up_tags": follow_up_tags,
        "disabled": disabled,
    }


def insert_mattermost_channel_config(
    db_session: Session,
    *,
    mattermost_bot_id: int,
    channel_id: str | None,
    channel_name: str | None = None,
    persona_id: int | None = None,
    channel_config: ChannelConfig | None = None,
    is_default: bool = False,
    is_ephemeral: bool = False,
    enabled: bool = True,
) -> MattermostChannelConfig:
    if not is_default and not channel_id:
        raise ValueError("Channel ID is required for non-default Mattermost configs.")
    if is_default and channel_id is not None:
        raise ValueError("Default Mattermost config cannot target a channel.")
    if is_default:
        existing_default = db_session.scalar(
            select(MattermostChannelConfig).where(
                MattermostChannelConfig.mattermost_bot_id == mattermost_bot_id,
                MattermostChannelConfig.is_default.is_(True),
            )
        )
        if existing_default is not None:
            raise ValueError("A default config already exists for this Mattermost bot.")
    config = MattermostChannelConfig(
        mattermost_bot_id=mattermost_bot_id,
        channel_id=channel_id,
        channel_name=channel_name,
        persona_id=persona_id,
        channel_config=channel_config
        or _default_mattermost_channel_config(channel_name=channel_name),
        is_default=is_default,
        is_ephemeral=is_ephemeral,
        enabled=enabled,
    )
    db_session.add(config)
    db_session.commit()
    return config


def fetch_mattermost_channel_config(
    db_session: Session,
    mattermost_channel_config_id: int,
) -> MattermostChannelConfig:
    config = db_session.scalar(
        select(MattermostChannelConfig).where(
            MattermostChannelConfig.id == mattermost_channel_config_id
        )
    )
    if config is None:
        raise ValueError(
            f"Unable to find Mattermost channel config with ID {mattermost_channel_config_id}"
        )
    return config


def fetch_mattermost_channel_configs(
    db_session: Session,
    *,
    mattermost_bot_id: int | None = None,
) -> list[MattermostChannelConfig]:
    stmt = select(MattermostChannelConfig)
    if mattermost_bot_id is not None:
        stmt = stmt.where(
            MattermostChannelConfig.mattermost_bot_id == mattermost_bot_id
        )
    return list(db_session.scalars(stmt).all())


def _fetch_enabled_mattermost_bot(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
) -> MattermostBot | None:
    return db_session.scalar(
        select(MattermostBot).where(
            MattermostBot.url == instance_id,
            MattermostBot.bot_user_id == bot_user_id,
            MattermostBot.enabled.is_(True),
        )
    )


def fetch_mattermost_channel_config_for_channel(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
    channel_id: str,
) -> MattermostChannelConfig | None:
    """Return the channel's own enabled config row, never the bot default.

    A None result means the channel has not been opted in to bot answers.
    """
    bot = _fetch_enabled_mattermost_bot(
        db_session, instance_id=instance_id, bot_user_id=bot_user_id
    )
    if bot is None:
        return None
    return db_session.scalar(
        select(MattermostChannelConfig).where(
            MattermostChannelConfig.mattermost_bot_id == bot.id,
            MattermostChannelConfig.channel_id == channel_id,
            MattermostChannelConfig.enabled.is_(True),
        )
    )


def fetch_mattermost_channel_config_for_bot_and_channel(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
    channel_id: str,
) -> MattermostChannelConfig | None:
    bot = _fetch_enabled_mattermost_bot(
        db_session, instance_id=instance_id, bot_user_id=bot_user_id
    )
    if bot is None:
        return None
    channel_config = db_session.scalar(
        select(MattermostChannelConfig).where(
            MattermostChannelConfig.mattermost_bot_id == bot.id,
            MattermostChannelConfig.channel_id == channel_id,
            MattermostChannelConfig.enabled.is_(True),
        )
    )
    if channel_config is not None:
        return channel_config
    return db_session.scalar(
        select(MattermostChannelConfig).where(
            MattermostChannelConfig.mattermost_bot_id == bot.id,
            MattermostChannelConfig.is_default.is_(True),
            MattermostChannelConfig.enabled.is_(True),
        )
    )


def update_mattermost_channel_config(
    db_session: Session,
    *,
    mattermost_channel_config_id: int,
    mattermost_bot_id: int | None = None,
    channel_id: str | None,
    channel_name: str | None = None,
    persona_id: int | None = None,
    channel_config: ChannelConfig | None = None,
    is_ephemeral: bool | None = None,
    enabled: bool | None = None,
) -> MattermostChannelConfig:
    config = fetch_mattermost_channel_config(
        db_session,
        mattermost_channel_config_id=mattermost_channel_config_id,
    )
    if mattermost_bot_id is not None:
        config.mattermost_bot_id = mattermost_bot_id
    if not config.is_default and not channel_id:
        raise ValueError("Channel ID is required for non-default Mattermost configs.")
    if config.is_default and channel_id is not None:
        raise ValueError("Default Mattermost config cannot target a channel.")
    config.channel_id = channel_id
    config.channel_name = channel_name
    config.persona_id = persona_id
    if channel_config is not None:
        config.channel_config = channel_config  # ty: ignore[invalid-assignment]
    if is_ephemeral is not None:
        config.is_ephemeral = is_ephemeral
    if enabled is not None:
        config.enabled = enabled
    db_session.commit()
    return config


def remove_mattermost_channel_config(
    db_session: Session,
    *,
    mattermost_channel_config_id: int,
) -> None:
    config = db_session.scalar(
        select(MattermostChannelConfig).where(
            MattermostChannelConfig.id == mattermost_channel_config_id
        )
    )
    if config is None:
        return
    db_session.delete(config)
    db_session.commit()


def fetch_mattermost_bot(
    db_session: Session,
    mattermost_bot_id: int,
) -> MattermostBot:
    mattermost_bot = db_session.scalar(
        select(MattermostBot).where(MattermostBot.id == mattermost_bot_id)
    )
    if mattermost_bot is None:
        raise ValueError(f"Unable to find Mattermost Bot with ID {mattermost_bot_id}")
    return mattermost_bot


def fetch_mattermost_bots(db_session: Session) -> list[MattermostBot]:
    return list(db_session.scalars(select(MattermostBot)).all())


def fetch_mattermost_bot_by_instance_and_user(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
) -> MattermostBot | None:
    return db_session.scalar(
        select(MattermostBot).where(
            MattermostBot.url == instance_id,
            MattermostBot.bot_user_id == bot_user_id,
            MattermostBot.enabled.is_(True),
        )
    )


def remove_mattermost_bot(db_session: Session, *, mattermost_bot_id: int) -> None:
    mattermost_bot = db_session.scalar(
        select(MattermostBot).where(MattermostBot.id == mattermost_bot_id)
    )
    if mattermost_bot is None:
        return
    db_session.delete(mattermost_bot)
    db_session.commit()


def fetch_mattermost_private_answer_channel_ids(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
) -> frozenset[str]:
    channel_configs = db_session.scalars(
        select(MattermostChannelConfig)
        .join(MattermostBot)
        .where(
            MattermostBot.enabled.is_(True),
            MattermostBot.bot_user_id == bot_user_id,
            MattermostChannelConfig.enabled.is_(True),
            MattermostChannelConfig.is_ephemeral.is_(True),
        )
    ).all()
    return frozenset(
        channel_config.channel_id
        for channel_config in channel_configs
        if channel_config.channel_id is not None
        if _canonical_mattermost_instance_id(channel_config.mattermost_bot.url)
        == instance_id
    )


def _canonical_mattermost_instance_id(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return url.rstrip("/")
    port = parsed.port
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


class MattermostThreadTombstonedError(RuntimeError):
    """Raised when a deleted Mattermost root must not reclaim its Onyx history."""


class MattermostClaimOutcome(str, Enum):
    PROCESS = "process"
    BUSY = "busy"
    COMPLETED = "completed"


@dataclass(frozen=True)
class MattermostEventClaim:
    outcome: MattermostClaimOutcome
    event: MattermostEventState
    claim_owner: UUID | None


def fetch_mattermost_slash_command_config(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
) -> MattermostSlashCommandConfig | None:
    return db_session.scalar(
        select(MattermostSlashCommandConfig).where(
            MattermostSlashCommandConfig.instance_id == instance_id,
            MattermostSlashCommandConfig.bot_user_id == bot_user_id,
        )
    )


def upsert_mattermost_slash_command_config(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
    token: str,
    enabled: bool,
) -> MattermostSlashCommandConfig:
    config = fetch_mattermost_slash_command_config(
        db_session,
        instance_id=instance_id,
        bot_user_id=bot_user_id,
    )
    if config is None:
        config = MattermostSlashCommandConfig(
            instance_id=instance_id,
            bot_user_id=bot_user_id,
            token=token,
            enabled=enabled,
        )
        db_session.add(config)
    else:
        config.token = token  # ty: ignore[invalid-assignment]
        config.enabled = enabled
    db_session.commit()
    return config


def get_or_bootstrap_mattermost_slash_command_config(
    db_session: Session,
    *,
    instance_id: str,
    bot_user_id: str,
    bootstrap_token: str | None,
) -> MattermostSlashCommandConfig | None:
    config = fetch_mattermost_slash_command_config(
        db_session,
        instance_id=instance_id,
        bot_user_id=bot_user_id,
    )
    if config is not None or bootstrap_token is None:
        return config
    return upsert_mattermost_slash_command_config(
        db_session,
        instance_id=instance_id,
        bot_user_id=bot_user_id,
        token=bootstrap_token,
        enabled=True,
    )


def claim_durable_mattermost_event(
    db_session: Session,
    *,
    instance_id: str,
    channel_id: str,
    dedupe_key: str,
    event_type: str,
    mapping_id: int | None,
    source_post_id: str,
    root_post_id: str | None = None,
    source_user_id: str | None = None,
    source_username: str | None = None,
    source_display_name: str | None = None,
    source_create_at: int | None = None,
    source_update_at: int | None = None,
    source_delete_at: int | None = None,
    now: datetime.datetime | None = None,
    lease_seconds: int = 300,
) -> MattermostEventClaim:
    if not dedupe_key:
        raise ValueError("Mattermost events require a stable dedupe key")
    claim_time = now or datetime.datetime.now(datetime.timezone.utc)
    owner = uuid4()
    lease_expires_at = claim_time + datetime.timedelta(seconds=lease_seconds)
    event_hash = hashlib.sha256(
        f"{instance_id}:{channel_id}:{dedupe_key}".encode()
    ).hexdigest()
    pending_post_id = event_hash[:26]
    inserted_id = db_session.scalar(
        postgresql.insert(MattermostEventState)
        .values(
            instance_id=instance_id,
            channel_id=channel_id,
            dedupe_key=dedupe_key,
            event_type=event_type,
            mapping_id=mapping_id,
            source_post_id=source_post_id,
            root_post_id=root_post_id,
            source_user_id=source_user_id,
            source_username=source_username,
            source_display_name=source_display_name,
            source_create_at=source_create_at,
            source_update_at=source_update_at,
            source_delete_at=source_delete_at,
            state="claimed",
            claim_owner=owner,
            lease_expires_at=lease_expires_at,
            mattermost_pending_post_id=pending_post_id,
        )
        .on_conflict_do_nothing(
            index_elements=["instance_id", "channel_id", "dedupe_key"]
        )
        .returning(MattermostEventState.id)
    )
    if inserted_id is not None:
        event = db_session.get(MattermostEventState, inserted_id)
        assert event is not None
        db_session.commit()
        return MattermostEventClaim(MattermostClaimOutcome.PROCESS, event, owner)

    event = db_session.scalar(
        select(MattermostEventState)
        .where(
            MattermostEventState.instance_id == instance_id,
            MattermostEventState.channel_id == channel_id,
            MattermostEventState.dedupe_key == dedupe_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert event is not None
    if event.state == "completed":
        db_session.commit()
        return MattermostEventClaim(MattermostClaimOutcome.COMPLETED, event, None)
    if event.lease_expires_at is not None and event.lease_expires_at > claim_time:
        db_session.commit()
        return MattermostEventClaim(MattermostClaimOutcome.BUSY, event, None)
    event.claim_owner = owner
    event.lease_expires_at = lease_expires_at
    db_session.commit()
    return MattermostEventClaim(MattermostClaimOutcome.PROCESS, event, owner)


def record_mattermost_attachment(
    db_session: Session,
    *,
    event_id: int,
    mattermost_file_id: str,
    source_post_id: str,
    uploader_user_id: str,
    filename: str,
    mime_type: str,
    channel_id: str,
    root_post_id: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    create_at: int | None = None,
    file_store_id: str | None = None,
    user_file_id: UUID | None = None,
) -> MattermostAttachment:
    inserted_id = db_session.scalar(
        postgresql.insert(MattermostAttachment)
        .values(
            event_id=event_id,
            mattermost_file_id=mattermost_file_id,
            source_post_id=source_post_id,
            uploader_user_id=uploader_user_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            channel_id=channel_id,
            root_post_id=root_post_id,
            create_at=create_at,
            file_store_id=file_store_id,
            user_file_id=user_file_id,
        )
        .on_conflict_do_update(
            constraint="uq_mattermost_attachment_event_file",
            set_={
                "source_post_id": source_post_id,
                "uploader_user_id": uploader_user_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "channel_id": channel_id,
                "root_post_id": root_post_id,
                "create_at": create_at,
                "file_store_id": file_store_id,
                "user_file_id": user_file_id,
            },
        )
        .returning(MattermostAttachment.id)
    )
    assert inserted_id is not None
    attachment = db_session.get(MattermostAttachment, inserted_id)
    assert attachment is not None
    db_session.commit()
    return attachment


def record_mattermost_attachment_placement_proposal(
    db_session: Session,
    *,
    proposal: MattermostAttachmentPlacementProposalDTO,
) -> MattermostAttachmentPlacementProposal:
    proposal_identity = mattermost_attachment_placement_proposal_identity(proposal)
    duplicate_conflict_evidence = {
        "same_name_paths": proposal.duplicate_conflict_evidence.same_name_paths,
        "byte_identical_paths": proposal.duplicate_conflict_evidence.byte_identical_paths,
        "same_name_revisions": proposal.duplicate_conflict_evidence.same_name_revisions,
        "byte_identical_revisions": proposal.duplicate_conflict_evidence.byte_identical_revisions,
    }
    inserted_id = db_session.scalar(
        postgresql.insert(MattermostAttachmentPlacementProposal)
        .values(
            attachment_id=proposal.identity.attachment_id,
            proposal_identity=proposal_identity,
            mattermost_file_id=proposal.identity.mattermost_file_id,
            source_post_id=proposal.identity.source_post_id,
            uploader_user_id=proposal.identity.uploader_user_id,
            channel_id=proposal.identity.channel_id,
            root_post_id=proposal.identity.root_post_id,
            sha256=proposal.identity.sha256,
            library_id=proposal.library_id,
            proposed_root=proposal.proposed_root,
            proposed_path=proposal.proposed_path,
            normalized_filename=proposal.normalized_filename,
            rationale=proposal.rationale,
            confidence=proposal.confidence,
            should_remain_temporary=proposal.should_remain_temporary,
            hierarchy_root_revision=proposal.hierarchy_root_revision,
            duplicate_conflict_evidence=duplicate_conflict_evidence,
        )
        .on_conflict_do_nothing(
            constraint="uq_mattermost_attachment_placement_proposal_identity"
        )
        .returning(MattermostAttachmentPlacementProposal.id)
    )
    if inserted_id is None:
        existing = db_session.scalar(
            select(MattermostAttachmentPlacementProposal).where(
                MattermostAttachmentPlacementProposal.proposal_identity
                == proposal_identity
            )
        )
        assert existing is not None
        db_session.commit()
        return existing
    placement = db_session.get(MattermostAttachmentPlacementProposal, inserted_id)
    assert placement is not None
    db_session.commit()
    return placement


def record_mattermost_attachment_promotion_receipt(
    db_session: Session,
    *,
    proposal_id: int,
    receipt: MattermostAttachmentPromotionReceipt,
) -> MattermostAttachmentPlacementProposal:
    proposal = db_session.get(MattermostAttachmentPlacementProposal, proposal_id)
    if proposal is None:
        raise ValueError("Mattermost attachment placement proposal not found")
    proposal.audit_evidence = receipt.audit_evidence
    proposal.rollback_data = receipt.rollback_data
    proposal.ingestion_freshness_proof = receipt.ingestion_freshness_proof
    proposal.readback_file_id = receipt.readback_file_id
    proposal.readback_revision = receipt.readback_revision
    db_session.commit()
    return proposal


def claim_mattermost_attachment_placement_promotion(
    db_session: Session,
    *,
    proposal_identity: str,
    confirmer_user_id: str,
    now: datetime.datetime | None = None,
) -> MattermostAttachmentPlacementProposal | None:
    """Fence one signed attachment-promotion confirmation before transport."""

    if not proposal_identity or not confirmer_user_id:
        raise ValueError("Mattermost attachment promotion identity is required")
    claim_time = now or datetime.datetime.now(datetime.timezone.utc)
    proposal = db_session.scalar(
        select(MattermostAttachmentPlacementProposal)
        .where(
            MattermostAttachmentPlacementProposal.proposal_identity == proposal_identity
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        db_session.commit()
        return None
    if (
        proposal.promotion_claimed_at is not None
        or proposal.readback_file_id is not None
    ):
        db_session.commit()
        return None
    proposal.promotion_confirmer_user_id = confirmer_user_id
    proposal.promotion_claimed_at = claim_time
    db_session.commit()
    return proposal


def mattermost_attachment_placement_proposal_identity(
    proposal: MattermostAttachmentPlacementProposalDTO,
) -> str:
    payload = {
        "attachment_id": proposal.identity.attachment_id,
        "mattermost_file_id": proposal.identity.mattermost_file_id,
        "source_post_id": proposal.identity.source_post_id,
        "uploader_user_id": proposal.identity.uploader_user_id,
        "channel_id": proposal.identity.channel_id,
        "root_post_id": proposal.identity.root_post_id,
        "sha256": proposal.identity.sha256,
        "library_id": proposal.library_id,
        "proposed_path": proposal.proposed_path,
        "hierarchy_root_revision": proposal.hierarchy_root_revision,
        "duplicate_conflict_evidence": {
            "same_name_paths": proposal.duplicate_conflict_evidence.same_name_paths,
            "byte_identical_paths": proposal.duplicate_conflict_evidence.byte_identical_paths,
            "same_name_revisions": proposal.duplicate_conflict_evidence.same_name_revisions,
            "byte_identical_revisions": proposal.duplicate_conflict_evidence.byte_identical_revisions,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _checkpoint_mattermost_event(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    values: dict[str, object],
) -> bool:
    updated_id = db_session.scalar(
        update(MattermostEventState)
        .where(
            MattermostEventState.id == event_id,
            MattermostEventState.claim_owner == claim_owner,
            MattermostEventState.state != "completed",
        )
        .values(**values)
        .returning(MattermostEventState.id)
    )
    db_session.commit()
    return updated_id is not None


def checkpoint_mattermost_post_attempt(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
) -> bool:
    """Persist the no-retry boundary before the first external POST."""
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={"state": "post_create_attempted"},
    )


def checkpoint_mattermost_post(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    post_id: str,
) -> bool:
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={"mattermost_post_id": post_id, "state": "post_created"},
    )


def checkpoint_mattermost_delivery_mode(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    delivery_mode: MattermostResponseDeliveryMode,
) -> bool:
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={"delivery_mode": delivery_mode.value},
    )


def checkpoint_mattermost_terminal_outcome(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    terminal_outcome: MattermostDeliveryTerminalOutcome,
    post_id: str | None = None,
) -> bool:
    values: dict[str, object] = {"terminal_outcome": terminal_outcome.value}
    if post_id is not None:
        values["mattermost_post_id"] = post_id
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values=values,
    )


def checkpoint_mattermost_turn(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    user_message_id: int,
    assistant_message_id: int,
) -> bool:
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={
            "onyx_user_message_id": user_message_id,
            "onyx_assistant_message_id": assistant_message_id,
            "state": "turn_created",
        },
    )


def checkpoint_mattermost_rendered_message(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    rendered_message: str,
) -> bool:
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={"rendered_message": rendered_message},
    )


def renew_mattermost_event_lease(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    now: datetime.datetime | None = None,
    lease_seconds: int = 300,
) -> bool:
    renewal_time = now or datetime.datetime.now(datetime.timezone.utc)
    return _checkpoint_mattermost_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
        values={
            "lease_expires_at": renewal_time + datetime.timedelta(seconds=lease_seconds)
        },
    )


def complete_mattermost_answer_event(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    loaded_context_post_ids: frozenset[str] = frozenset(),
    answer_post_ids: tuple[str, ...] | None = None,
) -> bool:
    event = db_session.scalar(
        select(MattermostEventState)
        .where(
            MattermostEventState.id == event_id,
            MattermostEventState.claim_owner == claim_owner,
            MattermostEventState.state != "completed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        db_session.rollback()
        return False
    delivered_ephemeral = (
        event.delivery_mode == MattermostResponseDeliveryMode.EPHEMERAL.value
        and event.terminal_outcome == MattermostDeliveryTerminalOutcome.DELIVERED.value
    )
    if (
        event.mapping_id is None
        or (event.mattermost_post_id is None and not delivered_ephemeral)
        or event.onyx_assistant_message_id is None
        or event.rendered_message is None
    ):
        db_session.rollback()
        return False
    mapping = db_session.get(
        MattermostThreadMapping, event.mapping_id, with_for_update=True
    )
    if mapping is None or not mapping.is_active:
        db_session.rollback()
        return False

    mapping.parent_message_id = event.onyx_assistant_message_id
    answer_post_message_ids = dict(mapping.answer_post_message_ids)
    post_ids = answer_post_ids or (
        (event.mattermost_post_id,) if event.mattermost_post_id is not None else ()
    )
    for post_id in post_ids:
        answer_post_message_ids[post_id] = event.onyx_assistant_message_id
    mapping.answer_post_message_ids = answer_post_message_ids
    processed_event_ids = list(mapping.processed_event_ids)
    if event.dedupe_key not in processed_event_ids:
        processed_event_ids.append(event.dedupe_key)
    for post_id in sorted(loaded_context_post_ids):
        context_event_id = f"{MATTERMOST_CONTEXT_POST_ID_PREFIX}{post_id}"
        if context_event_id not in processed_event_ids:
            processed_event_ids.append(context_event_id)
    mapping.processed_event_ids = processed_event_ids[-10_000:]
    event.state = "completed"
    event.claim_owner = None
    event.lease_expires_at = None
    db_session.commit()
    return True


def complete_mattermost_feedback_event(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    chat_message_id: int,
    is_positive: bool,
    feedback_text: str,
) -> bool:
    """Insert feedback and complete its durable event in one fenced transaction."""
    event = db_session.scalar(
        select(MattermostEventState)
        .where(
            MattermostEventState.id == event_id,
            MattermostEventState.claim_owner == claim_owner,
            MattermostEventState.state != "completed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        db_session.rollback()
        return False
    feedback = create_chat_message_feedback(
        is_positive=is_positive,
        feedback_text=feedback_text,
        chat_message_id=chat_message_id,
        user_id=None,
        db_session=db_session,
        commit=False,
    )
    db_session.flush()
    event.feedback_id = feedback.id
    event.state = "completed"
    event.claim_owner = None
    event.lease_expires_at = None
    db_session.commit()
    return True


def complete_mattermost_interactive_feedback_event(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
    chat_message_id: int,
    is_positive: bool | None,
    required_followup: bool | None,
    feedback_text: str,
) -> bool:
    """Insert interactive answer feedback and complete its durable event."""
    event = db_session.scalar(
        select(MattermostEventState)
        .where(
            MattermostEventState.id == event_id,
            MattermostEventState.claim_owner == claim_owner,
            MattermostEventState.state != "completed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        db_session.rollback()
        return False
    feedback = create_chat_message_feedback(
        is_positive=is_positive,
        feedback_text=feedback_text,
        chat_message_id=chat_message_id,
        user_id=None,
        db_session=db_session,
        required_followup=required_followup,
        commit=False,
    )
    db_session.flush()
    event.feedback_id = feedback.id
    event.state = "completed"
    event.claim_owner = None
    event.lease_expires_at = None
    db_session.commit()
    return True


def complete_mattermost_control_event(
    db_session: Session,
    *,
    event_id: int,
    claim_owner: UUID,
) -> bool:
    """Complete an auditable event that does not create an Onyx chat turn."""
    event = db_session.scalar(
        select(MattermostEventState)
        .where(
            MattermostEventState.id == event_id,
            MattermostEventState.claim_owner == claim_owner,
            MattermostEventState.state != "completed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        db_session.rollback()
        return False
    if event.mapping_id is not None:
        mapping = db_session.get(
            MattermostThreadMapping, event.mapping_id, with_for_update=True
        )
        if mapping is not None:
            processed_event_ids = list(mapping.processed_event_ids)
            if event.dedupe_key not in processed_event_ids:
                processed_event_ids.append(event.dedupe_key)
                mapping.processed_event_ids = processed_event_ids[-10_000:]
    event.state = "completed"
    event.claim_owner = None
    event.lease_expires_at = None
    db_session.commit()
    return True


def get_mattermost_session_key(
    server_id: str,
    channel_id: str,
    root_id: str,
) -> str:
    return f"mattermost:channel:{server_id}:{channel_id}:{root_id}"


def get_mattermost_thread_mapping(
    db_session: Session,
    server_id: str,
    channel_id: str,
    root_id: str,
    *,
    for_update: bool = False,
) -> MattermostThreadMapping | None:
    statement = select(MattermostThreadMapping).where(
        MattermostThreadMapping.server_id == server_id,
        MattermostThreadMapping.channel_id == channel_id,
        MattermostThreadMapping.root_id == root_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return db_session.scalar(statement)


def get_mattermost_thread_mapping_by_chat_session_id(
    db_session: Session,
    chat_session_id: UUID,
) -> MattermostThreadMapping | None:
    return db_session.scalar(
        select(MattermostThreadMapping).where(
            MattermostThreadMapping.chat_session_id == chat_session_id
        )
    )


def get_loaded_mattermost_context_post_ids(
    db_session: Session,
    mapping_id: int,
) -> frozenset[str]:
    """Return source posts already represented in an Onyx turn for this mapping."""

    handled_turn_post_ids = {
        post_id
        for post_id in db_session.scalars(
            select(MattermostEventState.source_post_id).where(
                MattermostEventState.mapping_id == mapping_id,
                MattermostEventState.onyx_user_message_id.is_not(None),
            )
        ).all()
        if post_id
    }
    mapping = db_session.get(MattermostThreadMapping, mapping_id)
    if mapping is None:
        return frozenset(handled_turn_post_ids)
    loaded_context_post_ids = {
        event_id.removeprefix(MATTERMOST_CONTEXT_POST_ID_PREFIX)
        for event_id in mapping.processed_event_ids
        if event_id.startswith(MATTERMOST_CONTEXT_POST_ID_PREFIX)
    }
    return frozenset(handled_turn_post_ids | loaded_context_post_ids)


def get_or_create_mattermost_thread_mapping(
    db_session: Session,
    server_id: str,
    channel_id: str,
    root_id: str,
    mattermost_user_id: str,
    persona_id: int | None,
    onyx_user_id: UUID | None,
) -> MattermostThreadMapping:
    existing_mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=server_id,
        channel_id=channel_id,
        root_id=root_id,
    )
    if existing_mapping is not None:
        if not existing_mapping.is_active:
            raise MattermostThreadTombstonedError(
                "Deleted Mattermost thread cannot reclaim its Onyx session"
            )
        return existing_mapping

    chat_session = create_chat_session(
        db_session=db_session,
        description=get_mattermost_session_key(
            server_id=server_id,
            channel_id=channel_id,
            root_id=root_id,
        ),
        user_id=onyx_user_id,
        persona_id=persona_id,
        onyxbot_flow=True,
    )
    root_message = get_or_create_root_message(
        chat_session_id=chat_session.id,
        db_session=db_session,
    )

    insert_stmt = (
        postgresql.insert(MattermostThreadMapping)
        .values(
            server_id=server_id,
            channel_id=channel_id,
            root_id=root_id,
            mattermost_user_id=mattermost_user_id,
            persona_id=persona_id,
            chat_session_id=chat_session.id,
            parent_message_id=root_message.id,
        )
        .on_conflict_do_nothing(
            constraint="uq_mattermost_thread_mapping_thread",
        )
        .returning(MattermostThreadMapping)
    )
    mapping = db_session.execute(insert_stmt).scalar_one_or_none()
    if mapping is not None:
        db_session.commit()
        return mapping

    db_session.delete(chat_session)
    db_session.commit()

    concurrent_mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=server_id,
        channel_id=channel_id,
        root_id=root_id,
    )
    if concurrent_mapping is None:
        raise RuntimeError("Failed to create Mattermost thread mapping")
    if not concurrent_mapping.is_active:
        raise MattermostThreadTombstonedError(
            "Deleted Mattermost thread cannot reclaim its Onyx session"
        )
    return concurrent_mapping


def update_mattermost_thread_parent_message(
    db_session: Session,
    mapping: MattermostThreadMapping,
    parent_message_id: int,
) -> MattermostThreadMapping:
    mapping.parent_message_id = parent_message_id
    db_session.commit()
    return mapping


def claim_mattermost_event(
    db_session: Session,
    server_id: str,
    channel_id: str,
    root_id: str,
    dedupe_key: str,
    *,
    max_processed_event_ids: int = 10_000,
) -> MattermostThreadMapping | None:
    """Lock a thread and stage one replay key in the caller's transaction."""

    mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=server_id,
        channel_id=channel_id,
        root_id=root_id,
        for_update=True,
    )
    if mapping is None or not mapping.is_active:
        return None
    processed_event_ids = list(mapping.processed_event_ids)
    if not dedupe_key or dedupe_key in processed_event_ids:
        return None
    processed_event_ids.append(dedupe_key)
    mapping.processed_event_ids = processed_event_ids[-max_processed_event_ids:]
    return mapping


def record_mattermost_event_state(
    db_session: Session,
    mapping: MattermostThreadMapping,
    dedupe_key: str,
    *,
    answer_post_id: str | None = None,
    message_id: int | None = None,
    max_processed_event_ids: int = 10_000,
) -> MattermostThreadMapping:
    """Persist replay protection and answer feedback ownership for one thread."""

    processed_event_ids = list(mapping.processed_event_ids)
    if dedupe_key and dedupe_key not in processed_event_ids:
        processed_event_ids.append(dedupe_key)
        mapping.processed_event_ids = processed_event_ids[-max_processed_event_ids:]

    if answer_post_id and message_id is not None:
        answer_post_message_ids = dict(mapping.answer_post_message_ids)
        answer_post_message_ids[answer_post_id] = message_id
        mapping.answer_post_message_ids = answer_post_message_ids

    db_session.commit()
    return mapping


def hydrate_mattermost_listener_config(
    db_session: Session,
    config: MattermostListenerConfig,
) -> None:
    """Restore durable adapter ownership and replay state into runtime config."""

    mappings = db_session.scalars(
        select(MattermostThreadMapping).order_by(MattermostThreadMapping.time_updated)
    ).all()
    for mapping in mappings:
        if not mapping.is_active:
            config.tombstoned_thread_root_ids.add(mapping.root_id)
            continue
        config.owned_thread_root_ids.add(mapping.root_id)
        for dedupe_key in mapping.processed_event_ids:
            if dedupe_key not in config.processed_event_ids:
                config.processed_event_ids.append(dedupe_key)
        for answer_post_id, message_id in mapping.answer_post_message_ids.items():
            config.owned_answer_post_root_ids[answer_post_id] = mapping.root_id
            config.owned_answer_post_message_ids[answer_post_id] = message_id


def tombstone_mattermost_thread_mapping(
    db_session: Session,
    server_id: str,
    channel_id: str,
    root_id: str,
) -> MattermostThreadMapping | None:
    """Relinquish adapter ownership while preserving the linked Onyx history."""

    mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=server_id,
        channel_id=channel_id,
        root_id=root_id,
    )
    if mapping is None:
        return None
    mapping.is_active = False
    db_session.commit()
    return mapping


def get_mattermost_chat_session_for_thread(
    db_session: Session,
    server_id: str,
    channel_id: str,
    root_id: str,
) -> ChatSession | None:
    mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=server_id,
        channel_id=channel_id,
        root_id=root_id,
    )
    if mapping is None:
        return None
    return mapping.chat_session
