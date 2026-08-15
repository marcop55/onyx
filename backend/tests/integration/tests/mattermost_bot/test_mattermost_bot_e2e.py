from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from onyx.background.celery.tasks.mattermost_feedback import (
    mattermost_feedback_reminder,
)
from onyx.chat.models import AnswerStreamPart
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.handler import (
    MattermostHandlerConfig,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.listener import MattermostEventNormalizer
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostFileInfo,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostReaction,
    MattermostUserInfo,
)
from onyx.onyxbot.mattermost.session import MattermostChatTarget
from onyx.server.query_and_chat.models import MessageResponseIDInfo
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    AgentResponseStart,
    CitationInfo,
    Packet,
    PacketObj,
    Placement,
)

_BOT_USER_ID = "bot_user_1"
_APPROVED_USER_ID = "mattermost_user_1"


@pytest.mark.asyncio
async def test_mattermost_dm_mention_followup_citations_streaming_and_dedupe() -> None:
    listener_config = _listener_config()
    normalizer = MattermostEventNormalizer(listener_config)
    db_session = MagicMock()
    client = _RecordingClient()
    target = _target(parent_message_id=11)

    dm_event = normalizer.normalize(
        _posted_envelope(
            post_id="dm-post-1",
            message="what can Onyx answer?",
            channel_id="dm-channel-1",
            channel_type="D",
            team_id="global",
            event_id="event-dm-1",
        )
    )
    mention_envelope = _posted_envelope(
        post_id="root-post-1",
        message="@onyx what changed?",
        event_id="event-mention-1",
    )
    mention_event = normalizer.normalize(mention_envelope)
    replayed_mention = normalizer.normalize(mention_envelope)
    followup_event = normalizer.normalize(
        _posted_envelope(
            post_id="reply-post-1",
            root_id="root-post-1",
            message="can you expand?",
            event_id="event-followup-1",
        )
    )

    assert dm_event is not None
    assert dm_event.event_type == MattermostNormalizedEventType.DIRECT_MESSAGE
    assert dm_event.session_key == "mattermost:dm:global:dm-channel-1"
    assert mention_event is not None
    assert mention_event.event_type == MattermostNormalizedEventType.CHANNEL_MENTION
    assert mention_event.text == "what changed?"
    assert (
        mention_event.session_key == "mattermost:channel:team-1:channel-1:root-post-1"
    )
    assert replayed_mention is not None
    assert replayed_mention.dedupe_key == mention_event.dedupe_key
    assert followup_event is not None
    assert (
        followup_event.event_type == MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP
    )
    assert followup_event.session_key == mention_event.session_key

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=target,
        ) as mock_get_target,
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=MagicMock(id=UUID("00000000-0000-0000-0000-000000000456")),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_persona_by_id",
            return_value=MagicMock(id=456),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.handle_stream_message_objects",
            side_effect=[
                _packets(message_id=22, answer="DM grounded answer [1]."),
                _packets(message_id=33, answer="Mention grounded answer [1]."),
                _packets(message_id=44, answer="Follow-up grounded answer [1]."),
            ],
        ) as mock_stream,
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            side_effect=[
                _processing_claim(event_id=1, pending_post_id="pending-1"),
                _processing_claim(event_id=2, pending_post_id="pending-2"),
                _processing_claim(event_id=3, pending_post_id="pending-3"),
            ],
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_loaded_mattermost_context_post_ids",
            return_value=frozenset(),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_delivery_mode",
            return_value=True,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.renew_mattermost_event_lease",
            return_value=True,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_post",
            return_value=True,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_turn",
            return_value=True,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_rendered_message",
            return_value=True,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_answer_event",
            return_value=True,
        ) as mock_complete,
    ):
        for event in (dm_event, mention_event, followup_event):
            handled = await handle_normalized_mattermost_event(
                event=event,
                config=MattermostHandlerConfig(
                    persona_id=456,
                    owned_thread_root_ids=listener_config.owned_thread_root_ids,
                    owned_answer_post_root_ids=listener_config.owned_answer_post_root_ids,
                    owned_answer_post_message_ids=(
                        listener_config.owned_answer_post_message_ids
                    ),
                ),
                client=client,
                db_session=db_session,
            )
            assert handled is True

    assert mock_get_target.call_count == 3
    assert mock_stream.call_count == 3
    assert [
        call.kwargs["new_msg_req"].message for call in mock_stream.call_args_list
    ] == [
        "what can Onyx answer?",
        "what changed?",
        "can you expand?",
    ]
    assert [
        call.kwargs["answer_post_ids"] for call in mock_complete.call_args_list
    ] == [
        ("bot-post-1",),
        ("bot-post-2",),
        ("bot-post-3",),
    ]
    assert client.created_posts == [
        {"channel_id": "dm-channel-1", "root_id": "", "message": "..."},
        {"channel_id": "channel-1", "root_id": "root-post-1", "message": "..."},
        {"channel_id": "channel-1", "root_id": "root-post-1", "message": "..."},
    ]
    assert client.updated_posts == [
        {
            "post_id": "bot-post-1",
            "message": "DM grounded answer [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
        },
        {
            "post_id": "bot-post-2",
            "message": "Mention grounded answer [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
        },
        {
            "post_id": "bot-post-3",
            "message": "Follow-up grounded answer [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
        },
    ]
    assert listener_config.owned_thread_root_ids == {
        "dm-post-1",
        "root-post-1",
    }
    assert listener_config.owned_answer_post_root_ids == {
        "bot-answer-1": "root-post-1",
        "bot-post-1": "dm-post-1",
        "bot-post-2": "root-post-1",
        "bot-post-3": "root-post-1",
    }
    assert listener_config.owned_answer_post_message_ids == {
        "bot-post-1": 22,
        "bot-post-2": 33,
        "bot-post-3": 44,
    }


@pytest.mark.asyncio
async def test_reaction_feedback_records_message_without_mattermost_credentials() -> (
    None
):
    normalizer = MattermostEventNormalizer(
        _listener_config(
            owned_answer_post_message_ids={"bot-answer-1": 44},
        )
    )
    feedback_event = normalizer.normalize(
        MattermostEventEnvelope(
            event="reaction_added",
            channel_id="channel-1",
            channel_type="O",
            team_id="team-1",
            user_id=_APPROVED_USER_ID,
            reaction=MattermostReaction(
                user_id=_APPROVED_USER_ID,
                post_id="bot-answer-1",
                emoji_name="+1",
                channel_id="channel-1",
            ),
            event_id="event-feedback-1",
        )
    )

    assert feedback_event is not None
    assert feedback_event.feedback_message_id == 44

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(event_id=1, pending_post_id="pending-1"),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_feedback_event",
            return_value=True,
        ) as mock_feedback,
    ):
        handled = await handle_normalized_mattermost_event(
            event=feedback_event,
            config=MattermostHandlerConfig(persona_id=456),
            client=_RecordingClient(),
            db_session=MagicMock(),
        )

    assert handled is True
    mock_feedback.assert_called_once()
    assert mock_feedback.call_args.kwargs["is_positive"] is True
    assert mock_feedback.call_args.kwargs["chat_message_id"] == 44


def test_scheduled_feedback_reminder_execution_is_visible_and_replay_safe() -> None:
    client = _ReminderExecutionClient(
        memberships=[True, True, True, True],
        create_error=MattermostClientError("committed but transport timed out"),
    )
    first_claim = _processing_claim(event_id=77, pending_post_id="pending-reminder")
    second_claim = _processing_claim(event_id=77, pending_post_id="pending-reminder")
    second_claim.event.state = "post_create_attempted"
    claims = iter(
        [
            first_claim,
            second_claim,
        ]
    )
    complete_results = iter([False, True])

    with (
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.get_session_with_current_tenant",
            _reminder_session_context,
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_bot_by_instance_and_user",
            return_value=SimpleNamespace(
                url="https://mattermost.example.test",
                token=_ReminderToken(),
                enabled=True,
            ),
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.fetch_mattermost_channel_config_for_bot_and_channel",
            return_value=SimpleNamespace(channel_config={}, enabled=True),
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.claim_durable_mattermost_event",
            side_effect=lambda *_args, **_kwargs: next(claims),
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post_attempt",
            return_value=True,
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.checkpoint_mattermost_post",
            return_value=True,
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.complete_mattermost_control_event",
            side_effect=lambda *_args, **_kwargs: next(complete_results),
        ),
        patch(
            "onyx.background.celery.tasks.mattermost_feedback.MattermostClient",
            return_value=client,
        ),
    ):
        first_result = mattermost_feedback_reminder(
            instance_id="https://mattermost.example.test",
            bot_user_id=_BOT_USER_ID,
            channel_id="channel-1",
            root_post_id="root-post-1",
            answer_post_id="answer-post-1",
            user_id=_APPROVED_USER_ID,
        )
        client.create_error = None
        second_result = mattermost_feedback_reminder(
            instance_id="https://mattermost.example.test",
            bot_user_id=_BOT_USER_ID,
            channel_id="channel-1",
            root_post_id="root-post-1",
            answer_post_id="answer-post-1",
            user_id=_APPROVED_USER_ID,
        )

    assert first_result["status"] == "ambiguous"
    assert second_result == {"status": "delivered", "post_id": "reminder-post-1"}
    assert [post["message"] for post in client.created_posts] == [
        "Please rate the Mattermost answer with Helpful or Not helpful, or mark it as needing follow-up or resolved."
    ]
    assert client.created_posts[0]["root_id"] == "root-post-1"


def _listener_config(
    *,
    owned_answer_post_message_ids: dict[str, int] | None = None,
) -> MattermostListenerConfig:
    return MattermostListenerConfig(
        bot_user_id=_BOT_USER_ID,
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1", "dm-channel-1"}),
        allowed_team_ids=frozenset({"team-1", "global"}),
        approved_user_ids=frozenset({_APPROVED_USER_ID}),
        owned_thread_root_ids={"root-post-1"},
        owned_answer_post_root_ids={"bot-answer-1": "root-post-1"},
        owned_answer_post_message_ids=owned_answer_post_message_ids or {},
    )


def _processing_claim(
    *,
    event_id: int,
    pending_post_id: str,
) -> MattermostEventClaim:
    ledger_event = MattermostEventState(
        id=event_id,
        instance_id="mattermost",
        channel_id="channel-1",
        dedupe_key=f"event_id:{event_id}",
        event_type="channel_mention",
        source_post_id=f"source-post-{event_id}",
        mattermost_pending_post_id=pending_post_id,
        state="pending",
    )
    claim_owner = UUID("00000000-0000-0000-0000-000000000999")
    return MattermostEventClaim(
        MattermostClaimOutcome.PROCESS, ledger_event, claim_owner
    )


def _posted_envelope(
    *,
    post_id: str,
    message: str,
    channel_id: str = "channel-1",
    channel_type: str = "O",
    team_id: str = "team-1",
    root_id: str = "",
    event_id: str,
) -> MattermostEventEnvelope:
    return MattermostEventEnvelope(
        event="posted",
        channel_id=channel_id,
        channel_type=channel_type,
        team_id=team_id,
        user_id=_APPROVED_USER_ID,
        event_id=event_id,
        post=MattermostPost(
            id=post_id,
            message=message,
            root_id=root_id,
            user_id=_APPROVED_USER_ID,
            channel_id=channel_id,
        ),
    )


def _target(*, parent_message_id: int) -> MattermostChatTarget:
    return MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=parent_message_id,
        persona_id=456,
        mapping=MagicMock(),
    )


def _packets(*, message_id: int, answer: str) -> Iterator[AnswerStreamPart]:
    return iter(
        [
            MessageResponseIDInfo(
                user_message_id=message_id - 1,
                reserved_assistant_message_id=message_id,
            ),
            _packet(AgentResponseStart(final_documents=[_search_doc()])),
            _packet(AgentResponseDelta(content=answer)),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )


def _packet(obj: PacketObj) -> Packet:
    return Packet(placement=Placement(turn_index=0), obj=obj)


def _search_doc() -> SearchDoc:
    return SearchDoc(
        document_id="doc-1",
        chunk_ind=0,
        semantic_identifier="Mattermost Doc",
        link="https://example.test/doc",
        blurb="",
        source_type=DocumentSource.WEB,
        boost=0,
        hidden=False,
        metadata={},
        score=1.0,
        match_highlights=[],
        updated_at=None,
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.created_posts: list[dict[str, object]] = []
        self.updated_posts: list[dict[str, object]] = []

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        _ = pending_post_id, props
        post_id = f"bot-post-{len(self.created_posts) + 1}"
        self.created_posts.append(
            {"channel_id": channel_id, "root_id": root_id, "message": message}
        )
        return MattermostPost(
            id=post_id,
            message=message,
            root_id=root_id,
            user_id=_BOT_USER_ID,
            channel_id=channel_id,
        )

    async def create_ephemeral_post(
        self,
        *,
        user_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        _ = user_id, channel_id, message, root_id, props
        raise AssertionError("unexpected ephemeral post")

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None:
        _ = channel_id, pending_post_id, event_key
        return None

    async def update_post(
        self,
        *,
        post_id: str,
        message: str,
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        _ = props
        self.updated_posts.append({"post_id": post_id, "message": message})
        return MattermostPost(
            id=post_id,
            message=message,
            root_id="root-post-1",
            user_id=_BOT_USER_ID,
            channel_id="channel-1",
        )

    async def get_file_info(self, file_id: str) -> MattermostFileInfo:
        raise AssertionError(f"unexpected file-info request: {file_id}")

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        raise AssertionError(f"unexpected user-info request: {user_id}")

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        _ = channel_id, user_id
        return True

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        _ = root_post_id
        return []

    async def download_file(self, file_id: str) -> bytes:
        raise AssertionError(f"unexpected file download: {file_id}")


class _ReminderToken:
    def get_value(self, *, apply_mask: bool) -> str:
        _ = apply_mask
        return "mattermost-token"


@contextmanager
def _reminder_session_context() -> Iterator[object]:
    yield object()


class _ReminderExecutionClient:
    def __init__(
        self,
        *,
        memberships: list[bool],
        create_error: Exception | None = None,
    ) -> None:
        self.memberships = memberships
        self.create_error = create_error
        self.created_posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _ReminderExecutionClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        _ = channel_id, user_id
        return self.memberships.pop(0)

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        return [
            MattermostPost(id=root_post_id, channel_id="channel-1"),
            MattermostPost(
                id="answer-post-1",
                root_id=root_post_id,
                channel_id="channel-1",
            ),
            *[
                MattermostPost(
                    id=str(post["id"]),
                    message=str(post["message"]),
                    root_id=str(post["root_id"]),
                    channel_id=str(post["channel_id"]),
                    pending_post_id=str(post["pending_post_id"]),
                    props=cast(dict[str, object], post["props"]),
                )
                for post in self.created_posts
            ],
        ]

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None:
        for post in self.created_posts:
            props = post["props"]
            if (
                post["pending_post_id"] == pending_post_id
                or props["onyx_event_key"] == event_key
            ):
                return MattermostPost(
                    id=str(post["id"]),
                    message=str(post["message"]),
                    root_id=str(post["root_id"]),
                    channel_id=channel_id,
                    pending_post_id=str(post["pending_post_id"]),
                    props=props,
                )
        return None

    async def create_post(self, **kwargs: Any) -> MattermostPost:
        post = {"id": f"reminder-post-{len(self.created_posts) + 1}", **kwargs}
        self.created_posts.append(post)
        if self.create_error is not None:
            raise self.create_error
        return MattermostPost(
            id=str(post["id"]),
            message=str(post["message"]),
            root_id=str(post["root_id"]),
            channel_id=str(post["channel_id"]),
            pending_post_id=str(post["pending_post_id"]),
            props=cast(dict[str, object], post["props"]),
        )
