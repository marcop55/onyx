from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.db.models import Connector, ConnectorCredentialPair, MattermostEventState
from onyx.onyxbot.mattermost.config import canonical_mattermost_instance_id

_RECENT_EVENT_LIMIT = 200
_STUCK_STATES = frozenset(
    {"claimed", "post_create_attempted", "post_created", "turn_created"}
)
_ATTACHMENT_FAILURE_STATES = frozenset({"attachment_failed", "file_failed"})
_RATE_LIMIT_STATES = frozenset({"rate_limited"})
_ATTACHMENT_FAILURE_TERMINAL_OUTCOMES = frozenset({"attachment_failed"})
_RATE_LIMIT_TERMINAL_OUTCOMES = frozenset({"rate_limited"})


class MattermostChannelHealth(BaseModel):
    id: str
    name: str
    display_name: str
    bot_is_member: bool


class MattermostDeliverySummary(BaseModel):
    total_events: int
    completed_events: int
    in_progress_events: int
    replayable_events: int
    attachment_failure_events: int
    rate_limited_events: int
    latest_event_at: datetime | None
    by_event_type: dict[str, int]


class MattermostIndexingConnectorHealth(BaseModel):
    id: int
    name: str
    status: str
    last_successful_index_time: datetime | None
    total_docs_indexed: int
    in_repeated_error_state: bool


class MattermostIndexingSummary(BaseModel):
    connectors: list[MattermostIndexingConnectorHealth]
    latest_successful_index_time: datetime | None
    total_docs_indexed: int


class MattermostObservabilitySnapshot(BaseModel):
    bot_id: int
    bot_name: str
    instance_id: str
    enabled: bool
    bot_user_id: str
    bot_username: str
    health_status: str
    health_error: str | None
    joined_channels: list[MattermostChannelHealth]
    delivery: MattermostDeliverySummary
    indexing: MattermostIndexingSummary


def summarize_delivery_state(
    events: list[Any], *, now: datetime | None = None
) -> MattermostDeliverySummary:
    current_time = now or datetime.now(timezone.utc)
    by_event_type: Counter[str] = Counter()
    completed_events = 0
    in_progress_events = 0
    replayable_events = 0
    attachment_failure_events = 0
    rate_limited_events = 0
    latest_event_at: datetime | None = None

    for event in events:
        state = _safe_str(getattr(event, "state", ""))
        terminal_outcome = _safe_str(getattr(event, "terminal_outcome", ""))
        event_type = _safe_str(getattr(event, "event_type", "unknown")) or "unknown"
        by_event_type[event_type] += 1
        updated_at = getattr(event, "time_updated", None) or getattr(
            event, "time_created", None
        )
        if isinstance(updated_at, datetime) and (
            latest_event_at is None or updated_at > latest_event_at
        ):
            latest_event_at = updated_at
        if state == "completed":
            completed_events += 1
        elif state in _STUCK_STATES:
            in_progress_events += 1
            lease_expires_at = getattr(event, "lease_expires_at", None)
            if (
                isinstance(lease_expires_at, datetime)
                and lease_expires_at <= current_time
            ):
                replayable_events += 1
        if (
            state in _ATTACHMENT_FAILURE_STATES
            or terminal_outcome in _ATTACHMENT_FAILURE_TERMINAL_OUTCOMES
        ):
            attachment_failure_events += 1
        if (
            state in _RATE_LIMIT_STATES
            or terminal_outcome in _RATE_LIMIT_TERMINAL_OUTCOMES
        ):
            rate_limited_events += 1

    return MattermostDeliverySummary(
        total_events=len(events),
        completed_events=completed_events,
        in_progress_events=in_progress_events,
        replayable_events=replayable_events,
        attachment_failure_events=attachment_failure_events,
        rate_limited_events=rate_limited_events,
        latest_event_at=latest_event_at,
        by_event_type=dict(sorted(by_event_type.items())),
    )


def build_mattermost_observability_snapshot(
    *,
    bot: Any,
    joined_channels: list[MattermostChannelHealth],
    events: list[Any],
    indexing_connectors: list[Any],
    now: datetime | None = None,
) -> MattermostObservabilitySnapshot:
    connector_health = [
        MattermostIndexingConnectorHealth(
            id=int(getattr(connector, "id")),
            name=_safe_str(getattr(connector, "name", "")),
            status=_safe_str(getattr(connector, "status", "")),
            last_successful_index_time=getattr(
                connector, "last_successful_index_time", None
            ),
            total_docs_indexed=int(getattr(connector, "total_docs_indexed", 0) or 0),
            in_repeated_error_state=bool(
                getattr(connector, "in_repeated_error_state", False)
            ),
        )
        for connector in indexing_connectors
    ]
    latest_success = max(
        (
            connector.last_successful_index_time
            for connector in connector_health
            if connector.last_successful_index_time is not None
        ),
        default=None,
    )
    return MattermostObservabilitySnapshot(
        bot_id=int(getattr(bot, "id")),
        bot_name=_safe_str(getattr(bot, "name", "")),
        instance_id=canonical_mattermost_instance_id(
            _safe_str(getattr(bot, "url", ""))
        ),
        enabled=bool(getattr(bot, "enabled", False)),
        bot_user_id=_safe_str(getattr(bot, "bot_user_id", "")),
        bot_username=_safe_str(getattr(bot, "bot_username", "")),
        health_status=_safe_str(getattr(bot, "health_status", "unknown")),
        health_error=getattr(bot, "health_error", None),
        joined_channels=joined_channels,
        delivery=summarize_delivery_state(events, now=now),
        indexing=MattermostIndexingSummary(
            connectors=connector_health,
            latest_successful_index_time=latest_success,
            total_docs_indexed=sum(
                connector.total_docs_indexed for connector in connector_health
            ),
        ),
    )


def collect_mattermost_observability(
    db_session: Session,
    bot: Any,
    *,
    joined_channels: list[MattermostChannelHealth] | None = None,
) -> MattermostObservabilitySnapshot:
    instance_id = canonical_mattermost_instance_id(_safe_str(getattr(bot, "url", "")))
    events = list(
        db_session.scalars(
            select(MattermostEventState)
            .where(MattermostEventState.instance_id == instance_id)
            .order_by(desc(MattermostEventState.time_updated))
            .limit(_RECENT_EVENT_LIMIT)
        ).all()
    )
    connectors = list(
        db_session.scalars(
            select(ConnectorCredentialPair)
            .join(Connector)
            .where(Connector.source == DocumentSource.MATTERMOST)
            .order_by(desc(ConnectorCredentialPair.last_successful_index_time))
        ).all()
    )
    return build_mattermost_observability_snapshot(
        bot=bot,
        joined_channels=joined_channels or [],
        events=events,
        indexing_connectors=connectors,
    )


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    value_attr = getattr(value, "value", None)
    if isinstance(value_attr, str):
        return value_attr
    return str(value)
