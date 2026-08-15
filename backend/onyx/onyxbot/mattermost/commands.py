"""Mattermost-native slash command handling for Orka."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)

MATTERMOST_SLASH_COMMAND_REJECTED_MESSAGE = "This Orka slash command is not authorized."
MATTERMOST_SLASH_COMMAND_UNAVAILABLE_MESSAGE = (
    "Orka is not available in this Mattermost channel."
)
MATTERMOST_SLASH_COMMAND_ACCEPTED_MESSAGE = "Orka is answering in this channel."
MATTERMOST_SLASH_COMMAND_REPLAY_MESSAGE = "That Orka command was already handled."
MATTERMOST_SLASH_COMMAND_HELP_TEXT = (
    "Use `/orka ask <question>` to ask Orka, `/orka sources <question>` to ask "
    "for an answer with sources, `/orka status` to check command readiness, or "
    "`/orka help` to show this help."
)
MATTERMOST_SLASH_COMMAND_STATUS_TEXT = "Orka slash commands are ready."


class MattermostSlashCommandAction(StrEnum):
    ASK = "ask"
    HELP = "help"
    STATUS = "status"
    SOURCES = "sources"


class MattermostSlashCommandClient(Protocol):
    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...


@dataclass(frozen=True)
class MattermostSlashCommandControl:
    instance_id: str
    bot_user_id: str
    token: str
    enabled: bool


@dataclass(frozen=True)
class MattermostSlashCommandResponse:
    text: str
    response_type: str = "ephemeral"
    status_code: int = 200

    def as_mattermost_payload(self) -> dict[str, str]:
        return {"response_type": self.response_type, "text": self.text}


@dataclass(frozen=True)
class MattermostSlashCommandPayload:
    token: str
    team_id: str
    channel_id: str
    user_id: str
    command: str
    text: str
    trigger_id: str
    team_domain: str = ""
    channel_name: str = ""
    user_name: str = ""

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, str]
    ) -> "MattermostSlashCommandPayload":
        return cls(
            token=payload.get("token", ""),
            team_id=payload.get("team_id", "") or "global",
            team_domain=payload.get("team_domain", ""),
            channel_id=payload.get("channel_id", ""),
            channel_name=payload.get("channel_name", ""),
            user_id=payload.get("user_id", ""),
            user_name=payload.get("user_name", ""),
            command=payload.get("command", ""),
            text=payload.get("text", ""),
            trigger_id=payload.get("trigger_id", ""),
        )


async def handle_mattermost_slash_command(
    *,
    payload: Mapping[str, str],
    command_control: MattermostSlashCommandControl | None = None,
    expected_token: str | None = None,
    bot_user_id: str | None = None,
    client: MattermostSlashCommandClient,
    handle_event: Callable[[NormalizedMattermostEvent], Awaitable[bool]],
) -> MattermostSlashCommandResponse:
    """Validate and route one Mattermost slash command payload.

    Mattermost slash commands authenticate with a per-command token in the form
    body. Treat that token as the signed native command boundary and do no
    side effects until it matches.
    """

    command_payload = MattermostSlashCommandPayload.from_mapping(payload)
    expected_command_token = expected_token
    authorized_bot_user_id = bot_user_id
    if command_control is not None:
        if not command_control.enabled:
            return MattermostSlashCommandResponse(
                text=MATTERMOST_SLASH_COMMAND_REJECTED_MESSAGE,
                status_code=403,
            )
        expected_command_token = command_control.token
        authorized_bot_user_id = command_control.bot_user_id
    if authorized_bot_user_id is None:
        return MattermostSlashCommandResponse(
            text=MATTERMOST_SLASH_COMMAND_REJECTED_MESSAGE,
            status_code=403,
        )
    if not _is_valid_token(command_payload.token, expected_command_token):
        return MattermostSlashCommandResponse(
            text=MATTERMOST_SLASH_COMMAND_REJECTED_MESSAGE,
            status_code=403,
        )
    if not _has_required_identity(command_payload):
        return MattermostSlashCommandResponse(
            text=MATTERMOST_SLASH_COMMAND_UNAVAILABLE_MESSAGE,
        )

    action, command_text = _parse_command_text(command_payload.text)
    user_info = await _authorize_and_resolve_sender(
        client=client,
        bot_user_id=authorized_bot_user_id,
        channel_id=command_payload.channel_id,
        user_id=command_payload.user_id,
    )
    if user_info is None:
        return MattermostSlashCommandResponse(
            text=MATTERMOST_SLASH_COMMAND_UNAVAILABLE_MESSAGE,
        )

    if action is MattermostSlashCommandAction.HELP:
        return MattermostSlashCommandResponse(text=MATTERMOST_SLASH_COMMAND_HELP_TEXT)
    if action is MattermostSlashCommandAction.STATUS:
        return MattermostSlashCommandResponse(text=MATTERMOST_SLASH_COMMAND_STATUS_TEXT)
    if not command_text:
        return MattermostSlashCommandResponse(text=MATTERMOST_SLASH_COMMAND_HELP_TEXT)

    event = _build_slash_command_event(
        payload=command_payload,
        action=action,
        command_text=command_text,
        user_info=user_info,
    )
    handled = await handle_event(event)
    if not handled:
        return MattermostSlashCommandResponse(
            text=MATTERMOST_SLASH_COMMAND_REPLAY_MESSAGE
        )
    return MattermostSlashCommandResponse(
        text=MATTERMOST_SLASH_COMMAND_ACCEPTED_MESSAGE,
        response_type="in_channel",
    )


async def _authorize_and_resolve_sender(
    *,
    client: MattermostSlashCommandClient,
    bot_user_id: str,
    channel_id: str,
    user_id: str,
) -> MattermostUserInfo | None:
    try:
        bot_is_member = await client.is_channel_member(
            channel_id=channel_id,
            user_id=bot_user_id,
        )
        if not bot_is_member:
            return None
        sender_is_member = await client.is_channel_member(
            channel_id=channel_id,
            user_id=user_id,
        )
        if not sender_is_member:
            return None
        user_info = await client.get_user_info(user_id)
    except Exception:
        return None
    if user_info.id != user_id:
        return None
    return user_info


def _is_valid_token(token: str, expected_token: str | None) -> bool:
    if not expected_token:
        return False
    return hmac.compare_digest(token, expected_token)


def _has_required_identity(payload: MattermostSlashCommandPayload) -> bool:
    return bool(payload.team_id and payload.channel_id and payload.user_id)


def _parse_command_text(text: str) -> tuple[MattermostSlashCommandAction, str]:
    stripped_text = " ".join(text.split())
    if not stripped_text:
        return MattermostSlashCommandAction.HELP, ""
    action_text, _, rest = stripped_text.partition(" ")
    try:
        action = MattermostSlashCommandAction(action_text.casefold())
    except ValueError:
        return MattermostSlashCommandAction.ASK, stripped_text
    return action, rest.strip()


def _build_slash_command_event(
    *,
    payload: MattermostSlashCommandPayload,
    action: MattermostSlashCommandAction,
    command_text: str,
    user_info: MattermostUserInfo,
) -> NormalizedMattermostEvent:
    dedupe_key = _slash_command_dedupe_key(payload)
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.SLASH_COMMAND,
        session_key=(
            f"mattermost:slash:{payload.team_id}:{payload.channel_id}:{payload.user_id}"
        ),
        team_id=payload.team_id,
        channel_id=payload.channel_id,
        post_id=dedupe_key,
        root_post_id=dedupe_key,
        user_id=payload.user_id,
        text=command_text,
        raw_event_type="slash_command",
        metadata={"slash_command": payload.command, "slash_action": action.value},
        source_username=user_info.username or payload.user_name or None,
        source_display_name=user_info.display_name or None,
        dedupe_key=dedupe_key,
    )


def _slash_command_dedupe_key(payload: MattermostSlashCommandPayload) -> str:
    if payload.trigger_id:
        return (
            f"slash:{payload.team_id}:{payload.channel_id}:"
            f"{payload.user_id}:{payload.trigger_id}"
        )
    digest = hashlib.sha256(
        "\0".join(
            [payload.team_id, payload.channel_id, payload.user_id, payload.text]
        ).encode("utf-8")
    ).hexdigest()
    return f"slash:{payload.team_id}:{payload.channel_id}:{payload.user_id}:{digest}"
