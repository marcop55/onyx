"""Private local service entrypoint for the Mattermost bot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.responses import JSONResponse

from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.mattermost_bot import hydrate_mattermost_listener_config
from onyx.onyxbot.mattermost.client import MattermostClient
from onyx.onyxbot.mattermost.config import (
    MattermostBotConfig,
    load_mattermost_bot_config_from_env,
    redacted_mattermost_bot_env,
)
from onyx.onyxbot.mattermost.handler import (
    MattermostHandlerConfig,
    handle_normalized_mattermost_event,
)
from onyx.onyxbot.mattermost.listener import MattermostEventListener
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
        if listener_task in done and not listener_ready.is_set():
            readiness_task.cancel()
            await listener_task
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

    return app


async def _run_bot(
    config: MattermostBotConfig,
    ready_event: asyncio.Event | None = None,
) -> None:
    with get_session_with_current_tenant() as db_session:
        hydrate_mattermost_listener_config(db_session, config.listener_config)

    handler_config = MattermostHandlerConfig(
        persona_id=config.persona_id,
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
    config = load_mattermost_bot_config_from_env()
    logger.info("Starting Mattermost bot service", extra=redacted_mattermost_bot_env())
    uvicorn.run(get_application(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
