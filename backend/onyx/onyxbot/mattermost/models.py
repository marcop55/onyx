"""Typed models for Mattermost bot events."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from onyx.configs.constants import QAFeedbackType

MattermostChannelType = Literal["D", "O", "P"]


class MattermostNormalizedEventType(StrEnum):
    """Supported Mattermost event classes for the adapter."""

    DIRECT_MESSAGE = "direct_message"
    CHANNEL_MENTION = "channel_mention"
    ROOT_ALLOWLISTED_POST = "root_allowlisted_post"
    THREAD_REPLY_FOLLOWUP = "thread_reply_followup"
    POST_UPDATE_RETRY = "post_update_retry"
    REACTION_FEEDBACK = "reaction_feedback"
    POST_DELETE_TOMBSTONE = "post_delete_tombstone"


@dataclass(frozen=True)
class MattermostPost:
    """Mattermost post fields used by the adapter."""

    id: str
    message: str = ""
    root_id: str = ""
    parent_id: str = ""
    user_id: str = ""
    channel_id: str = ""


@dataclass(frozen=True)
class MattermostReaction:
    """Mattermost reaction fields used by feedback handling."""

    user_id: str
    post_id: str
    emoji_name: str
    channel_id: str = ""


@dataclass(frozen=True)
class MattermostEventEnvelope:
    """Mattermost WebSocket event data normalized for decision logic."""

    event: str
    channel_id: str
    channel_type: MattermostChannelType | str
    team_id: str = "global"
    user_id: str = ""
    post: MattermostPost | None = None
    reaction: MattermostReaction | None = None
    event_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class MattermostListenerConfig:
    """Credential-independent routing config for Mattermost events."""

    bot_user_id: str
    bot_mentions: frozenset[str]
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_team_ids: frozenset[str] = frozenset()
    approved_user_ids: frozenset[str] = frozenset()
    root_post_channel_ids: frozenset[str] = frozenset()
    owned_thread_root_ids: frozenset[str] = frozenset()
    owned_answer_post_root_ids: dict[str, str] = field(default_factory=dict)
    bot_user_ids: frozenset[str] = frozenset()
    max_seen_event_ids: int = 10_000
    initial_reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        bot_user_ids = set(self.bot_user_ids)
        bot_user_ids.add(self.bot_user_id)
        object.__setattr__(self, "bot_user_ids", frozenset(bot_user_ids))


@dataclass(frozen=True)
class NormalizedMattermostEvent:
    """Mattermost event ready for Onyx handling."""

    event_type: MattermostNormalizedEventType
    session_key: str
    team_id: str
    channel_id: str
    post_id: str
    root_post_id: str
    user_id: str
    text: str = ""
    raw_event_type: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    feedback_answer_post_id: str | None = None
    feedback_action: QAFeedbackType | None = None
