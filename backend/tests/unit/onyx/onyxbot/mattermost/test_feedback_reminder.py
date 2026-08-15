from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, cast
from uuid import UUID

import pytest

from onyx.background.celery.tasks.mattermost_feedback import (
    MATTERMOST_FEEDBACK_REMINDER_MESSAGE,
    mattermost_feedback_reminder,
)
from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import MattermostPost


class _Token:
    def get_value(self, *, apply_mask: bool) -> str:
        assert apply_mask is False
        return "mattermost-token"


class _ReminderClient:
    def __init__(
        self,
        *,
        memberships: list[bool] | None = None,
        existing_posts: list[MattermostPost] | None = None,
        create_error: Exception | None = None,
    ) -> None:
        self.memberships = memberships or [True, True]
        self.existing_posts = existing_posts or []
        self.create_error = create_error
        self.membership_calls: list[tuple[str, str]] = []
        self.thread_calls: list[str] = []
        self.find_calls: list[tuple[str, str, str]] = []
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _ReminderClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_calls.append((channel_id, user_id))
        return self.memberships.pop(0)

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        self.thread_calls.append(root_post_id)
        return [
            MattermostPost(id="root-post-1", channel_id="channel-1"),
            MattermostPost(
                id="answer-post-1",
                root_id="root-post-1",
                channel_id="channel-1",
            ),
            *self.existing_posts,
        ]

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None:
        self.find_calls.append((channel_id, pending_post_id, event_key))
        for post in self.existing_posts:
            if (
                post.pending_post_id == pending_post_id
                or post.props.get("onyx_event_key") == event_key
            ):
                return post
        return None

    async def create_post(self, **kwargs: Any) -> MattermostPost:
        self.posts.append(kwargs)
        post = MattermostPost(
            id=f"feedback-reminder-{len(self.posts)}",
            message=str(kwargs["message"]),
            root_id=str(kwargs["root_id"]),
            channel_id=str(kwargs["channel_id"]),
            pending_post_id=str(kwargs["pending_post_id"]),
            props=cast(dict[str, object], kwargs["props"]),
        )
        self.existing_posts.append(post)
        if self.create_error is not None:
            raise self.create_error
        return post


@contextmanager
def _session_context() -> Iterator[object]:
    yield object()


def _claim(
    *,
    event_id: int = 77,
    state: str = "claimed",
    pending_post_id: str = "pending-reminder",
) -> MattermostEventClaim:
    return MattermostEventClaim(
        MattermostClaimOutcome.PROCESS,
        cast(
            MattermostEventState,
            SimpleNamespace(
                id=event_id,
                state=state,
                mattermost_pending_post_id=pending_post_id,
            ),
        ),
        UUID("00000000-0000-0000-0000-000000000999"),
    )


def test_feedback_reminder_task_posts_visible_thread_reminder_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ReminderClient()
    completed: list[tuple[int, object]] = []

    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.get_session_with_current_tenant",
        _session_context,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_bot_by_instance_and_user",
        lambda *_args, **_kwargs: SimpleNamespace(
            url="https://mattermost.example.test",
            token=_Token(),
            enabled=True,
        ),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_channel_config_for_bot_and_channel",
        lambda *_args, **_kwargs: SimpleNamespace(channel_config={}, enabled=True),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.claim_durable_mattermost_event",
        lambda *_args, **_kwargs: _claim(),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post_attempt",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.complete_mattermost_control_event",
        lambda *_args, **kwargs: (
            completed.append((cast(int, kwargs["event_id"]), kwargs["claim_owner"]))
            or True
        ),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.MattermostClient",
        lambda *_args, **_kwargs: client,
    )

    result = mattermost_feedback_reminder(
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-1",
        channel_id="channel-1",
        root_post_id="root-post-1",
        answer_post_id="answer-post-1",
        user_id="user-1",
    )

    assert result == {"status": "delivered", "post_id": "feedback-reminder-1"}
    assert client.membership_calls == [("channel-1", "bot-1"), ("channel-1", "user-1")]
    assert client.thread_calls == ["root-post-1"]
    assert client.posts == [
        {
            "channel_id": "channel-1",
            "root_id": "root-post-1",
            "message": MATTERMOST_FEEDBACK_REMINDER_MESSAGE,
            "pending_post_id": "pending-reminder",
            "props": {
                "onyx_event_key": "77",
                "onyx_mattermost_feedback_reminder": True,
                "onyx_mattermost_answer_post_id": "answer-post-1",
                "onyx_mattermost_recipient_user_id": "user-1",
            },
        }
    ]
    assert completed == [(77, UUID("00000000-0000-0000-0000-000000000999"))]


def test_feedback_reminder_replay_reuses_ambiguous_visible_post_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ReminderClient(
        memberships=[True, True, True, True],
        create_error=MattermostClientError("transport timeout"),
    )
    claims = iter(
        [
            _claim(state="claimed", pending_post_id="pending-reminder"),
            _claim(state="post_create_attempted", pending_post_id="pending-reminder"),
        ]
    )
    complete_results = iter([False, True])

    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.get_session_with_current_tenant",
        _session_context,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_bot_by_instance_and_user",
        lambda *_args, **_kwargs: SimpleNamespace(
            url="https://mattermost.example.test",
            token=_Token(),
            enabled=True,
        ),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_channel_config_for_bot_and_channel",
        lambda *_args, **_kwargs: SimpleNamespace(channel_config={}, enabled=True),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.claim_durable_mattermost_event",
        lambda *_args, **_kwargs: next(claims),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post_attempt",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.complete_mattermost_control_event",
        lambda *_args, **_kwargs: next(complete_results),
    )
    monkeypatch.setattr(
        "onyx.background.celery.tasks.mattermost_feedback.MattermostClient",
        lambda *_args, **_kwargs: client,
    )

    first_result = mattermost_feedback_reminder(
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-1",
        channel_id="channel-1",
        root_post_id="root-post-1",
        answer_post_id="answer-post-1",
        user_id="user-1",
    )
    client.create_error = None
    second_result = mattermost_feedback_reminder(
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-1",
        channel_id="channel-1",
        root_post_id="root-post-1",
        answer_post_id="answer-post-1",
        user_id="user-1",
    )

    assert first_result == {"status": "ambiguous", "post_id": "feedback-reminder-1"}
    assert second_result == {"status": "delivered", "post_id": "feedback-reminder-1"}
    assert len(client.posts) == 1
    assert client.find_calls == [
        ("channel-1", "pending-reminder", "77"),
        ("channel-1", "pending-reminder", "77"),
        ("channel-1", "pending-reminder", "77"),
    ]
