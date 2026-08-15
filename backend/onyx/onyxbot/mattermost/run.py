"""Private local service entrypoint for the Mattermost bot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import cast

import uvicorn
from fastapi import FastAPI, Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from onyx.configs.app_configs import (
    POSTGRES_API_SERVER_POOL_OVERFLOW,
    POSTGRES_API_SERVER_POOL_SIZE,
)
from onyx.db.engine.sql_engine import SqlEngine, get_session_with_current_tenant
from onyx.db.mattermost_bot import (
    fetch_mattermost_channel_config_for_bot_and_channel,
    fetch_mattermost_private_answer_channel_ids,
    get_or_bootstrap_mattermost_slash_command_config,
    hydrate_mattermost_listener_config,
)
from onyx.onyxbot.mattermost.client import MattermostClient
from onyx.onyxbot.mattermost.commands import (
    MattermostSlashCommandControl,
    MattermostSlashCommandResponse,
    handle_mattermost_slash_command,
)
from onyx.onyxbot.mattermost.config import (
    MattermostBotConfig,
    canonical_mattermost_instance_id,
    load_mattermost_bot_config_from_env,
    redacted_mattermost_bot_env,
)
from onyx.onyxbot.mattermost.handler import (
    MattermostHandlerConfig,
    dispatch_mattermost_attachment_promotion,
    dispatch_mattermost_mutation,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.interactive import handle_mattermost_interactive_action
from onyx.onyxbot.mattermost.listener import MattermostEventListener
from onyx.onyxbot.mattermost.models import NormalizedMattermostEvent
from onyx.onyxbot.mattermost.mutations import (
    AuthoritativePlatformGatewayBridge,
    MattermostMutationAdapter,
)
from onyx.onyxbot.mattermost.placement import (
    SeafileHierarchyEvidence,
    _validate_hierarchy,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

MATTERMOST_ATTACHMENT_PLACEMENT_HIERARCHY_REFRESH_SECONDS = 60


def get_application(config: MattermostBotConfig | None = None) -> FastAPI:
    """Build the Mattermost bot service app."""

    runtime_config = config or load_mattermost_bot_config_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        listener_ready = asyncio.Event()
        listener_task = asyncio.create_task(_run_bot(runtime_config, listener_ready))
        readiness_task = asyncio.create_task(listener_ready.wait())
        app.state.listener_task = listener_task
        app.state.listener_ready = listener_ready
        done, _ = await asyncio.wait(
            {listener_task, readiness_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if listener_task in done:
            readiness_task.cancel()
            await listener_task
            raise RuntimeError("Mattermost listener exited before becoming ready")
        if not readiness_task.done():
            readiness_task.cancel()
        try:
            yield
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Onyx Mattermost Bot", lifespan=lifespan)

    @app.get("/health")
    async def health() -> JSONResponse:
        listener_task: asyncio.Task[None] = app.state.listener_task
        listener_ready: asyncio.Event = app.state.listener_ready
        if listener_task.done() or not listener_ready.is_set():
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.post("/commands/orka")
    async def orka_command(request: Request) -> JSONResponse:
        return await _handle_slash_command_request(request, runtime_config)

    @app.post("/commands/orka/{action_name}")
    async def orka_action_command(
        request: Request,
        action_name: str,
    ) -> JSONResponse:
        return await _handle_slash_command_request(
            request,
            runtime_config,
            action_name=action_name,
        )

    @app.post("/interactive")
    async def interactive_action(request: Request) -> JSONResponse:
        return await _handle_interactive_action_request(request, runtime_config)

    return app


async def _handle_slash_command_request(
    request: Request,
    config: MattermostBotConfig,
    *,
    action_name: str | None = None,
) -> JSONResponse:
    form = await request.form()
    payload = {key: str(value) for key, value in form.multi_items()}
    if action_name in {"ask", "help", "status", "sources"}:
        text = payload.get("text", "")
        if not text.casefold().startswith(action_name):
            payload["text"] = f"{action_name} {text}".strip()

    instance_id = canonical_mattermost_instance_id(config.url)
    with get_session_with_current_tenant() as db_session:
        slash_command_config = get_or_bootstrap_mattermost_slash_command_config(
            db_session,
            instance_id=instance_id,
            bot_user_id=config.listener_config.bot_user_id,
            bootstrap_token=config.slash_command_bootstrap_token,
        )
    slash_command_control = (
        MattermostSlashCommandControl(
            instance_id=slash_command_config.instance_id,
            bot_user_id=slash_command_config.bot_user_id,
            token=slash_command_config.token.get_value(apply_mask=False),
            enabled=slash_command_config.enabled,
        )
        if slash_command_config is not None and slash_command_config.token is not None
        else None
    )
    with get_session_with_current_tenant() as db_session:
        handler_config = _build_handler_config(config, db_session)
    async with MattermostClient(
        config.url,
        config.token,
        request_timeout_seconds=config.request_timeout_seconds,
    ) as client:

        async def handle_event(event: NormalizedMattermostEvent) -> bool:
            with get_session_with_current_tenant() as db_session:
                return await handle_normalized_mattermost_event(
                    event=event,
                    config=handler_config,
                    client=client,
                    db_session=db_session,
                )

        response = await handle_mattermost_slash_command(
            payload=payload,
            command_control=slash_command_control,
            client=client,
            handle_event=handle_event,
        )
    return _slash_command_json_response(response)


async def _handle_interactive_action_request(
    request: Request,
    config: MattermostBotConfig,
) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400, content={"text": "Invalid Mattermost action."}
        )
    async with MattermostClient(
        config.url,
        config.token,
        request_timeout_seconds=config.request_timeout_seconds,
    ) as client:
        mutation_adapter: MattermostMutationAdapter | None = None
        bridge: AuthoritativePlatformGatewayBridge | None = None
        if config.mutation_gateway_factory is not None:
            bridge = AuthoritativePlatformGatewayBridge.from_factory_spec(
                config.mutation_gateway_factory
            )
            mutation_adapter = MattermostMutationAdapter(client, bridge)

        async def dispatch_confirmed_mutation(
            *, event: NormalizedMattermostEvent
        ) -> bool:
            return await dispatch_mattermost_mutation(
                event=event,
                client=client,
                adapter=mutation_adapter,
            )

        async def dispatch_confirmed_promotion(
            *, event: NormalizedMattermostEvent, proposal: object
        ) -> bool:
            active_bridge = bridge
            with get_session_with_current_tenant() as db_session:
                handler_config = replace(
                    _build_handler_config(config, db_session),
                    mutation_adapter=mutation_adapter,
                    mutation_gateway=active_bridge,
                    attachment_placement_hierarchy_provider=(
                        lambda _event: cast(
                            SeafileHierarchyEvidence | None,
                            active_bridge.get_mattermost_attachment_placement_hierarchy(),
                        )
                    )
                    if active_bridge is not None
                    else None,
                    attachment_promotion_read_back=(
                        active_bridge.read_mattermost_attachment_promotion_destination
                        if active_bridge is not None
                        else None
                    ),
                    attachment_promotion_freshness_check=(
                        active_bridge.get_mattermost_attachment_promotion_freshness
                        if active_bridge is not None
                        else None
                    ),
                )
                return await dispatch_mattermost_attachment_promotion(
                    event=event,
                    proposal=proposal,
                    client=client,
                    db_session=db_session,
                    config=handler_config,
                )

        with get_session_with_current_tenant() as db_session:
            await handle_mattermost_interactive_action(
                payload=payload,
                signing_secret=config.token,
                bot_user_id=config.listener_config.bot_user_id,
                client=client,
                db_session=db_session,
                instance_id=canonical_mattermost_instance_id(config.url),
                dispatch_mutation=dispatch_confirmed_mutation,
                dispatch_promotion=dispatch_confirmed_promotion,
            )
    return JSONResponse(status_code=200, content={"text": ""})


def _slash_command_json_response(
    response: MattermostSlashCommandResponse,
) -> JSONResponse:
    return JSONResponse(
        status_code=response.status_code,
        content=response.as_mattermost_payload(),
    )


async def _run_bot(
    config: MattermostBotConfig,
    ready_event: asyncio.Event | None = None,
) -> None:
    with get_session_with_current_tenant() as db_session:
        hydrate_mattermost_listener_config(db_session, config.listener_config)

    listener_config = replace(
        config.listener_config,
        managed_channel_config_resolver=_build_managed_channel_config_resolver(config),
    )
    async with MattermostClient(
        config.url,
        config.token,
        request_timeout_seconds=config.request_timeout_seconds,
    ) as client:
        await client.get_me()
        mutation_adapter = None
        hierarchy_refresh_task: asyncio.Task[None] | None = None
        attachment_placement_hierarchy_provider: (
            Callable[[NormalizedMattermostEvent], SeafileHierarchyEvidence | None]
            | None
        ) = None
        if config.mutation_gateway_factory is not None:
            bridge = AuthoritativePlatformGatewayBridge.from_factory_spec(
                config.mutation_gateway_factory
            )
            mutation_adapter = MattermostMutationAdapter(client, bridge)
            cached_attachment_placement_hierarchy = (
                _fetch_current_attachment_placement_hierarchy(bridge)
            )

            async def refresh_attachment_placement_hierarchy() -> None:
                nonlocal cached_attachment_placement_hierarchy
                while True:
                    await asyncio.sleep(
                        MATTERMOST_ATTACHMENT_PLACEMENT_HIERARCHY_REFRESH_SECONDS
                    )
                    try:
                        cached_attachment_placement_hierarchy = (
                            _fetch_current_attachment_placement_hierarchy(bridge)
                        )
                    except RuntimeError:
                        logger.exception(
                            "Mattermost attachment hierarchy refresh failed; retaining last valid snapshot"
                        )

            hierarchy_refresh_task = asyncio.create_task(
                refresh_attachment_placement_hierarchy()
            )

            def bridge_attachment_placement_hierarchy_provider(
                _event: NormalizedMattermostEvent,
            ) -> SeafileHierarchyEvidence | None:
                return cached_attachment_placement_hierarchy

            attachment_placement_hierarchy_provider = (
                bridge_attachment_placement_hierarchy_provider
            )
        try:
            listener = MattermostEventListener(client, listener_config)
            if ready_event is not None:
                ready_event.set()
            async for event in listener.normalized_events():
                with get_session_with_current_tenant() as db_session:
                    handler_config = replace(
                        _build_handler_config(config, db_session),
                        mutation_adapter=mutation_adapter,
                        attachment_placement_hierarchy_provider=(
                            attachment_placement_hierarchy_provider
                        ),
                    )
                    await handle_normalized_mattermost_event(
                        event=event,
                        config=handler_config,
                        client=client,
                        db_session=db_session,
                    )
        finally:
            if hierarchy_refresh_task is not None:
                hierarchy_refresh_task.cancel()


def _fetch_current_attachment_placement_hierarchy(
    bridge: AuthoritativePlatformGatewayBridge,
) -> SeafileHierarchyEvidence:
    hierarchy = cast(
        SeafileHierarchyEvidence | None,
        bridge.get_mattermost_attachment_placement_hierarchy(),
    )
    if type(hierarchy) is not SeafileHierarchyEvidence:
        raise RuntimeError("Mattermost attachment placement hierarchy is unavailable")
    try:
        _validate_hierarchy(hierarchy)
    except ValueError:
        raise RuntimeError(
            "Mattermost attachment placement hierarchy is unavailable"
        ) from None
    return hierarchy


def _build_managed_channel_config_resolver(
    config: MattermostBotConfig,
) -> Callable[[str], dict[str, object] | None]:
    instance_id = canonical_mattermost_instance_id(config.url)

    def resolve_channel_config(channel_id: str) -> dict[str, object] | None:
        with get_session_with_current_tenant() as db_session:
            channel_config = fetch_mattermost_channel_config_for_bot_and_channel(
                db_session,
                instance_id=instance_id,
                bot_user_id=config.listener_config.bot_user_id,
                channel_id=channel_id,
            )
        if channel_config is None:
            return None
        return dict(channel_config.channel_config)

    return resolve_channel_config


def _build_handler_config(
    config: MattermostBotConfig,
    db_session: Session,
) -> MattermostHandlerConfig:
    instance_id = canonical_mattermost_instance_id(config.url)
    ephemeral_response_channel_ids = fetch_mattermost_private_answer_channel_ids(
        db_session,
        instance_id=instance_id,
        bot_user_id=config.listener_config.bot_user_id,
    )
    return MattermostHandlerConfig(
        persona_id=config.persona_id,
        instance_id=instance_id,
        bot_user_id=config.listener_config.bot_user_id,
        owned_thread_root_ids=config.listener_config.owned_thread_root_ids,
        tombstoned_thread_root_ids=config.listener_config.tombstoned_thread_root_ids,
        owned_answer_post_root_ids=config.listener_config.owned_answer_post_root_ids,
        owned_answer_post_message_ids=config.listener_config.owned_answer_post_message_ids,
        ephemeral_response_channel_ids=ephemeral_response_channel_ids,
        interactive_signing_secret=config.token,
        interactive_url=_interactive_action_url(config),
    )


def _interactive_action_url(config: MattermostBotConfig) -> str:
    host = getattr(config, "host", "127.0.0.1")
    port = getattr(config, "port", 8091)
    return f"http://{host}:{port}/interactive"


def main() -> None:
    SqlEngine.init_engine(
        pool_size=POSTGRES_API_SERVER_POOL_SIZE,
        max_overflow=POSTGRES_API_SERVER_POOL_OVERFLOW,
    )
    config = load_mattermost_bot_config_from_env()
    logger.info("Starting Mattermost bot service", extra=redacted_mattermost_bot_env())
    uvicorn.run(get_application(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
