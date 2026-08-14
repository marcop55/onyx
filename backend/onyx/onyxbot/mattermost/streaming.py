"""Stream Onyx answer packets into one Mattermost post."""

from __future__ import annotations

from collections.abc import Iterator
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


class MattermostStreamingClient(Protocol):
    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
    ) -> MattermostPost: ...

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
    min_update_chars: int = MATTERMOST_MIN_UPDATE_CHARS,
) -> MattermostStreamResult:
    """Create one Mattermost post and update it from Onyx stream packets."""

    post = await client.create_post(
        channel_id=channel_id,
        root_id=root_id,
        message=MATTERMOST_STREAM_PLACEHOLDER,
    )
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
            if not isinstance(packet, Packet):
                continue

            if isinstance(packet.obj, AgentResponseStart):
                if packet.obj.final_documents:
                    top_documents = packet.obj.final_documents
            elif isinstance(packet.obj, AgentResponseDelta):
                answer += packet.obj.content
                if len(answer) - last_sent_answer_length >= min_update_chars:
                    await _update_once(
                        client=client,
                        post_id=post.id,
                        message=answer,
                        sent_messages=sent_messages,
                    )
                    last_sent_answer_length = len(answer)
            elif isinstance(packet.obj, CitationInfo):
                citations.append(packet.obj)
    except Exception as exc:
        await _show_failure(
            client=client,
            post_id=post.id,
            answer=answer,
            sent_messages=sent_messages,
        )
        raise MattermostStreamVisibleError(str(exc)) from exc

    if message_id is None:
        await _show_failure(
            client=client,
            post_id=post.id,
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
    await _update_once(
        client=client,
        post_id=post.id,
        message=final_message,
        sent_messages=sent_messages,
    )
    return MattermostStreamResult(message_id=message_id, post_id=post.id)


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
