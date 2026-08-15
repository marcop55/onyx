import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.mattermost_bot import (
    fetch_mattermost_bot,
    fetch_mattermost_bots,
    fetch_mattermost_channel_config,
    fetch_mattermost_channel_configs,
    insert_mattermost_bot,
    insert_mattermost_channel_config,
    remove_mattermost_bot,
    remove_mattermost_channel_config,
    update_mattermost_bot,
    update_mattermost_channel_config,
)
from onyx.db.models import ChannelConfig, User
from onyx.db.slack_channel_config import validate_standard_answer_categories_by_ids
from onyx.onyxbot.mattermost.client import MattermostClient, MattermostClientError
from onyx.onyxbot.mattermost.config import canonical_mattermost_instance_id
from onyx.onyxbot.mattermost.health import (
    MattermostChannelHealth,
    MattermostObservabilitySnapshot,
    collect_mattermost_observability,
)
from onyx.onyxbot.mattermost.models import MattermostUserInfo
from onyx.server.manage.models import (
    MattermostBot,
    MattermostBotCreationRequest,
    MattermostChannelConfig,
    MattermostChannelConfigCreationRequest,
)
from onyx.utils.errors import EERequiredError

router = APIRouter(prefix="/manage")
_MATTERMOST_JOINED_CHANNEL_DISCOVERY_ERROR = (
    "Mattermost joined-channel discovery failed"
)


def _form_channel_config(
    request: MattermostChannelConfigCreationRequest,
) -> ChannelConfig:
    return {
        "channel_name": request.channel_name,
        "respond_tag_only": request.respond_tag_only,
        "response_style": request.response_style.value,
        "response_type": request.response_type.value,
        "include_source_previews": request.include_source_previews,
        "answer_filters": request.answer_filters,
        "standard_answer_category_ids": request.standard_answer_category_ids,
        "follow_up_tags": request.follow_up_tags,
        "disabled": request.disabled,
    }


def _validate_standard_answer_category_ids(
    *,
    db_session: Session,
    standard_answer_category_ids: list[int],
) -> None:
    try:
        validate_standard_answer_categories_by_ids(
            db_session=db_session,
            standard_answer_category_ids=standard_answer_category_ids,
        )
    except (EERequiredError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    return asyncio.run(
        discover_joined_mattermost_channels(
            getattr(bot, "url"), raw_token, getattr(bot, "bot_user_id")
        )
    )


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
        url=canonical_mattermost_instance_id(mattermost_bot_creation_request.url),
        enabled=mattermost_bot_creation_request.enabled,
        token=mattermost_bot_creation_request.token,
        bot_user_id=identity.id,
        bot_username=identity.username,
        health_status="ok",
        health_error=None,
    )
    insert_mattermost_channel_config(
        db_session=db_session,
        mattermost_bot_id=mattermost_bot_model.id,
        channel_id=None,
        channel_name=None,
        persona_id=None,
        channel_config={
            "channel_name": None,
            "respond_tag_only": True,
            "response_style": "orka_concise",
            "response_type": "citations",
            "include_source_previews": False,
            "answer_filters": [],
            "standard_answer_category_ids": [],
            "follow_up_tags": None,
            "disabled": False,
        },
        is_default=True,
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
        url=canonical_mattermost_instance_id(mattermost_bot_creation_request.url),
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
    try:
        joined_channels = _discover_joined_channels_sync(bot)
    except MattermostClientError:
        bot.health_status = "error"
        bot.health_error = _MATTERMOST_JOINED_CHANNEL_DISCOVERY_ERROR
        joined_channels = []
    return collect_mattermost_observability(
        db_session,
        bot,
        joined_channels=joined_channels,
    )


@router.get("/admin/mattermost-app/bots")
def list_bots(
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> list[MattermostBot]:
    return [MattermostBot.from_model(bot) for bot in fetch_mattermost_bots(db_session)]


@router.post("/admin/mattermost-app/channel")
def create_mattermost_channel_config(
    request: MattermostChannelConfigCreationRequest,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostChannelConfig:
    fetch_mattermost_bot(db_session, request.mattermost_bot_id)
    _validate_standard_answer_category_ids(
        db_session=db_session,
        standard_answer_category_ids=request.standard_answer_category_ids,
    )
    config_model = insert_mattermost_channel_config(
        db_session=db_session,
        mattermost_bot_id=request.mattermost_bot_id,
        channel_id=request.channel_id,
        channel_name=request.channel_name,
        persona_id=request.persona_id,
        channel_config=_form_channel_config(request),
        is_default=request.is_default,
        is_ephemeral=request.is_ephemeral,
        enabled=request.enabled,
    )
    return MattermostChannelConfig.from_model(config_model)


@router.patch("/admin/mattermost-app/channel/{mattermost_channel_config_id}")
def patch_mattermost_channel_config(
    mattermost_channel_config_id: int,
    request: MattermostChannelConfigCreationRequest,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> MattermostChannelConfig:
    existing = fetch_mattermost_channel_config(
        db_session,
        mattermost_channel_config_id=mattermost_channel_config_id,
    )
    fetch_mattermost_bot(db_session, request.mattermost_bot_id)
    if existing.mattermost_bot_id != request.mattermost_bot_id:
        raise HTTPException(status_code=400, detail="Mattermost bot ID cannot change")
    _validate_standard_answer_category_ids(
        db_session=db_session,
        standard_answer_category_ids=request.standard_answer_category_ids,
    )
    config_model = update_mattermost_channel_config(
        db_session=db_session,
        mattermost_channel_config_id=mattermost_channel_config_id,
        channel_id=request.channel_id,
        channel_name=request.channel_name,
        persona_id=request.persona_id,
        channel_config=_form_channel_config(request),
        is_ephemeral=request.is_ephemeral,
        enabled=request.enabled,
    )
    return MattermostChannelConfig.from_model(config_model)


@router.delete("/admin/mattermost-app/channel/{mattermost_channel_config_id}")
def delete_mattermost_channel_config(
    mattermost_channel_config_id: int,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> None:
    remove_mattermost_channel_config(
        db_session=db_session,
        mattermost_channel_config_id=mattermost_channel_config_id,
    )


@router.get("/admin/mattermost-app/bots/{mattermost_bot_id}/config")
def list_mattermost_bot_configs(
    mattermost_bot_id: int,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> list[MattermostChannelConfig]:
    return [
        MattermostChannelConfig.from_model(config)
        for config in fetch_mattermost_channel_configs(
            db_session,
            mattermost_bot_id=mattermost_bot_id,
        )
    ]


@router.get("/admin/mattermost-app/channel")
def list_mattermost_channel_configs(
    mattermost_bot_id: int | None = None,
    db_session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> list[MattermostChannelConfig]:
    config_models = fetch_mattermost_channel_configs(
        db_session=db_session,
        mattermost_bot_id=mattermost_bot_id,
    )
    return [
        MattermostChannelConfig.from_model(config_model)
        for config_model in config_models
    ]
