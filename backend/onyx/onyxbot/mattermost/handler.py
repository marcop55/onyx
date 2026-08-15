"""Route normalized Mattermost events through Onyx chat."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from onyx.chat.models import AnswerStreamPart
from onyx.chat.process_message import handle_stream_message_objects
from onyx.configs.constants import MessageType, QAFeedbackType
from onyx.db.chat import TERMINATED_RESPONSE_PLACEHOLDER, get_chat_message
from onyx.db.mattermost_bot import (
    MattermostClaimOutcome,
    MattermostThreadTombstonedError,
    checkpoint_mattermost_post,
    checkpoint_mattermost_post_attempt,
    checkpoint_mattermost_rendered_message,
    checkpoint_mattermost_turn,
    claim_durable_mattermost_event,
    complete_mattermost_answer_event,
    complete_mattermost_control_event,
    complete_mattermost_feedback_event,
    fetch_mattermost_channel_config_for_bot_and_channel,
    get_loaded_mattermost_context_post_ids,
    get_mattermost_thread_mapping,
    record_mattermost_attachment,
    renew_mattermost_event_lease,
    tombstone_mattermost_thread_mapping,
)
from onyx.db.models import ChatMessage, MattermostEventState
from onyx.db.persona import get_persona_by_id
from onyx.db.users import get_or_create_mattermost_service_account
from onyx.file_store.file_store import get_default_file_store
from onyx.file_store.models import FileDescriptor
from onyx.onyxbot.mattermost.attachments import save_mattermost_attachments
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.context import (
    MattermostThreadContextFetchError,
    build_mattermost_turn_context,
)
from onyx.onyxbot.mattermost.formatting import (
    format_mattermost_answer as _format_mattermost_answer,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostPost,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.mutations import (
    MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE,
    MATTERMOST_MUTATION_REJECTED_MESSAGE,
    MATTERMOST_MUTATION_SUCCESS_MESSAGE,
    MATTERMOST_MUTATION_UNAVAILABLE_MESSAGE,
    MattermostMutationAdapter,
    MattermostMutationPermissionError,
    parse_mattermost_mutation_command,
)
from onyx.onyxbot.mattermost.session import (
    MattermostChatTarget,
    get_or_create_mattermost_chat_target,
)
from onyx.onyxbot.mattermost.streaming import (
    MATTERMOST_STREAM_PLACEHOLDER,
    MattermostLeaseLostError,
    MattermostStreamingClient,
    MattermostStreamVisibleError,
    stream_mattermost_answer,
)
from onyx.server.query_and_chat.models import (
    MessageOrigin,
    MessageResponseIDInfo,
    SendMessageRequest,
)
from shared_configs.contextvars import CURRENT_USER_ID_CONTEXTVAR

MATTERMOST_FAILURE_MESSAGE = (
    "Onyx could not answer this Mattermost message. Try again later."
)
MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE = "The configured Onyx agent is not available."


class MattermostPostReconciliationError(RuntimeError):
    """An ambiguous create could not be reconciled without risking duplication."""


@dataclass(frozen=True)
class MattermostHandlerConfig:
    """Runtime config for routing Mattermost events into one Onyx persona."""

    persona_id: int
    onyx_user_id: UUID | None = None
    mock_llm_response: str | None = None
    owned_thread_root_ids: set[str] | None = None
    tombstoned_thread_root_ids: set[str] | None = None
    owned_answer_post_root_ids: dict[str, str] | None = None
    owned_answer_post_message_ids: dict[str, int] | None = None
    instance_id: str = "mattermost"
    bot_user_id: str | None = None
    mutation_adapter: MattermostMutationAdapter | None = None


format_mattermost_answer = _format_mattermost_answer


async def dispatch_mattermost_mutation(
    *,
    event: NormalizedMattermostEvent,
    client: MattermostStreamingClient,
    adapter: MattermostMutationAdapter | None,
) -> bool:
    """Consume explicit mutation commands without changing ordinary chat routing."""

    try:
        request = parse_mattermost_mutation_command(event.text)
    except MattermostMutationPermissionError:
        await _post_failure(
            client=client,
            event=event,
            message=MATTERMOST_MUTATION_REJECTED_MESSAGE,
        )
        return True
    if request is None:
        return False
    if adapter is None:
        await _post_failure(
            client=client,
            event=event,
            message=MATTERMOST_MUTATION_UNAVAILABLE_MESSAGE,
        )
        return True
    try:
        await adapter.route(event, request)
    except MattermostMutationPermissionError:
        message = MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE
    except Exception:
        message = MATTERMOST_MUTATION_UNAVAILABLE_MESSAGE
    else:
        message = MATTERMOST_MUTATION_SUCCESS_MESSAGE
    await _post_failure(client=client, event=event, message=message)
    return True


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

    if await dispatch_mattermost_mutation(
        event=event,
        client=client,
        adapter=config.mutation_adapter,
    ):
        return True

    if event.event_type == MattermostNormalizedEventType.POST_DELETE_TOMBSTONE:
        mapping = get_mattermost_thread_mapping(
            db_session=db_session,
            server_id=event.team_id,
            channel_id=event.channel_id,
            root_id=event.root_post_id,
        )
        if mapping is None:
            return False
        claim = claim_durable_mattermost_event(
            db_session,
            instance_id=config.instance_id,
            channel_id=event.channel_id,
            dedupe_key=event.dedupe_key,
            event_type=event.event_type.value,
            mapping_id=mapping.id,
            source_post_id=event.post_id,
            root_post_id=event.root_post_id,
            source_user_id=event.user_id,
            source_username=event.source_username,
            source_display_name=event.source_display_name,
            source_create_at=event.source_create_at,
            source_update_at=event.source_update_at,
            source_delete_at=event.source_delete_at,
        )
        if (
            claim.outcome is not MattermostClaimOutcome.PROCESS
            or claim.claim_owner is None
        ):
            return False
        tombstone_mattermost_thread_mapping(
            db_session=db_session,
            server_id=event.team_id,
            channel_id=event.channel_id,
            root_id=event.root_post_id,
        )
        if config.owned_thread_root_ids is not None:
            config.owned_thread_root_ids.discard(event.root_post_id)
        if config.tombstoned_thread_root_ids is not None:
            config.tombstoned_thread_root_ids.add(event.root_post_id)
        answer_post_ids = [
            answer_post_id
            for answer_post_id, root_id in (
                config.owned_answer_post_root_ids or {}
            ).items()
            if root_id == event.root_post_id
        ]
        for answer_post_id in answer_post_ids:
            if config.owned_answer_post_root_ids is not None:
                config.owned_answer_post_root_ids.pop(answer_post_id, None)
            if config.owned_answer_post_message_ids is not None:
                config.owned_answer_post_message_ids.pop(answer_post_id, None)
        return complete_mattermost_control_event(
            db_session,
            event_id=claim.event.id,
            claim_owner=claim.claim_owner,
        )

    if event.event_type == MattermostNormalizedEventType.REACTION_FEEDBACK:
        mapping = get_mattermost_thread_mapping(
            db_session=db_session,
            server_id=event.team_id,
            channel_id=event.channel_id,
            root_id=event.root_post_id,
        )
        if (
            mapping is None
            or event.feedback_message_id is None
            or event.feedback_action is None
        ):
            return False
        feedback_claim = claim_durable_mattermost_event(
            db_session,
            instance_id=config.instance_id,
            channel_id=event.channel_id,
            dedupe_key=event.dedupe_key,
            event_type=event.event_type.value,
            mapping_id=mapping.id,
            source_post_id=event.post_id,
            root_post_id=event.root_post_id,
            source_user_id=event.user_id,
            source_username=event.source_username,
            source_display_name=event.source_display_name,
            source_create_at=event.source_create_at,
            source_update_at=event.source_update_at,
            source_delete_at=event.source_delete_at,
        )
        if (
            feedback_claim.outcome is not MattermostClaimOutcome.PROCESS
            or feedback_claim.claim_owner is None
        ):
            return False
        try:
            return complete_mattermost_feedback_event(
                db_session,
                event_id=feedback_claim.event.id,
                claim_owner=feedback_claim.claim_owner,
                chat_message_id=event.feedback_message_id,
                is_positive=event.feedback_action == QAFeedbackType.LIKE,
                feedback_text=f"Mattermost feedback from {event.user_id}",
            )
        except Exception:
            db_session.rollback()
            raise

    if not event.text.strip() and not event.file_ids:
        return False

    try:
        channel_config = _resolve_mattermost_channel_config(
            db_session=db_session,
            event=event,
            config=config,
        )
        if channel_config and channel_config.channel_config.get("disabled"):
            return False
        target = get_or_create_mattermost_chat_target(
            db_session=db_session,
            event=event,
            persona_id=(
                channel_config.persona_id
                if channel_config and channel_config.persona_id is not None
                else config.persona_id
            ),
            onyx_user_id=config.onyx_user_id,
        )
        claim = claim_durable_mattermost_event(
            db_session,
            instance_id=config.instance_id,
            channel_id=event.channel_id,
            dedupe_key=event.dedupe_key,
            event_type=event.event_type.value,
            mapping_id=target.mapping.id,
            source_post_id=event.post_id,
            root_post_id=event.root_post_id,
            source_user_id=event.user_id,
            source_username=event.source_username,
            source_display_name=event.source_display_name,
            source_create_at=event.source_create_at,
            source_update_at=event.source_update_at,
            source_delete_at=event.source_delete_at,
        )
        if claim.outcome is not MattermostClaimOutcome.PROCESS:
            return False
        if claim.claim_owner is None:
            raise RuntimeError("Mattermost ledger claim is missing its owner")
        claim_owner = claim.claim_owner
        service_user = get_or_create_mattermost_service_account(db_session)
        file_descriptors = await _save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=event,
            ledger_event=claim.event,
            service_user_id=service_user.id,
        )
        if not event.text.strip():
            return complete_mattermost_control_event(
                db_session,
                event_id=claim.event.id,
                claim_owner=claim_owner,
            )

        def renew_owner_fence() -> bool:
            return renew_mattermost_event_lease(
                db_session,
                event_id=claim.event.id,
                claim_owner=claim_owner,
            )

        thread_context = await build_mattermost_turn_context(
            client=client,
            event=event,
            previously_loaded_post_ids=get_loaded_mattermost_context_post_ids(
                db_session,
                target.mapping.id,
            ),
        )

        ledger_event = claim.event
        if (
            ledger_event.onyx_assistant_message_id is not None
            and ledger_event.rendered_message is None
        ):
            recovered_assistant = get_chat_message(
                chat_message_id=ledger_event.onyx_assistant_message_id,
                user_id=None,
                db_session=db_session,
            )
            if recovered_assistant.message == TERMINATED_RESPONSE_PLACEHOLDER:
                # The provider/tool outcome is ambiguous. Never rerun it automatically.
                return False
            if not checkpoint_mattermost_rendered_message(
                db_session,
                event_id=ledger_event.id,
                claim_owner=claim_owner,
                rendered_message=recovered_assistant.message,
            ):
                return False
            ledger_event.rendered_message = recovered_assistant.message

        if (
            ledger_event.rendered_message is not None
            and ledger_event.mattermost_post_id is not None
            and ledger_event.onyx_assistant_message_id is not None
        ):
            if not renew_owner_fence():
                return False
            await client.update_post(
                post_id=ledger_event.mattermost_post_id,
                message=ledger_event.rendered_message,
            )
            return complete_mattermost_answer_event(
                db_session,
                event_id=ledger_event.id,
                claim_owner=claim_owner,
                loaded_context_post_ids=(
                    thread_context.post_ids
                    if thread_context is not None
                    else frozenset()
                ),
            )

        post_id = ledger_event.mattermost_post_id
        if post_id is None:
            if not renew_owner_fence():
                return False
            post = await _find_reconciled_post(
                client=client,
                channel_id=event.channel_id,
                ledger_event=ledger_event,
            )
            if post is None and ledger_event.state == "post_create_attempted":
                # A previous POST may have committed remotely. A paginated search miss is
                # not authoritative, so fail closed instead of risking a duplicate POST.
                return False
            if post is None:
                if not checkpoint_mattermost_post_attempt(
                    db_session,
                    event_id=ledger_event.id,
                    claim_owner=claim_owner,
                ):
                    return False
                ledger_event.state = "post_create_attempted"
                try:
                    post = await client.create_post(
                        channel_id=event.channel_id,
                        root_id=_response_root_id(event),
                        message=MATTERMOST_STREAM_PLACEHOLDER,
                        pending_post_id=ledger_event.mattermost_pending_post_id,
                        props={"onyx_event_key": str(ledger_event.id)},
                    )
                except MattermostClientError as exc:
                    post = await _find_reconciled_post(
                        client=client,
                        channel_id=event.channel_id,
                        ledger_event=ledger_event,
                    )
                    if post is None:
                        raise MattermostPostReconciliationError(
                            "Mattermost create outcome is ambiguous"
                        ) from exc
            post_id = post.id
            if not checkpoint_mattermost_post(
                db_session,
                event_id=ledger_event.id,
                claim_owner=claim_owner,
                post_id=post_id,
            ):
                return False

        packets = _checkpoint_mattermost_turn_packets(
            packets=_stream_mattermost_answer_packets(
                db_session=db_session,
                event=event,
                target=target,
                config=config,
                service_user=service_user,
                file_descriptors=file_descriptors,
                external_idempotency_key=f"mattermost:event:{ledger_event.id}",
                thread_context=thread_context.text
                if thread_context is not None
                else None,
                response_style=channel_config.channel_config.get("response_style")
                if channel_config is not None
                else None,
            ),
            db_session=db_session,
            event_id=ledger_event.id,
            claim_owner=claim_owner,
        )

        def checkpoint_final(rendered_message: str, _message_id: int) -> None:
            if not checkpoint_mattermost_rendered_message(
                db_session,
                event_id=ledger_event.id,
                claim_owner=claim_owner,
                rendered_message=rendered_message,
            ):
                raise MattermostLeaseLostError(
                    "Mattermost event lease was lost while checkpointing answer"
                )

        stream_result = await stream_mattermost_answer(
            client=client,
            channel_id=event.channel_id,
            root_id=_response_root_id(event),
            post_id=post_id,
            packets=packets,
            checkpoint_final=checkpoint_final,
            before_external_update=renew_owner_fence,
        )
    except MattermostThreadTombstonedError:
        return False
    except ValueError:
        await _post_failure(
            client=client,
            event=event,
            message=MATTERMOST_PERSONA_ACCESS_DENIED_MESSAGE,
        )
        return True
    except MattermostStreamVisibleError:
        return True
    except MattermostPostReconciliationError:
        return False
    except MattermostLeaseLostError:
        return False
    except MattermostThreadContextFetchError:
        return False
    except MattermostClientError:
        # Once a durable answer post exists, any transport outcome is ambiguous.
        # Fail closed so the same post can be reconciled/resumed on replay.
        return False
    except Exception:
        await _post_failure(
            client=client, event=event, message=MATTERMOST_FAILURE_MESSAGE
        )
        return True

    if not complete_mattermost_answer_event(
        db_session,
        event_id=ledger_event.id,
        claim_owner=claim_owner,
        loaded_context_post_ids=(
            thread_context.post_ids if thread_context is not None else frozenset()
        ),
    ):
        return False
    _record_owned_answer(
        config=config,
        event=event,
        answer_post_id=stream_result.post_id,
        message_id=stream_result.message_id,
    )
    return True


async def _find_reconciled_post(
    *,
    client: MattermostStreamingClient,
    channel_id: str,
    ledger_event: MattermostEventState,
) -> MattermostPost | None:
    finder = getattr(client, "find_post_by_idempotency_fields", None)
    if finder is None or not iscoroutinefunction(finder):
        return None
    try:
        return await finder(
            channel_id=channel_id,
            pending_post_id=ledger_event.mattermost_pending_post_id,
            event_key=str(ledger_event.id),
        )
    except MattermostClientError as exc:
        raise MattermostPostReconciliationError(
            "Mattermost post reconciliation outcome is ambiguous"
        ) from exc


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


def _resolve_mattermost_channel_config(
    *,
    db_session: Session,
    event: NormalizedMattermostEvent,
    config: MattermostHandlerConfig,
) -> Any | None:
    if config.bot_user_id is None:
        return None
    return fetch_mattermost_channel_config_for_bot_and_channel(
        db_session,
        instance_id=config.instance_id,
        bot_user_id=config.bot_user_id,
        channel_id=event.channel_id,
    )


def _checkpoint_mattermost_turn_packets(
    *,
    packets: Iterator[AnswerStreamPart],
    db_session: Session,
    event_id: int,
    claim_owner: UUID,
) -> Iterator[AnswerStreamPart]:
    for packet in packets:
        if isinstance(packet, MessageResponseIDInfo):
            if packet.user_message_id is None:
                raise RuntimeError(
                    "Mattermost chat turn is missing its user message ID"
                )
            if not checkpoint_mattermost_turn(
                db_session,
                event_id=event_id,
                claim_owner=claim_owner,
                user_message_id=packet.user_message_id,
                assistant_message_id=packet.reserved_assistant_message_id,
            ):
                raise MattermostLeaseLostError(
                    "Mattermost event lease was lost while creating turn"
                )
        yield packet


def _stream_mattermost_answer_packets(
    *,
    db_session: Session,
    event: NormalizedMattermostEvent,
    target: MattermostChatTarget,
    config: MattermostHandlerConfig,
    service_user: Any,
    file_descriptors: list[FileDescriptor],
    external_idempotency_key: str,
    thread_context: str | None = None,
    response_style: str | None = None,
) -> Iterator[AnswerStreamPart]:
    if target.persona_id is None:
        raise ValueError("Mattermost thread mapping is missing persona")

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
        file_descriptors=file_descriptors,
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
                user=service_user,
                bypass_acl=False,
                additional_context=_build_mattermost_context(
                    event,
                    thread_context=thread_context,
                    response_style=response_style,
                ),
                external_idempotency_key=external_idempotency_key,
            )
        finally:
            CURRENT_USER_ID_CONTEXTVAR.reset(token)

    return _packets()


async def _save_mattermost_attachments(
    *,
    client: MattermostStreamingClient,
    db_session: Session,
    event: NormalizedMattermostEvent,
    ledger_event: MattermostEventState,
    service_user_id: UUID,
) -> list[FileDescriptor]:
    return await save_mattermost_attachments(
        client=client,
        db_session=db_session,
        event=event,
        ledger_event=ledger_event,
        service_user_id=service_user_id,
        get_file_store=get_default_file_store,
        record_attachment=record_mattermost_attachment,
    )


def _build_mattermost_context(
    event: NormalizedMattermostEvent,
    *,
    thread_context: str | None = None,
    response_style: str | None = None,
) -> str:
    base_context = (
        "The following message came from Mattermost. "
        "Use the configured Onyx persona and shared knowledge scope only.\n"
        f"Mattermost user: {event.user_id}\n"
        f"Mattermost channel: {event.channel_id}\n"
        f"Mattermost thread root: {event.root_post_id}"
    )
    if response_style == "orka_concise":
        base_context += (
            "\nMattermost response style control: selected Onyx Agent Instructions "
            "remain the only base personality source. For Mattermost delivery, "
            "be friendly and concise, lead with the answer, do not restate the "
            "question, use short paragraphs or at most five bullets, expand only "
            "on request, and preserve citations plus safety-critical detail."
        )
    if thread_context is None:
        return base_context
    return base_context + "\n\n" + thread_context


def _response_root_id(event: NormalizedMattermostEvent) -> str:
    if event.event_type in {
        MattermostNormalizedEventType.DIRECT_MESSAGE,
        MattermostNormalizedEventType.SLASH_COMMAND,
    }:
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
