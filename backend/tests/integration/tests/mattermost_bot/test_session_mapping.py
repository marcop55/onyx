import datetime
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from time import sleep
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from onyx.configs.constants import MessageType
from onyx.db.chat import (
    create_new_chat_message,
    get_chat_message_by_external_idempotency_key,
    reserve_message_id,
)
from onyx.db.engine.sql_engine import SqlEngine, get_session_with_current_tenant
from onyx.db.feedback import create_chat_message_feedback
from onyx.db.mattermost_bot import (
    MattermostClaimOutcome,
    MattermostThreadTombstonedError,
    checkpoint_mattermost_post,
    checkpoint_mattermost_rendered_message,
    checkpoint_mattermost_turn,
    claim_durable_mattermost_event,
    claim_mattermost_event,
    complete_mattermost_answer_event,
    complete_mattermost_feedback_event,
    get_mattermost_chat_session_for_thread,
    get_mattermost_session_key,
    get_mattermost_thread_mapping,
    get_mattermost_thread_mapping_by_chat_session_id,
    get_or_create_mattermost_thread_mapping,
    hydrate_mattermost_listener_config,
    record_mattermost_event_state,
    tombstone_mattermost_thread_mapping,
    update_mattermost_thread_parent_message,
)
from onyx.db.models import (
    ChatMessage,
    ChatMessageFeedback,
    ChatSession,
    MattermostEventState,
    MattermostThreadMapping,
    Persona,
)
from onyx.onyxbot.mattermost.listener import MattermostEventListener
from onyx.onyxbot.mattermost.models import (
    MattermostEventEnvelope,
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    MattermostPost,
    MattermostReaction,
)
from shared_configs.contextvars import CURRENT_TENANT_ID_CONTEXTVAR

BACKEND_DIR = Path(__file__).resolve().parents[4]


def _run_alembic(revision: str, *, downgrade: bool = False) -> None:
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    if downgrade:
        command.downgrade(alembic_cfg, revision)
    else:
        command.upgrade(alembic_cfg, revision)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    SqlEngine.init_engine(pool_size=10, max_overflow=5)

    token = CURRENT_TENANT_ID_CONTEXTVAR.set("public")
    try:
        with get_session_with_current_tenant() as session:
            yield session
    finally:
        CURRENT_TENANT_ID_CONTEXTVAR.reset(token)


@pytest.fixture
def test_persona(db_session: Session) -> Generator[Persona, None, None]:
    _cleanup_mattermost_rows(db_session)
    existing_persona = db_session.get(Persona, 804)
    if existing_persona is not None:
        db_session.delete(existing_persona)
        db_session.commit()

    persona = Persona(
        id=804,
        name="Mattermost Test Persona",
        description="Test persona for Mattermost mapping tests",
        is_listed=True,
        is_featured=False,
        deleted=False,
        builtin_persona=False,
    )
    db_session.add(persona)
    db_session.commit()

    yield persona

    _cleanup_mattermost_rows(db_session)
    db_session.delete(persona)
    db_session.commit()


def _cleanup_mattermost_rows(db_session: Session) -> None:
    chat_session_ids = list(
        db_session.scalars(
            select(MattermostThreadMapping.chat_session_id).where(
                or_(
                    MattermostThreadMapping.server_id.like("mattermost-test-%"),
                    MattermostThreadMapping.channel_id.like("mattermost-test-%"),
                )
            )
        )
    )
    db_session.execute(
        delete(MattermostThreadMapping).where(
            or_(
                MattermostThreadMapping.server_id.like("mattermost-test-%"),
                MattermostThreadMapping.channel_id.like("mattermost-test-%"),
            )
        )
    )
    if chat_session_ids:
        db_session.execute(
            delete(ChatMessage).where(ChatMessage.chat_session_id.in_(chat_session_ids))
        )
        db_session.execute(
            delete(ChatSession).where(ChatSession.id.in_(chat_session_ids))
        )
    db_session.commit()


def test_new_roots_replies_and_unrelated_roots_are_isolated(
    db_session: Session,
    test_persona: Persona,
) -> None:
    _cleanup_mattermost_rows(db_session)

    root_mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-1",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    reply_mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-1",
        mattermost_user_id="user-2",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    unrelated_mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-2",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )

    assert reply_mapping.id == root_mapping.id
    assert reply_mapping.chat_session_id == root_mapping.chat_session_id
    assert unrelated_mapping.id != root_mapping.id
    assert unrelated_mapping.chat_session_id != root_mapping.chat_session_id

    stored_mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-1",
    )
    assert stored_mapping is not None
    assert stored_mapping.chat_session_id == root_mapping.chat_session_id
    assert stored_mapping.mattermost_user_id == "user-1"
    assert stored_mapping.persona_id == test_persona.id
    assert stored_mapping.parent_message_id is not None

    chat_session = get_mattermost_chat_session_for_thread(
        db_session=db_session,
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-1",
    )
    assert chat_session is not None
    assert chat_session.id == root_mapping.chat_session_id
    assert chat_session.onyxbot_flow is True
    assert chat_session.description == get_mattermost_session_key(
        server_id="mattermost-test-team-1",
        channel_id="channel-1",
        root_id="root-1",
    )

    by_session = get_mattermost_thread_mapping_by_chat_session_id(
        db_session=db_session,
        chat_session_id=root_mapping.chat_session_id,
    )
    assert by_session is not None
    assert by_session.id == root_mapping.id

    assert (
        db_session.query(MattermostThreadMapping)
        .filter(
            MattermostThreadMapping.server_id == "mattermost-test-team-1",
            MattermostThreadMapping.channel_id == "channel-1",
            MattermostThreadMapping.root_id == "root-1",
        )
        .count()
        == 1
    )

    _cleanup_mattermost_rows(db_session)


def test_parent_message_mapping_can_advance(
    db_session: Session,
    test_persona: Persona,
) -> None:
    _cleanup_mattermost_rows(db_session)

    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-2",
        channel_id="channel-1",
        root_id="root-1",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    root_message = mapping.parent_message
    assert root_message is not None

    user_message = create_new_chat_message(
        chat_session_id=mapping.chat_session_id,
        parent_message=root_message,
        message="hello",
        token_count=1,
        message_type=MessageType.USER,
        db_session=db_session,
    )

    updated_mapping = update_mattermost_thread_parent_message(
        db_session=db_session,
        mapping=mapping,
        parent_message_id=user_message.id,
    )

    assert updated_mapping.parent_message_id == user_message.id

    _cleanup_mattermost_rows(db_session)


def test_listener_state_survives_restart(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-restart",
        channel_id="channel-1",
        root_id="root-restart",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    record_mattermost_event_state(
        db_session=db_session,
        mapping=mapping,
        dedupe_key="event_id:event-original",
        answer_post_id="answer-old",
        message_id=mapping.parent_message_id,
    )
    record_mattermost_event_state(
        db_session=db_session,
        mapping=mapping,
        dedupe_key="event_id:event-second",
        answer_post_id="answer-new",
        message_id=mapping.parent_message_id,
    )

    restarted_config = MattermostListenerConfig(
        bot_user_id="bot-1",
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_team_ids=frozenset({"mattermost-test-team-restart"}),
        approved_user_ids=frozenset({"user-1"}),
    )
    hydrate_mattermost_listener_config(db_session, restarted_config)
    restarted_listener = MattermostEventListener(MagicMock(), restarted_config)

    reply = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="posted",
            event_id="event-reply-after-restart",
            channel_id="channel-1",
            channel_type="O",
            team_id="mattermost-test-team-restart",
            user_id="user-1",
            post=MattermostPost(
                id="reply-after-restart",
                root_id="root-restart",
                channel_id="channel-1",
                user_id="user-1",
                message="continue",
            ),
        )
    )
    old_answer_reaction = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="reaction_added",
            event_id="event-reaction-after-restart",
            channel_id="channel-1",
            channel_type="O",
            team_id="mattermost-test-team-restart",
            user_id="user-1",
            reaction=MattermostReaction(
                user_id="user-1",
                post_id="answer-old",
                emoji_name="+1",
                channel_id="channel-1",
            ),
        )
    )
    replay = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="posted",
            event_id="event-original",
            channel_id="channel-1",
            channel_type="O",
            team_id="mattermost-test-team-restart",
            user_id="user-1",
            post=MattermostPost(
                id="root-restart",
                channel_id="channel-1",
                user_id="user-1",
                message="@onyx original",
            ),
        )
    )

    assert reply is not None
    assert reply.event_type == MattermostNormalizedEventType.THREAD_REPLY_FOLLOWUP
    assert old_answer_reaction is not None
    assert old_answer_reaction.feedback_message_id == mapping.parent_message_id
    assert replay is None


def test_direct_message_replay_state_survives_restart_without_channel_allowlist(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="global",
        channel_id="mattermost-test-dm-channel",
        root_id="mattermost-test-dm-channel",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    record_mattermost_event_state(
        db_session=db_session,
        mapping=mapping,
        dedupe_key="event_id:dm-original",
    )

    restarted_config = MattermostListenerConfig(
        bot_user_id="bot-1",
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_team_ids=frozenset({"mattermost-test-team-restart"}),
        approved_user_ids=frozenset({"user-1"}),
    )
    hydrate_mattermost_listener_config(db_session, restarted_config)
    restarted_listener = MattermostEventListener(MagicMock(), restarted_config)
    replay = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="posted",
            event_id="dm-original",
            channel_id="mattermost-test-dm-channel",
            channel_type="D",
            team_id="global",
            user_id="user-1",
            post=MattermostPost(
                id="dm-original-post",
                channel_id="mattermost-test-dm-channel",
                user_id="user-1",
                message="original direct message",
            ),
        )
    )

    assert replay is None


def test_root_deletion_tombstone_preserves_history_and_is_not_rehydrated(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-delete",
        channel_id="channel-1",
        root_id="root-delete",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    chat_session_id = mapping.chat_session_id
    parent_message_id = mapping.parent_message_id

    tombstoned = tombstone_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-delete",
        channel_id="channel-1",
        root_id="root-delete",
    )

    assert tombstoned is not None
    assert tombstoned.is_active is False
    assert db_session.get(ChatSession, chat_session_id) is not None
    assert parent_message_id is not None
    assert db_session.get(ChatMessage, parent_message_id) is not None
    with pytest.raises(MattermostThreadTombstonedError):
        get_or_create_mattermost_thread_mapping(
            db_session=db_session,
            server_id="mattermost-test-team-delete",
            channel_id="channel-1",
            root_id="root-delete",
            mattermost_user_id="user-1",
            persona_id=test_persona.id,
            onyx_user_id=None,
        )

    restarted_config = MattermostListenerConfig(
        bot_user_id="bot-1",
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_team_ids=frozenset({"mattermost-test-team-delete"}),
        approved_user_ids=frozenset({"user-1"}),
    )
    hydrate_mattermost_listener_config(db_session, restarted_config)
    restarted_listener = MattermostEventListener(MagicMock(), restarted_config)
    reply = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="posted",
            event_id="event-after-delete",
            channel_id="channel-1",
            channel_type="O",
            team_id="mattermost-test-team-delete",
            user_id="user-1",
            post=MattermostPost(
                id="reply-after-delete",
                root_id="root-delete",
                channel_id="channel-1",
                user_id="user-1",
                message="should be ignored",
            ),
        )
    )
    mentioned_reply = restarted_listener.normalize(
        MattermostEventEnvelope(
            event="posted",
            event_id="mentioned-event-after-delete",
            channel_id="channel-1",
            channel_type="O",
            team_id="mattermost-test-team-delete",
            user_id="user-1",
            post=MattermostPost(
                id="mentioned-reply-after-delete",
                root_id="root-delete",
                channel_id="channel-1",
                user_id="user-1",
                message="@onyx still should be ignored",
            ),
        )
    )

    assert "root-delete" not in restarted_config.owned_thread_root_ids
    assert reply is None
    assert mentioned_reply is None


def test_durable_event_claim_serializes_and_allows_expired_lease_takeover(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-ledger-team",
        channel_id="mattermost-test-ledger-channel",
        root_id="mattermost-test-ledger-root",
        mattermost_user_id="mattermost-user",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    claim_time = datetime.datetime(2026, 8, 14, tzinfo=datetime.timezone.utc)
    barrier = Barrier(2)

    def worker() -> tuple[MattermostClaimOutcome, UUID | None]:
        token = CURRENT_TENANT_ID_CONTEXTVAR.set("public")
        try:
            with get_session_with_current_tenant() as session:
                barrier.wait()
                claim = claim_durable_mattermost_event(
                    session,
                    instance_id="mattermost-test-instance",
                    channel_id=mapping.channel_id,
                    dedupe_key="event_id:ledger-concurrent",
                    event_type="channel_mention",
                    mapping_id=mapping.id,
                    source_post_id="mattermost-test-ledger-root",
                    now=claim_time,
                    lease_seconds=300,
                )
                return claim.outcome, claim.claim_owner
        finally:
            CURRENT_TENANT_ID_CONTEXTVAR.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: worker(), range(2)))

    assert sorted(outcome.value for outcome, _owner in outcomes) == ["busy", "process"]
    first_owner = next(
        owner
        for outcome, owner in outcomes
        if outcome is MattermostClaimOutcome.PROCESS
    )
    assert first_owner is not None

    takeover = claim_durable_mattermost_event(
        db_session,
        instance_id="mattermost-test-instance",
        channel_id=mapping.channel_id,
        dedupe_key="event_id:ledger-concurrent",
        event_type="channel_mention",
        mapping_id=mapping.id,
        source_post_id="mattermost-test-ledger-root",
        now=claim_time + datetime.timedelta(seconds=301),
        lease_seconds=300,
    )
    assert takeover.outcome is MattermostClaimOutcome.PROCESS
    assert takeover.claim_owner is not None
    assert takeover.claim_owner != first_owner
    assert mapping.parent_message_id is not None

    assert not checkpoint_mattermost_post(
        db_session,
        event_id=takeover.event.id,
        claim_owner=first_owner,
        post_id="stale-post",
    )
    assert checkpoint_mattermost_post(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
        post_id="mattermost-test-answer",
    )
    assert checkpoint_mattermost_turn(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
        user_message_id=mapping.parent_message_id,
        assistant_message_id=mapping.parent_message_id,
    )
    assert checkpoint_mattermost_rendered_message(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
        rendered_message="final answer",
    )
    assert complete_mattermost_answer_event(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
    )
    db_session.refresh(mapping)
    db_session.refresh(takeover.event)
    assert mapping.parent_message_id == takeover.event.onyx_assistant_message_id
    assert mapping.answer_post_message_ids == {
        "mattermost-test-answer": takeover.event.onyx_assistant_message_id
    }
    assert takeover.event.state == "completed"
    assert takeover.event.claim_owner is None
    assert not checkpoint_mattermost_rendered_message(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
        rendered_message="stale overwrite",
    )


def test_concurrent_event_claim_has_exactly_one_winner(
    db_session: Session,
    test_persona: Persona,
) -> None:
    get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-concurrent-team",
        channel_id="mattermost-test-concurrent-channel",
        root_id="mattermost-test-concurrent-root",
        mattermost_user_id="mattermost-user",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    barrier = Barrier(2)

    def claim() -> bool:
        token = CURRENT_TENANT_ID_CONTEXTVAR.set("public")
        try:
            with get_session_with_current_tenant() as session:
                barrier.wait()
                mapping = claim_mattermost_event(
                    db_session=session,
                    server_id="mattermost-test-concurrent-team",
                    channel_id="mattermost-test-concurrent-channel",
                    root_id="mattermost-test-concurrent-root",
                    dedupe_key="event_id:concurrent-event",
                )
                if mapping is None:
                    session.rollback()
                    return False
                sleep(0.05)
                session.commit()
                return True
        finally:
            CURRENT_TENANT_ID_CONTEXTVAR.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: claim(), range(2)))

    db_session.expire_all()
    mapping = get_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-concurrent-team",
        channel_id="mattermost-test-concurrent-channel",
        root_id="mattermost-test-concurrent-root",
    )
    assert mapping is not None
    assert sorted(outcomes) == [False, True]
    assert mapping.processed_event_ids.count("event_id:concurrent-event") == 1


def test_durable_feedback_is_atomic_and_replay_safe(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-feedback-team",
        channel_id="mattermost-test-feedback-channel",
        root_id="mattermost-test-feedback-root",
        mattermost_user_id="mattermost-user",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    assert mapping.parent_message_id is not None
    parent = db_session.get(ChatMessage, mapping.parent_message_id)
    assert parent is not None
    assistant = create_new_chat_message(
        chat_session_id=mapping.chat_session_id,
        parent_message=parent,
        message="answer",
        token_count=1,
        message_type=MessageType.ASSISTANT,
        db_session=db_session,
    )
    claim = claim_durable_mattermost_event(
        db_session,
        instance_id="mattermost-test-instance",
        channel_id=mapping.channel_id,
        dedupe_key="reaction:event-feedback",
        event_type="reaction_feedback",
        mapping_id=mapping.id,
        source_post_id="mattermost-test-answer",
    )
    assert claim.outcome is MattermostClaimOutcome.PROCESS
    assert claim.claim_owner is not None

    # Simulate process failure after the feedback INSERT is flushed but before the
    # event completion transaction commits. PostgreSQL rollback must remove both.
    feedback = create_chat_message_feedback(
        is_positive=True,
        feedback_text="Mattermost feedback from user-1",
        chat_message_id=assistant.id,
        user_id=None,
        db_session=db_session,
        commit=False,
    )
    db_session.flush()
    claim.event.feedback_id = feedback.id
    claim.event.state = "completed"
    db_session.rollback()
    assert (
        db_session.scalar(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.chat_message_id == assistant.id
            )
        )
        is None
    )
    db_session.expire_all()
    rolled_back_event = db_session.get(MattermostEventState, claim.event.id)
    assert rolled_back_event is not None
    assert rolled_back_event.state == "claimed"
    assert rolled_back_event.feedback_id is None

    takeover = claim_durable_mattermost_event(
        db_session,
        instance_id="mattermost-test-instance",
        channel_id=mapping.channel_id,
        dedupe_key="reaction:event-feedback",
        event_type="reaction_feedback",
        mapping_id=mapping.id,
        source_post_id="mattermost-test-answer",
        now=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=301),
    )
    assert takeover.outcome is MattermostClaimOutcome.PROCESS
    assert takeover.claim_owner is not None
    assert complete_mattermost_feedback_event(
        db_session,
        event_id=takeover.event.id,
        claim_owner=takeover.claim_owner,
        chat_message_id=assistant.id,
        is_positive=True,
        feedback_text="Mattermost feedback from user-1",
    )

    replay = claim_durable_mattermost_event(
        db_session,
        instance_id="mattermost-test-instance",
        channel_id=mapping.channel_id,
        dedupe_key="reaction:event-feedback",
        event_type="reaction_feedback",
        mapping_id=mapping.id,
        source_post_id="mattermost-test-answer",
    )
    assert replay.outcome is MattermostClaimOutcome.COMPLETED
    event = db_session.get(MattermostEventState, claim.event.id)
    assert event is not None
    assert event.state == "completed"
    assert event.feedback_id is not None
    feedback_rows = list(
        db_session.scalars(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.chat_message_id == assistant.id
            )
        )
    )
    assert len(feedback_rows) == 1
    assert feedback_rows[0].id == event.feedback_id


def test_concurrent_feedback_admission_creates_exactly_one_row(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-feedback-race-team",
        channel_id="mattermost-test-feedback-race-channel",
        root_id="mattermost-test-feedback-race-root",
        mattermost_user_id="mattermost-user",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    assert mapping.parent_message_id is not None
    parent = db_session.get(ChatMessage, mapping.parent_message_id)
    assert parent is not None
    assistant = create_new_chat_message(
        chat_session_id=mapping.chat_session_id,
        parent_message=parent,
        message="answer",
        token_count=1,
        message_type=MessageType.ASSISTANT,
        db_session=db_session,
    )
    mapping_id = mapping.id
    channel_id = mapping.channel_id
    assistant_id = assistant.id
    barrier = Barrier(2)

    def claim_and_record() -> bool:
        token = CURRENT_TENANT_ID_CONTEXTVAR.set("public")
        try:
            with get_session_with_current_tenant() as session:
                barrier.wait()
                claim = claim_durable_mattermost_event(
                    session,
                    instance_id="mattermost-test-instance",
                    channel_id=channel_id,
                    dedupe_key="reaction:event-feedback-race",
                    event_type="reaction_feedback",
                    mapping_id=mapping_id,
                    source_post_id="mattermost-test-answer-race",
                )
                if (
                    claim.outcome is not MattermostClaimOutcome.PROCESS
                    or claim.claim_owner is None
                ):
                    return False
                return complete_mattermost_feedback_event(
                    session,
                    event_id=claim.event.id,
                    claim_owner=claim.claim_owner,
                    chat_message_id=assistant_id,
                    is_positive=True,
                    feedback_text="Mattermost feedback from user-1",
                )
        finally:
            CURRENT_TENANT_ID_CONTEXTVAR.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: claim_and_record(), range(2)))

    db_session.expire_all()
    feedback_rows = list(
        db_session.scalars(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.chat_message_id == assistant_id
            )
        )
    )
    event = db_session.scalar(
        select(MattermostEventState).where(
            MattermostEventState.dedupe_key == "reaction:event-feedback-race"
        )
    )
    assert sorted(outcomes) == [False, True]
    assert len(feedback_rows) == 1
    assert event is not None
    assert event.state == "completed"
    assert event.feedback_id == feedback_rows[0].id


def test_concurrent_keyed_turn_create_or_load_has_one_pair(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-turn-race-team",
        channel_id="mattermost-test-turn-race-channel",
        root_id="mattermost-test-turn-race-root",
        mattermost_user_id="mattermost-user",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    assert mapping.parent_message_id is not None
    chat_session_id = mapping.chat_session_id
    parent_message_id = mapping.parent_message_id
    barrier = Barrier(2)
    user_key = "mattermost:event:turn-race:user"
    assistant_key = "mattermost:event:turn-race:assistant"

    def create_or_load() -> tuple[bool, int, int]:
        token = CURRENT_TENANT_ID_CONTEXTVAR.set("public")
        try:
            with get_session_with_current_tenant() as session:
                parent = session.get(ChatMessage, parent_message_id)
                assert parent is not None
                barrier.wait()
                try:
                    user_message = create_new_chat_message(
                        chat_session_id=chat_session_id,
                        parent_message=parent,
                        message="question",
                        token_count=1,
                        message_type=MessageType.USER,
                        db_session=session,
                        commit=False,
                        external_idempotency_key=user_key,
                    )
                    assistant_message = reserve_message_id(
                        db_session=session,
                        chat_session_id=chat_session_id,
                        parent_message=user_message.id,
                        message_type=MessageType.ASSISTANT,
                        external_idempotency_key=assistant_key,
                        commit=False,
                    )
                    session.commit()
                    return True, user_message.id, assistant_message.id
                except IntegrityError:
                    session.rollback()
                    user_message = get_chat_message_by_external_idempotency_key(
                        db_session=session,
                        external_idempotency_key=user_key,
                    )
                    assistant_message = get_chat_message_by_external_idempotency_key(
                        db_session=session,
                        external_idempotency_key=assistant_key,
                    )
                    assert user_message is not None
                    assert assistant_message is not None
                    return False, user_message.id, assistant_message.id
        finally:
            CURRENT_TENANT_ID_CONTEXTVAR.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: create_or_load(), range(2)))

    assert sorted(created for created, _, _ in outcomes) == [False, True]
    assert len({(user_id, assistant_id) for _, user_id, assistant_id in outcomes}) == 1
    assert (
        db_session.scalar(
            select(ChatMessage).where(ChatMessage.external_idempotency_key == user_key)
        )
        is not None
    )
    assert (
        db_session.scalar(
            select(ChatMessage).where(
                ChatMessage.external_idempotency_key == assistant_key
            )
        )
        is not None
    )


def test_model_matches_migration_shape(db_session: Session) -> None:
    if db_session.bind is None:
        raise RuntimeError("Database session is not bound")

    inspector = inspect(db_session.bind)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("mattermost_thread_mapping")
    }

    assert {
        "server_id",
        "channel_id",
        "root_id",
        "mattermost_user_id",
        "persona_id",
        "chat_session_id",
        "parent_message_id",
        "answer_post_message_ids",
        "processed_event_ids",
        "is_active",
    }.issubset(columns)
    for state_column in (
        "answer_post_message_ids",
        "processed_event_ids",
        "is_active",
    ):
        assert columns[state_column]["nullable"] is False
        assert columns[state_column]["default"] is not None

    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("mattermost_thread_mapping")
    }
    assert "uq_mattermost_thread_mapping_thread" in unique_constraints
    assert "uq_mattermost_thread_mapping_chat_session_id" in unique_constraints

    indexes = {
        index["name"] for index in inspector.get_indexes("mattermost_thread_mapping")
    }
    assert "ix_mattermost_thread_mapping_thread_lookup" in indexes


def test_migration_upgrade_from_legacy_mapping_and_downgrade(
    db_session: Session,
    test_persona: Persona,
) -> None:
    mapping = get_or_create_mattermost_thread_mapping(
        db_session=db_session,
        server_id="mattermost-test-team-migration",
        channel_id="channel-1",
        root_id="root-migration",
        mattermost_user_id="user-1",
        persona_id=test_persona.id,
        onyx_user_id=None,
    )
    mapping_id = mapping.id
    chat_session_id = mapping.chat_session_id
    db_session.expunge_all()

    _run_alembic("a14eb2f1d9c0", downgrade=True)
    with get_session_with_current_tenant() as legacy_session:
        if legacy_session.bind is None:
            raise RuntimeError("Database session is not bound")

        legacy_columns = {
            column["name"]
            for column in inspect(legacy_session.bind).get_columns(
                "mattermost_thread_mapping"
            )
        }
        assert (
            "mattermost_thread_mapping"
            in inspect(legacy_session.bind).get_table_names()
        )
        assert "answer_post_message_ids" not in legacy_columns
        assert "processed_event_ids" not in legacy_columns
        assert "is_active" not in legacy_columns
        legacy_root_id = legacy_session.scalar(
            text(
                "SELECT root_id FROM mattermost_thread_mapping WHERE id = :mapping_id"
            ),
            {"mapping_id": mapping_id},
        )
        assert legacy_root_id == "root-migration"

    _run_alembic("head")
    with get_session_with_current_tenant() as upgraded_session:
        if upgraded_session.bind is None:
            raise RuntimeError("Database session is not bound")

        upgraded_columns = {
            column["name"]
            for column in inspect(upgraded_session.bind).get_columns(
                "mattermost_thread_mapping"
            )
        }
        assert {
            "answer_post_message_ids",
            "processed_event_ids",
            "is_active",
        }.issubset(upgraded_columns)
        restored_state = upgraded_session.execute(
            text(
                "SELECT answer_post_message_ids, processed_event_ids, is_active "
                "FROM mattermost_thread_mapping WHERE id = :mapping_id"
            ),
            {"mapping_id": mapping_id},
        ).one()
        assert restored_state.answer_post_message_ids == {}
        assert restored_state.processed_event_ids == []
        assert restored_state.is_active is True
        upgraded_tables = set(inspect(upgraded_session.bind).get_table_names())
        assert "mattermost_event_state" in upgraded_tables
        chat_message_columns = {
            column["name"]
            for column in inspect(upgraded_session.bind).get_columns("chat_message")
        }
        assert "external_idempotency_key" in chat_message_columns

    _run_alembic("f57f35403f6c", downgrade=True)
    with get_session_with_current_tenant() as release_session:
        if release_session.bind is None:
            raise RuntimeError("Database session is not bound")
        release_inspector = inspect(release_session.bind)
        release_tables = set(release_inspector.get_table_names())
        assert "mattermost_thread_mapping" not in release_tables
        assert "mattermost_event_state" not in release_tables
        release_chat_columns = {
            column["name"] for column in release_inspector.get_columns("chat_message")
        }
        assert "external_idempotency_key" not in release_chat_columns

    _run_alembic("head")
    with get_session_with_current_tenant() as reupgraded_session:
        if reupgraded_session.bind is None:
            raise RuntimeError("Database session is not bound")
        reupgraded_inspector = inspect(reupgraded_session.bind)
        assert {
            "mattermost_thread_mapping",
            "mattermost_event_state",
        }.issubset(reupgraded_inspector.get_table_names())
        reupgraded_chat_columns = {
            column["name"]
            for column in reupgraded_inspector.get_columns("chat_message")
        }
        assert "external_idempotency_key" in reupgraded_chat_columns

    db_session.rollback()
    db_session.execute(delete(ChatSession).where(ChatSession.id == chat_session_id))
    db_session.commit()
    _cleanup_mattermost_rows(db_session)
