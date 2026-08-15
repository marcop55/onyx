from __future__ import annotations

import pytest

from onyx.context.search.models import Tag
from onyx.onyxbot.mattermost.channel_filters import (
    MATTERMOST_CHANNEL_FILTER_NO_RESULTS_PREFIX,
    MattermostChannelFilterAccessDenied,
    MattermostChannelFilterResolutionError,
    resolve_mattermost_channel_filters,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)


class _ChannelFilterClient:
    def __init__(
        self,
        *,
        channels_by_name: dict[str, object],
        memberships: set[tuple[str, str]],
        bot_user_id: str = "bot-1",
    ) -> None:
        self.channels_by_name = channels_by_name
        self.memberships = memberships
        self.bot_user_id = bot_user_id
        self.name_lookups: list[tuple[str, str]] = []
        self.membership_checks: list[tuple[str, str]] = []

    async def get_me(self) -> dict[str, object]:
        return {"id": self.bot_user_id}

    async def get_channel_by_name(self, *, team_id: str, channel_name: str) -> object:
        self.name_lookups.append((team_id, channel_name))
        return self.channels_by_name[channel_name]

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_checks.append((channel_id, user_id))
        return (channel_id, user_id) in self.memberships


def _event(text: str) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:source-channel:root-1",
        team_id="team-1",
        channel_id="source-channel",
        post_id="post-1",
        root_post_id="root-1",
        user_id="sender-1",
        text=text,
        dedupe_key="event-id:post-1",
    )


@pytest.mark.asyncio
async def test_channel_references_resolve_to_immutable_channel_id_tags() -> None:
    client = _ChannelFilterClient(
        channels_by_name={
            "town-square": {"id": "channel-id-1", "name": "town-square"},
        },
        memberships={("channel-id-1", "bot-1"), ("channel-id-1", "sender-1")},
    )

    result = await resolve_mattermost_channel_filters(
        event=_event("what did we decide in:town-square and ~town-square?"),
        client=client,
    )

    assert result.message == "what did we decide #town-square and #town-square?"
    assert result.tags == [Tag(tag_key="channel_id", tag_value="channel-id-1")]
    assert result.no_results_message == (
        f"{MATTERMOST_CHANNEL_FILTER_NO_RESULTS_PREFIX} #town-square. "
        "No indexed Mattermost posts matched this filtered query."
    )
    assert client.name_lookups == [("team-1", "town-square")]
    assert client.membership_checks == [
        ("channel-id-1", "bot-1"),
        ("channel-id-1", "sender-1"),
    ]


@pytest.mark.asyncio
async def test_inaccessible_referenced_channel_fails_closed() -> None:
    client = _ChannelFilterClient(
        channels_by_name={
            "secret": {"id": "channel-id-secret", "name": "secret"},
        },
        memberships={("channel-id-secret", "bot-1")},
    )

    with pytest.raises(MattermostChannelFilterAccessDenied, match="referenced channel"):
        await resolve_mattermost_channel_filters(
            event=_event("summarize in:secret"),
            client=client,
        )

    assert client.membership_checks == [
        ("channel-id-secret", "bot-1"),
        ("channel-id-secret", "sender-1"),
    ]


@pytest.mark.asyncio
async def test_ambiguous_or_missing_channel_reference_fails_closed() -> None:
    client = _ChannelFilterClient(channels_by_name={}, memberships=set())

    with pytest.raises(MattermostChannelFilterResolutionError):
        await resolve_mattermost_channel_filters(
            event=_event("summarize in:not-indexed"),
            client=client,
        )

    assert client.membership_checks == []


@pytest.mark.asyncio
async def test_replay_of_duplicate_references_keeps_single_durable_filter() -> None:
    client = _ChannelFilterClient(
        channels_by_name={
            "town-square": {"id": "channel-id-1", "name": "town-square"},
        },
        memberships={("channel-id-1", "bot-1"), ("channel-id-1", "sender-1")},
    )

    first = await resolve_mattermost_channel_filters(
        event=_event("in:town-square in:town-square"),
        client=client,
    )
    second = await resolve_mattermost_channel_filters(
        event=_event("in:town-square in:town-square"),
        client=client,
    )

    assert first == second
    assert first.tags == [Tag(tag_key="channel_id", tag_value="channel-id-1")]
    assert client.name_lookups == [("team-1", "town-square"), ("team-1", "town-square")]
