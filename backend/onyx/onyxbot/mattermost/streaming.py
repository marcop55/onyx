"""Stream Onyx answer packets into one Mattermost post."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from onyx.chat.models import AnswerStreamPart, ChatBasicResponse, StreamingError
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.formatting import format_mattermost_answer
from onyx.onyxbot.mattermost.models import MattermostPost
from onyx.server.query_and_chat.models import MessageResponseIDInfo
from onyx.server.query_and_chat.streaming_models import (
    AgentResponseDelta,
    AgentResponseStart,
    CitationInfo,
    Packet,
)

MATTERMOST_STREAM_PLACEHOLDER = "..."
MATTERMOST_STREAM_FAILURE_SUFFIX = (
    "Onyx stopped before it finished this answer. Try again later."
)
MATTERMOST_MIN_UPDATE_CHARS = 80


class MattermostStreamVisibleError(RuntimeError):
    """Raised after the Mattermost stream post shows the failure."""


class MattermostLeaseLostError(RuntimeError):
    """Raised before an external mutation when the durable owner fence is lost."""


class MattermostStreamingClient(Protocol):
    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None: ...

    async def update_post(self, *, post_id: str, message: str) -> MattermostPost: ...


@dataclass(frozen=True)
class MattermostStreamResult:
    message_id: int
    post_id: str


async def stream_mattermost_answer(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    root_id: str,
    packets: Iterator[AnswerStreamPart],
    post_id: str | None = None,
    checkpoint_final: Callable[[str, int], None] | None = None,
    before_external_update: Callable[[], bool] | None = None,
    min_update_chars: int = MATTERMOST_MIN_UPDATE_CHARS,
) -> MattermostStreamResult:
    """Create or resume one Mattermost post and update it from Onyx packets."""

    if post_id is None:
        _require_owner_fence(before_external_update)
        post = await client.create_post(
            channel_id=channel_id,
            root_id=root_id,
            message=MATTERMOST_STREAM_PLACEHOLDER,
        )
        post_id = post.id
    assert post_id is not None
    answer = ""
    citations: list[CitationInfo] = []
    top_documents: list[SearchDoc] = []
    message_id: int | None = None
    last_sent_answer_length = 0
    sent_messages: set[str] = {MATTERMOST_STREAM_PLACEHOLDER}

    try:
        for packet in packets:
            if isinstance(packet, MessageResponseIDInfo):
                message_id = packet.reserved_assistant_message_id
                continue
            if isinstance(packet, StreamingError):
                raise RuntimeError(packet.error)
            if isinstance(packet, ChatBasicResponse):
                answer = packet.answer
                top_documents = packet.top_documents
                citations = packet.citation_info
                message_id = packet.message_id
                continue
            if not isinstance(packet, Packet):
                continue

            if isinstance(packet.obj, AgentResponseStart):
                if packet.obj.final_documents:
                    top_documents = packet.obj.final_documents
            elif isinstance(packet.obj, AgentResponseDelta):
                answer += packet.obj.content
                if len(answer) - last_sent_answer_length >= min_update_chars:
                    _require_owner_fence(before_external_update)
                    await _update_once(
                        client=client,
                        post_id=post_id,
                        message=answer,
                        sent_messages=sent_messages,
                    )
                    last_sent_answer_length = len(answer)
            elif isinstance(packet.obj, CitationInfo):
                citations.append(packet.obj)
    except MattermostLeaseLostError as exc:
        raise MattermostStreamVisibleError(str(exc)) from exc
    except Exception as exc:
        _require_owner_fence(before_external_update)
        await _show_failure(
            client=client,
            post_id=post_id,
            answer=answer,
            sent_messages=sent_messages,
        )
        raise MattermostStreamVisibleError(str(exc)) from exc

    if message_id is None:
        _require_owner_fence(before_external_update)
        await _show_failure(
            client=client,
            post_id=post_id,
            answer=answer,
            sent_messages=sent_messages,
        )
        raise MattermostStreamVisibleError("Message ID is required")

    final_message = format_mattermost_answer(
        ChatBasicResponse(
            answer=answer,
            answer_citationless=answer,
            top_documents=top_documents,
            error_msg=None,
            message_id=message_id,
            citation_info=citations,
        )
    )
    if checkpoint_final is not None:
        checkpoint_final(final_message, message_id)
    _require_owner_fence(before_external_update)
    await _update_once(
        client=client,
        post_id=post_id,
        message=final_message,
        sent_messages=sent_messages,
    )
    return MattermostStreamResult(
        message_id=message_id,
        post_id=post_id,
    )


def _require_owner_fence(before_external_update: Callable[[], bool] | None) -> None:
    if before_external_update is not None and not before_external_update():
        raise MattermostLeaseLostError("Mattermost event lease was lost")


async def _update_once(
    *,
    client: MattermostStreamingClient,
    post_id: str,
    message: str,
    sent_messages: set[str],
) -> None:
    if message in sent_messages:
        return
    await client.update_post(post_id=post_id, message=message)
    sent_messages.add(message)


async def _show_failure(
    *,
    client: MattermostStreamingClient,
    post_id: str,
    answer: str,
    sent_messages: set[str],
) -> None:
    await _update_once(
        client=client,
        post_id=post_id,
        message=_format_failure_message(answer),
        sent_messages=sent_messages,
    )


def _format_failure_message(answer: str) -> str:
    if not answer:
        return MATTERMOST_STREAM_FAILURE_SUFFIX
    return answer + "\n\n" + MATTERMOST_STREAM_FAILURE_SUFFIX
