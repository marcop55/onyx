from __future__ import annotations

import pytest

from onyx.onyxbot.mattermost.commands import (
    MattermostSlashCommandControl,
    handle_mattermost_slash_command,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)

_BOT_USER_ID = "bot_user_1"
_COMMAND_TOKEN = "signed-token"
_INSTANCE_ID = "https://mattermost.example.test"


class _MattermostCommandClient:
    def __init__(
        self,
        *,
        bot_is_member: bool = True,
        sender_is_member: bool = True,
        raises: bool = False,
    ) -> None:
        self.bot_is_member = bot_is_member
        self.sender_is_member = sender_is_member
        self.raises = raises
        self.membership_checks: list[tuple[str, str]] = []

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        if self.raises:
            raise RuntimeError("mattermost unavailable")
        self.membership_checks.append((channel_id, user_id))
        if user_id == _BOT_USER_ID:
            return self.bot_is_member
        return self.sender_is_member

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        if self.raises:
            raise RuntimeError("mattermost unavailable")
        return MattermostUserInfo(
            id=user_id,
            username="ada",
            display_name="Ada Lovelace",
            roles="system_user",
        )


def _payload(**overrides: str) -> dict[str, str]:
    payload = {
        "token": _COMMAND_TOKEN,
        "team_id": "team_1",
        "team_domain": "oneqode",
        "channel_id": "channel_1",
        "channel_name": "town-square",
        "user_id": "user_1",
        "user_name": "ada",
        "command": "/orka",
        "text": "ask what changed?",
        "trigger_id": "trigger_1",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_orka_slash_command_uses_managed_instance_control_before_side_effects() -> (
    None
):
    handled = False

    async def handle(_event: NormalizedMattermostEvent) -> bool:
        nonlocal handled
        handled = True
        return True

    response = await handle_mattermost_slash_command(
        payload=_payload(),
        command_control=MattermostSlashCommandControl(
            instance_id=_INSTANCE_ID,
            bot_user_id=_BOT_USER_ID,
            token=_COMMAND_TOKEN,
            enabled=False,
        ),
        client=_MattermostCommandClient(),
        handle_event=handle,
    )

    assert response.status_code == 403
    assert response.response_type == "ephemeral"
    assert "not authorized" in response.text
    assert not handled


@pytest.mark.asyncio
async def test_orka_ask_slash_command_routes_authorized_member_to_durable_handler() -> (
    None
):
    handled_events: list[NormalizedMattermostEvent] = []

    async def handle(event: NormalizedMattermostEvent) -> bool:
        handled_events.append(event)
        return True

    response = await handle_mattermost_slash_command(
        payload=_payload(),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=handle,
    )

    assert response.status_code == 200
    assert response.response_type == "in_channel"
    assert handled_events == [
        NormalizedMattermostEvent(
            event_type=MattermostNormalizedEventType.SLASH_COMMAND,
            session_key="mattermost:slash:team_1:channel_1:user_1",
            team_id="team_1",
            channel_id="channel_1",
            post_id="slash:team_1:channel_1:user_1:trigger_1",
            root_post_id="slash:team_1:channel_1:user_1:trigger_1",
            user_id="user_1",
            text="what changed?",
            raw_event_type="slash_command",
            metadata={"slash_command": "/orka", "slash_action": "ask"},
            source_username="ada",
            source_display_name="Ada Lovelace",
            dedupe_key="slash:team_1:channel_1:user_1:trigger_1",
        )
    ]


@pytest.mark.asyncio
async def test_orka_ask_slash_command_fails_closed_when_sender_membership_denied() -> (
    None
):
    handled = False

    async def handle(_event: NormalizedMattermostEvent) -> bool:
        nonlocal handled
        handled = True
        return True

    client = _MattermostCommandClient(sender_is_member=False)

    response = await handle_mattermost_slash_command(
        payload=_payload(),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=client,
        handle_event=handle,
    )

    assert response.status_code == 200
    assert response.response_type == "ephemeral"
    assert "not available" in response.text
    assert not handled
    assert client.membership_checks == [
        ("channel_1", _BOT_USER_ID),
        ("channel_1", "user_1"),
    ]


@pytest.mark.asyncio
async def test_orka_ask_slash_command_preserves_stable_dedupe_key_on_replay() -> None:
    dedupe_keys: list[str] = []

    async def handle(event: NormalizedMattermostEvent) -> bool:
        dedupe_keys.append(event.dedupe_key)
        return len(dedupe_keys) == 1

    first_response = await handle_mattermost_slash_command(
        payload=_payload(),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=handle,
    )
    replay_response = await handle_mattermost_slash_command(
        payload=_payload(),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=handle,
    )

    assert first_response.status_code == 200
    assert replay_response.status_code == 200
    assert replay_response.response_type == "ephemeral"
    assert "already handled" in replay_response.text
    assert dedupe_keys == [
        "slash:team_1:channel_1:user_1:trigger_1",
        "slash:team_1:channel_1:user_1:trigger_1",
    ]


@pytest.mark.asyncio
async def test_orka_slash_command_rejects_unsigned_payload_before_side_effects() -> (
    None
):
    handled = False

    async def handle(_event: NormalizedMattermostEvent) -> bool:
        nonlocal handled
        handled = True
        return True

    response = await handle_mattermost_slash_command(
        payload=_payload(token="wrong"),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=handle,
    )

    assert response.status_code == 403
    assert response.response_type == "ephemeral"
    assert not handled


@pytest.mark.asyncio
async def test_orka_help_and_status_are_ephemeral_local_commands() -> None:
    async def unexpected_handle(_event: NormalizedMattermostEvent) -> bool:
        raise AssertionError("local commands must not hit Onyx chat")

    help_response = await handle_mattermost_slash_command(
        payload=_payload(text="help"),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=unexpected_handle,
    )
    status_response = await handle_mattermost_slash_command(
        payload=_payload(text="status"),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=unexpected_handle,
    )

    assert help_response.response_type == "ephemeral"
    assert "/orka ask" in help_response.text
    assert status_response.response_type == "ephemeral"
    assert "ready" in status_response.text


@pytest.mark.asyncio
async def test_orka_sources_routes_as_sources_action_without_narrowing_retrieval() -> (
    None
):
    handled_events: list[NormalizedMattermostEvent] = []

    async def handle(event: NormalizedMattermostEvent) -> bool:
        handled_events.append(event)
        return True

    response = await handle_mattermost_slash_command(
        payload=_payload(text="sources migration notes"),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(),
        handle_event=handle,
    )

    assert response.status_code == 200
    assert response.response_type == "in_channel"
    assert handled_events[0].text == "migration notes"
    assert handled_events[0].metadata == {
        "slash_command": "/orka",
        "slash_action": "sources",
    }


@pytest.mark.asyncio
async def test_orka_slash_command_fails_closed_on_mattermost_lookup_failure() -> None:
    async def unexpected_handle(_event: NormalizedMattermostEvent) -> bool:
        raise AssertionError("unauthorized commands must not hit Onyx chat")

    response = await handle_mattermost_slash_command(
        payload=_payload(),
        expected_token=_COMMAND_TOKEN,
        bot_user_id=_BOT_USER_ID,
        client=_MattermostCommandClient(raises=True),
        handle_event=unexpected_handle,
    )

    assert response.status_code == 200
    assert response.response_type == "ephemeral"
    assert "not available" in response.text
