from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import aiohttp
import pytest

from onyx.onyxbot.mattermost.client import (
    MattermostClient,
    MattermostClientError,
    MattermostResponseError,
)


class _FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        payload: dict[str, object] | None = None,
        content: bytes = b"",
        headers: Mapping[str, str] | None = None,
        text: str = "rate limited",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload or {}
        self._content = content
        self._text = text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def json(self) -> dict[str, object]:
        return self._payload

    async def read(self) -> bytes:
        return self._content


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = iter(responses)
        self.request_count = 0
        self.requests: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def request(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.request_count += 1
        self.requests.append((args, kwargs))
        return next(self._responses)


@pytest.mark.asyncio
async def test_create_post_sends_stable_pending_id_and_reconciliation_props() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                201,
                payload={
                    "id": "post-1",
                    "channel_id": "channel-1",
                    "message": "...",
                    "pending_post_id": "stable-pending-id",
                    "props": {"onyx_event_key": "ledger-key"},
                },
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    post = await client.create_post(
        channel_id="channel-1",
        message="...",
        pending_post_id="stable-pending-id",
        props={"onyx_event_key": "ledger-key"},
    )

    assert session.requests[0][1]["json"] == {
        "channel_id": "channel-1",
        "message": "...",
        "root_id": "",
        "pending_post_id": "stable-pending-id",
        "props": {"onyx_event_key": "ledger-key"},
    }
    assert post.pending_post_id == "stable-pending-id"
    assert post.props == {"onyx_event_key": "ledger-key"}


@pytest.mark.asyncio
async def test_find_post_by_idempotency_fields_matches_stored_post() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={
                    "order": ["other", "post-1"],
                    "posts": {
                        "other": {"id": "other", "channel_id": "channel-1"},
                        "post-1": {
                            "id": "post-1",
                            "channel_id": "channel-1",
                            "pending_post_id": "stable-pending-id",
                            "props": {"onyx_event_key": "ledger-key"},
                        },
                    },
                },
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    post = await client.find_post_by_idempotency_fields(
        channel_id="channel-1",
        pending_post_id="stable-pending-id",
        event_key="ledger-key",
    )

    assert post is not None
    assert post.id == "post-1"
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/channels/channel-1/posts?page=0&per_page=200",
    )


@pytest.mark.asyncio
async def test_get_thread_posts_fetches_thread_and_preserves_latest_post_state() -> (
    None
):
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={
                    "order": ["root-1", "reply-1", "deleted-1"],
                    "posts": {
                        "root-1": {
                            "id": "root-1",
                            "channel_id": "channel-1",
                            "message": "root text",
                            "user_id": "user-1",
                            "create_at": 100,
                        },
                        "reply-1": {
                            "id": "reply-1",
                            "channel_id": "channel-1",
                            "root_id": "root-1",
                            "message": "edited reply",
                            "user_id": "user-2",
                            "create_at": 200,
                            "update_at": 250,
                        },
                        "deleted-1": {
                            "id": "deleted-1",
                            "channel_id": "channel-1",
                            "root_id": "root-1",
                            "message": "deleted reply",
                            "user_id": "user-3",
                            "create_at": 300,
                            "delete_at": 350,
                        },
                    },
                },
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    posts = await client.get_thread_posts("root-1")

    assert [post.id for post in posts] == ["root-1", "reply-1", "deleted-1"]
    assert posts[1].message == "edited reply"
    assert posts[1].update_at == 250
    assert posts[2].delete_at == 350
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/posts/root-1/thread",
    )


@pytest.mark.asyncio
async def test_is_channel_member_checks_current_membership() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={"channel_id": "channel-1", "user_id": "user-1"},
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    assert await client.is_channel_member(channel_id="channel-1", user_id="user-1")
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/channels/channel-1/members/user-1",
    )


@pytest.mark.asyncio
async def test_is_channel_member_returns_false_when_user_was_removed() -> None:
    session = _FakeSession([_FakeResponse(404, text="not found")])
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    assert not await client.is_channel_member(channel_id="channel-1", user_id="user-1")


@pytest.mark.asyncio
async def test_get_channel_by_name_resolves_with_team_scope() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={"id": "channel-id-1", "name": "town-square"},
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    channel = await client.get_channel_by_name(
        team_id="team-1",
        channel_name="town-square",
    )

    assert channel == {"id": "channel-id-1", "name": "town-square"}
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/teams/team-1/channels/name/town-square",
    )


@pytest.mark.asyncio
async def test_get_file_info_preserves_authoritative_metadata() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={
                    "id": "file-1",
                    "user_id": "uploader-1",
                    "post_id": "post-1",
                    "name": "brief.pdf",
                    "mime_type": "application/pdf",
                    "size": 1234,
                    "create_at": 1786720000123,
                },
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    info = await client.get_file_info("file-1")

    assert info.id == "file-1"
    assert info.uploader_user_id == "uploader-1"
    assert info.post_id == "post-1"
    assert info.filename == "brief.pdf"
    assert info.mime_type == "application/pdf"
    assert info.size_bytes == 1234
    assert info.create_at == 1786720000123
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/files/file-1/info",
    )


@pytest.mark.asyncio
async def test_get_user_info_preserves_sender_attribution() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                payload={
                    "id": "user-1",
                    "username": "ada",
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "nickname": "Countess",
                    "roles": "system_user system_admin",
                },
            )
        ]
    )
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    info = await client.get_user_info("user-1")

    assert info.id == "user-1"
    assert info.username == "ada"
    assert info.display_name == "Ada Lovelace"
    assert info.roles == "system_user system_admin"
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/users/user-1",
    )


@pytest.mark.asyncio
async def test_download_file_returns_bot_authorized_content() -> None:
    session = _FakeSession([_FakeResponse(200, content=b"report bytes")])
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, session)),
    )

    content = await client.download_file("file-1")

    assert content == b"report bytes"
    assert session.requests[0][0][:2] == (
        "GET",
        "https://mattermost.example.test/api/v4/files/file-1",
    )


class _RaisingSession:
    def request(self, *args: object, **kwargs: object) -> _FakeResponse:
        _ = args, kwargs
        raise TimeoutError("response lost after remote commit")


@pytest.mark.asyncio
async def test_create_post_normalizes_ambiguous_transport_timeout() -> None:
    client = MattermostClient(
        "https://mattermost.example.test",
        "dummy-token",
        session=cast(aiohttp.ClientSession, cast(Any, _RaisingSession())),
    )

    with pytest.raises(MattermostClientError, match="POST transport failed"):
        await client.create_post(
            channel_id="channel-1",
            message="...",
            pending_post_id="stable-pending-id",
            props={"onyx_event_key": "ledger-key"},
        )


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
