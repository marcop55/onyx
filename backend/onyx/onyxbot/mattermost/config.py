"""Environment config for the Mattermost bot service."""

from __future__ import annotations

import os
from dataclasses import dataclass

from onyx.onyxbot.mattermost.models import MattermostListenerConfig

MATTERMOST_BOT_URL_ENV = "MATTERMOST_BOT_URL"
MATTERMOST_BOT_TOKEN_ENV = "MATTERMOST_BOT_TOKEN"
MATTERMOST_BOT_PERSONA_ID_ENV = "MATTERMOST_BOT_PERSONA_ID"
MATTERMOST_BOT_USER_ID_ENV = "MATTERMOST_BOT_USER_ID"
MATTERMOST_BOT_MENTIONS_ENV = "MATTERMOST_BOT_MENTIONS"
MATTERMOST_BOT_ALLOWED_CHANNEL_IDS_ENV = "MATTERMOST_BOT_ALLOWED_CHANNEL_IDS"
MATTERMOST_BOT_ALLOWED_TEAM_IDS_ENV = "MATTERMOST_BOT_ALLOWED_TEAM_IDS"
MATTERMOST_BOT_APPROVED_USER_IDS_ENV = "MATTERMOST_BOT_APPROVED_USER_IDS"
MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS_ENV = "MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS"
MATTERMOST_BOT_HOST_ENV = "MATTERMOST_BOT_HOST"
MATTERMOST_BOT_PORT_ENV = "MATTERMOST_BOT_PORT"
MATTERMOST_BOT_REQUEST_TIMEOUT_SECONDS_ENV = "MATTERMOST_BOT_REQUEST_TIMEOUT_SECONDS"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8091
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_BOT_MENTIONS = frozenset({"@onyx"})

_SECRET_ENV_VARS = frozenset({MATTERMOST_BOT_TOKEN_ENV})


class MattermostBotConfigError(ValueError):
    """Raised when the Mattermost bot service is not safe to start."""


@dataclass(frozen=True)
class MattermostBotConfig:
    """Fail-closed config for the private Mattermost bot service."""

    url: str
    token: str
    persona_id: int
    listener_config: MattermostListenerConfig
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    request_timeout_seconds: int = _DEFAULT_REQUEST_TIMEOUT_SECONDS


def load_mattermost_bot_config_from_env() -> MattermostBotConfig:
    """Load config from env and fail closed when required values are absent."""

    url = _required_env(MATTERMOST_BOT_URL_ENV)
    token = _required_env(MATTERMOST_BOT_TOKEN_ENV)
    persona_id = _required_int_env(MATTERMOST_BOT_PERSONA_ID_ENV)
    bot_user_id = _required_env(MATTERMOST_BOT_USER_ID_ENV)
    allowed_channel_ids = _required_csv_env(MATTERMOST_BOT_ALLOWED_CHANNEL_IDS_ENV)

    listener_config = MattermostListenerConfig(
        bot_user_id=bot_user_id,
        bot_mentions=_csv_env(MATTERMOST_BOT_MENTIONS_ENV) or _DEFAULT_BOT_MENTIONS,
        allowed_channel_ids=allowed_channel_ids,
        allowed_team_ids=_csv_env(MATTERMOST_BOT_ALLOWED_TEAM_IDS_ENV),
        approved_user_ids=_csv_env(MATTERMOST_BOT_APPROVED_USER_IDS_ENV),
        root_post_channel_ids=_csv_env(MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS_ENV),
    )

    return MattermostBotConfig(
        url=url,
        token=token,
        persona_id=persona_id,
        listener_config=listener_config,
        host=_private_bind_host_env(MATTERMOST_BOT_HOST_ENV),
        port=_int_env(MATTERMOST_BOT_PORT_ENV, _DEFAULT_PORT),
        request_timeout_seconds=_int_env(
            MATTERMOST_BOT_REQUEST_TIMEOUT_SECONDS_ENV,
            _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
    )


def redacted_mattermost_bot_env() -> dict[str, str]:
    """Return configured Mattermost env values with secrets redacted."""

    names = [
        MATTERMOST_BOT_URL_ENV,
        MATTERMOST_BOT_TOKEN_ENV,
        MATTERMOST_BOT_PERSONA_ID_ENV,
        MATTERMOST_BOT_USER_ID_ENV,
        MATTERMOST_BOT_MENTIONS_ENV,
        MATTERMOST_BOT_ALLOWED_CHANNEL_IDS_ENV,
        MATTERMOST_BOT_ALLOWED_TEAM_IDS_ENV,
        MATTERMOST_BOT_APPROVED_USER_IDS_ENV,
        MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS_ENV,
        MATTERMOST_BOT_HOST_ENV,
        MATTERMOST_BOT_PORT_ENV,
        MATTERMOST_BOT_REQUEST_TIMEOUT_SECONDS_ENV,
    ]
    redacted: dict[str, str] = {}
    for name in names:
        value = _env(name)
        if value is None:
            continue
        redacted[name] = "[redacted]" if name in _SECRET_ENV_VARS else value
    return redacted


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped_value = value.strip()
    return stripped_value or None


def _required_env(name: str) -> str:
    value = _env(name)
    if value is None:
        raise MattermostBotConfigError(f"{name} is required")
    return value


def _csv_env(name: str) -> frozenset[str]:
    value = _env(name)
    if value is None:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _required_csv_env(name: str) -> frozenset[str]:
    values = _csv_env(name)
    if not values:
        raise MattermostBotConfigError(f"{name} must include at least one value")
    return values


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed_value = int(value)
    except ValueError as exc:
        raise MattermostBotConfigError(f"{name} must be an integer") from exc
    if parsed_value <= 0:
        raise MattermostBotConfigError(f"{name} must be greater than 0")
    return parsed_value


def _required_int_env(name: str) -> int:
    _required_env(name)
    return _int_env(name, default=1)


def _private_bind_host_env(name: str) -> str:
    host = _env(name) or _DEFAULT_HOST
    if host in {"0.0.0.0", "::"}:  # noqa: S104 - this rejects public binds.
        raise MattermostBotConfigError(f"{name} must not bind to a public address")
    return host
