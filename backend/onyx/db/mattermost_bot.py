from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from onyx.db.chat import create_chat_session, get_or_create_root_message
from onyx.db.models import ChatSession, MattermostThreadMapping

DEFAULT_MATTERMOST_TEAM_ID = "global"


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
) -> MattermostThreadMapping | None:
    return db_session.scalar(
        select(MattermostThreadMapping).where(
            MattermostThreadMapping.server_id == server_id,
            MattermostThreadMapping.channel_id == channel_id,
            MattermostThreadMapping.root_id == root_id,
        )
    )


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
    return concurrent_mapping


def update_mattermost_thread_parent_message(
    db_session: Session,
    mapping: MattermostThreadMapping,
    parent_message_id: int,
) -> MattermostThreadMapping:
    mapping.parent_message_id = parent_message_id
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
