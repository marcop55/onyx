from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from onyx.chat.models import ChatBasicResponse
from onyx.configs.constants import (
    MATTERMOST_SERVICE_ACCOUNT_EMAIL,
    DocumentSource,
    QAFeedbackType,
)
from onyx.context.search.models import SearchDoc
from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState, User
from onyx.db.users import get_or_create_mattermost_service_account
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.handler import (
    MATTERMOST_FAILURE_MESSAGE,
    MattermostHandlerConfig,
    _build_mattermost_context,
    _save_mattermost_attachments,
    format_mattermost_answer,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.models import (
    MattermostFileInfo,
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
async def test_root_mention_creates_chat_with_configured_persona_and_posts_answer() -> (
    None
):
    db_session = MagicMock()
    owned_thread_root_ids: set[str] = set()
    owned_answer_post_root_ids: dict[str, str] = {}
    owned_answer_post_message_ids: dict[str, int] = {}
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
    client.get_thread_posts = AsyncMock(return_value=[])
    config = MattermostHandlerConfig(
        persona_id=456,
        owned_thread_root_ids=owned_thread_root_ids,
        owned_answer_post_root_ids=owned_answer_post_root_ids,
        owned_answer_post_message_ids=owned_answer_post_message_ids,
    )
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
    assert stream_request.chat_session_id == UUID(
        "00000000-0000-0000-0000-000000000001"
    )
    assert stream_request.parent_message_id == 11
    assert stream_request.origin.value == "mattermostbot"
    assert mock_handle_stream.call_args.kwargs["user"] is service_user
    assert mock_handle_stream.call_args.kwargs["bypass_acl"] is False
    assert owned_thread_root_ids == {"post-root-1"}
    assert owned_answer_post_root_ids == {"bot-post-1": "post-root-1"}
    assert owned_answer_post_message_ids == {"bot-post-1": 22}
    client.create_post.assert_awaited_once_with(
        channel_id="channel-1",
        root_id="post-root-1",
        message="...",
        pending_post_id="pending-1",
        props={"onyx_event_key": "1"},
    )
    client.update_post.assert_awaited_once_with(
        post_id=client.create_post.return_value.id,
        message="Onyx answer",
    )
    mock_complete.assert_called_once()


@pytest.mark.asyncio
async def test_managed_channel_config_selects_agent_and_bounded_response_style() -> (
    None
):
    db_session = MagicMock()
    event = _event(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        post_id="post-root-1",
        root_post_id="post-root-1",
        text="summarise this with sources",
    )
    client = MagicMock()
    client.create_post = AsyncMock()
    client.create_post.return_value.id = "bot-post-1"
    client.update_post = AsyncMock()
    client.get_thread_posts = AsyncMock(return_value=[])
    config = MattermostHandlerConfig(
        persona_id=456,
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
    )
    service_user = MagicMock()
    service_user.id = UUID("00000000-0000-0000-0000-000000000456")
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            Packet(
                placement=Placement(turn_index=0),
                obj=AgentResponseDelta(content="Onyx answer [1]"),
            ),
        ]
    )
    channel_config = SimpleNamespace(
        persona_id=789,
        channel_config={"response_style": "orka_concise", "disabled": False},
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.fetch_mattermost_channel_config_for_bot_and_channel",
            return_value=channel_config,
        ) as mock_fetch_channel_config,
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=_target(),
        ) as mock_get_target,
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=service_user,
        ),
        patch("onyx.onyxbot.mattermost.handler.get_persona_by_id") as mock_get_persona,
        patch(
            "onyx.onyxbot.mattermost.handler._stream_mattermost_answer_packets",
            return_value=packets,
        ) as mock_stream_packets,
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
        mock_get_persona.return_value = MagicMock(id=456)
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    mock_fetch_channel_config.assert_called_once_with(
        db_session,
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
        channel_id="channel-1",
    )
    mock_get_target.assert_called_once_with(
        db_session=db_session,
        event=event,
        persona_id=789,
        onyx_user_id=None,
    )
    assert mock_stream_packets.call_args.kwargs["response_style"] == "orka_concise"


def test_orka_concise_style_preserves_agent_ownership_citations_and_safety() -> None:
    context = _build_mattermost_context(
        _event(
            event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
            post_id="post-root-1",
            root_post_id="post-root-1",
            text="summarise this with sources",
        ),
        response_style="orka_concise",
    )

    assert (
        "selected Onyx Agent Instructions remain the only base personality source"
        in context
    )
    assert "preserve citations plus safety-critical detail" in context
    assert "system_prompt" not in context


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
    client.get_thread_posts = AsyncMock(return_value=[])
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
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    stream_request = mock_handle_stream.call_args.kwargs["new_msg_req"]
    assert stream_request.chat_session_id == UUID(
        "00000000-0000-0000-0000-000000000001"
    )
    assert stream_request.parent_message_id == 11
    assert stream_request.message == "can you expand?"


@pytest.mark.asyncio
async def test_post_file_ids_are_saved_as_turn_attachments() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-root-1",
        root_post_id="post-root-1",
        user_id="user-1",
        text="summarize this",
        raw_event_type="posted",
        file_ids=("mm-file-1",),
        dedupe_key="event_id:post-root-1",
    )
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="mm-file-1",
            uploader_user_id="user-2",
            post_id="post-root-1",
            filename="brief.txt",
            mime_type="text/plain",
            size_bytes=11,
            create_at=1786720000123,
        )
    )
    client.download_file = AsyncMock(return_value=b"hello world")
    file_store = MagicMock()
    file_store.save_file.return_value = "stored-file-1"
    ledger_event = MattermostEventState(
        id=1,
        instance_id="default",
        channel_id="channel-1",
        dedupe_key="event_id:post-root-1",
        event_type="channel_mention",
        source_post_id="post-root-1",
        mattermost_pending_post_id="pending-1",
        state="pending",
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.record_mattermost_attachment",
        ) as mock_record_attachment,
    ):
        file_descriptors = await _save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=event,
            ledger_event=ledger_event,
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    assert file_descriptors == [
        {
            "id": "stored-file-1",
            "type": "plain_text",
            "name": "brief.txt",
            "user_file_id": str(db_session.add.call_args.args[0].id),
        }
    ]
    client.get_file_info.assert_awaited_once_with("mm-file-1")
    client.download_file.assert_awaited_once_with("mm-file-1")
    file_store.save_file.assert_called_once()
    mock_record_attachment.assert_called_once()
    assert mock_record_attachment.call_args.kwargs["sha256"] == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )
    assert "promoted_seafile_path" not in mock_record_attachment.call_args.kwargs


@pytest.mark.asyncio
async def test_file_only_owned_thread_post_is_audited_and_ingests_attachments() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-file-only-1",
        root_post_id="post-root-1",
        user_id="user-1",
        text="   ",
        raw_event_type="posted",
        file_ids=("mm-file-1",),
        dedupe_key="event_id:file-only-1",
    )
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="mm-file-1",
            uploader_user_id="user-1",
            post_id="post-file-only-1",
            filename="only-file.txt",
            mime_type="text/plain",
            size_bytes=7,
        )
    )
    client.download_file = AsyncMock(return_value=b"content")
    client.create_post = AsyncMock()
    client.update_post = AsyncMock()
    file_store = MagicMock()
    file_store.save_file.return_value = "stored-file-only-1"
    service_user = MagicMock()
    service_user.id = UUID("00000000-0000-0000-0000-000000000456")
    target = _target()
    target.mapping.id = 7
    claim = _processing_claim()

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_chat_target",
            return_value=target,
        ) as mock_get_target,
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=claim,
        ) as mock_claim,
        patch(
            "onyx.onyxbot.mattermost.handler.get_or_create_mattermost_service_account",
            return_value=service_user,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.record_mattermost_attachment",
        ) as mock_record_attachment,
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_control_event",
            return_value=True,
        ) as mock_complete,
        patch(
            "onyx.onyxbot.mattermost.handler.handle_stream_message_objects"
        ) as mock_handle_stream,
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=db_session,
        )

    assert handled is True
    mock_get_target.assert_called_once()
    mock_claim.assert_called_once()
    client.get_file_info.assert_awaited_once_with("mm-file-1")
    client.download_file.assert_awaited_once_with("mm-file-1")
    mock_record_attachment.assert_called_once()
    mock_complete.assert_called_once_with(
        db_session,
        event_id=claim.event.id,
        claim_owner=claim.claim_owner,
    )
    mock_handle_stream.assert_not_called()
    client.create_post.assert_not_awaited()
    client.update_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_metadata_for_another_post_fails_before_content_read() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="post-1",
        user_id="user-1",
        text="summarize this",
        raw_event_type="posted",
        file_ids=("file-from-another-post",),
        dedupe_key="event_id:post-1",
    )
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-from-another-post",
            uploader_user_id="user-2",
            post_id="post-in-another-channel",
            filename="private.txt",
            mime_type="text/plain",
        )
    )
    client.download_file = AsyncMock(return_value=b"must not be read")
    ledger_event = MattermostEventState(
        id=2,
        instance_id="default",
        channel_id="channel-1",
        dedupe_key="event_id:post-1",
        event_type="channel_mention",
        source_post_id="post-1",
        mattermost_pending_post_id="pending-2",
        state="claimed",
    )

    with pytest.raises(MattermostClientError, match="does not belong to source post"):
        await _save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=event,
            ledger_event=ledger_event,
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    client.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_storage_identity_is_stable_across_replay() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="post-1",
        user_id="user-1",
        text="summarize",
        raw_event_type="posted",
        file_ids=("file-1",),
        dedupe_key="event_id:post-1",
    )
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-1",
            post_id="post-1",
            filename="same-name.txt",
            mime_type="text/plain",
            size_bytes=6,
        )
    )
    client.download_file = AsyncMock(return_value=b"stable")
    file_store = MagicMock()
    file_store.save_file.return_value = "stable-storage-id"
    ledger_event = MattermostEventState(
        id=3,
        instance_id="instance-1",
        channel_id="channel-1",
        dedupe_key="event_id:post-1",
        event_type="channel_mention",
        source_post_id="post-1",
        mattermost_pending_post_id="pending-3",
        state="claimed",
    )

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_default_file_store",
            return_value=file_store,
        ),
        patch("onyx.onyxbot.mattermost.handler.record_mattermost_attachment"),
    ):
        await _save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=event,
            ledger_event=ledger_event,
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    assert file_store.save_file.call_args.kwargs["file_id"] == (
        "mattermost/instance-1/channel-1/file-1"
    )


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


@pytest.mark.asyncio
async def test_root_deletion_relinquishes_owned_thread_without_deleting_history() -> (
    None
):
    db_session = MagicMock()
    client = MagicMock()
    config = MattermostHandlerConfig(
        persona_id=456,
        owned_thread_root_ids={"post-root-1"},
        owned_answer_post_root_ids={"bot-post-1": "post-root-1"},
        owned_answer_post_message_ids={"bot-post-1": 22},
    )
    event = _event(
        event_type=MattermostNormalizedEventType.POST_DELETE_TOMBSTONE,
        post_id="post-root-1",
        root_post_id="post-root-1",
        text="",
    )
    mapping = MagicMock(id=7, answer_post_message_ids={"bot-post-1": 22})
    ledger_event = MattermostEventState(
        id=91,
        instance_id="mattermost",
        channel_id="channel-1",
        dedupe_key=event.dedupe_key,
        event_type="post_delete_tombstone",
        mapping_id=7,
        source_post_id="post-root-1",
        mattermost_pending_post_id="pending-delete",
        state="claimed",
    )
    claim_owner = UUID("00000000-0000-0000-0000-000000000091")

    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_mattermost_thread_mapping",
            return_value=mapping,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=MattermostEventClaim(
                MattermostClaimOutcome.PROCESS, ledger_event, claim_owner
            ),
        ) as mock_claim,
        patch(
            "onyx.onyxbot.mattermost.handler.tombstone_mattermost_thread_mapping",
            return_value=mapping,
        ) as mock_tombstone,
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_control_event",
            return_value=True,
        ) as mock_complete,
    ):
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=config,
            client=client,
            db_session=db_session,
        )

    assert handled is True
    mock_tombstone.assert_called_once_with(
        db_session=db_session,
        server_id="team-1",
        channel_id="channel-1",
        root_id="post-root-1",
    )
    assert config.owned_thread_root_ids == set()
    assert config.owned_answer_post_root_ids == {}
    assert config.owned_answer_post_message_ids == {}
    assert mock_claim.call_args.kwargs["source_delete_at"] == event.source_delete_at
    mock_complete.assert_called_once_with(
        db_session,
        event_id=ledger_event.id,
        claim_owner=claim_owner,
    )
    client.create_post.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_feedback_records_chat_message_feedback() -> None:
    db_session = MagicMock()
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.REACTION_FEEDBACK,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="bot-answer-1",
        root_post_id="post-root-1",
        user_id="user-1",
        raw_event_type="reaction_added",
        feedback_answer_post_id="bot-answer-1",
        feedback_action=QAFeedbackType.LIKE,
        feedback_message_id=22,
        dedupe_key="reaction:event-record",
    )
    client = MagicMock()

    mapping = MagicMock(id=7)
    claim = _processing_claim()
    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_mattermost_thread_mapping",
            return_value=mapping,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=claim,
        ) as mock_claim,
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_feedback_event",
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
    mock_claim.assert_called_once()
    mock_complete.assert_called_once_with(
        db_session,
        event_id=claim.event.id,
        claim_owner=claim.claim_owner,
        chat_message_id=22,
        is_positive=True,
        feedback_text="Mattermost feedback from user-1",
    )
    db_session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_feedback_rolls_back_dedupe_when_feedback_fails() -> None:
    db_session = MagicMock()
    event = NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.REACTION_FEEDBACK,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="bot-answer-1",
        root_post_id="post-root-1",
        user_id="user-1",
        raw_event_type="reaction_added",
        feedback_answer_post_id="bot-answer-1",
        feedback_action=QAFeedbackType.LIKE,
        feedback_message_id=22,
        dedupe_key="reaction:event-1",
    )

    mapping = MagicMock(id=7)
    claim = _processing_claim()
    with (
        patch(
            "onyx.onyxbot.mattermost.handler.get_mattermost_thread_mapping",
            return_value=mapping,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
            return_value=claim,
        ),
        patch(
            "onyx.onyxbot.mattermost.handler.complete_mattermost_feedback_event",
            side_effect=RuntimeError("feedback insert failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="feedback insert failed"):
            await handle_normalized_mattermost_event(
                event=event,
                config=MattermostHandlerConfig(persona_id=456),
                client=MagicMock(),
                db_session=db_session,
            )

    db_session.rollback.assert_called_once_with()
    db_session.commit.assert_not_called()


def test_format_mattermost_answer_preserves_citations() -> None:
    answer = _answer(
        message_id=44,
        answer="Use this [1].",
        citation_info=[CitationInfo(citation_number=1, document_id="doc-1")],
        top_documents=[_search_doc(document_id="doc-1")],
    )

    formatted = format_mattermost_answer(answer)

    assert (
        formatted
        == "Use this [1].\n\nSources:\n[1] Mattermost Doc - https://example.test/doc"
    )


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


def _processing_claim() -> MattermostEventClaim:
    ledger_event = MattermostEventState(
        id=1,
        instance_id="default",
        channel_id="channel-1",
        dedupe_key="event_id:post-root-1",
        event_type="channel_mention",
        source_post_id="post-root-1",
        mattermost_pending_post_id="pending-1",
        state="pending",
    )
    claim_owner = UUID("00000000-0000-0000-0000-000000000999")
    return MattermostEventClaim(
        MattermostClaimOutcome.PROCESS, ledger_event, claim_owner
    )


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
        dedupe_key=f"event_id:{post_id}",
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
