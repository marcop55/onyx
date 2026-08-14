"""Mattermost event normalization and reconnect handling."""

import asyncio
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    NormalizedMattermostEvent,
)

_SLEEP = Callable[[float], Awaitable[None]]


class MattermostEventSource(Protocol):
    """Protocol for clients that can stream Mattermost events."""

    def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]: ...


class MattermostEventNormalizer:
    """Normalize Mattermost events and apply gating rules."""

    def __init__(self, config: MattermostListenerConfig) -> None:
        self._config = config
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()

    def normalize(
        self,
        envelope: MattermostEventEnvelope,
    ) -> NormalizedMattermostEvent | None:
        """Return a normalized event or None when the adapter must ignore it."""
        dedupe_key = self._dedupe_key(envelope)
        if self._already_seen(dedupe_key):
            return None

        if envelope.event not in {
            "posted",
            "post_edited",
            "reaction_added",
            "post_deleted",
        }:
            return None

        post = envelope.post
        if post is None:
            return None

        user_id = envelope.user_id or post.user_id
        if user_id in self._config.bot_user_ids:
            return None

        if not self._is_approved_user(user_id):
            return None

        team_id = envelope.team_id or "global"
        channel_id = envelope.channel_id or post.channel_id
        if not channel_id:
            return None

        root_post_id = post.root_id or post.id
        text = post.message

        if envelope.event == "posted":
            if envelope.channel_type == "D":
                return self._build_event(
                    MattermostNormalizedEventType.DIRECT_MESSAGE,
                    envelope,
                    post,
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    root_post_id=root_post_id,
                    text=text,
                    session_key=f"mattermost:dm:{team_id}:{channel_id}",
                )

            if not self._is_allowed_channel(team_id, channel_id):
                return None

            if post.root_id and post.root_id in self._config.owned_thread_root_ids:
                return self._build_channel_event(
                    MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP,
                    envelope,
                    post,
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    root_post_id=post.root_id,
                    text=text,
                )

            stripped_text = self._strip_bot_mention(text)
            if stripped_text != text:
                return self._build_channel_event(
                    MattermostNormalizedEventType.CHANNEL_MENTION,
                    envelope,
                    post,
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    root_post_id=root_post_id,
                    text=stripped_text,
                )

            if not post.root_id and channel_id in self._config.root_post_channel_ids:
                return self._build_channel_event(
                    MattermostNormalizedEventType.ROOT_ALLOWLISTED_POST,
                    envelope,
                    post,
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    root_post_id=root_post_id,
                    text=text,
                )

            return None

        if not self._is_allowed_channel(team_id, channel_id):
            return None

        if envelope.event == "post_edited":
            if root_post_id not in self._config.owned_thread_root_ids:
                return None
            stripped_text = self._strip_bot_mention(text)
            return self._build_channel_event(
                MattermostNormalizedEventType.POST_UPDATE_RETRY,
                envelope,
                post,
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                root_post_id=root_post_id,
                text=stripped_text,
            )

        if envelope.event == "reaction_added":
            if root_post_id not in self._config.owned_thread_root_ids:
                return None
            return self._build_channel_event(
                MattermostNormalizedEventType.REACTION_FEEDBACK,
                envelope,
                post,
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                root_post_id=root_post_id,
                text=text,
            )

        if envelope.event == "post_deleted":
            if root_post_id not in self._config.owned_thread_root_ids:
                return None
            return self._build_channel_event(
                MattermostNormalizedEventType.POST_DELETE_TOMBSTONE,
                envelope,
                post,
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                root_post_id=root_post_id,
                text="",
            )

        return None

    def _build_channel_event(
        self,
        event_type: MattermostNormalizedEventType,
        envelope: MattermostEventEnvelope,
        post: MattermostPost,
        *,
        user_id: str,
        team_id: str,
        channel_id: str,
        root_post_id: str,
        text: str,
    ) -> NormalizedMattermostEvent:
        return self._build_event(
            event_type,
            envelope,
            post,
            user_id=user_id,
            team_id=team_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            text=text,
            session_key=(f"mattermost:channel:{team_id}:{channel_id}:{root_post_id}"),
        )

    def _build_event(
        self,
        event_type: MattermostNormalizedEventType,
        envelope: MattermostEventEnvelope,
        post: MattermostPost,
        *,
        user_id: str,
        team_id: str,
        channel_id: str,
        root_post_id: str,
        text: str,
        session_key: str,
    ) -> NormalizedMattermostEvent:
        return NormalizedMattermostEvent(
            event_type=event_type,
            session_key=session_key,
            team_id=team_id,
            channel_id=channel_id,
            post_id=post.id,
            root_post_id=root_post_id,
            user_id=user_id,
            text=text.strip(),
            raw_event_type=envelope.event,
        )

    def _is_allowed_channel(self, team_id: str, channel_id: str) -> bool:
        if channel_id not in self._config.allowed_channel_ids:
            return False
        return (
            not self._config.allowed_team_ids
            or team_id in self._config.allowed_team_ids
        )

    def _is_approved_user(self, user_id: str) -> bool:
        return (
            not self._config.approved_user_ids
            or user_id in self._config.approved_user_ids
        )

    def _strip_bot_mention(self, text: str) -> str:
        updated_text = text
        for mention in sorted(self._config.bot_mentions, key=len, reverse=True):
            escaped_mention = re.escape(mention)
            updated_text = re.sub(
                rf"(?<!\w){escaped_mention}(?!\w)",
                " ",
                updated_text,
                flags=re.IGNORECASE,
            )
        return " ".join(updated_text.split())

    def _dedupe_key(self, envelope: MattermostEventEnvelope) -> str:
        if envelope.event_id:
            return f"event_id:{envelope.event_id}"
        if envelope.sequence is not None:
            return f"seq:{envelope.sequence}"
        post_id = envelope.post.id if envelope.post is not None else "none"
        return f"fallback:{envelope.event}:{post_id}"

    def _already_seen(self, dedupe_key: str) -> bool:
        if dedupe_key in self._seen_event_ids:
            self._seen_event_ids.move_to_end(dedupe_key)
            return True

        self._seen_event_ids[dedupe_key] = None
        while len(self._seen_event_ids) > self._config.max_seen_event_ids:
            self._seen_event_ids.popitem(last=False)
        return False


class MattermostEventListener:
    """Listen to Mattermost events with bounded reconnect backoff."""

    def __init__(
        self,
        client: MattermostEventSource,
        config: MattermostListenerConfig,
        *,
        sleep: _SLEEP = asyncio.sleep,
    ) -> None:
        self._client = client
        self._config = config
        self._normalizer = MattermostEventNormalizer(config)
        self._sleep = sleep
        self._stopped = False

    def stop(self) -> None:
        """Stop reconnecting after the current connection exits."""
        self._stopped = True

    async def normalized_events(self) -> AsyncIterator[NormalizedMattermostEvent]:
        """Yield normalized events and reconnect after connection failures."""
        backoff_seconds = self._config.initial_reconnect_backoff_seconds
        while not self._stopped:
            try:
                async for envelope in self._connect_events():
                    normalized_event = self._normalizer.normalize(envelope)
                    if normalized_event is not None:
                        yield normalized_event
                    backoff_seconds = self._config.initial_reconnect_backoff_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._sleep(backoff_seconds)
                backoff_seconds = min(
                    backoff_seconds * 2,
                    self._config.max_reconnect_backoff_seconds,
                )

    def normalize(
        self,
        envelope: MattermostEventEnvelope,
    ) -> NormalizedMattermostEvent | None:
        """Normalize one event. This is useful for tests and sync dispatchers."""
        return self._normalizer.normalize(envelope)

    async def _connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        async for envelope in self._client.connect_events():
            yield envelope
