"""Mattermost-native signed interactive answer controls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from sqlalchemy.orm import Session

from onyx.configs.constants import QAFeedbackType
from onyx.db.mattermost_bot import (
    MattermostClaimOutcome,
    claim_durable_mattermost_event,
    complete_mattermost_control_event,
    complete_mattermost_feedback_event,
    complete_mattermost_interactive_feedback_event,
    get_mattermost_thread_mapping,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)

MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE = (
    "This Mattermost action is no longer authorized. Ask Onyx again if needed."
)
MATTERMOST_INTERACTIVE_REPLAY_MESSAGE = "This Mattermost action was already handled."
MATTERMOST_INTERACTIVE_FEEDBACK_MESSAGE = "Thanks for your feedback."
MATTERMOST_INTERACTIVE_FOLLOWUP_MESSAGE = "Marked as needing follow-up."
MATTERMOST_INTERACTIVE_RESOLVED_MESSAGE = "Marked as resolved."
MATTERMOST_INTERACTIVE_MISSING_SOURCES_MESSAGE = (
    "Sources are included in the answer above."
)


class MattermostInteractiveAction(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"
    NEED_FOLLOWUP = "need_followup"
    RESOLVED = "resolved"
    VIEW_SOURCES = "view_sources"
    CONFIRM_MUTATION = "confirm_mutation"


class MattermostInteractiveActionResult(StrEnum):
    COMPLETED = "completed"
    REPLAYED = "replayed"
    UNAUTHORIZED = "unauthorized"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MattermostInteractiveControl:
    action: MattermostInteractiveAction
    team_id: str
    channel_id: str
    root_post_id: str
    answer_post_id: str
    answer_message_id: int
    user_id: str
    sources: tuple[str, ...] = ()
    mutation_command: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"interactive:{self.action.value}:{self.answer_post_id}:{self.user_id}"


class MattermostInteractiveClient(Protocol):
    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...

    async def create_ephemeral_post(
        self,
        *,
        user_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> object: ...

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> object: ...


FeedbackCompleter = Callable[..., bool]
MutationDispatcher = Callable[..., bool | object]
ControlClaimer = Callable[..., tuple[int, Any] | None]
ControlCompleter = Callable[..., bool]


def build_mattermost_answer_action_props(
    *,
    signing_secret: str,
    interactive_url: str | None = None,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    answer_message_id: int,
    requester_user_id: str,
    team_id: str = "global",
    sources: tuple[str, ...] = (),
    mutation_command: str | None = None,
) -> dict[str, object]:
    """Return Mattermost attachment props containing signed action buttons."""

    actions = [
        _button(
            "Helpful",
            MattermostInteractiveAction.LIKE,
            signing_secret=signing_secret,
            interactive_url=interactive_url,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            answer_message_id=answer_message_id,
            requester_user_id=requester_user_id,
            sources=sources,
        ),
        _button(
            "Not helpful",
            MattermostInteractiveAction.DISLIKE,
            signing_secret=signing_secret,
            interactive_url=interactive_url,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            answer_message_id=answer_message_id,
            requester_user_id=requester_user_id,
            sources=sources,
        ),
        _button(
            "Need follow-up",
            MattermostInteractiveAction.NEED_FOLLOWUP,
            signing_secret=signing_secret,
            interactive_url=interactive_url,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            answer_message_id=answer_message_id,
            requester_user_id=requester_user_id,
            sources=sources,
        ),
        _button(
            "Resolved",
            MattermostInteractiveAction.RESOLVED,
            signing_secret=signing_secret,
            interactive_url=interactive_url,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            answer_message_id=answer_message_id,
            requester_user_id=requester_user_id,
            sources=sources,
        ),
        _button(
            "View sources",
            MattermostInteractiveAction.VIEW_SOURCES,
            signing_secret=signing_secret,
            interactive_url=interactive_url,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            answer_message_id=answer_message_id,
            requester_user_id=requester_user_id,
            sources=sources,
        ),
    ]
    if mutation_command is not None:
        actions.append(
            _button(
                "Confirm admin action",
                MattermostInteractiveAction.CONFIRM_MUTATION,
                signing_secret=signing_secret,
                interactive_url=interactive_url,
                team_id=team_id,
                channel_id=channel_id,
                root_post_id=root_post_id,
                answer_post_id=answer_post_id,
                answer_message_id=answer_message_id,
                requester_user_id=requester_user_id,
                sources=sources,
                mutation_command=mutation_command,
            )
        )
    return {"attachments": [{"actions": actions}]}


def parse_mattermost_interactive_payload(
    payload: dict[str, object],
    *,
    signing_secret: str,
) -> MattermostInteractiveControl:
    raw_value = _action_value_from_payload(payload)
    encoded_payload, _, signature = raw_value.partition(".")
    if not encoded_payload or not signature:
        raise ValueError("Mattermost interactive action signature is missing")
    expected_signature = _signature(encoded_payload, signing_secret)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Mattermost interactive action signature is invalid")
    decoded_payload = _json_from_b64(encoded_payload)
    control = _control_from_mapping(decoded_payload)
    payload_user_id = _string_value(payload.get("user_id"))
    payload_channel_id = _string_value(payload.get("channel_id"))
    payload_post_id = _string_value(payload.get("post_id"))
    if payload_user_id != control.user_id:
        raise ValueError("Mattermost interactive action identity does not match")
    if payload_channel_id and payload_channel_id != control.channel_id:
        raise ValueError("Mattermost interactive action channel does not match")
    if payload_post_id and payload_post_id != control.answer_post_id:
        raise ValueError("Mattermost interactive action post does not match")
    return control


async def handle_mattermost_interactive_action(
    *,
    payload: dict[str, object],
    signing_secret: str,
    bot_user_id: str,
    client: MattermostInteractiveClient,
    db_session: Session | object,
    instance_id: str = "mattermost",
    complete_feedback: FeedbackCompleter | None = None,
    dispatch_mutation: MutationDispatcher | None = None,
    claim_mutation: ControlClaimer | None = None,
    complete_mutation: ControlCompleter | None = None,
    channel_config: dict[str, object] | None = None,
) -> MattermostInteractiveActionResult:
    try:
        control = parse_mattermost_interactive_payload(
            payload,
            signing_secret=signing_secret,
        )
    except ValueError:
        return MattermostInteractiveActionResult.REJECTED

    authorized_user = await _authorize_current_membership(
        client=client,
        bot_user_id=bot_user_id,
        control=control,
    )
    if authorized_user is None:
        await _post_ephemeral(
            client=client,
            control=control,
            message=MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE,
        )
        return MattermostInteractiveActionResult.UNAUTHORIZED

    if control.action is MattermostInteractiveAction.CONFIRM_MUTATION:
        if "system_admin" not in authorized_user.roles.split():
            await _post_ephemeral(
                client=client,
                control=control,
                message=MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE,
            )
            return MattermostInteractiveActionResult.UNAUTHORIZED
        if control.mutation_command is None or dispatch_mutation is None:
            return MattermostInteractiveActionResult.REJECTED
        claim_mutation = claim_mutation or _claim_control_for_action
        complete_mutation = complete_mutation or _complete_claimed_control
        claim = claim_mutation(db_session, instance_id=instance_id, control=control)
        if claim is None:
            await _post_ephemeral(
                client=client,
                control=control,
                message=MATTERMOST_INTERACTIVE_REPLAY_MESSAGE,
            )
            return MattermostInteractiveActionResult.REPLAYED
        claim_event_id, claim_owner = claim
        if not complete_mutation(
            db_session,
            event_id=claim_event_id,
            claim_owner=claim_owner,
        ):
            await _post_ephemeral(
                client=client,
                control=control,
                message=MATTERMOST_INTERACTIVE_REPLAY_MESSAGE,
            )
            return MattermostInteractiveActionResult.REPLAYED
        event = NormalizedMattermostEvent(
            event_type=MattermostNormalizedEventType.SLASH_COMMAND,
            session_key=f"mattermost:interactive:{control.team_id}:{control.channel_id}:{control.root_post_id}",
            team_id=control.team_id,
            channel_id=control.channel_id,
            post_id=control.answer_post_id,
            root_post_id=control.root_post_id,
            user_id=control.user_id,
            text=control.mutation_command,
            raw_event_type="interactive_action",
            dedupe_key=control.dedupe_key,
            source_username=authorized_user.username,
            source_display_name=authorized_user.display_name,
        )
        handled_result = dispatch_mutation(event=event)
        if inspect.isawaitable(handled_result):
            handled_result = await handled_result
        handled = bool(handled_result)
        return (
            MattermostInteractiveActionResult.COMPLETED
            if handled
            else MattermostInteractiveActionResult.REJECTED
        )

    if control.action is MattermostInteractiveAction.VIEW_SOURCES:
        if not _complete_control(db_session, instance_id=instance_id, control=control):
            await _post_ephemeral(
                client=client,
                control=control,
                message=MATTERMOST_INTERACTIVE_REPLAY_MESSAGE,
            )
            return MattermostInteractiveActionResult.REPLAYED
        await _post_ephemeral(
            client=client,
            control=control,
            message="\n".join(control.sources)
            or MATTERMOST_INTERACTIVE_MISSING_SOURCES_MESSAGE,
        )
        return MattermostInteractiveActionResult.COMPLETED

    feedback_action: QAFeedbackType | None = None
    required_followup: bool | None = None
    message = MATTERMOST_INTERACTIVE_FEEDBACK_MESSAGE
    if control.action is MattermostInteractiveAction.LIKE:
        feedback_action = QAFeedbackType.LIKE
    elif control.action is MattermostInteractiveAction.DISLIKE:
        feedback_action = QAFeedbackType.DISLIKE
    elif control.action is MattermostInteractiveAction.NEED_FOLLOWUP:
        required_followup = True
        message = MATTERMOST_INTERACTIVE_FOLLOWUP_MESSAGE
    elif control.action is MattermostInteractiveAction.RESOLVED:
        required_followup = False
        message = MATTERMOST_INTERACTIVE_RESOLVED_MESSAGE
    if complete_feedback is None:
        completed = _complete_feedback(
            db_session,
            instance_id=instance_id,
            control=control,
            feedback_action=feedback_action,
            required_followup=required_followup,
            feedback_text=f"Mattermost interactive action from {control.user_id}",
        )
    else:
        completed = complete_feedback(
            db_session,
            dedupe_key=control.dedupe_key,
            chat_message_id=control.answer_message_id,
            feedback_action=feedback_action,
            required_followup=required_followup,
            feedback_text=f"Mattermost interactive action from {control.user_id}",
        )
    if not completed:
        await _post_ephemeral(
            client=client,
            control=control,
            message=MATTERMOST_INTERACTIVE_REPLAY_MESSAGE,
        )
        return MattermostInteractiveActionResult.REPLAYED
    if control.action is MattermostInteractiveAction.NEED_FOLLOWUP:
        await _post_followup_request(
            client=client,
            control=control,
            follow_up_tags=_follow_up_tags_from_config(channel_config),
        )
    await _post_ephemeral(client=client, control=control, message=message)
    return MattermostInteractiveActionResult.COMPLETED


def _button(
    name: str,
    action: MattermostInteractiveAction,
    *,
    signing_secret: str,
    interactive_url: str | None,
    team_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    answer_message_id: int,
    requester_user_id: str,
    sources: tuple[str, ...],
    mutation_command: str | None = None,
) -> dict[str, object]:
    context = {
        "action_value": _signed_value(
            {
                "action": action.value,
                "team_id": team_id,
                "channel_id": channel_id,
                "root_post_id": root_post_id,
                "answer_post_id": answer_post_id,
                "answer_message_id": answer_message_id,
                "user_id": requester_user_id,
                "sources": list(sources),
                "mutation_command": mutation_command,
            },
            signing_secret,
        )
    }
    integration: dict[str, object] = {"context": context}
    if interactive_url is not None:
        integration["url"] = interactive_url
    button: dict[str, object] = {
        "id": action.value,
        "name": name,
        "type": "button",
        "integration": integration,
    }
    return button


def _signed_value(payload: dict[str, object], signing_secret: str) -> str:
    encoded_payload = _b64_json(payload)
    return f"{encoded_payload}.{_signature(encoded_payload, signing_secret)}"


def _signature(encoded_payload: str, signing_secret: str) -> str:
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _b64_json(payload: dict[str, object]) -> str:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (
        base64.urlsafe_b64encode(payload_json.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def _json_from_b64(value: str) -> dict[str, object]:
    padded = value + "=" * (-len(value) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Mattermost interactive action payload is invalid")
    return cast(dict[str, object], decoded)


def _control_from_mapping(mapping: dict[str, object]) -> MattermostInteractiveControl:
    action = MattermostInteractiveAction(_required_string(mapping, "action"))
    answer_message_id = mapping.get("answer_message_id")
    if not isinstance(answer_message_id, int):
        raise ValueError("Mattermost interactive action message id is invalid")
    sources_value = mapping.get("sources")
    sources = (
        tuple(item for item in sources_value if isinstance(item, str))
        if isinstance(sources_value, list)
        else ()
    )
    mutation_command = mapping.get("mutation_command")
    return MattermostInteractiveControl(
        action=action,
        team_id=_required_string(mapping, "team_id"),
        channel_id=_required_string(mapping, "channel_id"),
        root_post_id=_required_string(mapping, "root_post_id"),
        answer_post_id=_required_string(mapping, "answer_post_id"),
        answer_message_id=answer_message_id,
        user_id=_required_string(mapping, "user_id"),
        sources=sources,
        mutation_command=mutation_command
        if isinstance(mutation_command, str)
        else None,
    )


def _action_value_from_payload(payload: dict[str, object]) -> str:
    context = payload.get("context")
    if isinstance(context, Mapping):
        context_mapping = cast(Mapping[object, object], context)
        value = context_mapping.get("action_value")
        if isinstance(value, str):
            return value
    actions = payload.get("actions")
    if isinstance(actions, list) and actions:
        action = actions[0]
        if isinstance(action, Mapping):
            action_mapping = cast(Mapping[object, object], action)
            action_context = action_mapping.get("context")
            if isinstance(action_context, Mapping):
                action_context_mapping = cast(Mapping[object, object], action_context)
                value = action_context_mapping.get("action_value")
                if isinstance(value, str):
                    return value
            integration = action_mapping.get("integration")
            if isinstance(integration, Mapping):
                integration_mapping = cast(Mapping[object, object], integration)
                integration_context = integration_mapping.get("context")
                if isinstance(integration_context, Mapping):
                    integration_context_mapping = cast(
                        Mapping[object, object], integration_context
                    )
                    value = integration_context_mapping.get("action_value")
                    if isinstance(value, str):
                        return value
            value = action_mapping.get("value")
            if isinstance(value, str):
                return value
    raise ValueError("Mattermost interactive action value is missing")


async def _authorize_current_membership(
    *,
    client: MattermostInteractiveClient,
    bot_user_id: str,
    control: MattermostInteractiveControl,
) -> MattermostUserInfo | None:
    try:
        if not await client.is_channel_member(
            channel_id=control.channel_id, user_id=bot_user_id
        ):
            return None
        if not await client.is_channel_member(
            channel_id=control.channel_id, user_id=control.user_id
        ):
            return None
        return await client.get_user_info(control.user_id)
    except Exception:
        return None


async def _post_ephemeral(
    *,
    client: MattermostInteractiveClient,
    control: MattermostInteractiveControl,
    message: str,
) -> None:
    await client.create_ephemeral_post(
        user_id=control.user_id,
        channel_id=control.channel_id,
        root_id=control.root_post_id,
        message=message,
    )


async def _post_followup_request(
    *,
    client: MattermostInteractiveClient,
    control: MattermostInteractiveControl,
    follow_up_tags: list[str],
) -> None:
    tags_text = " ".join(_mention_tag(tag) for tag in follow_up_tags)
    message = "Received your request for more help."
    if tags_text:
        message = f"{message} Notifying {tags_text}."
    await client.create_post(
        channel_id=control.channel_id,
        root_id=control.root_post_id,
        message=message,
    )


def _follow_up_tags_from_config(channel_config: dict[str, object] | None) -> list[str]:
    if channel_config is None:
        return []
    tags = channel_config.get("follow_up_tags")
    if not isinstance(tags, list):
        return []
    return [tag for tag in tags if isinstance(tag, str) and tag]


def _mention_tag(tag: str) -> str:
    return tag if tag.startswith("@") else f"@{tag}"


def _complete_feedback(
    db_session: Session | object,
    *,
    instance_id: str,
    control: MattermostInteractiveControl,
    feedback_action: QAFeedbackType | None = None,
    required_followup: bool | None = None,
    feedback_text: str,
) -> bool:
    if not isinstance(db_session, Session):
        return False
    claim = _claim_control(db_session, instance_id=instance_id, control=control)
    if claim is None:
        return False
    claim_event_id, claim_owner = claim
    if required_followup is None:
        if feedback_action is None:
            return False
        return complete_mattermost_feedback_event(
            db_session,
            event_id=claim_event_id,
            claim_owner=claim_owner,
            chat_message_id=control.answer_message_id,
            is_positive=feedback_action == QAFeedbackType.LIKE,
            feedback_text=feedback_text,
        )
    return complete_mattermost_interactive_feedback_event(
        db_session,
        event_id=claim_event_id,
        claim_owner=claim_owner,
        chat_message_id=control.answer_message_id,
        is_positive=None,
        required_followup=required_followup,
        feedback_text=feedback_text,
    )


def _complete_control(
    db_session: Session | object,
    *,
    instance_id: str,
    control: MattermostInteractiveControl,
) -> bool:
    if not isinstance(db_session, Session):
        return True
    claim = _claim_control(db_session, instance_id=instance_id, control=control)
    if claim is None:
        return False
    claim_event_id, claim_owner = claim
    return complete_mattermost_control_event(
        db_session,
        event_id=claim_event_id,
        claim_owner=claim_owner,
    )


def _claim_control(
    db_session: Session,
    *,
    instance_id: str,
    control: MattermostInteractiveControl,
) -> tuple[int, Any] | None:
    mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id=control.team_id,
        channel_id=control.channel_id,
        root_id=control.root_post_id,
    )
    claim = claim_durable_mattermost_event(
        db_session,
        instance_id=instance_id,
        channel_id=control.channel_id,
        dedupe_key=control.dedupe_key,
        event_type=f"interactive_{control.action.value}",
        mapping_id=mapping.id if mapping is not None else None,
        source_post_id=control.answer_post_id,
        root_post_id=control.root_post_id,
        source_user_id=control.user_id,
    )
    if claim.outcome is not MattermostClaimOutcome.PROCESS or claim.claim_owner is None:
        return None
    return claim.event.id, claim.claim_owner


def _claim_control_for_action(
    db_session: Session | object,
    *,
    instance_id: str,
    control: MattermostInteractiveControl,
) -> tuple[int, Any] | None:
    if not isinstance(db_session, Session):
        return 0, None
    return _claim_control(db_session, instance_id=instance_id, control=control)


def _complete_claimed_control(
    db_session: Session | object,
    *,
    event_id: int,
    claim_owner: Any,
) -> bool:
    if not isinstance(db_session, Session):
        return True
    return complete_mattermost_control_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
    )


def _required_string(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Mattermost interactive action {key} is invalid")
    return value


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""
