"""Route normalized Mattermost events through Onyx chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from onyx.auth.users import get_anonymous_user
from onyx.chat.models import ChatBasicResponse
from onyx.chat.process_message import gather_stream, handle_stream_message_objects
from onyx.configs.constants import MessageType, QAFeedbackType
from onyx.db.chat import get_chat_message
from onyx.db.mattermost_bot import update_mattermost_thread_parent_message
from onyx.db.models import ChatMessage
from onyx.db.persona import get_persona_by_id
from onyx.db.users import get_or_create_mattermost_service_account
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.session import (
    MattermostChatTarget,
    get_or_create_mattermost_chat_target,
)
from onyx.server.query_and_chat.models import MessageOrigin, SendMessageRequest
from shared_configs.contextvars import CURRENT_USER_ID_CONTEXTVAR

MATTERMOST_FAILURE_MESSAGE = "Onyx could not answer this Mattermost message. Try again later."
MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE = "The configured Onyx agent is not available."


class MattermostPostClient(Protocol):
    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
    ) -> object: ...


@dataclass(frozen=True)
class MattermostHandlerConfig:
    """Runtime config for routing Mattermost events into one Onyx persona."""

    persona_id: int
    onyx_user_id: UUID | None = None


def format_mattermost_answer(answer: ChatBasicResponse) -> str:
    """Render an Onyx answer as Mattermost-safe Markdown."""

    if not answer.citation_info or not answer.top_documents:
        return answer.answer

    citation_lines = []
    for citation in sorted(answer.citation_info, key=lambda item: item.citation_number):
        document = next(
            (
                candidate
                for candidate in answer.top_documents
                if candidate.document_id == citation.document_id
            ),
            None,
        )
        if document is None:
            continue

        source_name = document.semantic_identifier or document.document_id
        if document.link:
            citation_lines.append(
                f"[{citation.citation_number}] {source_name} - {document.link}"
            )
        else:
            citation_lines.append(f"[{citation.citation_number}] {source_name}")

    if not citation_lines:
        return answer.answer
    return answer.answer + "\n\nSources:\n" + "\n".join(citation_lines)


async def handle_normalized_mattermost_event(
    *,
    event: NormalizedMattermostEvent,
    config: MattermostHandlerConfig,
    client: MattermostPostClient,
    db_session: Session,
) -> bool:
    """Handle one normalized Mattermost event.

    Returns True when the adapter posts or records a handled event.
    Returns False for events that do not route to Onyx chat.
    """

    if event.event_type in {
        MattermostNormalizedEventType.REACTION_FEEDBACK,
        MattermostNormalizedEventType.POST_DELETE_TOMBSTONE,
    }:
        return False

    if not event.text.strip():
        return False

    try:
        target = get_or_create_mattermost_chat_target(
            db_session=db_session,
            event=event,
            persona_id=config.persona_id,
            onyx_user_id=config.onyx_user_id,
        )
        answer = _get_mattermost_answer(
            db_session=db_session,
            event=event,
            target=target,
        )
    except ValueError:
        await _post_failure(
            client=client,
            event=event,
            message=MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE,
        )
        return True
    except Exception:
        await _post_failure(client=client, event=event, message=MATTERMOST_FAILURE_MESSAGE)
        return True

    await client.create_post(
        channel_id=event.channel_id,
        root_id=_response_root_id(event),
        message=format_mattermost_answer(answer),
    )
    update_mattermost_thread_parent_message(
        db_session=db_session,
        mapping=target.mapping,
        parent_message_id=answer.message_id,
    )
    return True


def _get_mattermost_answer(
    *,
    db_session: Session,
    event: NormalizedMattermostEvent,
    target: MattermostChatTarget,
) -> ChatBasicResponse:
    if target.persona_id is None:
        raise ValueError("Mattermost thread mapping is missing persona")

    service_user = get_or_create_mattermost_service_account(db_session)
    persona = get_persona_by_id(
        persona_id=target.persona_id,
        user=None,
        db_session=db_session,
        is_for_edit=False,
    )

    new_message_request = SendMessageRequest(
        message=event.text,
        allowed_tool_ids=None,
        file_descriptors=[],
        deep_research=False,
        origin=MessageOrigin.MATTERMOSTBOT,
        parent_message_id=target.parent_message_id,
        chat_session_id=target.chat_session_id,
    )

    token = CURRENT_USER_ID_CONTEXTVAR.set(str(service_user.id))
    try:
        packets = handle_stream_message_objects(
            new_msg_req=new_message_request,
            user=get_anonymous_user(),
            bypass_acl=False,
            additional_context=_build_mattermost_context(event),
        )
        answer = gather_stream(packets)
    finally:
        CURRENT_USER_ID_CONTEXTVAR.reset(token)

    if answer.error_msg:
        raise RuntimeError(answer.error_msg)
    if not persona.id:
        raise RuntimeError("Mattermost persona is invalid")
    return answer


def _build_mattermost_context(event: NormalizedMattermostEvent) -> str:
    return (
        "The following message came from Mattermost. "
        "Use the configured Onyx persona and shared knowledge scope only.\n"
        f"Mattermost user: {event.user_id}\n"
        f"Mattermost channel: {event.channel_id}\n"
        f"Mattermost thread root: {event.root_post_id}"
    )


def _response_root_id(event: NormalizedMattermostEvent) -> str:
    if event.event_type == MattermostNormalizedEventType.DIRECT_MESSAGE:
        return ""
    return event.root_post_id


async def _post_failure(
    *,
    client: MattermostPostClient,
    event: NormalizedMattermostEvent,
    message: str,
) -> None:
    try:
        await client.create_post(
            channel_id=event.channel_id,
            root_id=_response_root_id(event),
            message=message,
        )
    except MattermostClientError:
        raise


def mattermost_feedback_type(event: NormalizedMattermostEvent) -> QAFeedbackType | None:
    return event.feedback_action


def mattermost_message_type(event: NormalizedMattermostEvent) -> MessageType:
    if event.event_type == MattermostNormalizedEventType.REACTION_FEEDBACK:
        return MessageType.USER_REMINDER
    return MessageType.USER


def get_mattermost_parent_message(
    *,
    db_session: Session,
    target: MattermostChatTarget,
) -> ChatMessage:
    return get_chat_message(
        chat_message_id=target.parent_message_id,
        user_id=None,
        db_session=db_session,
    )
