"""Private local service entrypoint for the Mattermost bot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from onyx.configs.app_configs import (
    POSTGRES_API_SERVER_POOL_OVERFLOW,
    POSTGRES_API_SERVER_POOL_SIZE,
)
from onyx.db.engine.sql_engine import SqlEngine, get_session_with_current_tenant
from onyx.db.mattermost_bot import (
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
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.listener import MattermostEventListener
from onyx.onyxbot.mattermost.models import NormalizedMattermostEvent
from onyx.onyxbot.mattermost.mutations import (
    AuthoritativePlatformGatewayBridge,
    MattermostMutationAdapter,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()


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
    handler_config = MattermostHandlerConfig(
        persona_id=config.persona_id,
        instance_id=instance_id,
        bot_user_id=config.listener_config.bot_user_id,
        owned_thread_root_ids=config.listener_config.owned_thread_root_ids,
        tombstoned_thread_root_ids=config.listener_config.tombstoned_thread_root_ids,
        owned_answer_post_root_ids=config.listener_config.owned_answer_post_root_ids,
        owned_answer_post_message_ids=config.listener_config.owned_answer_post_message_ids,
    )
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

    handler_config = MattermostHandlerConfig(
        persona_id=config.persona_id,
        instance_id=canonical_mattermost_instance_id(config.url),
        bot_user_id=config.listener_config.bot_user_id,
        owned_thread_root_ids=config.listener_config.owned_thread_root_ids,
        tombstoned_thread_root_ids=(config.listener_config.tombstoned_thread_root_ids),
        owned_answer_post_root_ids=config.listener_config.owned_answer_post_root_ids,
        owned_answer_post_message_ids=config.listener_config.owned_answer_post_message_ids,
    )
    async with MattermostClient(
        config.url,
        config.token,
        request_timeout_seconds=config.request_timeout_seconds,
    ) as client:
        await client.get_me()
        if config.mutation_gateway_factory is not None:
            bridge = AuthoritativePlatformGatewayBridge.from_factory_spec(
                config.mutation_gateway_factory
            )
            handler_config = replace(
                handler_config,
                mutation_adapter=MattermostMutationAdapter(client, bridge),
            )
        listener = MattermostEventListener(client, config.listener_config)
        if ready_event is not None:
            ready_event.set()
        async for event in listener.normalized_events():
            with get_session_with_current_tenant() as db_session:
                await handle_normalized_mattermost_event(
                    event=event,
                    config=handler_config,
                    client=client,
                    db_session=db_session,
                )


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
