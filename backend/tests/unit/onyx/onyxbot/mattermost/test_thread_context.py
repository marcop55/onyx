from __future__ import annotations

import asyncio

import pytest

from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.context import build_mattermost_turn_context
from onyx.onyxbot.mattermost.listener import MattermostEventListener
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)


class _ThreadClient:
    def __init__(self, posts: list[MattermostPost]) -> None:
        self.posts = posts
        self.thread_roots: list[str] = []
        self.user_ids: list[str] = []

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        self.thread_roots.append(root_post_id)
        return self.posts

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        self.user_ids.append(user_id)
        return MattermostUserInfo(
            id=user_id,
            username=f"user-{user_id}",
            display_name=f"User {user_id}",
        )


class _FailingThreadClient(_ThreadClient):
    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        _ = root_post_id
        raise MattermostClientError("thread fetch failed")


@pytest.mark.asyncio
async def test_existing_thread_context_is_chronological_attributable_and_excludes_deleted_posts() -> (
    None
):
    client = _ThreadClient(
        [
            MattermostPost(
                id="later-reply",
                message="second useful detail",
                root_id="root-1",
                user_id="user-2",
                create_at=300,
                update_at=350,
            ),
            MattermostPost(
                id="deleted-reply",
                message="removed detail",
                root_id="root-1",
                user_id="user-3",
                create_at=200,
                delete_at=250,
            ),
            MattermostPost(
                id="root-1",
                message="original question",
                user_id="user-1",
                create_at=100,
            ),
            MattermostPost(
                id="current-post",
                message="@onyx answer now",
                root_id="root-1",
                user_id="user-4",
                create_at=400,
            ),
        ]
    )

    context = await build_mattermost_turn_context(
        client=client,
        event=_event(post_id="current-post"),
        previously_loaded_post_ids=frozenset(),
    )

    assert context is not None
    assert context.text == (
        "The following is the current Mattermost thread context before this turn. "
        "Posts are chronological, use latest edits, omit deleted posts, and preserve sender attribution.\n"
        "====================\n"
        "User user-1 (user-user-1, user-1):\noriginal question\n\n"
        "User user-2 (user-user-2, user-2):\nsecond useful detail"
    )
    assert context.post_ids == frozenset({"root-1", "later-reply"})
    assert client.thread_roots == ["root-1"]
    assert client.user_ids == ["user-1", "user-2"]


@pytest.mark.asyncio
async def test_subsequent_turn_context_injects_only_unseen_deltas() -> None:
    context = await build_mattermost_turn_context(
        client=_ThreadClient(
            [
                MattermostPost(
                    id="root-1",
                    message="already loaded root",
                    user_id="user-1",
                    create_at=100,
                ),
                MattermostPost(
                    id="unseen-reply",
                    message="new detail before current turn",
                    root_id="root-1",
                    user_id="user-2",
                    create_at=200,
                ),
                MattermostPost(
                    id="current-post",
                    message="@onyx answer this",
                    root_id="root-1",
                    user_id="user-3",
                    create_at=300,
                ),
            ]
        ),
        event=_event(post_id="current-post"),
        previously_loaded_post_ids=frozenset({"root-1"}),
    )

    assert context is not None
    assert "already loaded root" not in context.text
    assert "new detail before current turn" in context.text
    assert "@onyx answer this" not in context.text
    assert context.post_ids == frozenset({"unseen-reply"})


@pytest.mark.asyncio
async def test_thread_context_primary_fetch_failure_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="thread context lookup failed"):
        await build_mattermost_turn_context(
            client=_FailingThreadClient([]),
            event=_event(),
            previously_loaded_post_ids=frozenset(),
        )


@pytest.mark.asyncio
async def test_thread_context_authorization_denial_happens_before_loading_context() -> (
    None
):
    envelope = MattermostEventEnvelope(
        event="posted",
        channel_id="channel-1",
        channel_type="O",
        team_id="team-1",
        user_id="user-1",
        post=MattermostPost(
            id="root-1",
            message="@onyx secret",
            user_id="user-1",
            channel_id="channel-1",
        ),
    )
    client = _DeniedMembershipClient(envelope)
    listener = MattermostEventListener(
        client,
        MattermostListenerConfig(
            bot_user_id="bot-1",
            bot_mentions=frozenset({"@onyx"}),
        ),
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(listener.normalized_events()), timeout=0.01)
    assert client.thread_fetches == 0


def _event(*, post_id: str = "current-post") -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id=post_id,
        root_post_id="root-1",
        user_id="user-4",
        text="answer now",
        dedupe_key="event-id:current",
    )


class _DeniedMembershipClient:
    def __init__(self, envelope: MattermostEventEnvelope) -> None:
        self._envelope = envelope
        self.thread_fetches = 0

    async def connect_events(self):  # type: ignore[no-untyped-def]
        yield self._envelope
        await asyncio.Event().wait()

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        _ = channel_id, user_id
        return False

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        _ = user_id
        raise AssertionError("unauthorized events must not fetch attribution")

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        _ = root_post_id
        self.thread_fetches += 1
        raise AssertionError("unauthorized events must not fetch thread context")
