"""Mattermost feedback reminder task helpers."""

from __future__ import annotations

from typing import Any, cast

from celery import shared_task

from onyx.configs.onyxbot_configs import ONYX_BOT_FEEDBACK_REMINDER

MATTERMOST_FEEDBACK_REMINDER_EXPIRES_SECONDS = 60 * 60
MATTERMOST_FEEDBACK_REMINDER_MESSAGE = (
    "Please rate the Mattermost answer with Helpful or Not helpful, "
    "or mark it as needing follow-up or resolved."
)


def schedule_mattermost_feedback_reminder(
    *,
    instance_id: str,
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
    event_id: int,
) -> str | None:
    if not ONYX_BOT_FEEDBACK_REMINDER:
        return None
    task_id = f"mattermost-feedback-reminder:{event_id}"
    task = cast(Any, mattermost_feedback_reminder)
    task.apply_async(
        kwargs={
            "instance_id": instance_id,
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
    channel_id: str,
    root_post_id: str,
    answer_post_id: str,
    user_id: str,
) -> dict[str, str]:
    """Return a durable reminder payload for the Mattermost runtime dispatcher.

    The active Mattermost client is process-local to the bot runtime, so the Celery
    side deliberately does not hold Mattermost credentials or post directly. The
    typed payload is idempotent and expires at enqueue time.
    """

    return {
        "instance_id": instance_id,
        "channel_id": channel_id,
        "root_post_id": root_post_id,
        "answer_post_id": answer_post_id,
        "user_id": user_id,
        "message": MATTERMOST_FEEDBACK_REMINDER_MESSAGE,
    }
