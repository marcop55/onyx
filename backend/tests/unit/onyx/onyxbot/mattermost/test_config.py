"""Unit tests for Mattermost bot service config."""

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from onyx.onyxbot.mattermost.config import (
    MATTERMOST_BOT_ALLOWED_CHANNEL_IDS_ENV,
    MATTERMOST_BOT_PERSONA_ID_ENV,
    MATTERMOST_BOT_TOKEN_ENV,
    MATTERMOST_BOT_URL_ENV,
    MATTERMOST_BOT_USER_ID_ENV,
    MATTERMOST_SLASH_COMMAND_BOOTSTRAP_TOKEN_ENV,
    MattermostBotConfigError,
    canonical_mattermost_instance_id,
    load_mattermost_bot_config_from_env,
    redacted_mattermost_bot_env,
)
from onyx.onyxbot.mattermost.run import get_application

_REQUIRED_ENV = {
    MATTERMOST_BOT_URL_ENV: "https://mattermost.example.test",
    MATTERMOST_BOT_TOKEN_ENV: "mattermost-secret-token",
    MATTERMOST_BOT_PERSONA_ID_ENV: "7",
    MATTERMOST_BOT_USER_ID_ENV: "bot_user_1",
}


def test_canonical_instance_id_is_stable_and_installation_specific() -> None:
    assert (
        canonical_mattermost_instance_id("HTTPS://Mattermost.Example.Test:443/base/")
        == "https://mattermost.example.test/base"
    )
    assert canonical_mattermost_instance_id(
        "https://other.example.test/base"
    ) != canonical_mattermost_instance_id("https://mattermost.example.test/base")


@contextmanager
def _mattermost_env(values: dict[str, str]) -> Iterator[None]:
    original_values = {
        key: os.environ.get(key)
        for key in set(_REQUIRED_ENV) | set(values)
        if key.startswith("MATTERMOST_BOT_") or key.startswith("MATTERMOST_SLASH_")
    }
    for key in list(os.environ):
        if key.startswith("MATTERMOST_BOT_") or key.startswith("MATTERMOST_SLASH_"):
            os.environ.pop(key)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("MATTERMOST_BOT_") or key.startswith("MATTERMOST_SLASH_"):
                os.environ.pop(key)
        for key, value in original_values.items():
            if value is not None:
                os.environ[key] = value


@pytest.mark.parametrize(
    "missing_env",
    [
        MATTERMOST_BOT_URL_ENV,
        MATTERMOST_BOT_TOKEN_ENV,
        MATTERMOST_BOT_PERSONA_ID_ENV,
        MATTERMOST_BOT_USER_ID_ENV,
    ],
)
def test_missing_required_config_blocks_startup(missing_env: str) -> None:
    values = dict(_REQUIRED_ENV)
    values.pop(missing_env)

    with _mattermost_env(values), pytest.raises(MattermostBotConfigError) as exc_info:
        load_mattermost_bot_config_from_env()

    assert missing_env in str(exc_info.value)


def test_load_mattermost_bot_config_from_env() -> None:
    with _mattermost_env(
        {
            **_REQUIRED_ENV,
            MATTERMOST_BOT_ALLOWED_CHANNEL_IDS_ENV: "channel_1, channel_2",
            "MATTERMOST_BOT_ALLOWED_TEAM_IDS": "team_1",
            "MATTERMOST_BOT_APPROVED_USER_IDS": "user_1,user_2",
            "MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS": "channel_2",
            "MATTERMOST_BOT_MENTIONS": "@onyx,@bot_user_1",
            "MATTERMOST_BOT_HOST": "127.0.0.1",
            "MATTERMOST_BOT_PORT": "8092",
            MATTERMOST_SLASH_COMMAND_BOOTSTRAP_TOKEN_ENV: "slash-secret-token",
        }
    ):
        config = load_mattermost_bot_config_from_env()

    assert config.url == "https://mattermost.example.test"
    assert config.token == "mattermost-secret-token"
    assert config.persona_id == 7
    assert config.host == "127.0.0.1"
    assert config.port == 8092
    assert config.listener_config.bot_user_id == "bot_user_1"
    assert config.listener_config.bot_mentions == frozenset({"@onyx", "@bot_user_1"})
    assert config.listener_config.allowed_channel_ids == frozenset(
        {"channel_1", "channel_2"}
    )
    assert config.listener_config.allowed_team_ids == frozenset({"team_1"})
    assert config.listener_config.approved_user_ids == frozenset({"user_1", "user_2"})
    assert config.listener_config.root_post_channel_ids == frozenset({"channel_2"})
    assert config.slash_command_bootstrap_token == "slash-secret-token"


def test_empty_emergency_restrictions_are_valid() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()

    assert config.listener_config.allowed_channel_ids == frozenset()
    assert config.listener_config.allowed_team_ids == frozenset()
    assert config.listener_config.approved_user_ids == frozenset()


def test_redacted_mattermost_bot_env_never_returns_token_value() -> None:
    with _mattermost_env(
        {
            **_REQUIRED_ENV,
            MATTERMOST_SLASH_COMMAND_BOOTSTRAP_TOKEN_ENV: "slash-secret-token",
        }
    ):
        redacted = redacted_mattermost_bot_env()

    assert redacted[MATTERMOST_BOT_TOKEN_ENV] == "[redacted]"
    assert redacted[MATTERMOST_SLASH_COMMAND_BOOTSTRAP_TOKEN_ENV] == "[redacted]"
    assert "mattermost-secret-token" not in str(redacted)
    assert "slash-secret-token" not in str(redacted)


@pytest.mark.parametrize("public_host", ["0.0.0.0", "::"])
def test_public_bind_host_blocks_startup(public_host: str) -> None:
    with _mattermost_env({**_REQUIRED_ENV, "MATTERMOST_BOT_HOST": public_host}):
        with pytest.raises(MattermostBotConfigError, match="public address"):
            load_mattermost_bot_config_from_env()


def test_startup_fails_when_listener_exits_before_ready() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()

    async def stopped_runner(*_args: object) -> None:
        return

    with (
        patch("onyx.onyxbot.mattermost.run._run_bot", stopped_runner),
        pytest.raises(RuntimeError, match="listener exited before becoming ready"),
    ):
        app = get_application(config)
        with TestClient(app):
            pass


def test_health_becomes_unavailable_when_listener_exits() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()
    stop_listener = threading.Event()

    async def stopped_runner(*args: object) -> None:
        ready_event = args[1]
        assert isinstance(ready_event, asyncio.Event)
        ready_event.set()
        while not stop_listener.is_set():
            await asyncio.sleep(0.001)

    with patch("onyx.onyxbot.mattermost.run._run_bot", stopped_runner):
        app = get_application(config)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/health")
            assert response.status_code == 200
            stop_listener.set()
            for _ in range(50):
                response = client.get("/health")
                if response.status_code == 503:
                    break
                time.sleep(0.002)

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_startup_fails_when_listener_initialization_fails() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()

    async def failing_runner(*_args: object) -> None:
        raise RuntimeError("listener initialization failed")

    with (
        patch("onyx.onyxbot.mattermost.run._run_bot", failing_runner),
        pytest.raises(RuntimeError, match="listener initialization failed"),
    ):
        app = get_application(config)
        with TestClient(app):
            pass


def test_health_is_ready_only_while_listener_is_running() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()

    async def running_runner(*args: object) -> None:
        ready_event = args[1]
        assert isinstance(ready_event, asyncio.Event)
        ready_event.set()
        await asyncio.Event().wait()

    with patch("onyx.onyxbot.mattermost.run._run_bot", running_runner):
        app = get_application(config)
        with TestClient(app) as client:
            response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mattermost_bot_service_exposes_health_route() -> None:
    with _mattermost_env(_REQUIRED_ENV):
        config = load_mattermost_bot_config_from_env()

    app = get_application(config)
    route_paths = {route.path for route in app.routes if isinstance(route, Route)}

    assert "/health" in route_paths
    assert "/commands/orka" in route_paths
    assert "/commands/orka/{action_name}" in route_paths
