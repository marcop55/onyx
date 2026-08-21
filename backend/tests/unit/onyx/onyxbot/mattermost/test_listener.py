"""Unit tests for Mattermost listener normalization."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest

from onyx.onyxbot.mattermost.client import (
    MattermostClientError,
    mattermost_event_from_payload,
)
from onyx.onyxbot.mattermost.listener import (
    MattermostEventListener,
    MattermostEventNormalizer,
)
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostUserInfo,
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
        owned_thread_root_ids={"post_root_1"},
        owned_answer_post_root_ids={"bot_answer_1": "post_root_1"},
        owned_answer_post_message_ids={"bot_answer_1": 22},
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
        owned_thread_root_ids=_set_override(
            overrides, "owned_thread_root_ids", config.owned_thread_root_ids
        ),
        owned_answer_post_root_ids=_dict_override(
            overrides,
            "owned_answer_post_root_ids",
            config.owned_answer_post_root_ids,
        ),
        owned_answer_post_message_ids=_int_dict_override(
            overrides,
            "owned_answer_post_message_ids",
            config.owned_answer_post_message_ids,
        ),
        managed_channel_config_resolver=_resolver_override(
            overrides,
            "managed_channel_config_resolver",
            config.managed_channel_config_resolver,
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


def _set_override(
    overrides: dict[str, object],
    key: str,
    default: set[str],
) -> set[str]:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, set):
        return cast(set[str], value)
    raise TypeError(f"{key} must be a set")


def _float_override(overrides: dict[str, object], key: str, default: float) -> float:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"{key} must be numeric")


def _dict_override(
    overrides: dict[str, object],
    key: str,
    default: dict[str, str],
) -> dict[str, str]:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, dict):
        return cast(dict[str, str], value)
    raise TypeError(f"{key} must be a dict")


def _int_dict_override(
    overrides: dict[str, object],
    key: str,
    default: dict[str, int],
) -> dict[str, int]:
    value = overrides.get(key)
    if value is None:
        return default
    if isinstance(value, dict):
        return cast(dict[str, int], value)
    raise TypeError(f"{key} must be a dict")


def _resolver_override(
    overrides: dict[str, object],
    key: str,
    default: Callable[[str], dict[str, object] | None] | None,
) -> Callable[[str], dict[str, object] | None] | None:
    value = overrides.get(key)
    if value is None:
        return default
    if callable(value):
        return cast(Callable[[str], dict[str, object] | None], value)
    raise TypeError(f"{key} must be callable")


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
    normalizer = MattermostEventNormalizer(
        _config(allowed_channel_ids=frozenset(), allowed_team_ids=frozenset())
    )

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


def test_uncompleted_replayed_event_ids_reach_durable_admission() -> None:
    normalizer = MattermostEventNormalizer(_config())
    envelope = _posted_event(
        post_id="post_root_1",
        message="@onyx what changed?",
        event_id="event_1",
    )

    first_event = normalizer.normalize(envelope)
    second_event = normalizer.normalize(envelope)

    assert first_event is not None
    assert second_event is not None
    assert second_event.dedupe_key == first_event.dedupe_key


def test_uncompleted_replayed_fallback_ids_reach_durable_admission() -> None:
    normalizer = MattermostEventNormalizer(_config())
    envelope = _posted_event(post_id="post_root_1", message="@onyx what changed?")

    first_event = normalizer.normalize(envelope)
    second_event = normalizer.normalize(envelope)

    assert first_event is not None
    assert second_event is not None
    assert second_event.dedupe_key == first_event.dedupe_key


def test_fallback_ids_distinguish_post_edit_revisions_and_dedupe_exact_replay() -> None:
    normalizer = MattermostEventNormalizer(_config())

    def edited_envelope(
        *, update_at: int, file_ids: tuple[str, ...]
    ) -> MattermostEventEnvelope:
        return MattermostEventEnvelope(
            event="post_edited",
            channel_id="channel_1",
            channel_type="O",
            team_id="team_1",
            user_id=_APPROVED_USER_ID,
            post=MattermostPost(
                id="post_root_1",
                message="@onyx same text",
                user_id=_APPROVED_USER_ID,
                channel_id="channel_1",
                update_at=update_at,
                file_ids=file_ids,
            ),
        )

    first_revision = normalizer.normalize(
        edited_envelope(update_at=1786720000100, file_ids=("file_a",))
    )
    exact_replay = normalizer.normalize(
        edited_envelope(update_at=1786720000100, file_ids=("file_a",))
    )
    timestamp_revision = normalizer.normalize(
        edited_envelope(update_at=1786720000200, file_ids=("file_a",))
    )
    attachment_revision = normalizer.normalize(
        edited_envelope(update_at=1786720000200, file_ids=("file_a", "file_b"))
    )

    assert first_revision is not None
    assert exact_replay is not None
    assert timestamp_revision is not None
    assert attachment_revision is not None
    assert exact_replay.dedupe_key == first_revision.dedupe_key
    assert timestamp_revision.dedupe_key != first_revision.dedupe_key
    assert attachment_revision.dedupe_key != timestamp_revision.dedupe_key


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


def test_managed_respond_tag_only_false_allows_root_posts_at_runtime() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            root_post_channel_ids=frozenset(),
            managed_channel_config_resolver=lambda _channel_id: {
                "respond_tag_only": False,
                "disabled": False,
            },
        )
    )

    event = normalizer.normalize(
        _posted_event(
            post_id="post_managed_root",
            message="summarize this",
            channel_id="channel_1",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.ROOT_ALLOWLISTED_POST


def test_managed_respond_tag_only_true_suppresses_unmentioned_root_posts() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            root_post_channel_ids=frozenset({"channel_1"}),
            managed_channel_config_resolver=lambda _channel_id: {
                "respond_tag_only": True,
                "disabled": False,
            },
        )
    )

    event = normalizer.normalize(
        _posted_event(
            post_id="post_managed_root",
            message="summarize this",
            channel_id="channel_1",
        )
    )

    assert event is None


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("morning all\nstandup in 5", id="multiline"),
        pytest.param("todo:\n- deploy\n- test", id="markdown_list"),
        pytest.param("summarize this ", id="trailing_space"),
        pytest.param(" summarize this", id="leading_space"),
        pytest.param("summarize  this", id="double_space"),
    ],
)
def test_respond_tag_only_suppresses_unmentioned_root_posts_with_loose_whitespace(
    message: str,
) -> None:
    """Whitespace shape must not stand in for a bot mention."""

    normalizer = MattermostEventNormalizer(
        _config(
            root_post_channel_ids=frozenset(),
            managed_channel_config_resolver=lambda _channel_id: {
                "respond_tag_only": True,
                "disabled": False,
            },
        )
    )

    event = normalizer.normalize(
        _posted_event(
            post_id="post_managed_root",
            message=message,
            channel_id="channel_1",
        )
    )

    assert event is None


def test_channel_mention_survives_loose_whitespace_in_tag_only_channel() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            root_post_channel_ids=frozenset(),
            managed_channel_config_resolver=lambda _channel_id: {
                "respond_tag_only": True,
                "disabled": False,
            },
        )
    )

    event = normalizer.normalize(
        _posted_event(
            post_id="post_managed_mention",
            message="@onyx  can you summarize this?\nthanks",
            channel_id="channel_1",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.CHANNEL_MENTION
    assert event.text == "can you summarize this? thanks"


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


def test_answer_post_deletion_does_not_relinquish_root_ownership() -> None:
    normalizer = MattermostEventNormalizer(_config())
    envelope = MattermostEventEnvelope(
        event="post_deleted",
        team_id="team_1",
        channel_id="channel_1",
        channel_type="O",
        user_id=_APPROVED_USER_ID,
        post=MattermostPost(
            id="bot_answer_1",
            root_id="post_root_1",
            message="",
            user_id=_APPROVED_USER_ID,
            channel_id="channel_1",
        ),
    )

    assert normalizer.normalize(envelope) is None


@pytest.mark.parametrize(
    ("raw_event_type", "expected_event_type"),
    [
        ("post_edited", MattermostNormalizedEventType.POST_UPDATE_RETRY),
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


def test_mattermost_post_payload_preserves_file_ids_and_timestamps() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "posted",
            "seq": 14,
            "data": {
                "post": (
                    '{"id":"post_with_files","channel_id":"channel_1",'
                    '"user_id":"user_1","message":"see attached",'
                    '"file_ids":["file_a","file_b"],'
                    '"create_at":1786720000123,"update_at":1786720000456}'
                ),
            },
            "broadcast": {"team_id": "team_1", "channel_id": "channel_1"},
        }
    )

    assert envelope.post is not None
    assert envelope.post.file_ids == ("file_a", "file_b")
    assert envelope.post.create_at == 1786720000123
    assert envelope.post.update_at == 1786720000456


def test_real_reaction_added_payload_emits_typed_feedback_event() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "reaction_added",
            "seq": 13,
            "data": {
                "reaction": (
                    '{"user_id":"user_1","post_id":"bot_answer_1",'
                    '"emoji_name":"+1","channel_id":"channel_1"}'
                ),
                "team_id": "team_1",
            },
            "broadcast": {"channel_id": "channel_1", "team_id": "team_1"},
        }
    )
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(envelope)

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.REACTION_FEEDBACK
    assert event.post_id == "bot_answer_1"
    assert event.root_post_id == "post_root_1"
    assert event.feedback_answer_post_id == "bot_answer_1"
    assert event.feedback_action == "like"
    assert event.feedback_message_id == 22
    assert event.metadata == {
        "feedback_answer_post_id": "bot_answer_1",
        "feedback_action": "like",
        "feedback_message_id": "22",
    }


def test_malformed_reaction_added_payload_is_rejected() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "reaction_added",
            "data": {"reaction": "{not-json"},
            "broadcast": {"channel_id": "channel_1", "team_id": "team_1"},
        }
    )

    assert envelope.reaction is None
    assert MattermostEventNormalizer(_config()).normalize(envelope) is None


def test_reaction_feedback_ignores_unowned_answer_posts() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "reaction_added",
            "data": {
                "reaction": {
                    "user_id": _APPROVED_USER_ID,
                    "post_id": "other_bot_post",
                    "emoji_name": "+1",
                    "channel_id": "channel_1",
                },
                "team_id": "team_1",
            },
            "broadcast": {"channel_id": "channel_1", "team_id": "team_1"},
        }
    )
    normalizer = MattermostEventNormalizer(_config())

    event = normalizer.normalize(envelope)

    assert event is None


def test_reaction_feedback_fallback_dedupe_uses_answer_post_id() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            owned_answer_post_root_ids={
                "bot_answer_1": "post_root_1",
                "bot_answer_2": "post_root_1",
            },
            owned_answer_post_message_ids={
                "bot_answer_1": 22,
                "bot_answer_2": 23,
            },
        )
    )
    first_envelope = mattermost_event_from_payload(
        {
            "event": "reaction_added",
            "data": {
                "reaction": {
                    "user_id": _APPROVED_USER_ID,
                    "post_id": "bot_answer_1",
                    "emoji_name": "+1",
                    "channel_id": "channel_1",
                },
                "team_id": "team_1",
            },
            "broadcast": {"channel_id": "channel_1", "team_id": "team_1"},
        }
    )
    second_envelope = mattermost_event_from_payload(
        {
            "event": "reaction_added",
            "data": {
                "reaction": {
                    "user_id": _APPROVED_USER_ID,
                    "post_id": "bot_answer_2",
                    "emoji_name": "+1",
                    "channel_id": "channel_1",
                },
                "team_id": "team_1",
            },
            "broadcast": {"channel_id": "channel_1", "team_id": "team_1"},
        }
    )

    first_event = normalizer.normalize(first_envelope)
    replayed_event = normalizer.normalize(first_envelope)
    second_event = normalizer.normalize(second_envelope)

    assert first_event is not None
    assert replayed_event is not None
    assert replayed_event.dedupe_key == first_event.dedupe_key
    assert second_event is not None


class _FlakyClient:
    def __init__(self, envelope: MattermostEventEnvelope) -> None:
        self.calls = 0
        self._envelope = envelope

    async def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        self.calls += 1
        if self.calls <= 2:
            raise RuntimeError("connection dropped")
        yield self._envelope

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        _ = channel_id, user_id
        return True

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        return MattermostUserInfo(
            id=user_id,
            username="ada",
            display_name="Ada Lovelace",
        )


class _MembershipClient:
    def __init__(
        self,
        envelope: MattermostEventEnvelope,
        memberships: dict[tuple[str, str], bool],
        *,
        fail_membership_lookup: bool = False,
    ) -> None:
        self._envelope = envelope
        self._memberships = memberships
        self._fail_membership_lookup = fail_membership_lookup
        self.membership_checks: list[tuple[str, str]] = []

    async def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        yield self._envelope
        await asyncio.Event().wait()

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_checks.append((channel_id, user_id))
        if self._fail_membership_lookup:
            raise MattermostClientError("membership lookup failed")
        return self._memberships.get((channel_id, user_id), False)

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        return MattermostUserInfo(
            id=user_id,
            username="ada",
            display_name="Ada Lovelace",
        )


class _ChangingMembershipClient:
    def __init__(
        self,
        envelopes: list[MattermostEventEnvelope],
        membership_snapshots: list[dict[tuple[str, str], bool]],
    ) -> None:
        self._envelopes = envelopes
        self._membership_snapshots = membership_snapshots
        self.membership_checks: list[tuple[str, str]] = []

    async def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        for envelope in self._envelopes:
            yield envelope
        await asyncio.Event().wait()

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_checks.append((channel_id, user_id))
        snapshot_index = min(
            max((len(self.membership_checks) - 1) // 2, 0),
            len(self._membership_snapshots) - 1,
        )
        return self._membership_snapshots[snapshot_index].get(
            (channel_id, user_id), False
        )

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        return MattermostUserInfo(
            id=user_id,
            username="ada",
            display_name="Ada Lovelace",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_type", "channel_id", "team_id", "expected_event_type"),
    [
        (
            "O",
            "public_channel_1",
            "team_1",
            MattermostNormalizedEventType.CHANNEL_MENTION,
        ),
        (
            "P",
            "private_channel_1",
            "team_1",
            MattermostNormalizedEventType.CHANNEL_MENTION,
        ),
        ("D", "dm_channel_1", "global", MattermostNormalizedEventType.DIRECT_MESSAGE),
    ],
)
async def test_listener_authorizes_events_with_current_membership(
    channel_type: str,
    channel_id: str,
    team_id: str,
    expected_event_type: MattermostNormalizedEventType,
) -> None:
    envelope = _posted_event(
        post_id=f"post_{channel_id}",
        message="@onyx help" if channel_type != "D" else "help",
        channel_id=channel_id,
        channel_type=channel_type,
        team_id=team_id,
    )
    client = _MembershipClient(
        envelope,
        {
            (channel_id, _APPROVED_USER_ID): True,
            (channel_id, _BOT_USER_ID): True,
        },
    )
    listener = MattermostEventListener(
        client,
        _config(
            allowed_channel_ids=frozenset(),
            allowed_team_ids=frozenset(),
            approved_user_ids=frozenset(),
        ),
    )

    event = await anext(listener.normalized_events())

    assert event.event_type == expected_event_type
    assert client.membership_checks == [
        (channel_id, _BOT_USER_ID),
        (channel_id, _APPROVED_USER_ID),
    ]
    assert event.source_username == "ada"
    assert event.source_display_name == "Ada Lovelace"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "memberships",
    [
        {("channel_1", _BOT_USER_ID): True, ("channel_1", _APPROVED_USER_ID): False},
        {("channel_1", _BOT_USER_ID): False, ("channel_1", _APPROVED_USER_ID): True},
    ],
)
async def test_listener_rejects_when_sender_or_bot_is_not_a_current_member(
    memberships: dict[tuple[str, str], bool],
) -> None:
    listener = MattermostEventListener(
        _MembershipClient(
            _posted_event(post_id="post_root_1", message="@onyx help"),
            memberships,
        ),
        _config(approved_user_ids=frozenset()),
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(listener.normalized_events()), timeout=0.01)


@pytest.mark.asyncio
async def test_listener_rejects_when_membership_api_fails() -> None:
    listener = MattermostEventListener(
        _MembershipClient(
            _posted_event(post_id="post_root_1", message="@onyx help"),
            {},
            fail_membership_lookup=True,
        ),
        _config(approved_user_ids=frozenset()),
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(listener.normalized_events()), timeout=0.01)


@pytest.mark.asyncio
async def test_listener_rechecks_membership_before_each_event() -> None:
    first_envelope = _posted_event(
        post_id="post_root_1",
        message="@onyx first",
        event_id="event_first",
    )
    removed_sender_envelope = _posted_event(
        post_id="post_root_2",
        message="@onyx second",
        event_id="event_after_sender_removed",
    )
    client = _ChangingMembershipClient(
        [first_envelope, removed_sender_envelope],
        [
            {("channel_1", _BOT_USER_ID): True, ("channel_1", _APPROVED_USER_ID): True},
            {
                ("channel_1", _BOT_USER_ID): True,
                ("channel_1", _APPROVED_USER_ID): False,
            },
        ],
    )
    event_stream = MattermostEventListener(
        client,
        _config(approved_user_ids=frozenset()),
    ).normalized_events()

    first_event = await anext(event_stream)

    assert first_event.post_id == "post_root_1"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(event_stream), timeout=0.01)
    assert client.membership_checks == [
        ("channel_1", _BOT_USER_ID),
        ("channel_1", _APPROVED_USER_ID),
        ("channel_1", _BOT_USER_ID),
        ("channel_1", _APPROVED_USER_ID),
    ]


def test_empty_emergency_restrictions_do_not_reject_valid_members() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            allowed_channel_ids=frozenset(),
            allowed_team_ids=frozenset(),
            approved_user_ids=frozenset(),
        )
    )

    event = normalizer.normalize(
        _posted_event(
            post_id="post_root_1",
            message="@onyx help",
            channel_id="newly_invited_channel",
            team_id="new_team",
            user_id="new_user",
        )
    )

    assert event is not None
    assert event.event_type == MattermostNormalizedEventType.CHANNEL_MENTION


def test_optional_emergency_restrictions_can_narrow_access() -> None:
    normalizer = MattermostEventNormalizer(
        _config(
            allowed_channel_ids=frozenset({"channel_1"}),
            allowed_team_ids=frozenset({"team_1"}),
            approved_user_ids=frozenset({_APPROVED_USER_ID}),
        )
    )

    assert (
        normalizer.normalize(
            _posted_event(
                post_id="restricted_channel",
                message="@onyx help",
                channel_id="other_channel",
            )
        )
        is None
    )
    assert (
        normalizer.normalize(
            _posted_event(
                post_id="restricted_user",
                message="@onyx help",
                user_id="other_user",
            )
        )
        is None
    )


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
