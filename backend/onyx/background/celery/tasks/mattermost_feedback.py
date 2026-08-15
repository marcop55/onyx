"""Mattermost feedback reminder task helpers."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

from celery import shared_task

from onyx.configs.onyxbot_configs import ONYX_BOT_FEEDBACK_REMINDER
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.mattermost_bot import (
    MattermostClaimOutcome,
    checkpoint_mattermost_post,
    checkpoint_mattermost_post_attempt,
    claim_durable_mattermost_event,
    complete_mattermost_control_event,
    fetch_mattermost_bot_by_instance_and_user,
    fetch_mattermost_channel_config_for_bot_and_channel,
)
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.client import MattermostClient, MattermostClientError
from onyx.onyxbot.mattermost.models import MattermostPost

MATTERMOST_FEEDBACK_REMINDER_EXPIRES_SECONDS = 60 * 60
MATTERMOST_FEEDBACK_REMINDER_MESSAGE = (
    "Please rate the Mattermost answer with Helpful or Not helpful, "
    "or mark it as needing follow-up or resolved."
)


class MattermostFeedbackReminderClient(Protocol):
    async def __aenter__(self) -> "MattermostFeedbackReminderClient": ...

    async def __aexit__(self, *_args: object) -> None: ...

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]: ...

    async def find_post_by_idempotency_fields(
        self,
        *,
        channel_id: str,
        pending_post_id: str,
        event_key: str,
    ) -> MattermostPost | None: ...

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...


def schedule_mattermost_feedback_reminder(
    *,
    instance_id: str,
    bot_user_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
    event_id: int,
) -> str | None:
    if not ONYX_BOT_FEEDBACK_REMINDER:
        return None
    if not bot_user_id:
        return None
    task_id = f"mattermost-feedback-reminder:{event_id}"
    task = cast(Any, mattermost_feedback_reminder)
    task.apply_async(
        kwargs={
            "instance_id": instance_id,
            "bot_user_id": bot_user_id,
            "channel_id": channel_id,
            "root_post_id": root_post_id,
            "answer_post_id": answer_post_id,
            "user_id": user_id,
        },
        countdown=ONYX_BOT_FEEDBACK_REMINDER * 60,
        expires=MATTERMOST_FEEDBACK_REMINDER_EXPIRES_SECONDS,
        task_id=task_id,
    )
    return task_id


@shared_task(name="mattermost_feedback_reminder")
def mattermost_feedback_reminder(
    *,
    instance_id: str,
    bot_user_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
) -> dict[str, str]:
    """Deliver one replay-safe visible reminder into the original thread."""

    with get_session_with_current_tenant() as db_session:
        bot = fetch_mattermost_bot_by_instance_and_user(
            db_session,
            instance_id=instance_id,
            bot_user_id=bot_user_id,
        )
        if bot is None or bot.token is None:
            return {"status": "skipped"}
        channel_config = fetch_mattermost_channel_config_for_bot_and_channel(
            db_session,
            instance_id=instance_id,
            bot_user_id=bot_user_id,
            channel_id=channel_id,
        )
        if channel_config is None or not channel_config.enabled:
            return {"status": "skipped"}
        if bool(channel_config.channel_config.get("disabled", False)):
            return {"status": "skipped"}
        token = bot.token.get_value(apply_mask=False)
        if not token:
            return {"status": "skipped"}
        client = MattermostClient(bot.url, token)
        return asyncio.run(
            _deliver_mattermost_feedback_reminder(
                db_session=db_session,
                client=cast(MattermostFeedbackReminderClient, client),
                instance_id=instance_id,
                bot_user_id=bot_user_id,
                channel_id=channel_id,
                root_post_id=root_post_id,
                answer_post_id=answer_post_id,
                user_id=user_id,
            )
        )


async def _deliver_mattermost_feedback_reminder(
    *,
    db_session: Any,
    client: MattermostFeedbackReminderClient,
    instance_id: str,
    bot_user_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
) -> dict[str, str]:
    async with client:
        if not await _is_authorized_reminder_context(
            client=client,
            bot_user_id=bot_user_id,
            channel_id=channel_id,
            root_post_id=root_post_id,
            answer_post_id=answer_post_id,
            user_id=user_id,
        ):
            return {"status": "skipped"}

        claim = claim_durable_mattermost_event(
            db_session,
            instance_id=instance_id,
            channel_id=channel_id,
            dedupe_key=f"feedback_reminder:{answer_post_id}:{user_id}",
            event_type="feedback_reminder",
            mapping_id=None,
            source_post_id=answer_post_id,
            root_post_id=root_post_id,
            source_user_id=user_id,
        )
        if claim.outcome is MattermostClaimOutcome.COMPLETED:
            post_id = claim.event.mattermost_post_id
            return {"status": "replayed", **({"post_id": post_id} if post_id else {})}
        if claim.outcome is MattermostClaimOutcome.BUSY or claim.claim_owner is None:
            return {"status": "busy"}

        post = await _find_reconciled_reminder_post(
            client=client,
            channel_id=channel_id,
            event=claim.event,
        )
        if post is None and claim.event.state == "post_create_attempted":
            return {"status": "ambiguous"}
        if post is None:
            if not checkpoint_mattermost_post_attempt(
                db_session,
                event_id=claim.event.id,
                claim_owner=claim.claim_owner,
            ):
                return {"status": "busy"}
            claim.event.state = "post_create_attempted"
            try:
                post = await client.create_post(
                    channel_id=channel_id,
                    root_id=root_post_id,
                    message=MATTERMOST_FEEDBACK_REMINDER_MESSAGE,
                    pending_post_id=claim.event.mattermost_pending_post_id,
                    props=_feedback_reminder_props(
                        event_id=claim.event.id,
                        answer_post_id=answer_post_id,
                        user_id=user_id,
                    ),
                )
            except MattermostClientError:
                post = await _find_reconciled_reminder_post(
                    client=client,
                    channel_id=channel_id,
                    event=claim.event,
                )
                if post is None:
                    return {"status": "ambiguous"}
                status = "ambiguous"
            else:
                status = "delivered"
        else:
            status = "delivered"

        if not checkpoint_mattermost_post(
            db_session,
            event_id=claim.event.id,
            claim_owner=claim.claim_owner,
            post_id=post.id,
        ):
            return {"status": "ambiguous", "post_id": post.id}
        if not complete_mattermost_control_event(
            db_session,
            event_id=claim.event.id,
            claim_owner=claim.claim_owner,
        ):
            return {"status": "ambiguous", "post_id": post.id}
        return {"status": status, "post_id": post.id}


async def _is_authorized_reminder_context(
    *,
    client: MattermostFeedbackReminderClient,
    bot_user_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
) -> bool:
    try:
        if not await client.is_channel_member(
            channel_id=channel_id, user_id=bot_user_id
        ):
            return False
        if not await client.is_channel_member(channel_id=channel_id, user_id=user_id):
            return False
        thread_posts = await client.get_thread_posts(root_post_id)
    except MattermostClientError:
        return False
    root_seen = any(
        post.id == root_post_id and post.channel_id == channel_id
        for post in thread_posts
    )
    answer_seen = any(
        post.id == answer_post_id
        and post.channel_id == channel_id
        and post.root_id == root_post_id
        for post in thread_posts
    )
    return root_seen and answer_seen


async def _find_reconciled_reminder_post(
    *,
    client: MattermostFeedbackReminderClient,
    channel_id: str,
    event: MattermostEventState,
) -> MattermostPost | None:
    try:
        return await client.find_post_by_idempotency_fields(
            channel_id=channel_id,
            pending_post_id=event.mattermost_pending_post_id,
            event_key=str(event.id),
        )
    except MattermostClientError:
        return None


def _feedback_reminder_props(
    *,
    event_id: int,
    answer_post_id: str,
    user_id: str,
) -> dict[str, object]:
    return {
        "onyx_event_key": str(event_id),
        "onyx_mattermost_feedback_reminder": True,
        "onyx_mattermost_answer_post_id": answer_post_id,
        "onyx_mattermost_recipient_user_id": user_id,
    }
