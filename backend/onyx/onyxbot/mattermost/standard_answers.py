"""Mattermost-native standard answer workflow."""

from __future__ import annotations

import re
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.db.mattermost_bot import (
    MattermostClaimOutcome,
    MattermostEventClaim,
    claim_durable_mattermost_event,
    complete_mattermost_control_event,
)
from onyx.db.models import StandardAnswer, StandardAnswerCategory
from onyx.onyxbot.mattermost.models import MattermostPost, NormalizedMattermostEvent

MATTERMOST_STANDARD_ANSWER_UNAUTHORIZED_MESSAGE = (
    "This Mattermost standard answer is no longer authorized. Ask Onyx again if needed."
)
MATTERMOST_STANDARD_ANSWER_CONFIG_KEY = "standard_answer_category_ids"


@dataclass(frozen=True)
class MattermostStandardAnswer:
    id: int
    answer: str
    match: str


class MattermostStandardAnswerClient(Protocol):
    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool: ...

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str = "",
        pending_post_id: str | None = None,
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...

    async def create_ephemeral_post(
        self,
        *,
        user_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        props: dict[str, object] | None = None,
    ) -> MattermostPost: ...


StandardAnswerMatcher = Callable[..., list[MattermostStandardAnswer]]
StandardAnswerClaimer = Callable[..., MattermostEventClaim]
StandardAnswerCompleter = Callable[..., bool]


async def handle_mattermost_standard_answer_event(
    *,
    event: NormalizedMattermostEvent,
    bot_user_id: str | None,
    client: MattermostStandardAnswerClient,
    db_session: Session | object,
    channel_config: dict[str, object] | None,
    instance_id: str = "mattermost",
    find_matches: StandardAnswerMatcher | None = None,
    claim_event: StandardAnswerClaimer | None = None,
    complete_event: StandardAnswerCompleter | None = None,
) -> bool:
    """Post a configured Mattermost standard answer and consume the event once."""

    if bot_user_id is None:
        return False
    category_ids = _category_ids_from_config(channel_config)
    if not category_ids:
        return False
    if not await _authorized(client=client, event=event, bot_user_id=bot_user_id):
        await client.create_ephemeral_post(
            user_id=event.user_id,
            channel_id=event.channel_id,
            root_id=event.root_post_id,
            message=MATTERMOST_STANDARD_ANSWER_UNAUTHORIZED_MESSAGE,
        )
        return True

    matcher = find_matches or find_matching_mattermost_standard_answers
    matches = matcher(
        category_ids=category_ids,
        query=event.text,
        db_session=db_session,
    )
    if not matches:
        return False

    claimer = claim_event or _claim_standard_answer_event
    claim = claimer(
        db_session=db_session,
        instance_id=instance_id,
        event=event,
    )
    if claim.outcome is not MattermostClaimOutcome.PROCESS or claim.claim_owner is None:
        return True

    message = _render_standard_answers(matches)
    await client.create_post(
        channel_id=event.channel_id,
        root_id=event.root_post_id,
        message=message,
        pending_post_id=claim.event.mattermost_pending_post_id,
        props={
            "onyx_event_key": str(claim.event.id),
            "onyx_standard_answer_ids": [match.id for match in matches],
            "onyx_standard_answer_matches": [match.match for match in matches],
        },
    )
    completer = complete_event or _complete_standard_answer_event
    return completer(
        db_session=db_session,
        event_id=claim.event.id,
        claim_owner=claim.claim_owner,
    )


def find_matching_mattermost_standard_answers(
    *,
    category_ids: Sequence[int],
    query: str,
    db_session: Session | object,
) -> list[MattermostStandardAnswer]:
    if not isinstance(db_session, Session):
        return []
    answers = db_session.scalars(
        select(StandardAnswer)
        .join(StandardAnswer.categories)
        .where(StandardAnswer.active.is_(True))
        .where(StandardAnswerCategory.id.in_(category_ids))
    ).unique()
    return [
        MattermostStandardAnswer(id=answer.id, answer=answer.answer, match=match)
        for answer in answers
        if (match := _standard_answer_match(answer, query)) is not None
    ]


def _standard_answer_match(answer: StandardAnswer, query: str) -> str | None:
    if answer.match_regex:
        maybe_match = re.search(answer.keyword, query, re.IGNORECASE)
        return maybe_match.group(0) if maybe_match is not None else None

    keyword_words = set(_words(answer.keyword))
    query_words = _words(query)
    if answer.match_any_keywords:
        return next((word for word in query_words if word in keyword_words), None)
    if keyword_words.issubset(set(query_words)):
        return re.sub(r"\s+?", ", ", answer.keyword)
    return None


def _words(text: str) -> list[str]:
    return "".join(
        char.lower() for char in text if char not in string.punctuation
    ).split()


def _category_ids_from_config(channel_config: dict[str, object] | None) -> list[int]:
    if channel_config is None:
        return []
    raw_ids = channel_config.get(MATTERMOST_STANDARD_ANSWER_CONFIG_KEY)
    if not isinstance(raw_ids, list):
        return []
    return [item for item in raw_ids if isinstance(item, int)]


async def _authorized(
    *,
    client: MattermostStandardAnswerClient,
    event: NormalizedMattermostEvent,
    bot_user_id: str,
) -> bool:
    try:
        return await client.is_channel_member(
            channel_id=event.channel_id,
            user_id=bot_user_id,
        ) and await client.is_channel_member(
            channel_id=event.channel_id,
            user_id=event.user_id,
        )
    except Exception:
        return False


def _claim_standard_answer_event(
    *,
    db_session: Session | object,
    instance_id: str,
    event: NormalizedMattermostEvent,
) -> MattermostEventClaim:
    if not isinstance(db_session, Session):
        raise TypeError("db_session must be a SQLAlchemy Session")
    return claim_durable_mattermost_event(
        db_session,
        instance_id=instance_id,
        channel_id=event.channel_id,
        dedupe_key=f"standard_answer:{event.dedupe_key}",
        event_type="standard_answer",
        mapping_id=None,
        source_post_id=event.post_id,
        root_post_id=event.root_post_id,
        source_user_id=event.user_id,
        source_username=event.source_username,
        source_display_name=event.source_display_name,
        source_create_at=event.source_create_at,
        source_update_at=event.source_update_at,
        source_delete_at=event.source_delete_at,
    )


def _complete_standard_answer_event(
    *,
    db_session: Session | object,
    event_id: int,
    claim_owner: Any,
) -> bool:
    if not isinstance(db_session, Session):
        return False
    return complete_mattermost_control_event(
        db_session,
        event_id=event_id,
        claim_owner=claim_owner,
    )


def _render_standard_answers(matches: list[MattermostStandardAnswer]) -> str:
    return "\n\n".join(match.answer for match in matches)
