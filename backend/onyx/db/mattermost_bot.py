from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from onyx.db.chat import create_chat_session, get_or_create_root_message
from onyx.db.models import ChatSession, MattermostThreadMapping
from onyx.onyxbot.mattermost.models import MattermostListenerConfig

DEFAULT_MATTERMOST_TEAM_ID = "global"


class MattermostThreadTombstonedError(RuntimeError):
    """Raised when a deleted Mattermost root must not reclaim its Onyx history."""


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
