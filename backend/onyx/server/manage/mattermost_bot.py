import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.mattermost_bot import (
    fetch_mattermost_bot,
    fetch_mattermost_bots,
    insert_mattermost_bot,
    remove_mattermost_bot,
    update_mattermost_bot,
)
from onyx.db.models import User
from onyx.onyxbot.mattermost.client import MattermostClient, MattermostClientError
from onyx.onyxbot.mattermost.health import (
    MattermostChannelHealth,
    MattermostObservabilitySnapshot,
    collect_mattermost_observability,
)
from onyx.onyxbot.mattermost.models import MattermostUserInfo
from onyx.server.manage.models import MattermostBot, MattermostBotCreationRequest

router = APIRouter(prefix="/manage")


async def validate_mattermost_bot_identity(url: str, token: str) -> MattermostUserInfo:
    async with MattermostClient(url, token) as client:
        identity_payload = await client.get_me()
    user_id = identity_payload.get("id")
    username = identity_payload.get("username")
    display_name = (
        identity_payload.get("nickname") or identity_payload.get("first_name") or ""
    )
    roles = identity_payload.get("roles") or ""
    if not isinstance(user_id, str) or not user_id:
        raise MattermostClientError("Mattermost identity response is missing id")
    if not isinstance(username, str) or not username:
        raise MattermostClientError("Mattermost identity response is missing username")
    return MattermostUserInfo(
        id=user_id,
        username=username,
        display_name=str(display_name),
        roles=str(roles),
    )


def _validate_identity_sync(url: str, token: str) -> MattermostUserInfo:
    try:
        return asyncio.run(validate_mattermost_bot_identity(url, token))
    except MattermostClientError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid Mattermost bot token"
        ) from exc


async def discover_joined_mattermost_channels(
    url: str,
    token: str,
    bot_user_id: str,
) -> list[MattermostChannelHealth]:
    async with MattermostClient(url, token) as client:
        raw_channels = await client.get_joined_channels(bot_user_id)
    return [
        MattermostChannelHealth(
            id=str(channel.get("id") or ""),
            name=str(channel.get("name") or ""),
            display_name=str(channel.get("display_name") or channel.get("name") or ""),
            bot_is_member=True,
        )
        for channel in raw_channels
        if channel.get("id")
    ]


def _discover_joined_channels_sync(bot: object) -> list[MattermostChannelHealth]:
    token = getattr(bot, "token", None)
    if token is None:
        return []
    raw_token = token.get_value(apply_mask=False)
    try:
        return asyncio.run(
            discover_joined_mattermost_channels(
                getattr(bot, "url"), raw_token, getattr(bot, "bot_user_id")
            )
        )
    except MattermostClientError:
        return []


@router.post("/admin/mattermost-app/bots")
def create_bot(
    mattermost_bot_creation_request: MattermostBotCreationRequest,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostBot:
    if mattermost_bot_creation_request.token is None:
        raise HTTPException(status_code=400, detail="Mattermost bot token is required")
    identity = _validate_identity_sync(
        mattermost_bot_creation_request.url,
        mattermost_bot_creation_request.token,
    )
    mattermost_bot_model = insert_mattermost_bot(
        db_session=db_session,
        name=mattermost_bot_creation_request.name,
        url=mattermost_bot_creation_request.url,
        enabled=mattermost_bot_creation_request.enabled,
        token=mattermost_bot_creation_request.token,
        bot_user_id=identity.id,
        bot_username=identity.username,
        health_status="ok",
        health_error=None,
    )
    return MattermostBot.from_model(mattermost_bot_model)


@router.patch("/admin/mattermost-app/bots/{mattermost_bot_id}")
def patch_bot(
    mattermost_bot_id: int,
    mattermost_bot_creation_request: MattermostBotCreationRequest,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostBot:
    existing = fetch_mattermost_bot(db_session, mattermost_bot_id)
    token = mattermost_bot_creation_request.token
    existing_token = existing.token
    if token is not None:
        raw_token = token
    elif existing_token is not None:
        raw_token = existing_token.get_value(apply_mask=False)
    else:
        raise HTTPException(status_code=400, detail="Mattermost bot token is required")
    identity = _validate_identity_sync(mattermost_bot_creation_request.url, raw_token)
    mattermost_bot_model = update_mattermost_bot(
        db_session=db_session,
        mattermost_bot_id=mattermost_bot_id,
        name=mattermost_bot_creation_request.name,
        url=mattermost_bot_creation_request.url,
        enabled=mattermost_bot_creation_request.enabled,
        token=token,
        bot_user_id=identity.id,
        bot_username=identity.username,
        health_status="ok",
        health_error=None,
    )
    return MattermostBot.from_model(mattermost_bot_model)


@router.delete("/admin/mattermost-app/bots/{mattermost_bot_id}")
def delete_bot(
    mattermost_bot_id: int,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> None:
    remove_mattermost_bot(db_session=db_session, mattermost_bot_id=mattermost_bot_id)


@router.get("/admin/mattermost-app/bots/{mattermost_bot_id}")
def get_bot_by_id(
    mattermost_bot_id: int,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostBot:
    return MattermostBot.from_model(fetch_mattermost_bot(db_session, mattermost_bot_id))


@router.get("/admin/mattermost-app/bots/{mattermost_bot_id}/observability")
def get_bot_observability(
    mattermost_bot_id: int,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostObservabilitySnapshot:
    bot = fetch_mattermost_bot(db_session, mattermost_bot_id)
    return collect_mattermost_observability(
        db_session,
        bot,
        joined_channels=_discover_joined_channels_sync(bot),
    )


@router.get("/admin/mattermost-app/bots")
def list_bots(
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> list[MattermostBot]:
    return [MattermostBot.from_model(bot) for bot in fetch_mattermost_bots(db_session)]
