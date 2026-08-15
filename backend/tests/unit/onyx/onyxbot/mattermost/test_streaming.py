from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from onyx.chat.models import ChatBasicResponse, StreamingError
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import (
    MattermostFileInfo,
    MattermostPost,
    MattermostUserInfo,
)
from onyx.onyxbot.mattermost.streaming import (
    MATTERMOST_NO_CITATIONS_MESSAGE,
    MATTERMOST_STREAM_FAILURE_SUFFIX,
    MattermostLeaseLostError,
    MattermostStreamResult,
    MattermostStreamVisibleError,
    stream_mattermost_answer,
)
from onyx.server.query_and_chat.models import MessageResponseIDInfo, SendMessageRequest
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    AgentResponseStart,
    CitationInfo,
    Packet,
    PacketObj,
    Placement,
)


def test_public_chat_request_cannot_claim_transport_idempotency_key() -> None:
    request = SendMessageRequest.model_validate(
        {
            "message": "hello",
            "external_idempotency_key": "mattermost:event:preclaim",
        }
    )

    assert "external_idempotency_key" not in request.model_dump()
    assert not hasattr(request, "external_idempotency_key")


def test_recovered_keyed_turn_never_runs_provider_or_tools() -> None:
    from onyx.chat.process_message import _stream_chat_turn

    response = ChatBasicResponse(
        answer="recovered answer",
        answer_citationless="recovered answer",
        top_documents=[],
        error_msg=None,
        message_id=22,
        citation_info=[],
    )
    setup = MagicMock()
    setup.recovered_response = response
    setup.chat_session.id = UUID("00000000-0000-0000-0000-000000000001")
    setup.cache = MagicMock()

    def _build_turn(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield MessageResponseIDInfo(
            user_message_id=10, reserved_assistant_message_id=22
        )
        return setup

    @contextmanager
    def _session():  # type: ignore[no-untyped-def]
        yield MagicMock()

    request = MagicMock(mock_llm_response=None, internal_search_filters=None)
    user = MagicMock(is_anonymous=True)
    with (
        patch(
            "onyx.chat.process_message.get_session_with_current_tenant",
            side_effect=_session,
        ),
        patch("onyx.chat.process_message.build_chat_turn", side_effect=_build_turn),
        patch("onyx.chat.process_message._run_models") as run_models,
        patch("onyx.chat.process_message.set_processing_status"),
    ):
        packets = list(
            _stream_chat_turn(
                new_msg_req=request,
                user=user,
                external_idempotency_key="mattermost:event:1",
            )
        )

    assert packets[-1] == response
    run_models.assert_not_called()


@pytest.mark.asyncio
async def test_stream_mattermost_answer_preserves_recovered_keyed_response() -> None:
    client = _RecordingClient()
    recovered = ChatBasicResponse(
        answer="recovered answer [1]",
        answer_citationless="recovered answer",
        top_documents=[_search_doc()],
        error_msg=None,
        message_id=22,
        citation_info=[CitationInfo(citation_number=1, document_id="doc-1")],
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=iter(
            [
                MessageResponseIDInfo(
                    user_message_id=10, reserved_assistant_message_id=22
                ),
                recovered,
            ]
        ),
    )

    assert result == MattermostStreamResult(message_id=22, post_id="checkpointed-post")
    assert client.created_posts == []
    assert client.updated_posts == [
        {
            "post_id": "checkpointed-post",
            "message": "recovered answer [1]\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
        }
    ]


@pytest.mark.asyncio
async def test_stream_mattermost_answer_overrides_empty_channel_filter_results() -> (
    None
):
    client = _RecordingClient()

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=iter(
            [
                MessageResponseIDInfo(
                    user_message_id=10, reserved_assistant_message_id=22
                ),
                ChatBasicResponse(
                    answer="hallucinated answer",
                    answer_citationless="hallucinated answer",
                    top_documents=[],
                    error_msg=None,
                    message_id=22,
                    citation_info=[],
                ),
            ]
        ),
        no_results_message="No indexed data found for #town-square.",
    )

    assert result == MattermostStreamResult(message_id=22, post_id="checkpointed-post")
    assert client.updated_posts == [
        {
            "post_id": "checkpointed-post",
            "message": "No indexed data found for #town-square.",
        }
    ]


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
async def test_stream_existing_post_checkpoints_render_before_final_put() -> None:
    client = _RecordingClient()
    checkpoints: list[tuple[str, int]] = []
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="final answer")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=packets,
        min_update_chars=100,
        checkpoint_final=lambda message, message_id: checkpoints.append(
            (message, message_id)
        ),
    )

    assert result == MattermostStreamResult(message_id=22, post_id="checkpointed-post")
    assert client.created_posts == []
    assert checkpoints == [("final answer", 22)]
    assert client.updated_posts == [
        {"post_id": "checkpointed-post", "message": "final answer"}
    ]


@pytest.mark.asyncio
async def test_stream_answer_filter_replaces_uncited_answer_with_no_citations_message() -> (
    None
):
    client = _RecordingClient()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="uncited answer")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        packets=packets,
        min_update_chars=100,
        require_citations=True,
    )

    assert result == MattermostStreamResult(message_id=22, post_id="bot-post-1")
    assert client.updated_posts == [
        {"post_id": "bot-post-1", "message": MATTERMOST_NO_CITATIONS_MESSAGE}
    ]


@pytest.mark.asyncio
async def test_stream_answer_filter_requires_citation_to_linked_source() -> None:
    client = _RecordingClient()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseStart(final_documents=[_search_doc(link="")])),
            _packet(AgentResponseDelta(content="looks sourced [1].")),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        packets=packets,
        min_update_chars=100,
        require_citations=True,
    )

    assert result == MattermostStreamResult(message_id=22, post_id="bot-post-1")
    assert client.updated_posts == [
        {"post_id": "bot-post-1", "message": MATTERMOST_NO_CITATIONS_MESSAGE}
    ]


@pytest.mark.asyncio
async def test_stream_answer_filter_suppresses_unsourced_partial_updates() -> None:
    client = _RecordingClient()
    unsourced_answer = "uncited answer " * 8
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content=unsourced_answer)),
        ]
    )

    await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        packets=packets,
        min_update_chars=20,
        require_citations=True,
    )

    assert client.updated_posts == [
        {"post_id": "bot-post-1", "message": MATTERMOST_NO_CITATIONS_MESSAGE}
    ]
    assert unsourced_answer.strip() not in {
        update["message"] for update in client.updated_posts
    }


@pytest.mark.asyncio
async def test_stream_over_limit_answer_emits_sequential_posts_with_sources_once() -> (
    None
):
    client = _RecordingClient()
    answer = "first cited paragraph [1].\n\n" + ("second paragraph " * 12)
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseStart(final_documents=[_search_doc()])),
            _packet(AgentResponseDelta(content=answer)),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=packets,
        min_update_chars=10_000,
        max_part_chars=90,
    )

    assert result.message_id == 22
    assert result.post_id == "checkpointed-post"
    assert result.post_ids == (
        "checkpointed-post",
        "bot-post-1",
        "bot-post-2",
        "bot-post-3",
    )
    delivered_messages = [str(update["message"]) for update in client.updated_posts] + [
        str(post["message"]) for post in client.created_posts
    ]
    assert all(len(message) <= 90 for message in delivered_messages)
    assert "".join(delivered_messages).count("Sources:") == 1
    assert client.updated_posts == [
        {"post_id": "checkpointed-post", "message": "first cited paragraph [1]."}
    ]
    assert client.created_posts == [
        {
            "channel_id": "channel-1",
            "root_id": "root-post-1",
            "message": "second paragraph second paragraph second paragraph second paragraph second paragraph",
            "pending_post_id": "checkpointed-post:part:2",
            "props": {"onyx_event_key": "checkpointed-post:part:2"},
        },
        {
            "channel_id": "channel-1",
            "root_id": "root-post-1",
            "message": "second paragraph second paragraph second paragraph second paragraph second paragraph",
            "pending_post_id": "checkpointed-post:part:3",
            "props": {"onyx_event_key": "checkpointed-post:part:3"},
        },
        {
            "channel_id": "channel-1",
            "root_id": "root-post-1",
            "message": "second paragraph second paragraph\n\nSources:\n[1] Mattermost Doc - https://example.test/doc",
            "pending_post_id": "checkpointed-post:part:4",
            "props": {"onyx_event_key": "checkpointed-post:part:4"},
        },
    ]


@pytest.mark.asyncio
async def test_stream_over_limit_source_only_parts_remain_bounded() -> None:
    client = _RecordingClient()
    max_part_chars = 72
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(
                AgentResponseStart(
                    final_documents=[
                        _search_doc(
                            semantic_identifier="long-source-name-without-spaces" * 3,
                            link="https://example.test/" + ("path" * 20),
                            blurb="preview " * 20,
                        )
                    ]
                )
            ),
            _packet(AgentResponseDelta(content="short cited [1].")),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=packets,
        min_update_chars=10_000,
        include_source_previews=True,
        max_part_chars=max_part_chars,
    )

    delivered_messages = [str(update["message"]) for update in client.updated_posts] + [
        str(post["message"]) for post in client.created_posts
    ]
    assert result.post_ids
    assert all(len(message) <= max_part_chars for message in delivered_messages)
    assert "".join(delivered_messages).count("Sources:") == 1
    assert "long-source-name" in "".join(delivered_messages)


@pytest.mark.asyncio
async def test_stream_over_limit_replay_reuses_existing_split_post() -> None:
    client = _RecordingClient()
    client.reconciled_post = MattermostPost(
        id="existing-part-2",
        message="Sources:\n[1] Mattermost Doc - https://example.test/doc",
        root_id="root-post-1",
        user_id="bot-user-1",
        channel_id="channel-1",
        pending_post_id="checkpointed-post:part:2",
        props={"onyx_event_key": "checkpointed-post:part:2"},
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseStart(final_documents=[_search_doc()])),
            _packet(AgentResponseDelta(content="short cited [1].")),
            _packet(CitationInfo(citation_number=1, document_id="doc-1")),
        ]
    )

    result = await stream_mattermost_answer(
        client=client,
        channel_id="channel-1",
        root_id="root-post-1",
        post_id="checkpointed-post",
        packets=packets,
        min_update_chars=10_000,
        max_part_chars=70,
    )

    assert result.post_ids == ("checkpointed-post", "existing-part-2")
    assert client.created_posts == []
    assert client.reconciliation_requests == [
        {
            "channel_id": "channel-1",
            "pending_post_id": "checkpointed-post:part:2",
            "event_key": "checkpointed-post:part:2",
        }
    ]


@pytest.mark.asyncio
async def test_stream_stops_before_partial_update_when_owner_fence_is_lost() -> None:
    client = _RecordingClient()
    checks = iter([True, True, False])
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(AgentResponseDelta(content="first update")),
            _packet(AgentResponseDelta(content=" must be fenced")),
        ]
    )

    with pytest.raises(MattermostStreamVisibleError, match="lease"):
        await stream_mattermost_answer(
            client=client,
            channel_id="channel-1",
            root_id="root-post-1",
            packets=packets,
            min_update_chars=1,
            before_external_update=lambda: next(checks),
        )

    assert client.updated_posts == [
        {"post_id": "bot-post-1", "message": "first update"}
    ]


@pytest.mark.asyncio
async def test_missing_message_id_failure_is_fenced_before_external_put() -> None:
    client = _RecordingClient()

    with pytest.raises(MattermostLeaseLostError, match="lease"):
        await stream_mattermost_answer(
            client=client,
            channel_id="channel-1",
            root_id="root-post-1",
            post_id="existing-post",
            packets=iter([]),
            before_external_update=lambda: False,
        )

    assert client.updated_posts == []


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


@pytest.mark.parametrize("ambiguous_create", [False, True])
@pytest.mark.asyncio
async def test_handle_normalized_event_streams_and_records_parent_message(
    ambiguous_create: bool,
) -> None:
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
    if ambiguous_create:
        client.create_error = MattermostClientError("response lost after remote commit")
        client.reconciled_post_after_create_error = MattermostPost(
            id="bot-post-1",
            message="...",
            root_id="root-post-1",
            user_id="bot-user-1",
            channel_id="channel-1",
        )
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
        dedupe_key="event_id:root-post-1",
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(),
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
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=db_session,
        )

    assert handled is True
    stream_request = mock_handle_stream.call_args.kwargs["new_msg_req"]
    assert stream_request.message == "what changed?"
    assert (
        mock_handle_stream.call_args.kwargs["external_idempotency_key"]
        == "mattermost:event:1"
    )
    assert client.created_posts == [_checkpointed_placeholder_post()]
    assert client.updated_posts == [{"post_id": "bot-post-1", "message": "Onyx answer"}]
    mock_complete.assert_called_once()


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
        dedupe_key="event_id:root-post-1",
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(),
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
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=db_session,
        )

    assert handled is True
    assert client.created_posts == [_checkpointed_placeholder_post()]
    assert client.updated_posts == [
        {
            "post_id": "bot-post-1",
            "message": "partial answer\n\n" + MATTERMOST_STREAM_FAILURE_SUFFIX,
        }
    ]
    mock_complete.assert_not_called()


def test_turn_checkpoint_lease_loss_uses_fenced_error() -> None:
    from onyx.onyxbot.mattermost.handler import _checkpoint_mattermost_turn_packets

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_turn",
            return_value=False,
        ),
        pytest.raises(MattermostLeaseLostError, match="creating turn"),
    ):
        list(
            _checkpoint_mattermost_turn_packets(
                packets=iter(
                    [
                        MessageResponseIDInfo(
                            user_message_id=10,
                            reserved_assistant_message_id=22,
                        )
                    ]
                ),
                db_session=MagicMock(),
                event_id=1,
                claim_owner=UUID("00000000-0000-0000-0000-000000000999"),
            )
        )


@pytest.mark.parametrize(
    "stream_error",
    [
        MattermostLeaseLostError("lease lost"),
        MattermostClientError("final update outcome ambiguous"),
    ],
)
@pytest.mark.asyncio
async def test_fenced_or_transport_stream_failure_never_emits_fallback_post(
    stream_error: Exception,
) -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.models import (
        MattermostNormalizedEventType,
        NormalizedMattermostEvent,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
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
        dedupe_key="event_id:root-post-1",
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(mattermost_post_id="bot-post-1"),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.stream_mattermost_answer",
            side_effect=stream_error,
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is False
    assert client.created_posts == []
    assert client.updated_posts == []


@pytest.mark.parametrize("reconciliation_error", [False, True])
@pytest.mark.asyncio
async def test_ambiguous_post_replay_never_issues_second_post_after_search_miss(
    reconciliation_error: bool,
) -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.models import (
        MattermostNormalizedEventType,
        NormalizedMattermostEvent,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    if reconciliation_error:
        client.reconciliation_error = MattermostClientError("search transport failed")
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
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
        dedupe_key="event_id:root-post-1",
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim("post_create_attempted"),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.renew_mattermost_event_lease",
            return_value=True,
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is False
    assert len(client.reconciliation_requests) == 1
    assert client.created_posts == []
    assert client.updated_posts == []


@pytest.mark.asyncio
async def test_unconfirmed_typed_mutation_attaches_confirmation_button_for_fresh_system_admin() -> (
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
    from onyx.onyxbot.mattermost.mutations import MATTERMOST_MUTATION_COMMAND_PREFIX
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    command = (
        MATTERMOST_MUTATION_COMMAND_PREFIX
        + '{"action":"update","repo_id":"repo-1","path":"/automation/note.md",'
        '"expected_revision":"rev-1","content":"new content",'
        '"destination_path":null,"confirmed":false,"scope_prefix":"/automation"}'
    )
    client = _RecordingClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.DIRECT_MESSAGE,
        session_key="mattermost:dm:team-1:channel-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="user-1",
        text=command,
        dedupe_key="event_id:root-post-1",
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(
                AgentResponseDelta(content="Review and confirm this admin action.")
            ),
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(),
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
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(
                persona_id=456,
                mutation_adapter=cast(Any, object()),
                interactive_signing_secret="secret",
                interactive_url="http://127.0.0.1:8091/interactive",
                bot_user_id="bot-user-1",
            ),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    confirm_actions = _confirm_mutation_actions(client.updated_posts[0])
    assert len(confirm_actions) == 1
    assert cast(dict[str, object], confirm_actions[0]["integration"])["url"] == (
        "http://127.0.0.1:8091/interactive"
    )
    assert client.membership_calls == [
        ("channel-1", "bot-user-1"),
        ("channel-1", "user-1"),
    ]
    assert client.identity_calls == ["user-1"]


@pytest.mark.asyncio
async def test_rendered_message_replay_restores_interactive_props_before_completion() -> (
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
    from onyx.onyxbot.mattermost.mutations import MATTERMOST_MUTATION_COMMAND_PREFIX
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    command = (
        MATTERMOST_MUTATION_COMMAND_PREFIX
        + '{"action":"update","repo_id":"repo-1","path":"/automation/note.md",'
        '"expected_revision":"rev-1","content":"new content",'
        '"destination_path":null,"confirmed":false,"scope_prefix":"/automation"}'
    )
    client = _RecordingClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.DIRECT_MESSAGE,
        session_key="mattermost:dm:team-1:channel-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="user-1",
        text=command,
        dedupe_key="event_id:root-post-1",
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(
                state="turn_created",
                mattermost_post_id="bot-post-1",
                onyx_assistant_message_id=22,
                rendered_message="Review and confirm this admin action.",
            ),
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_answer_event",
            return_value=True,
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(
                persona_id=456,
                mutation_adapter=cast(Any, object()),
                interactive_signing_secret="secret",
                interactive_url="http://127.0.0.1:8091/interactive",
                bot_user_id="bot-user-1",
            ),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    assert len(client.updated_posts) == 1
    confirm_actions = _confirm_mutation_actions(client.updated_posts[0])
    assert len(confirm_actions) == 1
    integration = cast(dict[str, object], confirm_actions[0]["integration"])
    assert integration["url"] == "http://127.0.0.1:8091/interactive"
    assert cast(dict[str, object], integration["context"])["action_value"]
    assert client.membership_calls == [
        ("channel-1", "bot-user-1"),
        ("channel-1", "user-1"),
    ]
    assert client.identity_calls == ["user-1"]


@pytest.mark.parametrize(
    "memberships,identity",
    [
        (
            [True, True],
            MattermostUserInfo("user-1", "ordinary", "Ordinary", "system_user"),
        ),
        (
            [True, True],
            MattermostUserInfo("user-1", "stale", "Stale", "system_administer"),
        ),
        ([True, True], MattermostClientError("role lookup failed")),
        (
            [True, False],
            MattermostUserInfo("user-1", "admin", "Admin", "system_admin"),
        ),
        ([False], MattermostUserInfo("user-1", "admin", "Admin", "system_admin")),
    ],
)
@pytest.mark.asyncio
async def test_unconfirmed_typed_mutation_omits_confirmation_without_fresh_system_admin(
    memberships: list[bool], identity: MattermostUserInfo | Exception
) -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.models import (
        MattermostNormalizedEventType,
        NormalizedMattermostEvent,
    )
    from onyx.onyxbot.mattermost.mutations import MATTERMOST_MUTATION_COMMAND_PREFIX
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    command = (
        MATTERMOST_MUTATION_COMMAND_PREFIX
        + '{"action":"update","repo_id":"repo-1","path":"/automation/note.md",'
        '"expected_revision":"rev-1","content":"new content",'
        '"destination_path":null,"confirmed":false,"scope_prefix":"/automation"}'
    )
    client = _RecordingClient(memberships=memberships, identity=identity)
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.DIRECT_MESSAGE,
        session_key="mattermost:dm:team-1:channel-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="user-1",
        text=command,
        dedupe_key="event_id:root-post-1",
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            _packet(
                AgentResponseDelta(content="Review and confirm this admin action.")
            ),
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
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=_processing_claim(),
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
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(
                persona_id=456,
                mutation_adapter=cast(Any, object()),
                interactive_signing_secret="secret",
                interactive_url="http://127.0.0.1:8091/interactive",
                bot_user_id="bot-user-1",
            ),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    assert _confirm_mutation_actions(client.updated_posts[0]) == []


def _confirm_mutation_actions(
    updated_post: dict[str, object],
) -> list[dict[str, object]]:
    actions = cast(
        list[dict[str, object]],
        cast(
            list[dict[str, object]],
            cast(dict[str, object], updated_post["props"])["attachments"],
        )[0]["actions"],
    )
    return [action for action in actions if action["id"] == "confirm_mutation"]


class _RecordingClient:
    def __init__(
        self,
        *,
        memberships: list[bool] | None = None,
        identity: MattermostUserInfo | Exception | None = None,
    ) -> None:
        self.created_posts: list[dict[str, object]] = []
        self.updated_posts: list[dict[str, object]] = []
        self.reconciliation_requests: list[dict[str, str]] = []
        self.memberships = memberships or [True, True]
        self.identity = identity
        self.membership_calls: list[tuple[str, str]] = []
        self.identity_calls: list[str] = []
        self.create_error: MattermostClientError | None = None
        self.reconciliation_error: MattermostClientError | None = None
        self.reconciled_post: MattermostPost | None = None
        self.reconciled_post_after_create_error: MattermostPost | None = None

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        created_post: dict[str, object] = {
            "channel_id": channel_id,
            "root_id": root_id,
            "message": message,
        }
        if pending_post_id is not None:
            created_post["pending_post_id"] = pending_post_id
        if props is not None:
            created_post["props"] = props
        self.created_posts.append(created_post)
        if self.create_error is not None:
            raise self.create_error
        post_id = f"bot-post-{len(self.created_posts)}"
        return MattermostPost(
            id=post_id,
            message=message,
            root_id=root_id,
            user_id="bot-user-1",
            channel_id=channel_id,
            pending_post_id=pending_post_id or "",
            props=props or {},
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
        _ = props
        return MattermostPost(
            id="ephemeral-post-1",
            message=message,
            root_id=root_id,
            user_id=user_id,
            channel_id=channel_id,
        )

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None:
        self.reconciliation_requests.append(
            {
                "channel_id": channel_id,
                "pending_post_id": pending_post_id,
                "event_key": event_key,
            }
        )
        if self.reconciliation_error is not None:
            raise self.reconciliation_error
        if self.created_posts and self.reconciled_post_after_create_error is not None:
            return self.reconciled_post_after_create_error
        return self.reconciled_post

    async def update_post(
        self,
        *,
        post_id: str,
        message: str,
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        updated_post: dict[str, object] = {"post_id": post_id, "message": message}
        if props is not None:
            updated_post["props"] = props
        self.updated_posts.append(updated_post)
        return MattermostPost(
            id=post_id,
            message=message,
            root_id="root-post-1",
            user_id="bot-user-1",
            channel_id="channel-1",
        )

    async def get_file_info(self, file_id: str) -> MattermostFileInfo:
        raise AssertionError(f"unexpected file-info request: {file_id}")

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        self.identity_calls.append(user_id)
        if self.identity is None:
            raise AssertionError(f"unexpected user-info request: {user_id}")
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_calls.append((channel_id, user_id))
        return self.memberships.pop(0)

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        _ = root_post_id
        return []

    async def download_file(self, file_id: str) -> bytes:
        raise AssertionError(f"unexpected file download: {file_id}")


def _processing_claim(
    state: str = "pending",
    mattermost_post_id: str | None = None,
    onyx_assistant_message_id: int | None = None,
    rendered_message: str | None = None,
) -> MattermostEventClaim:
    ledger_event = MattermostEventState(
        id=1,
        instance_id="instance-1",
        channel_id="channel-1",
        dedupe_key="event_id:root-post-1",
        event_type="channel_mention",
        source_post_id="root-post-1",
        mattermost_pending_post_id="pending-1",
        mattermost_post_id=mattermost_post_id,
        state=state,
        onyx_assistant_message_id=onyx_assistant_message_id,
        rendered_message=rendered_message,
    )
    claim_owner = UUID("00000000-0000-0000-0000-000000000999")
    return MattermostEventClaim(
        MattermostClaimOutcome.PROCESS, ledger_event, claim_owner
    )


def _checkpointed_placeholder_post() -> dict[str, object]:
    return {
        "channel_id": "channel-1",
        "root_id": "root-post-1",
        "message": "...",
        "pending_post_id": "pending-1",
        "props": {"onyx_event_key": "1"},
    }


def _packet(obj: PacketObj) -> Packet:
    return Packet(placement=Placement(turn_index=0), obj=obj)


def _search_doc(
    *,
    semantic_identifier: str = "Mattermost Doc",
    link: str = "https://example.test/doc",
    blurb: str = "",
) -> SearchDoc:
    return SearchDoc(
        document_id="doc-1",
        chunk_ind=0,
        semantic_identifier=semantic_identifier,
        link=link,
        blurb=blurb,
        source_type=DocumentSource.WEB,
        boost=0,
        hidden=False,
        metadata={},
        score=1.0,
        match_highlights=[],
        updated_at=None,
    )
