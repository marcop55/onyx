"""Session helpers for routing Mattermost events to Onyx chats."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from onyx.db.mattermost_bot import get_or_create_mattermost_thread_mapping
from onyx.db.models import MattermostThreadMapping
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)


@dataclass(frozen=True)
class MattermostChatTarget:
    """Resolved Onyx chat target for one Mattermost event."""

    chat_session_id: UUID
    parent_message_id: int
    persona_id: int | None
    mapping: MattermostThreadMapping


def get_mattermost_mapping_root_id(event: NormalizedMattermostEvent) -> str:
    """Return the DB mapping root for a normalized Mattermost event."""

    if event.event_type == MattermostNormalizedEventType.DIRECT_MESSAGE:
        return event.channel_id
    return event.root_post_id


def get_or_create_mattermost_chat_target(
    *,
    db_session: Session,
    event: NormalizedMattermostEvent,
    persona_id: int | None,
    onyx_user_id: UUID | None,
) -> MattermostChatTarget:
    """Create or fetch the Onyx chat that owns the Mattermost conversation."""

    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id=event.team_id,
        channel_id=event.channel_id,
        root_id=get_mattermost_mapping_root_id(event),
        mattermost_user_id=event.user_id,
        persona_id=persona_id,
        onyx_user_id=onyx_user_id,
    )
    if mapping.parent_message_id is None:
        raise RuntimeError("Mattermost thread mapping is missing parent message")

    return MattermostChatTarget(
        chat_session_id=mapping.chat_session_id,
        parent_message_id=mapping.parent_message_id,
        persona_id=mapping.persona_id,
        mapping=mapping,
    )
