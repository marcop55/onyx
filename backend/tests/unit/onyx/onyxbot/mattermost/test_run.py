from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import onyx.onyxbot.mattermost.run as run
from onyx.onyxbot.mattermost.config import MattermostBotConfig
from onyx.onyxbot.mattermost.handler import MattermostHandlerConfig
from onyx.onyxbot.mattermost.models import MattermostListenerConfig


def test_main_initializes_sql_engine_before_starting_uvicorn(monkeypatch: Any) -> None:
    calls: list[tuple[str, object]] = []
    config = SimpleNamespace(host="127.0.0.1", port=8181)
    app = object()

    class FakeSqlEngine:
        @staticmethod
        def init_engine(*, pool_size: int, max_overflow: int) -> None:
            calls.append(("init_engine", (pool_size, max_overflow)))

    monkeypatch.setattr(run, "SqlEngine", FakeSqlEngine, raising=False)
    monkeypatch.setattr(run, "load_mattermost_bot_config_from_env", lambda: config)
    monkeypatch.setattr(run, "redacted_mattermost_bot_env", lambda: {})
    monkeypatch.setattr(
        run,
        "get_application",
        lambda value: calls.append(("get_application", value)) or app,
    )
    monkeypatch.setattr(
        run.uvicorn,
        "run",
        lambda value, **_kwargs: calls.append(("uvicorn", value)),
    )

    run.main()

    assert calls == [
        (
            "init_engine",
            (
                run.POSTGRES_API_SERVER_POOL_SIZE,
                run.POSTGRES_API_SERVER_POOL_OVERFLOW,
            ),
        ),
        ("get_application", config),
        ("uvicorn", app),
    ]


def test_handler_config_hydrates_managed_private_answer_channels(
    monkeypatch: Any,
) -> None:
    db_session = MagicMock()
    config = MattermostBotConfig(
        persona_id=7,
        url="https://mattermost.example.test",
        token="mattermost-token",
        request_timeout_seconds=30,
        listener_config=MattermostListenerConfig(
            bot_user_id="bot-user-1",
            bot_mentions=frozenset({"@onyx"}),
            owned_thread_root_ids={"owned-root-1"},
            tombstoned_thread_root_ids={"deleted-root-1"},
            owned_answer_post_root_ids={"answer-post-1": "owned-root-1"},
            owned_answer_post_message_ids={"answer-post-1": 123},
        ),
    )
    monkeypatch.setattr(
        run,
        "fetch_mattermost_private_answer_channel_ids",
        lambda *_args, **_kwargs: frozenset({"channel-private-1"}),
    )

    handler_config = run._build_handler_config(config, db_session)

    assert handler_config.instance_id == "https://mattermost.example.test"
    assert handler_config.ephemeral_response_channel_ids == frozenset(
        {"channel-private-1"}
    )
    assert handler_config.owned_thread_root_ids == {"owned-root-1"}


def test_slash_command_runtime_uses_managed_private_answer_channels(
    monkeypatch: Any,
) -> None:
    captured_handler_config: list[MattermostHandlerConfig] = []
    config = SimpleNamespace(
        persona_id=7,
        url="https://mattermost.example.test",
        token="mattermost-token",
        request_timeout_seconds=30,
        slash_command_bootstrap_token=None,
        listener_config=MattermostListenerConfig(
            bot_user_id="bot-user-1",
            bot_mentions=frozenset({"@onyx"}),
        ),
    )

    class FakeRequest:
        async def form(self) -> "FakeForm":
            return FakeForm()

    class FakeForm:
        def multi_items(self) -> list[tuple[str, str]]:
            return [("token", "slash-token"), ("text", "hello")]

    class FakeMattermostClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeMattermostClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def fake_handle_slash_command(
        **kwargs: object,
    ) -> run.MattermostSlashCommandResponse:
        handle_event = cast(Any, kwargs["handle_event"])
        await handle_event(cast(Any, SimpleNamespace()))
        return run.MattermostSlashCommandResponse(text="ok")

    async def fake_handle_normalized_mattermost_event(**kwargs: object) -> bool:
        captured_handler_config.append(cast(MattermostHandlerConfig, kwargs["config"]))
        return True

    monkeypatch.setattr(run, "MattermostClient", FakeMattermostClient)
    monkeypatch.setattr(
        run, "handle_mattermost_slash_command", fake_handle_slash_command
    )
    monkeypatch.setattr(
        run,
        "handle_normalized_mattermost_event",
        fake_handle_normalized_mattermost_event,
    )
    monkeypatch.setattr(
        run,
        "get_or_bootstrap_mattermost_slash_command_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        run,
        "fetch_mattermost_private_answer_channel_ids",
        lambda *_args, **_kwargs: frozenset({"channel-private-1"}),
    )
    monkeypatch.setattr(run, "get_session_with_current_tenant", lambda: _DbContext())

    response = run.asyncio.run(
        run._handle_slash_command_request(cast(Any, FakeRequest()), cast(Any, config))
    )

    assert response.status_code == 200
    assert len(captured_handler_config) == 1
    assert captured_handler_config[0].ephemeral_response_channel_ids == frozenset(
        {"channel-private-1"}
    )


class _DbContext:
    def __enter__(self) -> MagicMock:
        return MagicMock()

    def __exit__(self, *_args: object) -> None:
        return None
