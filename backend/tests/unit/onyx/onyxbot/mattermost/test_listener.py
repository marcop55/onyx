"""Unit tests for Mattermost listener normalization."""

from collections.abc import AsyncIterator
from typing import cast

import pytest

from onyx.onyxbot.mattermost.client import mattermost_event_from_payload
from onyx.onyxbot.mattermost.listener import (
    MattermostEventListener,
    MattermostEventNormalizer,
)
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
)

_APPROVED_USER_ID = "user_1"
_BOT_USER_ID = "bot_user_1"


def _config(**overrides: object) -> MattermostListenerConfig:
    config = MattermostListenerConfig(
        bot_user_id=_BOT_USER_ID,
        bot_mentions=frozenset({"@onyx", "@bot_user_1"}),
        allowed_channel_ids=frozenset({"channel_1", "channel_2"}),
        allowed_team_ids=frozenset({"team_1"}),
        approved_user_ids=frozenset({_APPROVED_USER_ID}),
        root_post_channel_ids=frozenset({"channel_2"}),
        owned_thread_root_ids=frozenset({"post_root_1"}),
        initial_reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=3.0,
    )

    if not overrides:
        return config

    return MattermostListenerConfig(
        bot_user_id=str(overrides.get("bot_user_id", config.bot_user_id)),
        bot_mentions=_frozenset_override(
            overrides, "bot_mentions", config.bot_mentions
        ),
        allowed_channel_ids=_frozenset_override(
            overrides, "allowed_channel_ids", config.allowed_channel_ids
        ),
        allowed_team_ids=_frozenset_override(
            overrides, "allowed_team_ids", config.allowed_team_ids
        ),
        approved_user_ids=_frozenset_override(
            overrides, "approved_user_ids", config.approved_user_ids
        ),
        root_post_channel_ids=_frozenset_override(
            overrides, "root_post_channel_ids", config.root_post_channel_ids
        ),
        owned_thread_root_ids=_frozenset_override(
            overrides, "owned_thread_root_ids", config.owned_thread_root_ids
        ),
        initial_reconnect_backoff_seconds=_float_override(
            overrides,
            "initial_reconnect_backoff_seconds",
            config.initial_reconnect_backoff_seconds,
        ),
        max_reconnect_backoff_seconds=_float_override(
            overrides,
            "max_reconnect_backoff_seconds",
            config.max_reconnect_backoff_seconds,
        ),
    )


def _frozenset_override(
    overrides: dict[str, object],
    key: str,
    default: frozenset[str],
) -> frozenset[str]:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, frozenset):
        return cast(frozenset[str], value)
    raise TypeError(f"{key} must be a frozenset")


def _float_override(overrides: dict[str, object], key: str, default: float) -> float:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"{key} must be numeric")


def _posted_event(
    *,
    post_id: str,
    message: str,
    channel_id: str = "channel_1",
    channel_type: str = "O",
    team_id: str = "team_1",
    root_id: str = "",
    user_id: str = _APPROVED_USER_ID,
    event_id: str | None = None,
    sequence: int | None = None,
) -> MattermostEventEnvelope:
    return MattermostEventEnvelope(
        event="posted",
        channel_id=channel_id,
        channel_type=channel_type,
        team_id=team_id,
        user_id=user_id,
        event_id=event_id,
        sequence=sequence,
        post=MattermostPost(
            id=post_id,
            root_id=root_id,
            message=message,
            user_id=user_id,
            channel_id=channel_id,
        ),
    )


def test_direct_message_emits_normalized_event_without_mention() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="post_dm_1",
            message="help",
            channel_id="dm_channel_1",
            channel_type="D",
            team_id="global",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.DIRECT_MESSAGE
    assert event.text == "help"
    assert event.session_key == "mattermost:dm:global:dm_channel_1"


def test_channel_mention_emits_one_normalized_event_and_strips_mention() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(post_id="post_root_1", message="@onyx what changed?")
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.CHANNEL_MENTION
    assert event.text == "what changed?"
    assert event.session_key == "mattermost:channel:team_1:channel_1:post_root_1"


def test_bot_posts_emit_no_event() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="bot_post_1",
            message="answer",
            user_id=_BOT_USER_ID,
            root_id="post_root_1",
        )
    )

    assert event is None


def test_disallowed_channel_posts_emit_no_event() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="post_root_3",
            message="@onyx should be ignored",
            channel_id="not_allowed",
        )
    )

    assert event is None


def test_unapproved_user_posts_emit_no_event() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="post_root_4",
            message="@onyx should be ignored",
            user_id="user_2",
        )
    )

    assert event is None


def test_replayed_event_ids_are_ignored() -> None:
    normalizer = MattermostEventNormalizer(_config())
    envelope = _posted_event(
        post_id="post_root_1",
        message="@onyx what changed?",
        event_id="event_1",
    )

    first_event = normalizer.normalize(envelope)
    second_event = normalizer.normalize(envelope)

    assert first_event is not None
    assert second_event is None


def test_replayed_post_event_fallback_ids_are_ignored() -> None:
    normalizer = MattermostEventNormalizer(_config())
    envelope = _posted_event(post_id="post_root_1", message="@onyx what changed?")

    first_event = normalizer.normalize(envelope)
    second_event = normalizer.normalize(envelope)

    assert first_event is not None
    assert second_event is None


def test_root_allowlisted_post_emits_normalized_event_when_enabled() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="post_root_2",
            message="summarize this",
            channel_id="channel_2",
            channel_type="P",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.ROOT_ALLOWLISTED_POST
    assert event.session_key == "mattermost:channel:team_1:channel_2:post_root_2"


def test_thread_reply_followup_emits_normalized_event_for_owned_thread() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        _posted_event(
            post_id="post_reply_1",
            message="can you expand?",
            root_id="post_root_1",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP
    assert event.session_key == "mattermost:channel:team_1:channel_1:post_root_1"


@pytest.mark.parametrize(
    ("raw_event_type", "expected_event_type"),
    [
        ("post_edited", MattermostNormalizedEventType.POST_UPDATE_RETRY),
        ("reaction_added", MattermostNormalizedEventType.REACTION_FEEDBACK),
        ("post_deleted", MattermostNormalizedEventType.POST_DELETE_TOMBSTONE),
    ],
)
def test_owned_thread_events_emit_normalized_events(
    raw_event_type: str,
    expected_event_type: MattermostNormalizedEventType,
) -> None:
    normalizer = MattermostEventNormalizer(_config())
    post = MattermostPost(
        id="post_root_1",
        root_id="",
        message="@onyx retry",
        user_id=_APPROVED_USER_ID,
        channel_id="channel_1",
    )
    envelope = MattermostEventEnvelope(
        event=raw_event_type,
        team_id="team_1",
        channel_id="channel_1",
        channel_type="O",
        user_id=_APPROVED_USER_ID,
        post=post,
    )

    event = normalizer.normalize(envelope)

    assert event is not None
    assert event.event_type == expected_event_type
    assert event.session_key == "mattermost:channel:team_1:channel_1:post_root_1"


def test_non_content_system_events_emit_no_event() -> None:
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(
        MattermostEventEnvelope(
            event="user_added",
            channel_id="channel_1",
            channel_type="O",
            team_id="team_1",
            user_id=_APPROVED_USER_ID,
            post=None,
        )
    )

    assert event is None


def test_mattermost_websocket_payload_is_parsed_to_typed_event() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "posted",
            "seq": 12,
            "data": {
                "channel_type": "D",
                "post": (
                    '{"id":"post_dm_1","channel_id":"dm_channel_1",'
                    '"user_id":"user_1","message":"help"}'
                ),
            },
            "broadcast": {"team_id": "team_1", "channel_id": "dm_channel_1"},
        }
    )

    assert envelope.event == "posted"
    assert envelope.sequence == 12
    assert envelope.channel_type == "D"
    assert envelope.post is not None
    assert envelope.post.id == "post_dm_1"
    assert envelope.post.message == "help"


class _FlakyClient:
    def __init__(self, envelope: MattermostEventEnvelope) -> None:
        self.calls = 0
        self._envelope = envelope

    async def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        self.calls += 1
        if self.calls <= 2:
            raise RuntimeError("connection dropped")
        yield self._envelope


@pytest.mark.asyncio
async def test_listener_reconnect_uses_bounded_backoff() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    envelope = _posted_event(
        post_id="post_root_1",
        message="@onyx what changed?",
        event_id="event_after_reconnect",
    )
    listener = MattermostEventListener(
        _FlakyClient(envelope),
        _config(
            initial_reconnect_backoff_seconds=1.0, max_reconnect_backoff_seconds=3.0
        ),
        sleep=record_sleep,
    )

    event = await anext(listener.normalized_events())

    assert event.event_type == MattermostNormalizedEventType.CHANNEL_MENTION
    assert sleeps == [1.0, 2.0]
