"""Mattermost event normalization and reconnect handling."""

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Protocol

from onyx.configs.constants import QAFeedbackType
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)

_SLEEP = Callable[[float], Awaitable[None]]


class MattermostEventSource(Protocol):
    """Protocol for clients that can stream Mattermost events."""

    def connect_events(self) -> AsyncIterator[MattermostEventEnvelope]: ...

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...


class MattermostEventNormalizer:
    """Normalize Mattermost events and apply gating rules."""

    def __init__(self, config: MattermostListenerConfig) -> None:
        self._config = config
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict(
            (event_id, None) for event_id in config.processed_event_ids
        )

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

        if envelope.event == "reaction_added":
            return self._normalize_reaction_feedback(envelope)

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

            channel_config = self._resolve_channel_participation(team_id, channel_id)
            if channel_config is None:
                return None

            if post.root_id and post.root_id in self._config.tombstoned_thread_root_ids:
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

            if self._mentions_bot(text):
                return self._build_channel_event(
                    MattermostNormalizedEventType.CHANNEL_MENTION,
                    envelope,
                    post,
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    root_post_id=root_post_id,
                    text=self._strip_bot_mention(text),
                )

            if not post.root_id and self._allows_unmentioned_root_post(
                channel_config, channel_id
            ):
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

        if envelope.event == "post_deleted":
            if post.id != root_post_id:
                return None
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

    def _normalize_reaction_feedback(
        self,
        envelope: MattermostEventEnvelope,
    ) -> NormalizedMattermostEvent | None:
        reaction = envelope.reaction
        if reaction is None:
            return None

        user_id = envelope.user_id or reaction.user_id
        if user_id in self._config.bot_user_ids:
            return None
        if not self._is_approved_user(user_id):
            return None

        team_id = envelope.team_id or "global"
        channel_id = envelope.channel_id or reaction.channel_id
        if not channel_id:
            return None

        root_post_id = self._config.owned_answer_post_root_ids.get(reaction.post_id)
        if root_post_id is None:
            return None
        message_id = self._config.owned_answer_post_message_ids.get(reaction.post_id)

        feedback_action = _feedback_action_from_emoji(reaction.emoji_name)
        if feedback_action is None:
            return None

        return NormalizedMattermostEvent(
            event_type=MattermostNormalizedEventType.REACTION_FEEDBACK,
            session_key=(f"mattermost:channel:{team_id}:{channel_id}:{root_post_id}"),
            team_id=team_id,
            channel_id=channel_id,
            post_id=reaction.post_id,
            root_post_id=root_post_id,
            user_id=user_id,
            raw_event_type=envelope.event,
            metadata={
                "feedback_answer_post_id": reaction.post_id,
                "feedback_action": feedback_action.value,
                **(
                    {"feedback_message_id": str(message_id)}
                    if message_id is not None
                    else {}
                ),
            },
            feedback_answer_post_id=reaction.post_id,
            feedback_action=feedback_action,
            feedback_message_id=message_id,
            dedupe_key=self._dedupe_key(envelope),
        )

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
            file_ids=post.file_ids,
            source_create_at=post.create_at,
            source_update_at=post.update_at,
            source_delete_at=post.delete_at,
            dedupe_key=self._dedupe_key(envelope),
        )

    def _resolve_channel_participation(
        self, team_id: str, channel_id: str
    ) -> dict[str, object] | None:
        """Return a channel's managed config, or None when it is not opted in.

        Channels are default-deny: answering requires the channel's own enabled
        config row. Direct messages never reach this check.
        """
        if (
            self._config.allowed_team_ids
            and team_id not in self._config.allowed_team_ids
        ):
            return None
        if (
            self._config.allowed_channel_ids
            and channel_id not in self._config.allowed_channel_ids
        ):
            return None

        resolver = self._config.managed_channel_config_resolver
        if resolver is None:
            if channel_id not in self._config.allowed_channel_ids:
                return None
            return {}

        channel_config = resolver(channel_id)
        if channel_config is None or channel_config.get("disabled") is True:
            return None
        return channel_config

    def _is_approved_user(self, user_id: str) -> bool:
        return (
            not self._config.approved_user_ids
            or user_id in self._config.approved_user_ids
        )

    def _allows_unmentioned_root_post(
        self, channel_config: dict[str, object], channel_id: str
    ) -> bool:
        respond_tag_only = channel_config.get("respond_tag_only")
        if respond_tag_only is not None:
            return respond_tag_only is False
        return channel_id in self._config.root_post_channel_ids

    def _mentions_bot(self, text: str) -> bool:
        """Report whether the text actually addresses the bot by mention."""
        return any(
            re.search(_bot_mention_pattern(mention), text, flags=re.IGNORECASE)
            for mention in self._config.bot_mentions
        )

    def _strip_bot_mention(self, text: str) -> str:
        updated_text = text
        for mention in sorted(
            self._config.bot_mentions, key=lambda value: len(value), reverse=True
        ):
            updated_text = re.sub(
                _bot_mention_pattern(mention),
                " ",
                updated_text,
                flags=re.IGNORECASE,
            )
        return " ".join(updated_text.split())

    def _dedupe_key(self, envelope: MattermostEventEnvelope) -> str:
        if envelope.event_id:
            return f"event_id:{envelope.event_id}"
        if envelope.reaction is not None:
            reaction = envelope.reaction
            return (
                f"fallback:{envelope.event}:{reaction.user_id}:"
                f"{reaction.post_id}:{reaction.emoji_name}"
            )
        if envelope.post is not None:
            content_digest = hashlib.sha256(
                json.dumps(
                    {
                        "message": envelope.post.message,
                        "file_ids": envelope.post.file_ids,
                        "create_at": envelope.post.create_at,
                        "update_at": envelope.post.update_at,
                        "delete_at": envelope.post.delete_at,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return f"fallback:{envelope.event}:{envelope.post.id}:{content_digest}"
        return (
            f"fallback:{envelope.event}:{envelope.channel_id}:"
            f"{envelope.user_id or 'unknown'}"
        )

    def _already_seen(self, dedupe_key: str) -> bool:
        """Use only hydrated, durably completed IDs as a local fast path."""
        if dedupe_key not in self._seen_event_ids:
            return False
        self._seen_event_ids.move_to_end(dedupe_key)
        return True


def _bot_mention_pattern(mention: str) -> str:
    return rf"(?<!\w){re.escape(mention)}(?!\w)"


def _feedback_action_from_emoji(emoji_name: str) -> QAFeedbackType | None:
    if emoji_name in {"+1", "thumbsup"}:
        return QAFeedbackType.LIKE
    if emoji_name in {"-1", "thumbsdown"}:
        return QAFeedbackType.DISLIKE
    return None


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
                        authorized_event = await self._authorize_and_attribute_event(
                            normalized_event
                        )
                        if authorized_event is not None:
                            yield authorized_event
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

    async def _authorize_and_attribute_event(
        self, event: NormalizedMattermostEvent
    ) -> NormalizedMattermostEvent | None:
        try:
            bot_is_member = await self._client.is_channel_member(
                channel_id=event.channel_id,
                user_id=self._config.bot_user_id,
            )
            if not bot_is_member:
                return None
            sender_is_member = await self._client.is_channel_member(
                channel_id=event.channel_id,
                user_id=event.user_id,
            )
            if not sender_is_member:
                return None
            user_info = await self._client.get_user_info(event.user_id)
            return replace(
                event,
                source_username=str(getattr(user_info, "username", "")) or None,
                source_display_name=(
                    str(getattr(user_info, "display_name", "")) or None
                ),
            )
        except Exception:
            return None

    async def _connect_events(self) -> AsyncIterator[MattermostEventEnvelope]:
        async for envelope in self._client.connect_events():
            yield envelope
