from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import onyx.onyxbot.mattermost.run as run
from onyx.onyxbot.mattermost.config import MattermostBotConfig
from onyx.onyxbot.mattermost.handler import MattermostHandlerConfig
from onyx.onyxbot.mattermost.models import (
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.placement import (
    APPROVED_ONEQODE_ROOTS,
    SeafileHierarchyEvidence,
)


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
    assert handler_config.interactive_url == "http://127.0.0.1:8091/interactive"


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


def test_listener_runtime_reloads_managed_private_answer_channels_per_event(
    monkeypatch: Any,
) -> None:
    captured_handler_config: list[MattermostHandlerConfig] = []
    config = MattermostBotConfig(
        persona_id=7,
        url="https://mattermost.example.test",
        token="mattermost-token",
        request_timeout_seconds=30,
        listener_config=MattermostListenerConfig(
            bot_user_id="bot-user-1",
            bot_mentions=frozenset({"@onyx"}),
        ),
    )
    events = [_event("channel-public-1"), _event("channel-private-1")]

    class FakeMattermostClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeMattermostClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get_me(self) -> object:
            return object()

    class FakeMattermostEventListener:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
            for event in events:
                yield event

    def fake_build_handler_config(
        config: MattermostBotConfig,
        _db_session: object,
    ) -> MattermostHandlerConfig:
        channel_id = events[len(captured_handler_config)].channel_id
        return MattermostHandlerConfig(
            persona_id=config.persona_id,
            instance_id="https://mattermost.example.test",
            bot_user_id="bot-user-1",
            ephemeral_response_channel_ids=frozenset({channel_id}),
        )

    async def fake_handle_normalized_mattermost_event(**kwargs: object) -> bool:
        captured_handler_config.append(cast(MattermostHandlerConfig, kwargs["config"]))
        if len(captured_handler_config) == len(events):
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(run, "MattermostClient", FakeMattermostClient)
    monkeypatch.setattr(run, "MattermostEventListener", FakeMattermostEventListener)
    monkeypatch.setattr(run, "_build_handler_config", fake_build_handler_config)
    monkeypatch.setattr(
        run,
        "handle_normalized_mattermost_event",
        fake_handle_normalized_mattermost_event,
    )
    monkeypatch.setattr(run, "hydrate_mattermost_listener_config", lambda *_args: None)
    monkeypatch.setattr(run, "get_session_with_current_tenant", lambda: _DbContext())

    try:
        run.asyncio.run(run._run_bot(config))
    except asyncio.CancelledError:
        pass

    assert [
        handler_config.ephemeral_response_channel_ids
        for handler_config in captured_handler_config
    ] == [frozenset({"channel-public-1"}), frozenset({"channel-private-1"})]


def test_listener_initializes_attachment_hierarchy_before_first_event(
    monkeypatch: Any,
) -> None:
    captured_hierarchies: list[SeafileHierarchyEvidence | None] = []
    config = _listener_config(mutation_gateway_factory="tests.fake_gateway:create")
    hierarchy = _hierarchy("revision-current")

    class FakeMattermostClient(_FakeMattermostClient):
        pass

    class FakeBridge:
        def get_mattermost_attachment_placement_hierarchy(
            self,
        ) -> SeafileHierarchyEvidence:
            return hierarchy

    class FakeMattermostEventListener:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
            yield _event("channel-public-1")

    async def fake_handle_normalized_mattermost_event(**kwargs: object) -> bool:
        handler_config = cast(MattermostHandlerConfig, kwargs["config"])
        assert handler_config.attachment_placement_hierarchy_provider is not None
        captured_hierarchies.append(
            handler_config.attachment_placement_hierarchy_provider(
                cast(NormalizedMattermostEvent, kwargs["event"])
            )
        )
        raise asyncio.CancelledError

    _patch_listener_runtime(
        monkeypatch,
        client_cls=FakeMattermostClient,
        listener_cls=FakeMattermostEventListener,
        bridge=FakeBridge(),
        handle_event=fake_handle_normalized_mattermost_event,
    )

    try:
        run.asyncio.run(run._run_bot(config))
    except asyncio.CancelledError:
        pass

    assert captured_hierarchies == [hierarchy]


def test_listener_ready_waits_for_initial_attachment_hierarchy_refresh(
    monkeypatch: Any,
) -> None:
    captured_ready_states: list[bool] = []
    ready_event = asyncio.Event()
    config = _listener_config(mutation_gateway_factory="tests.fake_gateway:create")

    class FakeBridge:
        def __init__(self) -> None:
            self.calls = 0

        def get_mattermost_attachment_placement_hierarchy(
            self,
        ) -> SeafileHierarchyEvidence:
            self.calls += 1
            captured_ready_states.append(ready_event.is_set())
            return _hierarchy(f"revision-{self.calls}")

    class FakeMattermostEventListener:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
            yield _event("channel-public-1")

    async def fake_handle_normalized_mattermost_event(**_kwargs: object) -> bool:
        raise asyncio.CancelledError

    _patch_listener_runtime(
        monkeypatch,
        client_cls=_FakeMattermostClient,
        listener_cls=FakeMattermostEventListener,
        bridge=FakeBridge(),
        handle_event=fake_handle_normalized_mattermost_event,
    )

    try:
        run.asyncio.run(run._run_bot(config, ready_event))
    except asyncio.CancelledError:
        pass

    assert captured_ready_states[0] is False
    assert ready_event.is_set()


def test_listener_refreshes_attachment_hierarchy_on_bounded_cadence(
    monkeypatch: Any,
) -> None:
    captured_revisions: list[str | None] = []
    config = _listener_config(mutation_gateway_factory="tests.fake_gateway:create")
    monkeypatch.setattr(
        run,
        "MATTERMOST_ATTACHMENT_PLACEMENT_HIERARCHY_REFRESH_SECONDS",
        0.001,
    )

    class FakeBridge:
        def __init__(self) -> None:
            self.calls = 0

        def get_mattermost_attachment_placement_hierarchy(
            self,
        ) -> SeafileHierarchyEvidence:
            self.calls += 1
            return _hierarchy(f"revision-{self.calls}")

    class FakeMattermostEventListener:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
            yield _event("channel-public-1")
            await asyncio.sleep(0.01)
            yield _event("channel-public-2")

    async def fake_handle_normalized_mattermost_event(**kwargs: object) -> bool:
        handler_config = cast(MattermostHandlerConfig, kwargs["config"])
        assert handler_config.attachment_placement_hierarchy_provider is not None
        hierarchy = handler_config.attachment_placement_hierarchy_provider(
            cast(NormalizedMattermostEvent, kwargs["event"])
        )
        captured_revisions.append(hierarchy.root_revision if hierarchy else None)
        if len(captured_revisions) == 2:
            raise asyncio.CancelledError
        return True

    _patch_listener_runtime(
        monkeypatch,
        client_cls=_FakeMattermostClient,
        listener_cls=FakeMattermostEventListener,
        bridge=FakeBridge(),
        handle_event=fake_handle_normalized_mattermost_event,
    )

    try:
        run.asyncio.run(run._run_bot(config))
    except asyncio.CancelledError:
        pass

    assert captured_revisions[0] == "revision-1"
    assert captured_revisions[1] not in {None, "revision-1"}


def test_listener_fails_closed_when_initial_attachment_hierarchy_is_unavailable(
    monkeypatch: Any,
) -> None:
    handled_events: list[NormalizedMattermostEvent] = []
    ready_event = asyncio.Event()
    config = _listener_config(mutation_gateway_factory="tests.fake_gateway:create")

    class FakeBridge:
        def get_mattermost_attachment_placement_hierarchy(self) -> None:
            return None

    class FakeMattermostEventListener:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
            yield _event("channel-public-1")

    async def fake_handle_normalized_mattermost_event(**kwargs: object) -> bool:
        handled_events.append(cast(NormalizedMattermostEvent, kwargs["event"]))
        return True

    _patch_listener_runtime(
        monkeypatch,
        client_cls=_FakeMattermostClient,
        listener_cls=FakeMattermostEventListener,
        bridge=FakeBridge(),
        handle_event=fake_handle_normalized_mattermost_event,
    )

    try:
        run.asyncio.run(run._run_bot(config, ready_event))
    except RuntimeError as error:
        assert str(error) == "Mattermost attachment placement hierarchy is unavailable"
    else:
        raise AssertionError(
            "listener accepted events without initial hierarchy evidence"
        )

    assert not ready_event.is_set()
    assert handled_events == []


class _DbContext:
    def __enter__(self) -> MagicMock:
        return MagicMock()

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeMattermostClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_FakeMattermostClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_me(self) -> object:
        return object()


def _listener_config(
    *, mutation_gateway_factory: str | None = None
) -> MattermostBotConfig:
    return MattermostBotConfig(
        persona_id=7,
        url="https://mattermost.example.test",
        token="mattermost-token",
        request_timeout_seconds=30,
        mutation_gateway_factory=mutation_gateway_factory,
        listener_config=MattermostListenerConfig(
            bot_user_id="bot-user-1",
            bot_mentions=frozenset({"@onyx"}),
        ),
    )


def _patch_listener_runtime(
    monkeypatch: Any,
    *,
    client_cls: type[object],
    listener_cls: type[object],
    bridge: object,
    handle_event: Any,
) -> None:
    monkeypatch.setattr(run, "MattermostClient", client_cls)
    monkeypatch.setattr(run, "MattermostEventListener", listener_cls)
    monkeypatch.setattr(
        run.AuthoritativePlatformGatewayBridge,
        "from_factory_spec",
        lambda _factory_spec: bridge,
    )
    monkeypatch.setattr(run, "_build_handler_config", _fake_build_handler_config)
    monkeypatch.setattr(run, "handle_normalized_mattermost_event", handle_event)
    monkeypatch.setattr(run, "hydrate_mattermost_listener_config", lambda *_args: None)
    monkeypatch.setattr(run, "get_session_with_current_tenant", lambda: _DbContext())


def _fake_build_handler_config(
    config: MattermostBotConfig,
    _db_session: object,
) -> MattermostHandlerConfig:
    return MattermostHandlerConfig(
        persona_id=config.persona_id,
        instance_id="https://mattermost.example.test",
        bot_user_id="bot-user-1",
    )


def _hierarchy(root_revision: str) -> SeafileHierarchyEvidence:
    return SeafileHierarchyEvidence(
        library_id="repo1",
        library_name="OneQode",
        root_path="/",
        root_revision=root_revision,
        approved_roots=APPROVED_ONEQODE_ROOTS,
        files=(),
    )


def _event(channel_id: str) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key=f"mattermost:channel:team-1:{channel_id}:post-root-1",
        team_id="team-1",
        channel_id=channel_id,
        post_id="post-root-1",
        root_post_id="post-root-1",
        user_id="user-1",
        text="hello",
        raw_event_type="posted",
        dedupe_key=f"event:{channel_id}",
    )
