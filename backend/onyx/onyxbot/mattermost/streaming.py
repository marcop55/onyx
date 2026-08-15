"""Stream Onyx answer packets into one Mattermost post."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from onyx.chat.models import AnswerStreamPart, ChatBasicResponse, StreamingError
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.formatting import (
    MATTERMOST_DEFAULT_MAX_PART_CHARS,
    MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR,
    format_mattermost_answer_parts,
    has_linked_mattermost_source,
)
from onyx.onyxbot.mattermost.models import (
    MattermostFileInfo,
    MattermostPost,
    MattermostUserInfo,
)
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
MATTERMOST_NO_CITATIONS_MESSAGE = "Found no citations or quotes when trying to answer."
MATTERMOST_EPHEMERAL_TOO_LONG_MESSAGE = (
    "Answer too long for one ephemeral message. Ask in a thread instead."
)
MATTERMOST_MIN_UPDATE_CHARS = 80
MattermostFinalPropsFactory = Callable[[int, str], Awaitable[dict[str, object] | None]]


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

    async def create_ephemeral_post(
        self,
        *,
        user_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None: ...

    async def update_post(
        self,
        *,
        post_id: str,
        message: str,
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...

    async def get_file_info(self, file_id: str) -> MattermostFileInfo: ...

    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]: ...

    async def download_file(self, file_id: str) -> bytes: ...


@dataclass(frozen=True)
class MattermostStreamResult:
    message_id: int
    post_id: str
    post_ids: tuple[str, ...] = ()


async def stream_mattermost_answer(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    root_id: str,
    packets: Iterator[AnswerStreamPart],
    post_id: str | None = None,
    checkpoint_final: Callable[[str, int], None] | None = None,
    before_external_update: Callable[[], bool] | None = None,
    final_props_factory: MattermostFinalPropsFactory | None = None,
    min_update_chars: int = MATTERMOST_MIN_UPDATE_CHARS,
    response_type: str = "citations",
    include_source_previews: bool = False,
    require_citations: bool = False,
    no_results_message: str | None = None,
    max_part_chars: int = MATTERMOST_DEFAULT_MAX_PART_CHARS,
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
                if (
                    not require_citations
                    and len(answer) - last_sent_answer_length >= min_update_chars
                ):
                    await deliver_mattermost_rendered_messages(
                        client=client,
                        channel_id=channel_id,
                        root_id=root_id,
                        post_id=post_id,
                        rendered_message=_serialize_rendered_messages(
                            [answer]
                            if len(answer) <= max_part_chars
                            else format_mattermost_answer_parts(
                                ChatBasicResponse(
                                    answer=answer,
                                    answer_citationless=answer,
                                    top_documents=[],
                                    error_msg=None,
                                    message_id=message_id or 0,
                                    citation_info=[],
                                ),
                                max_part_chars=max_part_chars,
                            )
                        ),
                        sent_messages=sent_messages,
                        before_external_update=before_external_update,
                        max_part_chars=max_part_chars,
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
            channel_id=channel_id,
            root_id=root_id,
            post_id=post_id,
            answer=answer,
            sent_messages=sent_messages,
            max_part_chars=max_part_chars,
        )
        raise MattermostStreamVisibleError(str(exc)) from exc

    if message_id is None:
        _require_owner_fence(before_external_update)
        await _show_failure(
            client=client,
            channel_id=channel_id,
            root_id=root_id,
            post_id=post_id,
            answer=answer,
            sent_messages=sent_messages,
            max_part_chars=max_part_chars,
        )
        raise MattermostStreamVisibleError("Message ID is required")

    if no_results_message and not top_documents and not citations:
        final_messages = _bounded_text_parts(no_results_message, max_part_chars)
    elif require_citations and not has_linked_mattermost_source(
        citations, top_documents
    ):
        final_messages = [MATTERMOST_NO_CITATIONS_MESSAGE]
    else:
        final_messages = format_mattermost_answer_parts(
            ChatBasicResponse(
                answer=answer,
                answer_citationless=answer,
                top_documents=top_documents,
                error_msg=None,
                message_id=message_id,
                citation_info=citations,
            ),
            response_type=response_type,
            include_source_previews=include_source_previews,
            max_part_chars=max_part_chars,
        )
    final_message = _serialize_rendered_messages(final_messages)
    if checkpoint_final is not None:
        checkpoint_final(final_message, message_id)
    post_ids = await deliver_mattermost_rendered_messages(
        client=client,
        channel_id=channel_id,
        root_id=root_id,
        post_id=post_id,
        rendered_message=final_message,
        sent_messages=sent_messages,
        before_external_update=before_external_update,
        props=await final_props_factory(message_id, final_message)
        if final_props_factory is not None
        else None,
        max_part_chars=max_part_chars,
    )
    return MattermostStreamResult(
        message_id=message_id,
        post_id=post_id,
        post_ids=post_ids if len(post_ids) > 1 else (),
    )


async def stream_mattermost_ephemeral_answer(
    *,
    client: MattermostStreamingClient,
    user_id: str,
    channel_id: str,
    root_id: str,
    packets: Iterator[AnswerStreamPart],
    checkpoint_final: Callable[[str, int], None] | None = None,
    before_external_update: Callable[[], bool] | None = None,
    before_ephemeral_delivery: Callable[[], bool] | None = None,
    after_ephemeral_delivery: Callable[[str], bool] | None = None,
    no_results_message: str | None = None,
    props: dict[str, object] | None = None,
    response_type: str = "citations",
    include_source_previews: bool = False,
    require_citations: bool = False,
    max_part_chars: int = MATTERMOST_DEFAULT_MAX_PART_CHARS,
) -> MattermostStreamResult:
    """Render one Onyx answer and send it as a Mattermost ephemeral post."""

    answer = ""
    citations: list[CitationInfo] = []
    top_documents: list[SearchDoc] = []
    message_id: int | None = None

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
            elif isinstance(packet.obj, CitationInfo):
                citations.append(packet.obj)
    except Exception as exc:
        await _deliver_ephemeral_once(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            root_id=root_id,
            message=_format_bounded_failure_message(answer, max_part_chars),
            before_external_update=before_external_update,
            before_ephemeral_delivery=before_ephemeral_delivery,
            after_ephemeral_delivery=after_ephemeral_delivery,
            props=props,
        )
        raise MattermostStreamVisibleError(str(exc)) from exc

    if message_id is None:
        await _deliver_ephemeral_once(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            root_id=root_id,
            message=_format_bounded_failure_message(answer, max_part_chars),
            before_external_update=before_external_update,
            before_ephemeral_delivery=before_ephemeral_delivery,
            after_ephemeral_delivery=after_ephemeral_delivery,
            props=props,
        )
        raise MattermostStreamVisibleError("Message ID is required")

    if no_results_message and not top_documents and not citations:
        final_messages = _bounded_text_parts(no_results_message, max_part_chars)
    elif require_citations and not has_linked_mattermost_source(
        citations, top_documents
    ):
        final_messages = [MATTERMOST_NO_CITATIONS_MESSAGE]
    else:
        final_messages = format_mattermost_answer_parts(
            ChatBasicResponse(
                answer=answer,
                answer_citationless=answer,
                top_documents=top_documents,
                error_msg=None,
                message_id=message_id,
                citation_info=citations,
            ),
            response_type=response_type,
            include_source_previews=include_source_previews,
            max_part_chars=max_part_chars,
        )
    final_message = _bounded_ephemeral_message(final_messages, max_part_chars)
    if checkpoint_final is not None:
        checkpoint_final(final_message, message_id)
    post = await _deliver_ephemeral_once(
        client=client,
        user_id=user_id,
        channel_id=channel_id,
        root_id=root_id,
        message=final_message,
        before_external_update=before_external_update,
        before_ephemeral_delivery=before_ephemeral_delivery,
        after_ephemeral_delivery=after_ephemeral_delivery,
        props=props,
    )
    return MattermostStreamResult(message_id=message_id, post_id=post.id)


async def deliver_mattermost_rendered_messages(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    root_id: str,
    post_id: str,
    rendered_message: str,
    sent_messages: set[str] | None = None,
    before_external_update: Callable[[], bool] | None = None,
    props: dict[str, object] | None = None,
    max_part_chars: int = MATTERMOST_DEFAULT_MAX_PART_CHARS,
) -> tuple[str, ...]:
    messages = _bounded_rendered_message_parts(rendered_message, max_part_chars)
    sent_message_set = sent_messages if sent_messages is not None else set()

    _require_owner_fence(before_external_update)
    await _update_once(
        client=client,
        post_id=post_id,
        message=messages[0],
        sent_messages=sent_message_set,
        props=props,
    )
    delivered_post_ids = [post_id]
    for part_index, message in enumerate(messages[1:], start=2):
        _require_owner_fence(before_external_update)
        idempotency_key = _part_idempotency_key(post_id, part_index)
        post = await _find_or_create_part_post(
            client=client,
            channel_id=channel_id,
            root_id=root_id,
            pending_post_id=idempotency_key,
            event_key=idempotency_key,
            message=message,
        )
        delivered_post_ids.append(post.id)
    return tuple(delivered_post_ids)


async def _deliver_ephemeral_once(
    *,
    client: MattermostStreamingClient,
    user_id: str,
    channel_id: str,
    root_id: str,
    message: str,
    before_external_update: Callable[[], bool] | None,
    before_ephemeral_delivery: Callable[[], bool] | None,
    after_ephemeral_delivery: Callable[[str], bool] | None,
    props: dict[str, object] | None,
) -> MattermostPost:
    _require_owner_fence(before_external_update)
    if before_ephemeral_delivery is not None and not before_ephemeral_delivery():
        raise MattermostLeaseLostError("Mattermost event lease was lost")
    post = await client.create_ephemeral_post(
        user_id=user_id,
        channel_id=channel_id,
        root_id=root_id,
        message=message,
        props=props,
    )
    if after_ephemeral_delivery is not None and not after_ephemeral_delivery(post.id):
        raise MattermostLeaseLostError("Mattermost event lease was lost")
    return post


async def _find_or_create_part_post(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    root_id: str,
    pending_post_id: str,
    event_key: str,
    message: str,
) -> MattermostPost:
    existing_post = await client.find_post_by_idempotency_fields(
        channel_id=channel_id,
        pending_post_id=pending_post_id,
        event_key=event_key,
    )
    if existing_post is not None:
        if existing_post.message != message:
            return await client.update_post(post_id=existing_post.id, message=message)
        return existing_post
    return await client.create_post(
        channel_id=channel_id,
        root_id=root_id,
        message=message,
        pending_post_id=pending_post_id,
        props={"onyx_event_key": event_key},
    )


def _serialize_rendered_messages(messages: list[str]) -> str:
    return MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR.join(messages)


def _deserialize_rendered_messages(rendered_message: str) -> list[str]:
    return rendered_message.split(
        MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR
    )


def _part_idempotency_key(primary_post_id: str, part_index: int) -> str:
    return f"{primary_post_id}:part:{part_index}"


def _require_owner_fence(before_external_update: Callable[[], bool] | None) -> None:
    if before_external_update is not None and not before_external_update():
        raise MattermostLeaseLostError("Mattermost event lease was lost")


async def _update_once(
    *,
    client: MattermostStreamingClient,
    post_id: str,
    message: str,
    sent_messages: set[str],
    props: dict[str, object] | None = None,
) -> None:
    if props is None and message in sent_messages:
        return
    if props is None:
        await client.update_post(post_id=post_id, message=message)
    else:
        await client.update_post(post_id=post_id, message=message, props=props)
    sent_messages.add(message)


async def _show_failure(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    root_id: str,
    post_id: str,
    answer: str,
    sent_messages: set[str],
    max_part_chars: int,
) -> None:
    await deliver_mattermost_rendered_messages(
        client=client,
        channel_id=channel_id,
        root_id=root_id,
        post_id=post_id,
        rendered_message=_serialize_rendered_messages(
            _format_failure_message_parts(answer, max_part_chars)
        ),
        sent_messages=sent_messages,
        max_part_chars=max_part_chars,
    )


def _format_bounded_failure_message(answer: str, max_part_chars: int) -> str:
    return _format_failure_message_parts(answer, max_part_chars)[-1]


def _format_failure_message_parts(answer: str, max_part_chars: int) -> list[str]:
    if not answer:
        return _bounded_text_parts(MATTERMOST_STREAM_FAILURE_SUFFIX, max_part_chars)
    answer_parts = _bounded_text_parts(answer, max_part_chars)
    final_answer_part = answer_parts[-1]
    if (
        len(final_answer_part) + len(MATTERMOST_STREAM_FAILURE_SUFFIX) + 2
        <= max_part_chars
    ):
        answer_parts[-1] = final_answer_part + "\n\n" + MATTERMOST_STREAM_FAILURE_SUFFIX
        return answer_parts
    return answer_parts + _bounded_text_parts(
        MATTERMOST_STREAM_FAILURE_SUFFIX, max_part_chars
    )


def bound_mattermost_ephemeral_rendered_message(
    rendered_message: str,
    max_part_chars: int = MATTERMOST_DEFAULT_MAX_PART_CHARS,
) -> str:
    return _bounded_ephemeral_message(
        _bounded_rendered_message_parts(rendered_message, max_part_chars),
        max_part_chars,
    )


def _bounded_ephemeral_message(messages: list[str], max_part_chars: int) -> str:
    if len(messages) == 1 and len(messages[0]) <= max_part_chars:
        return messages[0]
    return _bounded_text_parts(
        MATTERMOST_EPHEMERAL_TOO_LONG_MESSAGE,
        max_part_chars,
    )[0]


def _bounded_rendered_message_parts(
    rendered_message: str, max_part_chars: int
) -> list[str]:
    messages = _deserialize_rendered_messages(rendered_message)
    if not messages:
        messages = [""]
    bounded_messages: list[str] = []
    for message in messages:
        bounded_messages.extend(_bounded_text_parts(message, max_part_chars))
    return bounded_messages


def _bounded_text_parts(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts
