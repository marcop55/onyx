from __future__ import annotations

from types import TracebackType
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import aiohttp
import pytest

from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.client import MattermostClient, MattermostClientError
from onyx.onyxbot.mattermost.models import (
    MattermostDeliveryTerminalOutcome,
    MattermostFileInfo,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostResponseDeliveryMode,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)
from onyx.server.query_and_chat.models import MessageResponseIDInfo
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    Packet,
    Placement,
)


class _FakeResponse:
    def __init__(self, status: int, payload: dict[str, object] | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return "failed"

    async def json(self) -> dict[str, object]:
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def request(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.requests.append((args, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_client_uses_mattermost_native_ephemeral_post_endpoint() -> None:
    session = _FakeSession(
        _FakeResponse(
            201,
            {
                "id": "ephemeral-post-1",
                "channel_id": "channel-1",
                "root_id": "root-post-1",
                "message": "private answer",
            },
        )
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    post = await client.create_ephemeral_post(
        user_id="sender-1",
        channel_id="channel-1",
        root_id="root-post-1",
        message="private answer",
        props={"onyx_event_key": "1"},
    )

    assert session.requests[0][0][:2] == (
        "POST",
        "https://mattermost.example.test/api/v4/posts/ephemeral",
    )
    assert session.requests[0][1]["json"] == {
        "user_id": "sender-1",
        "post": {
            "channel_id": "channel-1",
            "message": "private answer",
            "root_id": "root-post-1",
            "props": {"onyx_event_key": "1"},
        },
    }
    assert post.id == "ephemeral-post-1"


@pytest.mark.asyncio
async def test_slash_command_answer_is_ephemeral_and_records_terminal_outcome() -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
    )
    event = _slash_event()
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            Packet(
                placement=Placement(turn_index=0),
                obj=AgentResponseDelta(content="private slash answer"),
            ),
        ]
    )

    with _patched_chat_path(target=target, packets=packets) as calls:
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    assert client.created_posts == []
    assert client.updated_posts == []
    assert client.created_ephemeral_posts == [
        {
            "user_id": "sender-1",
            "channel_id": "channel-1",
            "root_id": "",
            "message": "private slash answer",
        }
    ]
    assert calls.delivery_modes == [MattermostResponseDeliveryMode.EPHEMERAL]
    assert calls.terminal_outcomes == [
        MattermostDeliveryTerminalOutcome.DELIVERY_FAILED,
        MattermostDeliveryTerminalOutcome.DELIVERED,
    ]
    calls.complete.assert_called_once()


@pytest.mark.asyncio
async def test_private_persona_denial_never_posts_public_identity_leak() -> None:
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
    )
    event = _channel_event()

    with _patched_chat_path(target=target, persona_error=ValueError("denied")) as calls:
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(
                persona_id=456,
                ephemeral_response_channel_ids=frozenset({"channel-1"}),
            ),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    assert client.created_posts == []
    assert client.created_ephemeral_posts == [
        {
            "user_id": "sender-1",
            "channel_id": "channel-1",
            "root_id": "root-post-1",
            "message": "The configured Onyx agent is not available.",
        }
    ]
    calls.complete.assert_not_called()


@pytest.mark.asyncio
async def test_completed_private_delivery_replay_never_reruns_model_or_reposts() -> (
    None
):
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
    )
    event = _slash_event()
    claim = _processing_claim(
        delivery_mode=MattermostResponseDeliveryMode.EPHEMERAL,
        terminal_outcome=MattermostDeliveryTerminalOutcome.DELIVERED,
        onyx_assistant_message_id=22,
        rendered_message="already delivered",
    )

    with _patched_chat_path(target=target, claim=claim) as calls:
        handled = await handle_normalized_mattermost_event(
            event=event,
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is True
    assert client.created_posts == []
    assert client.created_ephemeral_posts == []
    assert client.updated_posts == []
    calls.handle_stream.assert_not_called()
    calls.complete.assert_called_once()


@pytest.mark.asyncio
async def test_ephemeral_delivery_failure_is_terminal_and_never_falls_back_public() -> (
    None
):
    from onyx.onyxbot.mattermost.handler import (
        MattermostHandlerConfig,
        handle_normalized_mattermost_event,
    )
    from onyx.onyxbot.mattermost.session import MattermostChatTarget

    client = _RecordingClient()
    client.ephemeral_error = MattermostClientError("ephemeral outcome ambiguous")
    target = MattermostChatTarget(
        chat_session_id=UUID("00000000-0000-0000-0000-000000000001"),
        parent_message_id=11,
        persona_id=456,
        mapping=MagicMock(id=7),
    )
    packets = iter(
        [
            MessageResponseIDInfo(user_message_id=10, reserved_assistant_message_id=22),
            Packet(
                placement=Placement(turn_index=0),
                obj=AgentResponseDelta(content="private answer"),
            ),
        ]
    )

    with _patched_chat_path(target=target, packets=packets) as calls:
        handled = await handle_normalized_mattermost_event(
            event=_slash_event(),
            config=MattermostHandlerConfig(persona_id=456),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is False
    assert client.created_posts == []
    assert client.updated_posts == []
    assert client.created_ephemeral_posts == [
        {
            "user_id": "sender-1",
            "channel_id": "channel-1",
            "root_id": "",
            "message": "private answer",
        }
    ]
    assert calls.terminal_outcomes == [
        MattermostDeliveryTerminalOutcome.DELIVERY_FAILED
    ]
    calls.complete.assert_not_called()


class _PatchContext:
    def __init__(
        self,
        *,
        target: object,
        packets: object | None = None,
        claim: MattermostEventClaim | None = None,
        persona_error: Exception | None = None,
    ) -> None:
        self.delivery_modes: list[MattermostResponseDeliveryMode] = []
        self.terminal_outcomes: list[MattermostDeliveryTerminalOutcome] = []
        self.handle_stream = MagicMock()
        self.complete = MagicMock()
        self._patches = [
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
                side_effect=persona_error,
                return_value=MagicMock(id=456),
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.handle_stream_message_objects",
                return_value=packets or iter([]),
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.claim_durable_mattermost_event",
                return_value=claim or _processing_claim(),
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.get_loaded_mattermost_context_post_ids",
                return_value=frozenset(),
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_delivery_mode",
                side_effect=self._record_delivery_mode,
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.checkpoint_mattermost_terminal_outcome",
                side_effect=self._record_terminal_outcome,
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
            patch(
                "onyx.onyxbot.mattermost.handler.complete_mattermost_control_event",
                return_value=True,
            ),
            patch(
                "onyx.onyxbot.mattermost.handler.renew_mattermost_event_lease",
                return_value=True,
            ),
        ]

    def _record_delivery_mode(self, *_args: object, **kwargs: object) -> bool:
        self.delivery_modes.append(
            cast(MattermostResponseDeliveryMode, kwargs["delivery_mode"])
        )
        return True

    def _record_terminal_outcome(self, *_args: object, **kwargs: object) -> bool:
        self.terminal_outcomes.append(
            cast(MattermostDeliveryTerminalOutcome, kwargs["terminal_outcome"])
        )
        return True

    def __enter__(self) -> _PatchContext:
        for index, patcher in enumerate(self._patches):
            started = patcher.__enter__()
            if index == 3:
                self.handle_stream = cast(MagicMock, started)
            elif index == 11:
                self.complete = cast(MagicMock, started)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for patcher in reversed(self._patches):
            patcher.__exit__(exc_type, exc_value, traceback)


def _patched_chat_path(
    *,
    target: object,
    packets: object | None = None,
    claim: MattermostEventClaim | None = None,
    persona_error: Exception | None = None,
) -> _PatchContext:
    return _PatchContext(
        target=target,
        packets=packets,
        claim=claim,
        persona_error=persona_error,
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.created_posts: list[dict[str, str]] = []
        self.created_ephemeral_posts: list[dict[str, str]] = []
        self.updated_posts: list[dict[str, str]] = []
        self.ephemeral_error: MattermostClientError | None = None

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
        self.created_posts.append(
            {"channel_id": channel_id, "root_id": root_id, "message": message}
        )
        return MattermostPost(id="bot-post-1", channel_id=channel_id, message=message)

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
        self.created_ephemeral_posts.append(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "root_id": root_id,
                "message": message,
            }
        )
        if self.ephemeral_error is not None:
            raise self.ephemeral_error
        return MattermostPost(
            id="ephemeral-post-1",
            channel_id=channel_id,
            root_id=root_id,
            message=message,
        )

    async def find_post_by_idempotency_fields(
        self, *, channel_id: str, pending_post_id: str, event_key: str
    ) -> MattermostPost | None:
        _ = channel_id, pending_post_id, event_key
        return None

    async def update_post(self, *, post_id: str, message: str) -> MattermostPost:
        self.updated_posts.append({"post_id": post_id, "message": message})
        return MattermostPost(id=post_id, message=message)

    async def get_file_info(self, file_id: str) -> MattermostFileInfo:
        raise AssertionError(f"unexpected file-info request: {file_id}")

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        raise AssertionError(f"unexpected user-info request: {user_id}")

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        _ = root_post_id
        return []

    async def download_file(self, file_id: str) -> bytes:
        raise AssertionError(f"unexpected file download: {file_id}")


def _processing_claim(
    *,
    delivery_mode: MattermostResponseDeliveryMode | None = None,
    terminal_outcome: MattermostDeliveryTerminalOutcome | None = None,
    onyx_assistant_message_id: int | None = None,
    rendered_message: str | None = None,
) -> MattermostEventClaim:
    ledger_event = MattermostEventState(
        id=1,
        instance_id="instance-1",
        channel_id="channel-1",
        dedupe_key="event_id:slash-post-1",
        event_type="slash_command",
        source_post_id="slash-post-1",
        mattermost_pending_post_id="pending-1",
        state="claimed",
        delivery_mode=delivery_mode.value if delivery_mode is not None else None,
        terminal_outcome=terminal_outcome.value
        if terminal_outcome is not None
        else None,
        onyx_assistant_message_id=onyx_assistant_message_id,
        rendered_message=rendered_message,
    )
    return MattermostEventClaim(
        MattermostClaimOutcome.PROCESS,
        ledger_event,
        UUID("00000000-0000-0000-0000-000000000999"),
    )


def _slash_event() -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.SLASH_COMMAND,
        session_key="mattermost:channel:team-1:channel-1:slash-post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="slash-post-1",
        root_post_id="slash-post-1",
        user_id="sender-1",
        text="ask private question",
        dedupe_key="event_id:slash-post-1",
    )


def _channel_event() -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:root-post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-post-1",
        root_post_id="root-post-1",
        user_id="sender-1",
        text="what changed?",
        dedupe_key="event_id:root-post-1",
    )
