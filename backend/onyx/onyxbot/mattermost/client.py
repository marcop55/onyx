"""Typed Mattermost REST and WebSocket client."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import cast

import aiohttp

from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostFileInfo,
    MattermostPost,
    MattermostReaction,
    MattermostUserInfo,
)


class MattermostClientError(Exception):
    """Base Mattermost client error."""


class MattermostResponseError(MattermostClientError):
    """Mattermost returned an unsuccessful response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


_SLEEP = Callable[[float], Awaitable[None]]


class MattermostClient:
    """Async client for Mattermost REST and WebSocket APIs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout_seconds: int = 30,
        session: aiohttp.ClientSession | None = None,
        max_rate_limit_retries: int = 3,
        max_rate_limit_backoff_seconds: float = 30.0,
        sleep: _SLEEP = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._request_timeout_seconds = request_timeout_seconds
        self._session = session
        self._owns_session = session is None
        self._max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._max_rate_limit_backoff_seconds = max(0.0, max_rate_limit_backoff_seconds)
        self._sleep = sleep

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
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        """Create a Mattermost post."""
        payload: dict[str, object] = {
            "channel_id": channel_id,
            "message": message,
            "root_id": root_id,
        }
        if pending_post_id is not None:
            payload["pending_post_id"] = pending_post_id
        if props is not None:
            payload["props"] = props
        response = await self._request_json(
            "POST",
            "/api/v4/posts",
            json=payload,
        )
        return _post_from_mapping(cast(Mapping[object, object], response))

    async def create_ephemeral_post(
        self,
        *,
        user_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> MattermostPost:
        """Create a Mattermost ephemeral post visible only to one user."""

        post_payload: dict[str, object] = {
            "channel_id": channel_id,
            "message": message,
            "root_id": root_id,
        }
        if props is not None:
            post_payload["props"] = props
        response = await self._request_json(
            "POST",
            "/api/v4/posts/ephemeral",
            json={"user_id": user_id, "post": post_payload},
        )
        return _post_from_mapping(cast(Mapping[object, object], response))

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None:
        """Reconcile a possibly committed create against recent channel posts."""
        response = await self._request_json(
            "GET",
            f"/api/v4/channels/{channel_id}/posts?page=0&per_page=200",
        )
        raw_posts = response.get("posts")
        if not isinstance(raw_posts, dict):
            raise MattermostClientError("Mattermost channel posts payload is invalid")
        for raw_post in raw_posts.values():
            if not isinstance(raw_post, Mapping):
                continue
            post = _post_from_mapping(cast(Mapping[object, object], raw_post))
            if (
                post.pending_post_id == pending_post_id
                or post.props.get("onyx_event_key") == event_key
            ):
                return post
        return None

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]:
        """Return the latest Mattermost post state for one thread."""

        response = await self._request_json(
            "GET",
            f"/api/v4/posts/{root_post_id}/thread",
        )
        raw_posts = response.get("posts")
        if not isinstance(raw_posts, dict):
            raise MattermostClientError("Mattermost thread posts payload is invalid")
        posts_by_id: dict[str, MattermostPost] = {}
        for raw_post_id, raw_post in raw_posts.items():
            if not isinstance(raw_post_id, str) or not isinstance(raw_post, Mapping):
                continue
            posts_by_id[raw_post_id] = _post_from_mapping(
                cast(Mapping[object, object], raw_post)
            )
        raw_order = response.get("order")
        if isinstance(raw_order, list):
            ordered_posts = [
                posts_by_id[post_id]
                for post_id in raw_order
                if isinstance(post_id, str) and post_id in posts_by_id
            ]
            ordered_posts.extend(
                post
                for post_id, post in posts_by_id.items()
                if post_id not in raw_order
            )
            return ordered_posts
        return list(posts_by_id.values())

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

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        """Return whether a user is a current member of a Mattermost channel."""
        try:
            payload = await self._request_json(
                "GET",
                f"/api/v4/channels/{channel_id}/members/{user_id}",
            )
        except MattermostResponseError as exc:
            if exc.status_code == 404:
                return False
            raise
        return (
            payload.get("channel_id") == channel_id
            and payload.get("user_id") == user_id
        )

    async def get_channel_by_name(
        self, *, team_id: str, channel_name: str
    ) -> dict[str, object]:
        """Resolve a Mattermost channel name within a team."""
        return await self._request_json(
            "GET",
            f"/api/v4/teams/{team_id}/channels/name/{channel_name}",
        )

    async def get_file_info(self, file_id: str) -> MattermostFileInfo:
        """Return Mattermost metadata for one bot-authorized file."""
        response = await self._request_json("GET", f"/api/v4/files/{file_id}/info")
        return _file_info_from_mapping(cast(Mapping[object, object], response))

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        """Return the authoritative Mattermost identity for one user."""
        response = await self._request_json("GET", f"/api/v4/users/{user_id}")
        return _user_info_from_mapping(cast(Mapping[object, object], response))

    async def download_file(self, file_id: str) -> bytes:
        """Return Mattermost file bytes using the bot token."""
        return await self._request_bytes("GET", f"/api/v4/files/{file_id}")

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
        for attempt in range(self._max_rate_limit_retries + 1):
            try:
                async with session.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json,
                    headers=self._headers,
                ) as response:
                    if response.status == 429:
                        text = await response.text()
                        if attempt >= self._max_rate_limit_retries:
                            raise MattermostResponseError(text, response.status)
                        retry_after = _retry_after_seconds(
                            response.headers.get("Retry-After")
                        )
                        fallback_backoff = float(2**attempt)
                        await self._sleep(
                            min(
                                retry_after
                                if retry_after is not None
                                else fallback_backoff,
                                self._max_rate_limit_backoff_seconds,
                            )
                        )
                        continue
                    if response.status >= 400:
                        text = await response.text()
                        raise MattermostResponseError(text, response.status)
                    payload = await response.json()
                    if not isinstance(payload, dict):
                        raise MattermostClientError(
                            "Mattermost returned a non-object payload"
                        )
                    return payload
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise MattermostClientError(
                    f"Mattermost {method} transport failed"
                ) from exc

        raise RuntimeError("Mattermost rate-limit retry loop exhausted unexpectedly")

    async def _request_bytes(self, method: str, path: str) -> bytes:
        session = self._require_session()
        try:
            async with session.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
            ) as response:
                if response.status >= 400:
                    text = await response.text()
                    raise MattermostResponseError(text, response.status)
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise MattermostClientError(
                f"Mattermost {method} transport failed"
            ) from exc


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def mattermost_event_from_payload(
    payload: dict[object, object],
) -> MattermostEventEnvelope:
    """Build an event envelope from a Mattermost WebSocket payload."""
    event = _string_value(payload.get("event"))
    data = _object_mapping(payload.get("data"))
    broadcast = _object_mapping(payload.get("broadcast"))

    post = _post_from_payload(data.get("post"))
    reaction = _reaction_from_payload(data.get("reaction"))
    channel_id = _first_present_string(
        data.get("channel_id"),
        broadcast.get("channel_id"),
        reaction.channel_id if reaction else "",
        post.channel_id if post else "",
    )
    team_id = _first_present_string(data.get("team_id"), broadcast.get("team_id"))
    user_id = _first_present_string(
        data.get("user_id"),
        broadcast.get("user_id"),
        reaction.user_id if reaction else "",
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
        reaction=reaction,
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
        pending_post_id=_string_value(mapping.get("pending_post_id")),
        file_ids=_string_tuple_value(mapping.get("file_ids")),
        create_at=_int_value(mapping.get("create_at")),
        update_at=_int_value(mapping.get("update_at")),
        delete_at=_int_value(mapping.get("delete_at")),
        props=_props_value(mapping.get("props")),
    )


def _reaction_from_payload(value: object) -> MattermostReaction | None:
    if isinstance(value, str):
        try:
            import json

            decoded = json.loads(value)
        except ValueError:
            return None
        if not isinstance(decoded, dict):
            return None
        value = decoded

    if not isinstance(value, dict):
        return None

    mapping = cast(Mapping[object, object], value)
    return MattermostReaction(
        user_id=_string_value(mapping.get("user_id")),
        post_id=_string_value(mapping.get("post_id")),
        emoji_name=_string_value(mapping.get("emoji_name")),
        channel_id=_string_value(mapping.get("channel_id")),
    )


def _file_info_from_mapping(mapping: Mapping[object, object]) -> MattermostFileInfo:
    return MattermostFileInfo(
        id=_string_value(mapping.get("id")),
        uploader_user_id=_string_value(mapping.get("user_id")),
        post_id=_string_value(mapping.get("post_id")),
        filename=_string_value(mapping.get("name")),
        mime_type=_string_value(mapping.get("mime_type")),
        size_bytes=_int_value(mapping.get("size")),
        create_at=_int_value(mapping.get("create_at")),
    )


def _user_info_from_mapping(mapping: Mapping[object, object]) -> MattermostUserInfo:
    username = _string_value(mapping.get("username"))
    full_name = " ".join(
        value
        for value in (
            _string_value(mapping.get("first_name")),
            _string_value(mapping.get("last_name")),
        )
        if value
    )
    display_name = full_name or _string_value(mapping.get("nickname")) or username
    return MattermostUserInfo(
        id=_string_value(mapping.get("id")),
        username=username,
        display_name=display_name,
        roles=_string_value(mapping.get("roles")),
    )


def _props_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _object_mapping(value: object) -> Mapping[object, object]:
    if isinstance(value, dict):
        return cast(Mapping[object, object], value)
    return {}


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _string_tuple_value(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _int_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _first_present_string(*values: object) -> str:
    for value in values:
        text = _string_value(value)
        if text:
            return text
    return ""
