"""Private local service entrypoint for the Mattermost bot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from onyx.db.engine.sql_engine import get_session_with_current_tenant
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
        listener_task = asyncio.create_task(_run_bot(runtime_config))
        app.state.listener_task = listener_task
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
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _run_bot(config: MattermostBotConfig) -> None:
    handler_config = MattermostHandlerConfig(
        persona_id=config.persona_id,
        owned_thread_root_ids=config.listener_config.owned_thread_root_ids,
        owned_answer_post_root_ids=config.listener_config.owned_answer_post_root_ids,
        owned_answer_post_message_ids=config.listener_config.owned_answer_post_message_ids,
    )
    async with MattermostClient(
        config.url,
        config.token,
        request_timeout_seconds=config.request_timeout_seconds,
    ) as client:
        listener = MattermostEventListener(client, config.listener_config)
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
