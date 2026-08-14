"""Route normalized Mattermost events through Onyx chat."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from onyx.auth.users import get_anonymous_user
from onyx.chat.models import AnswerStreamPart
from onyx.chat.process_message import handle_stream_message_objects
from onyx.configs.constants import MessageType, QAFeedbackType
from onyx.db.chat import get_chat_message
from onyx.db.feedback import create_chat_message_feedback
from onyx.db.mattermost_bot import update_mattermost_thread_parent_message
from onyx.db.models import ChatMessage
from onyx.db.persona import get_persona_by_id
from onyx.db.users import get_or_create_mattermost_service_account
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.formatting import (
    format_mattermost_answer as _format_mattermost_answer,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.session import (
    MattermostChatTarget,
    get_or_create_mattermost_chat_target,
)
from onyx.onyxbot.mattermost.streaming import (
    MattermostStreamingClient,
    MattermostStreamVisibleError,
    stream_mattermost_answer,
)
from onyx.server.query_and_chat.models import MessageOrigin, SendMessageRequest
from shared_configs.contextvars import CURRENT_USER_ID_CONTEXTVAR

MATTERMOST_FAILURE_MESSAGE = (
    "Onyx could not answer this Mattermost message. Try again later."
)
MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE = "The configured Onyx agent is not available."


@dataclass(frozen=True)
class MattermostHandlerConfig:
    """Runtime config for routing Mattermost events into one Onyx persona."""

    persona_id: int
    onyx_user_id: UUID | None = None
    mock_llm_response: str | None = None
    owned_thread_root_ids: set[str] | None = None
    owned_answer_post_root_ids: dict[str, str] | None = None
    owned_answer_post_message_ids: dict[str, int] | None = None


format_mattermost_answer = _format_mattermost_answer


async def handle_normalized_mattermost_event(
    *,
    event: NormalizedMattermostEvent,
    config: MattermostHandlerConfig,
    client: MattermostStreamingClient,
    db_session: Session,
) -> bool:
    """Handle one normalized Mattermost event.

    Returns True when the adapter posts or records a handled event.
    Returns False for events that do not route to Onyx chat.
    """

    if event.event_type in {
        MattermostNormalizedEventType.POST_DELETE_TOMBSTONE,
    }:
        return False

    if event.event_type == MattermostNormalizedEventType.REACTION_FEEDBACK:
        return _record_feedback_event(event=event, db_session=db_session)

    if not event.text.strip():
        return False

    try:
        target = get_or_create_mattermost_chat_target(
            db_session=db_session,
            event=event,
            persona_id=config.persona_id,
            onyx_user_id=config.onyx_user_id,
        )
        packets = _stream_mattermost_answer_packets(
            db_session=db_session,
            event=event,
            target=target,
            config=config,
        )
        stream_result = await stream_mattermost_answer(
            client=client,
            channel_id=event.channel_id,
            root_id=_response_root_id(event),
            packets=packets,
        )
    except ValueError:
        await _post_failure(
            client=client,
            event=event,
            message=MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE,
        )
        return True
    except MattermostStreamVisibleError:
        return True
    except Exception:
        await _post_failure(
            client=client, event=event, message=MATTERMOST_FAILURE_MESSAGE
        )
        return True

    update_mattermost_thread_parent_message(
        db_session=db_session,
        mapping=target.mapping,
        parent_message_id=stream_result.message_id,
    )
    _record_owned_answer(
        config=config,
        event=event,
        answer_post_id=stream_result.post_id,
        message_id=stream_result.message_id,
    )
    return True


def _record_feedback_event(
    *,
    event: NormalizedMattermostEvent,
    db_session: Session,
) -> bool:
    if event.feedback_message_id is None or event.feedback_action is None:
        return False

    create_chat_message_feedback(
        is_positive=event.feedback_action == QAFeedbackType.LIKE,
        feedback_text=f"Mattermost feedback from {event.user_id}",
        chat_message_id=event.feedback_message_id,
        user_id=None,
        db_session=db_session,
    )
    return True


def _record_owned_answer(
    *,
    config: MattermostHandlerConfig,
    event: NormalizedMattermostEvent,
    answer_post_id: str,
    message_id: int,
) -> None:
    if config.owned_thread_root_ids is not None:
        config.owned_thread_root_ids.add(event.root_post_id)
    if config.owned_answer_post_root_ids is not None:
        config.owned_answer_post_root_ids[answer_post_id] = event.root_post_id
    if config.owned_answer_post_message_ids is not None:
        config.owned_answer_post_message_ids[answer_post_id] = message_id


def _stream_mattermost_answer_packets(
    *,
    db_session: Session,
    event: NormalizedMattermostEvent,
    target: MattermostChatTarget,
    config: MattermostHandlerConfig,
) -> Iterator[AnswerStreamPart]:
    if target.persona_id is None:
        raise ValueError("Mattermost thread mapping is missing persona")

    service_user = get_or_create_mattermost_service_account(db_session)
    persona = get_persona_by_id(
        persona_id=target.persona_id,
        user=None,
        db_session=db_session,
        is_for_edit=False,
    )
    if not persona.id:
        raise RuntimeError("Mattermost persona is invalid")

    new_message_request = SendMessageRequest(
        message=event.text,
        allowed_tool_ids=None,
        file_descriptors=[],
        deep_research=False,
        origin=MessageOrigin.MATTERMOSTBOT,
        parent_message_id=target.parent_message_id,
        chat_session_id=target.chat_session_id,
        mock_llm_response=config.mock_llm_response,
    )

    def _packets() -> Iterator[AnswerStreamPart]:
        token = CURRENT_USER_ID_CONTEXTVAR.set(str(service_user.id))
        try:
            yield from handle_stream_message_objects(
                new_msg_req=new_message_request,
                user=get_anonymous_user(),
                bypass_acl=False,
                additional_context=_build_mattermost_context(event),
            )
        finally:
            CURRENT_USER_ID_CONTEXTVAR.reset(token)

    return _packets()


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
    client: MattermostStreamingClient,
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
