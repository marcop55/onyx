from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import onyx.onyxbot.mattermost.run as run


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
