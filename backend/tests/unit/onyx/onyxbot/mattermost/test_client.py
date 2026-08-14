from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import aiohttp
import pytest

from onyx.onyxbot.mattermost.client import MattermostClient, MattermostResponseError


class _FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        payload: dict[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        text: str = "rate limited",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def json(self) -> dict[str, object]:
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = iter(responses)
        self.request_count = 0

    def request(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        self.request_count += 1
        return next(self._responses)


@pytest.mark.asyncio
async def test_rate_limit_retry_honors_retry_after_with_cap_then_succeeds() -> None:
    session = _FakeSession(
        [
            _FakeResponse(429, headers={"Retry-After": "10"}),
            _FakeResponse(
                201,
                payload={
                    "id": "post-1",
                    "channel_id": "channel-1",
                    "message": "hello",
                    "root_id": "",
                },
            ),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
        max_rate_limit_retries=2,
        max_rate_limit_backoff_seconds=3.0,
        sleep=record_sleep,
    )

    post = await client.create_post(channel_id="channel-1", message="hello")

    assert post.id == "post-1"
    assert session.request_count == 2
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_rate_limit_retry_is_bounded_and_surfaces_final_failure() -> None:
    session = _FakeSession(
        [
            _FakeResponse(429, headers={"Retry-After": "1"}),
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(429, headers={"Retry-After": "30"}),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
        max_rate_limit_retries=2,
        max_rate_limit_backoff_seconds=4.0,
        sleep=record_sleep,
    )

    with pytest.raises(MattermostResponseError) as exc_info:
        await client.create_post(channel_id="channel-1", message="hello")

    assert exc_info.value.status_code == 429
    assert session.request_count == 3
    assert sleeps == [1.0, 2.0]
