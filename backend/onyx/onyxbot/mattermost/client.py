"""Typed Mattermost REST and WebSocket client."""

from collections.abc import AsyncIterator, Mapping
from typing import cast

import aiohttp

from onyx.onyxbot.mattermost.models import MattermostEventEnvelope, MattermostPost


class MattermostClientError(Exception):
    """Base Mattermost client error."""


class MattermostResponseError(MattermostClientError):
    """Mattermost returned an unsuccessful response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MattermostClient:
    """Async client for Mattermost REST and WebSocket APIs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout_seconds: int = 30,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._request_timeout_seconds = request_timeout_seconds
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "MattermostClient":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def initialize(self) -> None:
        """Create the HTTP session when the caller did not inject one."""
        if self._session is not None:
            return

        timeout = aiohttp.ClientTimeout(total=self._request_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers)

    async def close(self) -> None:
        """Close an owned HTTP session."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @property
    def _websocket_url(self) -> str:
        if self._base_url.startswith("https://"):
            return "wss://" + self._base_url.removeprefix("https://")
        if self._base_url.startswith("http://"):
            return "ws://" + self._base_url.removeprefix("http://")
        return self._base_url

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise MattermostClientError("Mattermost client is not initialized")
        return self._session

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
    ) -> MattermostPost:
        """Create a Mattermost post."""
        response = await self._request_json(
            "POST",
            "/api/v4/posts",
            json={"channel_id": channel_id, "message": message, "root_id": root_id},
        )
        return _post_from_mapping(cast(Mapping[object, object], response))

    async def update_post(self, *, post_id: str, message: str) -> MattermostPost:
        """Update a Mattermost post message."""
        response = await self._request_json(
            "PUT",
            f"/api/v4/posts/{post_id}",
            json={"id": post_id, "message": message},
        )
        return _post_from_mapping(cast(Mapping[object, object], response))

    async def get_me(self) -> dict[str, object]:
        """Return the authenticated Mattermost user."""
        return await self._request_json("GET", "/api/v4/users/me")

    async def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        """Connect to Mattermost WebSocket events and yield event envelopes."""
        session = self._require_session()
        url = f"{self._websocket_url}/api/v4/websocket"
        async with session.ws_connect(url, headers=self._headers) as websocket:
            async for message in websocket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                if not isinstance(data, dict):
                    continue
                yield mattermost_event_from_payload(data)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        session = self._require_session()
        async with session.request(
            method,
            f"{self._base_url}{path}",
            json=json,
            headers=self._headers,
        ) as response:
            if response.status >= 400:
                text = await response.text()
                raise MattermostResponseError(text, response.status)
            payload = await response.json()
            if not isinstance(payload, dict):
                raise MattermostClientError("Mattermost returned a non-object payload")
            return payload


def mattermost_event_from_payload(
    payload: dict[object, object],
) -> MattermostEventEnvelope:
    """Build an event envelope from a Mattermost WebSocket payload."""
    event = _string_value(payload.get("event"))
    data = _object_mapping(payload.get("data"))
    broadcast = _object_mapping(payload.get("broadcast"))

    post = _post_from_payload(data.get("post"))
    channel_id = _first_present_string(
        data.get("channel_id"),
        broadcast.get("channel_id"),
        post.channel_id if post else "",
    )
    team_id = _first_present_string(data.get("team_id"), broadcast.get("team_id"))
    user_id = _first_present_string(
        data.get("user_id"),
        broadcast.get("user_id"),
        post.user_id if post else "",
    )
    channel_type = _first_present_string(
        data.get("channel_type"),
        broadcast.get("channel_type"),
    )
    sequence = payload.get("seq")

    return MattermostEventEnvelope(
        event=event,
        channel_id=channel_id,
        channel_type=channel_type,
        team_id=team_id or "global",
        user_id=user_id,
        post=post,
        event_id=_string_value(payload.get("event_id")) or None,
        sequence=sequence if isinstance(sequence, int) else None,
    )


def _post_from_payload(value: object) -> MattermostPost | None:
    if isinstance(value, str):
        try:
            import json

            decoded = json.loads(value)
        except ValueError:
            return None
        if not isinstance(decoded, dict):
            return None
        return _post_from_mapping(cast(Mapping[object, object], decoded))

    if isinstance(value, dict):
        return _post_from_mapping(cast(Mapping[object, object], value))

    return None


def _post_from_mapping(mapping: Mapping[object, object]) -> MattermostPost:
    return MattermostPost(
        id=_string_value(mapping.get("id")),
        message=_string_value(mapping.get("message")),
        root_id=_string_value(mapping.get("root_id")),
        parent_id=_string_value(mapping.get("parent_id")),
        user_id=_string_value(mapping.get("user_id")),
        channel_id=_string_value(mapping.get("channel_id")),
    )


def _object_mapping(value: object) -> Mapping[object, object]:
    if isinstance(value, dict):
        return cast(Mapping[object, object], value)
    return {}


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _first_present_string(*values: object) -> str:
    for value in values:
        text = _string_value(value)
        if text:
            return text
    return ""
