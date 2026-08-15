import datetime
import hashlib
from dataclasses import dataclass
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from onyx.db.chat import create_chat_session, get_or_create_root_message
from onyx.db.feedback import create_chat_message_feedback
from onyx.db.models import (
    ChatSession,
    MattermostAttachment,
    MattermostEventState,
    MattermostSlashCommandConfig,
    MattermostThreadMapping,
)
from onyx.onyxbot.mattermost.models import MattermostListenerConfig

DEFAULT_MATTERMOST_TEAM_ID = "global"


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
    if (
        event.mapping_id is None
        or event.mattermost_post_id is None
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
    answer_post_message_ids[event.mattermost_post_id] = event.onyx_assistant_message_id
    mapping.answer_post_message_ids = answer_post_message_ids
    processed_event_ids = list(mapping.processed_event_ids)
    if event.dedupe_key not in processed_event_ids:
        processed_event_ids.append(event.dedupe_key)
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
