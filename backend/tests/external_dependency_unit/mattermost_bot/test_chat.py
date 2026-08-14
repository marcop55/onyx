from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from onyx.chat.models import ChatBasicResponse
from onyx.configs.constants import MATTERMOST_SERVICE_ACCOUNT_EMAIL, DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.db.models import User
from onyx.db.users import get_or_create_mattermost_service_account
from onyx.onyxbot.mattermost.handler import (
    MATTERMOST_FAILURE_MESSAGE,
    MattermostHandlerConfig,
    format_mattermost_answer,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.session import MattermostChatTarget
from onyx.server.query_and_chat.models import MessageResponseIDInfo
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    CitationInfo,
    Packet,
    Placement,
)


@pytest.mark.asyncio
async def test_root_mention_creates_chat_with_configured_persona_and_posts_answer(
) -> None:
    db_session = MagicMock()
    event = _event(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        post_id="post-root-1",
        root_post_id="post-root-1",
        text="what changed?",
    )
    client = MagicMock()
    client.create_post = AsyncMock()
    client.create_post.return_value.id = "bot-post-1"
    client.update_post = AsyncMock()
    config = MattermostHandlerConfig(persona_id=456)
    service_user = MagicMock()
    service_user.id = UUID("00000000-0000-0000-0000-000000000456")
    target = _target()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            Packet(
                placement=Placement(turn_index=0),
                obj=AgentResponseDelta(content="Onyx answer"),
            ),
        ]
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=target,
        ) as mock_get_target,
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=service_user,
        ) as mock_get_service_user,
        patch("onyx.onyxbot.mattermost.handler.get_persona_by_id") as mock_get_persona,
        patch(
            "onyx.onyxbot.mattermost.handler.handle_stream_message_objects",
            return_value=packets,
        ) as mock_handle_stream,
        patch(
            "onyx.onyxbot.mattermost.handler.update_mattermost_thread_parent_message"
        ) as mock_update_parent,
    ):
        mock_get_persona.return_value = MagicMock(id=456)

        handled = await handle_normalized_mattermost_event(
            event=event,
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    mock_get_target.assert_called_once_with(
        db_session=db_session,
        event=event,
        persona_id=456,
        onyx_user_id=None,
    )
    mock_get_service_user.assert_called_once_with(db_session)
    mock_get_persona.assert_called_once_with(
        persona_id=456,
        user=None,
        db_session=db_session,
        is_for_edit=False,
    )
    stream_request = mock_handle_stream.call_args.kwargs["new_msg_req"]
    assert stream_request.message == "what changed?"
    assert stream_request.chat_session_id == UUID("00000000-0000-0000-0000-000000000001")
    assert stream_request.parent_message_id == 11
    assert stream_request.origin.value == "mattermostbot"
    client.create_post.assert_awaited_once_with(
        channel_id="channel-1",
        root_id="post-root-1",
        message="...",
    )
    client.update_post.assert_awaited_once_with(
        post_id=client.create_post.return_value.id,
        message="Onyx answer",
    )
    mock_update_parent.assert_called_once()
    assert mock_update_parent.call_args.kwargs["parent_message_id"] == 22


@pytest.mark.asyncio
async def test_reply_continues_existing_parent_message() -> None:
    db_session = MagicMock()
    event = _event(
        event_type=MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP,
        post_id="post-reply-1",
        root_post_id="post-root-1",
        text="can you expand?",
    )
    client = MagicMock()
    client.create_post = AsyncMock()
    client.create_post.return_value.id = "bot-post-1"
    client.update_post = AsyncMock()
    config = MattermostHandlerConfig(persona_id=456)
    service_user = MagicMock()
    service_user.id = UUID("00000000-0000-0000-0000-000000000456")
    target = _target()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=33),
            Packet(
                placement=Placement(turn_index=0),
                obj=AgentResponseDelta(content="Followup answer"),
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
            return_value=service_user,
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
        ),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    stream_request = mock_handle_stream.call_args.kwargs["new_msg_req"]
    assert stream_request.chat_session_id == UUID("00000000-0000-0000-0000-000000000001")
    assert stream_request.parent_message_id == 11
    assert stream_request.message == "can you expand?"


@pytest.mark.asyncio
async def test_failure_posts_safe_thread_message() -> None:
    db_session = MagicMock()
    event = _event(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        post_id="post-root-1",
        root_post_id="post-root-1",
        text="please fail",
    )
    client = MagicMock()
    client.create_post = AsyncMock()
    config = MattermostHandlerConfig(persona_id=456)

    with patch(
        "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
        side_effect=RuntimeError("provider secret missing"),
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    client.create_post.assert_awaited_once_with(
        channel_id="channel-1",
        root_id="post-root-1",
        message=MATTERMOST_FAILURE_MESSAGE,
    )


def test_format_mattermost_answer_preserves_citations() -> None:
    answer = _answer(
        message_id=44,
        answer="Use this [1].",
        citation_info=[CitationInfo(citation_number=1, document_id="doc-1")],
        top_documents=[_search_doc(document_id="doc-1")],
    )

    formatted = format_mattermost_answer(answer)

    assert formatted == "Use this [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc"


def test_create_mattermost_service_account() -> None:
    db_session = MagicMock()
    with (
        patch("onyx.db.users.get_user_by_email", return_value=None),
        patch("onyx.db.users._generate_password_hash", return_value="hash"),
    ):
        created = get_or_create_mattermost_service_account(db_session)

    assert created.email == MATTERMOST_SERVICE_ACCOUNT_EMAIL
    assert created.hashed_password == "hash"
    db_session.add.assert_called_once_with(created)
    db_session.commit.assert_called_once_with()


def test_reuse_mattermost_service_account() -> None:
    db_session = MagicMock()
    existing = MagicMock()
    with patch("onyx.db.users.get_user_by_email", return_value=existing):
        resolved = get_or_create_mattermost_service_account(db_session)

    assert resolved is existing
    db_session.add.assert_not_called()
    db_session.commit.assert_not_called()


def test_session_target_uses_root_thread_and_parent_message() -> None:
    db_session = MagicMock()
    user = MagicMock(spec=User)
    user.id = UUID("00000000-0000-0000-0000-000000000123")
    event = _event(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        post_id="post-root-db",
        root_post_id="post-root-db",
        user_id="mattermost-user-db",
        text="hello",
    )

    from onyx.onyxbot.mattermost.session import get_or_create_mattermost_chat_target

    mapping = MagicMock()
    mapping.chat_session_id = UUID("00000000-0000-0000-0000-000000000321")
    mapping.parent_message_id = 77
    mapping.persona_id = 456
    with patch(
        "onyx.onyxbot.mattermost.session.get_or_create_mattermost_thread_mapping",
        return_value=mapping,
    ) as mock_get_mapping:
        target = get_or_create_mattermost_chat_target(
            db_session=db_session,
            event=event,
            persona_id=456,
            onyx_user_id=user.id,
        )

    mock_get_mapping.assert_called_once_with(
        db_session=db_session,
        server_id="team-1",
        channel_id="channel-1",
        root_id="post-root-db",
        mattermost_user_id="mattermost-user-db",
        persona_id=456,
        onyx_user_id=user.id,
    )
    assert target.chat_session_id == UUID("00000000-0000-0000-0000-000000000321")
    assert target.parent_message_id == 77
    assert target.persona_id == 456


def _event(
    *,
    event_type: MattermostNormalizedEventType,
    post_id: str,
    root_post_id: str,
    text: str,
    user_id: str = "user-1",
) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=event_type,
        session_key=f"mattermost:channel:team-1:channel-1:{root_post_id}",
        team_id="team-1",
        channel_id="channel-1",
        post_id=post_id,
        root_post_id=root_post_id,
        user_id=user_id,
        text=text,
        raw_event_type="posted",
    )


def _target() -> MattermostChatTarget:
    return MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(),
    )


def _answer(
    *,
    message_id: int,
    answer: str,
    citation_info: list[CitationInfo] | None = None,
    top_documents: list[SearchDoc] | None = None,
) -> ChatBasicResponse:
    return ChatBasicResponse(
        answer=answer,
        answer_citationless=answer,
        top_documents=top_documents or [],
        error_msg=None,
        message_id=message_id,
        citation_info=citation_info or [],
    )


def _search_doc(*, document_id: str) -> SearchDoc:
    return SearchDoc(
        document_id=document_id,
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
