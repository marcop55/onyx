from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from onyx.db.mattermost_bot import (
    fetch_mattermost_channel_config_for_bot_and_channel,
)
from onyx.onyxbot.mattermost.handler import (
    MattermostHandlerConfig,
    _build_mattermost_context,
    _resolve_mattermost_channel_config,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.server.manage.mattermost_bot import _form_channel_config
from onyx.server.manage.models import (
    MattermostChannelConfigCreationRequest,
    MattermostResponseStyle,
)


def test_success_resolves_channel_override_agent_and_response_style() -> None:
    db_session = MagicMock()
    event = _event(text="summarise this")
    config = MattermostHandlerConfig(
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
        persona_id=10,
    )
    channel_config = SimpleNamespace(
        persona_id=20,
        channel_config={"response_style": "orka_concise", "disabled": False},
    )

    with patch(
        "onyx.onyxbot.mattermost.handler.fetch_mattermost_channel_config_for_bot_and_channel",
        return_value=channel_config,
    ) as mock_fetch:
        resolved = _resolve_mattermost_channel_config(
            db_session=db_session,
            event=event,
            config=config,
        )

    assert resolved is channel_config
    mock_fetch.assert_called_once_with(
        db_session,
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
        channel_id="channel-1",
    )
    context = _build_mattermost_context(event, response_style="orka_concise")
    assert (
        "selected Onyx Agent Instructions remain the only base personality source"
        in context
    )
    assert "preserve citations plus safety-critical detail" in context


def test_authorization_denial_cannot_be_bypassed_by_channel_config_without_bot_identity() -> (
    None
):
    with patch(
        "onyx.onyxbot.mattermost.handler.fetch_mattermost_channel_config_for_bot_and_channel"
    ) as mock_fetch:
        resolved = _resolve_mattermost_channel_config(
            db_session=MagicMock(),
            event=_event(text="hello"),
            config=MattermostHandlerConfig(persona_id=10, bot_user_id=None),
        )

    assert resolved is None
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_channel_config_fails_closed_without_visible_answer() -> None:
    client = MagicMock()
    client.create_post = AsyncMock()
    channel_config = SimpleNamespace(
        persona_id=20,
        channel_config={"response_style": "orka_concise", "disabled": True},
    )

    with patch(
        "onyx.onyxbot.mattermost.handler.fetch_mattermost_channel_config_for_bot_and_channel",
        return_value=channel_config,
    ):
        handled = await handle_normalized_mattermost_event(
            event=_event(text="should not answer"),
            config=MattermostHandlerConfig(
                instance_id="https://mattermost.example.test",
                bot_user_id="bot-user-1",
                persona_id=10,
            ),
            client=client,
            db_session=MagicMock(),
        )

    assert handled is False
    client.create_post.assert_not_called()


def test_replay_safe_managed_config_preserves_bounded_controls_only() -> None:
    request = MattermostChannelConfigCreationRequest(
        mattermost_bot_id=1,
        channel_id="channel-1",
        channel_name="Design",
        persona_id=42,
        respond_tag_only=True,
        response_style=MattermostResponseStyle.ORKA_CONCISE,
        disabled=False,
    )

    config = _form_channel_config(request)

    assert config == {
        "channel_name": "Design",
        "respond_tag_only": True,
        "response_style": "orka_concise",
        "disabled": False,
    }
    assert "system_prompt" not in config
    assert "instructions" not in config


def test_disabled_channel_config_row_falls_back_to_enabled_default() -> None:
    disabled_channel_config = SimpleNamespace(
        id=20,
        channel_config={"respond_tag_only": True},
    )
    default_config = SimpleNamespace(
        id=10,
        channel_config={"respond_tag_only": False},
    )
    db_session = MagicMock()
    scalar_calls = 0

    def scalar(stmt: object) -> object | None:
        nonlocal scalar_calls
        scalar_calls += 1
        if scalar_calls == 1:
            return SimpleNamespace(id=1)
        stmt_text = str(stmt)
        if scalar_calls == 2:
            if "mattermost_channel_config.enabled IS true" in stmt_text:
                return None
            return disabled_channel_config
        return default_config

    db_session.scalar.side_effect = scalar

    resolved = fetch_mattermost_channel_config_for_bot_and_channel(
        db_session,
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
        channel_id="channel-1",
    )

    assert resolved is default_config
    assert resolved is not None
    assert resolved.channel_config["respond_tag_only"] is False


def _event(*, text: str) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-root-1",
        root_post_id="post-root-1",
        user_id="user-1",
        text=text,
        raw_event_type="posted",
        dedupe_key="event_id:post-root-1",
    )
