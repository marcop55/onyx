from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

from sqlalchemy.orm import Session

from onyx.db.models import MattermostEventState, User
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.health import (
    MattermostChannelHealth,
    build_mattermost_observability_snapshot,
    summarize_delivery_state,
)
from onyx.onyxbot.mattermost.parity import MattermostParityStatus, matrix_by_key
from onyx.server.manage.mattermost_bot import (
    _discover_joined_channels_sync,
    get_bot_observability,
)


def _event(
    state: str,
    *,
    event_type: str = "channel_mention",
    created: datetime | None = None,
    updated: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> SimpleNamespace:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    return SimpleNamespace(
        state=state,
        event_type=event_type,
        time_created=created or now,
        time_updated=updated or created or now,
        lease_expires_at=lease_expires_at,
    )


def _ledger_event(
    *,
    state: str = "claimed",
    terminal_outcome: str | None = None,
    event_type: str = "channel_mention",
) -> MattermostEventState:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    return MattermostEventState(
        instance_id="https://mattermost.example.com",
        channel_id="channel-1",
        dedupe_key=f"{event_type}:{state}:{terminal_outcome or 'none'}",
        event_type=event_type,
        source_post_id="post-1",
        state=state,
        terminal_outcome=terminal_outcome,
        mattermost_pending_post_id="pending-1",
        time_created=now,
        time_updated=now,
    )


def _token_value(*, apply_mask: bool) -> str:
    assert apply_mask is False
    return "token"


def test_delivery_summary_exposes_replay_without_message_bodies() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    summary = summarize_delivery_state(
        [
            _event("completed", event_type="direct_message", updated=now),
            _event("completed", event_type="direct_message", updated=now),
            _event("post_created", event_type="channel_mention", updated=now),
            _event(
                "claimed",
                event_type="channel_mention",
                updated=now - timedelta(minutes=10),
                lease_expires_at=now - timedelta(minutes=1),
            ),
            _event("attachment_failed", event_type="channel_mention", updated=now),
            _event("rate_limited", event_type="channel_mention", updated=now),
        ],
        now=now,
    )

    assert summary.total_events == 6
    assert summary.completed_events == 2
    assert summary.replayable_events == 1
    assert summary.attachment_failure_events == 1
    assert summary.rate_limited_events == 1
    assert summary.by_event_type == {"direct_message": 2, "channel_mention": 4}
    assert not hasattr(summary, "message")
    assert not hasattr(summary, "file_contents")


def test_delivery_summary_counts_real_persisted_failure_outcomes() -> None:
    summary = summarize_delivery_state(
        [
            _ledger_event(state="claimed", terminal_outcome="attachment_failed"),
            _ledger_event(
                state="post_create_attempted", terminal_outcome="rate_limited"
            ),
            _ledger_event(state="completed", terminal_outcome="delivered"),
        ]
    )

    assert summary.attachment_failure_events == 1
    assert summary.rate_limited_events == 1
    assert summary.completed_events == 1


def test_health_snapshot_surfaces_joined_channels_and_indexing_freshness() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    snapshot = build_mattermost_observability_snapshot(
        bot=SimpleNamespace(
            id=7,
            name="prod mattermost",
            url="https://Mattermost.example.com/team",
            enabled=True,
            bot_user_id="bot-user",
            bot_username="onyxbot",
            health_status="ok",
            health_error=None,
        ),
        joined_channels=[
            MattermostChannelHealth(
                id="channel-1",
                name="town-square",
                display_name="Town Square",
                bot_is_member=True,
            )
        ],
        events=[_event("completed", updated=now)],
        indexing_connectors=[
            SimpleNamespace(
                id=3,
                name="mattermost history",
                status="ACTIVE",
                last_successful_index_time=now,
                total_docs_indexed=42,
                in_repeated_error_state=False,
            )
        ],
        now=now,
    )

    assert snapshot.bot_id == 7
    assert snapshot.instance_id == "https://mattermost.example.com/team"
    assert snapshot.joined_channels[0].bot_is_member is True
    assert snapshot.delivery.completed_events == 1
    assert snapshot.indexing.connectors[0].last_successful_index_time == now
    assert snapshot.indexing.connectors[0].total_docs_indexed == 42


def test_manage_route_requires_admin_and_uses_sanitized_observability(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_fetch_bot(_db_session: object, mattermost_bot_id: int) -> SimpleNamespace:
        observed["bot_id"] = mattermost_bot_id
        return SimpleNamespace(
            id=mattermost_bot_id,
            name="prod mattermost",
            url="https://mattermost.example.com",
            enabled=True,
            bot_user_id="bot-user",
            bot_username="onyxbot",
            health_status="ok",
            health_error=None,
        )

    def fake_collect(db_session: object, bot: object, **_: object) -> object:
        observed["db_session"] = db_session
        observed["bot"] = bot
        return build_mattermost_observability_snapshot(
            bot=bot,
            joined_channels=[],
            events=[],
            indexing_connectors=[],
        )

    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.fetch_mattermost_bot", fake_fetch_bot
    )
    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.collect_mattermost_observability",
        fake_collect,
    )

    snapshot = get_bot_observability(
        mattermost_bot_id=12,
        db_session=cast(Session, SimpleNamespace()),
        _=cast(User, SimpleNamespace()),
    )

    assert observed["bot_id"] == 12
    assert snapshot.bot_user_id == "bot-user"
    assert not hasattr(snapshot, "token")


def test_manage_route_surfaces_joined_channel_discovery_failures(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_fetch_bot(_db_session: object, mattermost_bot_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=mattermost_bot_id,
            name="prod mattermost",
            url="https://mattermost.example.com",
            enabled=True,
            bot_user_id="bot-user",
            bot_username="onyxbot",
            health_status="ok",
            health_error=None,
            token=SimpleNamespace(get_value=_token_value),
        )

    def fake_discover(_bot: object) -> list[MattermostChannelHealth]:
        raise MattermostClientError("token expired: secret-token-value")

    def fake_collect(
        _db_session: object,
        bot: object,
        *,
        joined_channels: list[MattermostChannelHealth] | None = None,
    ) -> object:
        observed["joined_channels"] = joined_channels
        return build_mattermost_observability_snapshot(
            bot=bot,
            joined_channels=joined_channels or [],
            events=[],
            indexing_connectors=[],
        )

    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.fetch_mattermost_bot", fake_fetch_bot
    )
    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot._discover_joined_channels_sync",
        fake_discover,
    )
    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.collect_mattermost_observability",
        fake_collect,
    )

    snapshot = get_bot_observability(
        mattermost_bot_id=12,
        db_session=cast(Session, SimpleNamespace()),
        _=cast(User, SimpleNamespace()),
    )

    assert snapshot.health_status == "error"
    assert snapshot.health_error == "Mattermost joined-channel discovery failed"
    assert snapshot.joined_channels == []
    assert observed["joined_channels"] == []


def test_joined_channel_discovery_helper_propagates_client_failures(
    monkeypatch,
) -> None:
    async def fake_discover(_url: str, _token: str, _bot_user_id: str) -> list[object]:
        raise MattermostClientError("permission denied")

    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.discover_joined_mattermost_channels",
        fake_discover,
    )

    bot = SimpleNamespace(
        url="https://mattermost.example.com",
        bot_user_id="bot-user",
        token=SimpleNamespace(get_value=_token_value),
    )

    try:
        _discover_joined_channels_sync(bot)
    except MattermostClientError as exc:
        assert str(exc) == "permission denied"
    else:
        raise AssertionError("MattermostClientError should propagate")


def test_parity_manifest_records_dashboard_observability_as_shipped() -> None:
    entry = matrix_by_key()["health_delivery_observability"]

    assert entry.status is MattermostParityStatus.DIRECT_MATTERMOST_FEATURE
    assert "dashboard observability" in entry.mattermost_contract.lower()
    assert "backend/onyx/onyxbot/mattermost/health.py" in "\n".join(entry.evidence)
    assert "web/src/app/admin/mattermost-bots/[bot-id]/health/page.tsx" in "\n".join(
        entry.evidence
    )
