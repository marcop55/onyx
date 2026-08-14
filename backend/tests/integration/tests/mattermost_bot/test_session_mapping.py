from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from onyx.configs.constants import MessageType
from onyx.db.chat import create_new_chat_message
from onyx.db.engine.sql_engine import SqlEngine, get_session_with_current_tenant
from onyx.db.mattermost_bot import (
    get_mattermost_chat_session_for_thread,
    get_mattermost_session_key,
    get_mattermost_thread_mapping,
    get_mattermost_thread_mapping_by_chat_session_id,
    get_or_create_mattermost_thread_mapping,
    update_mattermost_thread_parent_message,
)
from onyx.db.models import ChatMessage, ChatSession, MattermostThreadMapping, Persona
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

    db_session.delete(persona)
    db_session.commit()


def _cleanup_mattermost_rows(db_session: Session) -> None:
    chat_session_ids = list(
        db_session.scalars(
            select(MattermostThreadMapping.chat_session_id).where(
                MattermostThreadMapping.server_id.like("mattermost-test-%")
            )
        )
    )
    db_session.execute(
        delete(MattermostThreadMapping).where(
            MattermostThreadMapping.server_id.like("mattermost-test-%")
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


def test_model_matches_migration_shape(db_session: Session) -> None:
    if db_session.bind is None:
        raise RuntimeError("Database session is not bound")

    inspector = inspect(db_session.bind)
    column_names = {
        column["name"] for column in inspector.get_columns("mattermost_thread_mapping")
    }

    assert {
        "server_id",
        "channel_id",
        "root_id",
        "mattermost_user_id",
        "persona_id",
        "chat_session_id",
        "parent_message_id",
    }.issubset(column_names)

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


def test_migration_upgrade_and_downgrade() -> None:
    _run_alembic("f57f35403f6c", downgrade=True)
    with get_session_with_current_tenant() as db_session:
        if db_session.bind is None:
            raise RuntimeError("Database session is not bound")

        assert (
            "mattermost_thread_mapping"
            not in inspect(db_session.bind).get_table_names()
        )

    _run_alembic("head")
    with get_session_with_current_tenant() as db_session:
        if db_session.bind is None:
            raise RuntimeError("Database session is not bound")

        assert "mattermost_thread_mapping" in inspect(db_session.bind).get_table_names()
