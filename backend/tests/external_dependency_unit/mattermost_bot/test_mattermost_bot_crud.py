from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from onyx.db.engine.sql_engine import get_session
from onyx.db.mattermost_bot import (
    fetch_mattermost_bot,
    fetch_mattermost_bots,
    fetch_mattermost_channel_config_for_bot_and_channel,
    insert_mattermost_bot,
    insert_mattermost_channel_config,
    remove_mattermost_bot,
    update_mattermost_bot,
)
from onyx.db.models import MattermostBot, User
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import MattermostUserInfo
from onyx.server.manage.mattermost_bot import create_bot, router
from onyx.server.manage.models import MattermostBot as MattermostBotView
from onyx.server.manage.models import MattermostBotCreationRequest
from onyx.utils.sensitive import SensitiveValue


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def cleanup_mattermost_bots(db_session: Session) -> None:
    db_session.execute(delete(MattermostBot))
    db_session.commit()


def test_insert_mattermost_bot_returns_write_only_encrypted_token(
    db_session: Session,
) -> None:
    token = _unique("mmtoken")

    bot = insert_mattermost_bot(
        db_session=db_session,
        name=_unique("mattermost"),
        url="https://mattermost.example.com",
        enabled=True,
        token=token,
        bot_user_id="bot-user",
        bot_username="onyxbot",
    )

    assert isinstance(bot.token, SensitiveValue)
    assert bot.token.get_value(apply_mask=False) == token
    view = MattermostBotView.from_model(bot)
    assert view.token == ""
    assert view.bot_user_id == "bot-user"
    assert view.health_status == "unknown"


def test_update_mattermost_bot_preserves_token_when_no_rotation_requested(
    db_session: Session,
) -> None:
    bot = insert_mattermost_bot(
        db_session=db_session,
        name="before",
        url="https://mattermost.example.com",
        enabled=True,
        token="original-token",
        bot_user_id="bot-user",
        bot_username="onyxbot",
    )

    updated = update_mattermost_bot(
        db_session=db_session,
        mattermost_bot_id=bot.id,
        name="after",
        url="https://mattermost.example.com/team",
        enabled=False,
        token=None,
        bot_user_id="bot-user-2",
        bot_username="onyxbot2",
        health_status="ok",
        health_error=None,
    )

    assert updated.name == "after"
    assert updated.enabled is False
    assert updated.token is not None
    assert updated.token.get_value(apply_mask=False) == "original-token"
    assert updated.bot_user_id == "bot-user-2"
    assert updated.health_status == "ok"
    assert fetch_mattermost_bot(db_session, bot.id).bot_username == "onyxbot2"


def test_mattermost_bot_crud_is_idempotent_for_delete(db_session: Session) -> None:
    bot = insert_mattermost_bot(
        db_session=db_session,
        name="to-delete",
        url="https://mattermost.example.com",
        enabled=True,
        token="token",
        bot_user_id="bot-user",
        bot_username="onyxbot",
    )

    remove_mattermost_bot(db_session=db_session, mattermost_bot_id=bot.id)
    remove_mattermost_bot(db_session=db_session, mattermost_bot_id=bot.id)

    assert fetch_mattermost_bots(db_session) == []


def test_channel_config_lookup_ignores_disabled_channel_row_and_falls_back(
    db_session: Session,
) -> None:
    bot = insert_mattermost_bot(
        db_session=db_session,
        name="fallback-bot",
        url="https://mattermost.example.com",
        enabled=True,
        token="token",
        bot_user_id="bot-user",
        bot_username="onyxbot",
    )
    default_config = insert_mattermost_channel_config(
        db_session=db_session,
        mattermost_bot_id=bot.id,
        channel_id=None,
        channel_name=None,
        persona_id=None,
        channel_config={
            "channel_name": None,
            "respond_tag_only": False,
            "response_style": "orka_concise",
            "disabled": False,
        },
        is_default=True,
        enabled=True,
    )
    insert_mattermost_channel_config(
        db_session=db_session,
        mattermost_bot_id=bot.id,
        channel_id="channel-disabled",
        channel_name="Disabled",
        persona_id=None,
        channel_config={
            "channel_name": "Disabled",
            "respond_tag_only": True,
            "response_style": "orka_concise",
            "disabled": False,
        },
        enabled=False,
    )

    resolved = fetch_mattermost_channel_config_for_bot_and_channel(
        db_session,
        instance_id="https://mattermost.example.com",
        bot_user_id="bot-user",
        channel_id="channel-disabled",
    )

    assert resolved is not None
    assert resolved.id == default_config.id
    assert resolved.channel_config["respond_tag_only"] is False


def test_create_route_requires_admin_permission(db_session: Session) -> None:
    app = FastAPI()
    app.include_router(router)

    def override_get_session() -> Session:
        return db_session

    def deny_admin() -> None:
        raise HTTPException(status_code=403, detail="admin required")

    app.dependency_overrides[get_session] = override_get_session
    admin_dependency = next(
        dependency.call
        for route in router.routes
        if isinstance(route, APIRoute)
        for dependency in route.dependant.dependencies
        if getattr(dependency.call, "_required_permission", None) is not None
    )
    assert admin_dependency is not None
    app.dependency_overrides[admin_dependency] = deny_admin

    response = TestClient(app, raise_server_exceptions=False).post(
        "/manage/admin/mattermost-app/bots",
        json={
            "name": "denied",
            "url": "https://mattermost.example.com",
            "enabled": True,
            "token": "secret-token",
        },
    )

    assert response.status_code == 403


def test_create_route_validates_identity_and_records_health(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[tuple[str, str]] = []

    async def fake_validate(url: str, token: str) -> MattermostUserInfo:
        validated.append((url, token))
        return MattermostUserInfo(
            id="bot-user",
            username="onyxbot",
            display_name="Onyx Bot",
            roles="system_user",
        )

    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.validate_mattermost_bot_identity",
        fake_validate,
    )

    created = MattermostBotCreationRequest(
        name="mattermost managed",
        url="https://mattermost.example.com",
        enabled=True,
        token="secret-token",
    )
    view = create_bot(created, db_session, cast(User, None))

    assert validated == [("https://mattermost.example.com", "secret-token")]
    assert view.token == ""
    assert view.bot_user_id == "bot-user"
    assert view.bot_username == "onyxbot"
    assert view.health_status == "ok"
    stored_token = fetch_mattermost_bots(db_session)[0].token
    assert stored_token is not None
    assert stored_token.get_value(apply_mask=False) == "secret-token"


def test_create_route_fails_closed_when_identity_validation_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_validate(url: str, token: str) -> MattermostUserInfo:  # noqa: ARG001
        raise MattermostClientError("connection refused")

    monkeypatch.setattr(
        "onyx.server.manage.mattermost_bot.validate_mattermost_bot_identity",
        fake_validate,
    )

    with pytest.raises(HTTPException) as exc_info:
        create_bot(
            MattermostBotCreationRequest(
                name="invalid",
                url="https://mattermost.example.com",
                enabled=True,
                token="bad-token",
            ),
            db_session,
            cast(User, None),
        )

    assert exc_info.value.status_code == 400
    assert fetch_mattermost_bots(db_session) == []
