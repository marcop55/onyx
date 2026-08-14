from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from onyx.chat.models import AnswerStreamPart
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.handler import (
    MattermostHandlerConfig,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.listener import MattermostEventNormalizer
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostReaction,
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
    assert replayed_mention is None
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
            "onyx.onyxbot.mattermost.handler.update_mattermost_thread_parent_message"
        ) as mock_update_parent,
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
        call.kwargs["parent_message_id"] for call in mock_update_parent.call_args_list
    ] == [
        22,
        33,
        44,
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

    with patch(
        "onyx.onyxbot.mattermost.handler.create_chat_message_feedback"
    ) as mock_feedback:
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


def _listener_config(
    *,
    owned_answer_post_message_ids: dict[str, int] | None = None,
) -> MattermostListenerConfig:
    return MattermostListenerConfig(
        bot_user_id=_BOT_USER_ID,
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_team_ids=frozenset({"team-1"}),
        approved_user_ids=frozenset({_APPROVED_USER_ID}),
        owned_thread_root_ids={"root-post-1"},
        owned_answer_post_root_ids={"bot-answer-1": "root-post-1"},
        owned_answer_post_message_ids=owned_answer_post_message_ids or {},
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
        self.created_posts: list[dict[str, str]] = []
        self.updated_posts: list[dict[str, str]] = []

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
    ) -> MattermostPost:
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

    async def update_post(self, *, post_id: str, message: str) -> MattermostPost:
        self.updated_posts.append({"post_id": post_id, "message": message})
        return MattermostPost(
            id=post_id,
            message=message,
            root_id="root-post-1",
            user_id=_BOT_USER_ID,
            channel_id="channel-1",
        )
