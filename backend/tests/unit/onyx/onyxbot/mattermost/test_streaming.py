from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from onyx.chat.models import StreamingError
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.models import MattermostPost
from onyx.onyxbot.mattermost.streaming import (
    MATTERMOST_STREAM_FAILURE_SUFFIX,
    MattermostStreamResult,
    MattermostStreamVisibleError,
    stream_mattermost_answer,
)
from onyx.server.query_and_chat.models import MessageResponseIDInfo
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    AgentResponseStart,
    CitationInfo,
    Packet,
    PacketObj,
    Placement,
)


@pytest.mark.asyncio
async def test_stream_mattermost_answer_updates_one_rooted_post_with_final_citations() -> (
    None
):
    client = _RecordingClient()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseStart(final_documents=[_search_doc()])),
            _packet(AgentResponseDelta(content="Use ")),
            _packet(AgentResponseDelta(content="this [1].")),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        packets=packets,
        min_update_chars=100,
    )

    assert result == MattermostStreamResult(message_id=22, post_id="bot-post-1")
    assert client.created_posts == [
        {"channel_id": "channel-1", "root_id": "root-post-1", "message": "..."}
    ]
    assert client.updated_posts == [
        {
            "post_id": "bot-post-1",
            "message": "Use this [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
        }
    ]


@pytest.mark.asyncio
async def test_stream_mattermost_answer_rate_bounds_partial_updates() -> None:
    client = _RecordingClient()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="one ")),
            _packet(AgentResponseDelta(content="two ")),
            _packet(AgentResponseDelta(content="three")),
        ]
    )

    await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        packets=packets,
        min_update_chars=8,
    )

    assert client.updated_posts == [
        {"post_id": "bot-post-1", "message": "one two "},
        {"post_id": "bot-post-1", "message": "one two three"},
    ]


@pytest.mark.asyncio
async def test_stream_mattermost_answer_failure_updates_existing_post_once() -> None:
    client = _RecordingClient()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="partial answer")),
            StreamingError(error="model failed"),
        ]
    )

    with pytest.raises(MattermostStreamVisibleError, match="model failed"):
        await stream_mattermost_answer(
            client=client,
            channel_id="channel-1",
            root_id="root-post-1",
            packets=packets,
            min_update_chars=100,
        )

    assert len(client.created_posts) == 1
    assert client.updated_posts == [
        {
            "post_id": "bot-post-1",
            "message": "partial answer\n\n" + MATTERMOST_STREAM_FAILURE_SUFFIX,
        }
    ]


@pytest.mark.asyncio
async def test_handle_normalized_event_streams_and_records_parent_message() -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.models import (
        MattermostNormalizedEventType,
        NormalizedMattermostEvent,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    db_session = MagicMock()
    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:root-post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="user-1",
        text="what changed?",
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="Onyx answer")),
        ]
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=target,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000456"),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_persona_by_id",
            return_value=MagicMock(id=456),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.handle_stream_message_objects",
            return_value=packets,
        ) as mock_handle_stream,
        patch(
            "onyx.onyxbot.mattermost.handler.update_mattermost_thread_parent_message"
        ) as mock_update_parent,
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=db_session,
        )

    assert handled is True
    stream_request = mock_handle_stream.call_args.kwargs["new_msg_req"]
    assert stream_request.message == "what changed?"
    assert client.created_posts == [
        {"channel_id": "channel-1", "root_id": "root-post-1", "message": "..."}
    ]
    assert client.updated_posts == [{"post_id": "bot-post-1", "message": "Onyx answer"}]
    mock_update_parent.assert_called_once_with(
        db_session=db_session,
        mapping=target.mapping,
        parent_message_id=22,
    )


@pytest.mark.asyncio
async def test_handle_normalized_event_does_not_duplicate_visible_stream_failure() -> (
    None
):
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.models import (
        MattermostNormalizedEventType,
        NormalizedMattermostEvent,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    db_session = MagicMock()
    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:root-post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="user-1",
        text="what changed?",
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="partial answer")),
            StreamingError(error="model failed"),
        ]
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=target,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000456"),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_persona_by_id",
            return_value=MagicMock(id=456),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.handle_stream_message_objects",
            return_value=packets,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.update_mattermost_thread_parent_message"
        ) as mock_update_parent,
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=db_session,
        )

    assert handled is True
    assert client.created_posts == [
        {"channel_id": "channel-1", "root_id": "root-post-1", "message": "..."}
    ]
    assert client.updated_posts == [
        {
            "post_id": "bot-post-1",
            "message": "partial answer\n\n" + MATTERMOST_STREAM_FAILURE_SUFFIX,
        }
    ]
    mock_update_parent.assert_not_called()


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
        self.created_posts.append(
            {"channel_id": channel_id, "root_id": root_id, "message": message}
        )
        return MattermostPost(
            id="bot-post-1",
            message=message,
            root_id=root_id,
            user_id="bot-user-1",
            channel_id=channel_id,
        )

    async def update_post(self, *, post_id: str, message: str) -> MattermostPost:
        self.updated_posts.append({"post_id": post_id, "message": message})
        return MattermostPost(
            id=post_id,
            message=message,
            root_id="root-post-1",
            user_id="bot-user-1",
            channel_id="channel-1",
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
