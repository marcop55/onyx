from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from onyx.context.search.models import Tag
from onyx.onyxbot.mattermost.models import NormalizedMattermostEvent

MATTERMOST_CHANNEL_FILTER_NO_RESULTS_PREFIX = "No indexed data found for"
MATTERMOST_CHANNEL_FILTER_DENIED_MESSAGE = (
    "Onyx could not safely filter to the referenced Mattermost channel. "
    "The channel was not found, was ambiguous, or is not accessible to both "
    "the sender and the bot."
)

_IN_CHANNEL_PATTERN = re.compile(r"(?<!\S)in:#?(?P<name>[A-Za-z0-9_-]+)\b")
_CHANNEL_REFERENCE_PATTERN = re.compile(r"(?<!\w)~(?P<name>[A-Za-z0-9_-]+)\b")


class MattermostChannelFilterResolutionError(RuntimeError):
    """A referenced Mattermost channel could not be resolved safely."""


class MattermostChannelFilterAccessDenied(MattermostChannelFilterResolutionError):
    """The bot or sender is not a current member of a referenced channel."""


class MattermostChannelFilterClient(Protocol):
    async def get_me(self) -> Mapping[str, object]: ...

    async def get_channel_by_name(
        self, *, team_id: str, channel_name: str
    ) -> object: ...

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...


@dataclass(frozen=True)
class MattermostChannelFilterResult:
    message: str
    tags: list[Tag]
    no_results_message: str | None = None


@dataclass(frozen=True)
class _ResolvedChannel:
    id: str
    name: str


async def resolve_mattermost_channel_filters(
    *,
    event: NormalizedMattermostEvent,
    client: MattermostChannelFilterClient,
) -> MattermostChannelFilterResult:
    """Resolve Mattermost channel filters to immutable indexed channel IDs."""

    references = _extract_channel_references(event.text)
    if not references:
        return MattermostChannelFilterResult(message=event.text, tags=[])

    bot_user_id = _string_value((await client.get_me()).get("id"))
    if not bot_user_id:
        raise MattermostChannelFilterResolutionError(
            "Mattermost bot identity is unavailable"
        )

    resolved_by_name: dict[str, _ResolvedChannel] = {}
    for channel_name in references:
        try:
            raw_channel = await client.get_channel_by_name(
                team_id=event.team_id,
                channel_name=channel_name,
            )
        except Exception as exc:
            raise MattermostChannelFilterResolutionError(
                "referenced channel could not be resolved"
            ) from exc
        channel = _coerce_channel(raw_channel)
        if channel.id in {existing.id for existing in resolved_by_name.values()}:
            resolved_by_name[channel_name] = channel
            continue
        await _verify_channel_access(
            client=client,
            channel_id=channel.id,
            bot_user_id=bot_user_id,
            sender_user_id=event.user_id,
        )
        resolved_by_name[channel_name] = channel

    message = event.text
    for requested_name, channel in resolved_by_name.items():
        message = _replace_channel_reference(message, requested_name, channel.name)

    ordered_channels = _unique_channels(resolved_by_name.values())
    channel_labels = ", ".join(f"#{channel.name}" for channel in ordered_channels)
    return MattermostChannelFilterResult(
        message=message,
        tags=[
            Tag(tag_key="channel_id", tag_value=channel.id)
            for channel in ordered_channels
        ],
        no_results_message=(
            f"{MATTERMOST_CHANNEL_FILTER_NO_RESULTS_PREFIX} {channel_labels}. "
            "No indexed Mattermost posts matched this filtered query."
        ),
    )


def _extract_channel_references(message: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for pattern in (_IN_CHANNEL_PATTERN, _CHANNEL_REFERENCE_PATTERN):
        for match in pattern.finditer(message):
            name = match.group("name")
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def _coerce_channel(raw_channel: object) -> _ResolvedChannel:
    if isinstance(raw_channel, Mapping):
        channel_mapping = cast(Mapping[object, object], raw_channel)
        channel_id = _string_value(channel_mapping.get("id"))
        channel_name = _string_value(channel_mapping.get("name"))
    else:
        channel_id = _string_value(getattr(raw_channel, "id", ""))
        channel_name = _string_value(getattr(raw_channel, "name", ""))
    if not channel_id or not channel_name:
        raise MattermostChannelFilterResolutionError(
            "Mattermost channel lookup returned an invalid channel"
        )
    return _ResolvedChannel(id=channel_id, name=channel_name)


async def _verify_channel_access(
    *,
    client: MattermostChannelFilterClient,
    channel_id: str,
    bot_user_id: str,
    sender_user_id: str,
) -> None:
    for user_id in (bot_user_id, sender_user_id):
        try:
            is_member = await client.is_channel_member(
                channel_id=channel_id,
                user_id=user_id,
            )
        except Exception as exc:
            raise MattermostChannelFilterAccessDenied(
                "referenced channel membership lookup failed"
            ) from exc
        if not is_member:
            raise MattermostChannelFilterAccessDenied(
                "referenced channel is not accessible to the bot and sender"
            )


def _replace_channel_reference(
    message: str, requested_name: str, channel_name: str
) -> str:
    escaped_name = re.escape(requested_name)
    message = re.sub(
        rf"(?<!\S)in:#?{escaped_name}\b",
        f"#{channel_name}",
        message,
    )
    return re.sub(
        rf"(?<!\w)~{escaped_name}\b",
        f"#{channel_name}",
        message,
    )


def _unique_channels(channels: Iterable[_ResolvedChannel]) -> list[_ResolvedChannel]:
    unique: list[_ResolvedChannel] = []
    seen_ids: set[str] = set()
    for channel in channels:
        if channel.id in seen_ids:
            continue
        seen_ids.add(channel.id)
        unique.append(channel)
    return unique


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""
