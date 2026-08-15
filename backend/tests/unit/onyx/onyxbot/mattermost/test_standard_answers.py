from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from onyx.db.mattermost_bot import MattermostClaimOutcome, MattermostEventClaim
from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostPost,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.standard_answers import (
    MATTERMOST_STANDARD_ANSWER_UNAUTHORIZED_MESSAGE,
    MattermostStandardAnswer,
    handle_mattermost_standard_answer_event,
)


class StandardAnswerClient:
    def __init__(self, *, memberships: list[bool] | None = None) -> None:
        self.memberships = memberships or [True, True]
        self.membership_calls: list[tuple[str, str]] = []
        self.posts: list[dict[str, Any]] = []
        self.ephemeral_posts: list[dict[str, Any]] = []

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_calls.append((channel_id, user_id))
        return self.memberships.pop(0)

    async def create_post(self, **kwargs: Any) -> MattermostPost:
        self.posts.append(kwargs)
        return MattermostPost(id=f"standard-answer-{len(self.posts)}")

    async def create_ephemeral_post(self, **kwargs: Any) -> MattermostPost:
        self.ephemeral_posts.append(kwargs)
        return MattermostPost(id=f"ephemeral-{len(self.ephemeral_posts)}")


@pytest.mark.asyncio
async def test_success_posts_matching_standard_answer_after_membership_recheck() -> (
    None
):
    client = StandardAnswerClient()
    claim_owner = uuid4()
    claimed_event = SimpleNamespace(
        id=99,
        mattermost_pending_post_id="pending-standard-answer",
    )
    completed: list[tuple[int, object]] = []

    handled = await handle_mattermost_standard_answer_event(
        event=_event(text="Where is the incident runbook?"),
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        channel_config={"standard_answer_category_ids": [7]},
        find_matches=lambda **_kwargs: [
            MattermostStandardAnswer(
                id=3,
                answer="Use the incident runbook in Seafile.",
                match="incident runbook",
            )
        ],
        claim_event=lambda **_kwargs: MattermostEventClaim(
            MattermostClaimOutcome.PROCESS,
            cast(MattermostEventState, claimed_event),
            claim_owner,
        ),
        complete_event=lambda **kwargs: (
            completed.append((kwargs["event_id"], kwargs["claim_owner"])) or True
        ),
    )

    assert handled is True
    assert client.membership_calls == [("channel-1", "bot-1"), ("channel-1", "user-1")]
    assert client.posts == [
        {
            "channel_id": "channel-1",
            "root_id": "root-1",
            "message": "Use the incident runbook in Seafile.",
            "pending_post_id": "pending-standard-answer",
            "props": {
                "onyx_event_key": "99",
                "onyx_standard_answer_ids": [3],
                "onyx_standard_answer_matches": ["incident runbook"],
            },
        }
    ]
    assert completed == [(99, claim_owner)]


@pytest.mark.asyncio
async def test_authorization_denial_fails_closed_before_standard_answer_lookup() -> (
    None
):
    client = StandardAnswerClient(memberships=[True, False])
    lookup_calls: list[object] = []

    handled = await handle_mattermost_standard_answer_event(
        event=_event(text="Where is the incident runbook?"),
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        channel_config={"standard_answer_category_ids": [7]},
        find_matches=lambda **kwargs: lookup_calls.append(kwargs) or [],
    )

    assert handled is True
    assert lookup_calls == []
    assert client.posts == []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_STANDARD_ANSWER_UNAUTHORIZED_MESSAGE
    ]


@pytest.mark.asyncio
async def test_replay_does_not_duplicate_standard_answer_post() -> None:
    client = StandardAnswerClient()

    handled = await handle_mattermost_standard_answer_event(
        event=_event(text="Where is the incident runbook?"),
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        channel_config={"standard_answer_category_ids": [7]},
        find_matches=lambda **_kwargs: [
            MattermostStandardAnswer(
                id=3,
                answer="Use the incident runbook in Seafile.",
                match="incident runbook",
            )
        ],
        claim_event=lambda **_kwargs: MattermostEventClaim(
            MattermostClaimOutcome.COMPLETED,
            cast(
                MattermostEventState,
                SimpleNamespace(id=99, mattermost_pending_post_id="pending"),
            ),
            None,
        ),
    )

    assert handled is True
    assert client.posts == []
    assert client.ephemeral_posts == []


@pytest.mark.asyncio
async def test_primary_failure_mode_membership_lookup_error_fails_closed() -> None:
    class FailingMembershipClient(StandardAnswerClient):
        async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
            self.membership_calls.append((channel_id, user_id))
            raise RuntimeError("mattermost unavailable")

    client = FailingMembershipClient()
    claim_calls: list[object] = []

    def claim_event(**kwargs: object) -> MattermostEventClaim:
        claim_calls.append(kwargs)
        raise AssertionError("claim must not run after authorization failure")

    handled = await handle_mattermost_standard_answer_event(
        event=_event(text="Where is the incident runbook?"),
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        channel_config={"standard_answer_category_ids": [7]},
        find_matches=lambda **_kwargs: [
            MattermostStandardAnswer(
                id=3,
                answer="Use the incident runbook in Seafile.",
                match="incident runbook",
            )
        ],
        claim_event=claim_event,
    )

    assert handled is True
    assert claim_calls == []
    assert client.posts == []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_STANDARD_ANSWER_UNAUTHORIZED_MESSAGE
    ]


def _event(*, text: str) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="root-1",
        root_post_id="root-1",
        user_id="user-1",
        text=text,
        raw_event_type="posted",
        dedupe_key="event_id:root-1",
    )
