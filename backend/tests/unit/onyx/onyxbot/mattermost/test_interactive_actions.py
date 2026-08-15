from __future__ import annotations

from typing import Any, cast

import pytest

from onyx.configs.constants import QAFeedbackType
from onyx.onyxbot.mattermost.interactive import (
    MATTERMOST_INTERACTIVE_REPLAY_MESSAGE,
    MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE,
    MattermostInteractiveAction,
    MattermostInteractiveActionResult,
    build_mattermost_answer_action_props,
    handle_mattermost_interactive_action,
    parse_mattermost_interactive_payload,
)
from onyx.onyxbot.mattermost.models import MattermostUserInfo


class InteractiveClient:
    def __init__(
        self,
        *,
        memberships: list[bool] | None = None,
        identity: MattermostUserInfo | Exception | None = None,
    ) -> None:
        self.memberships = memberships or [True, True]
        self.identity = identity or MattermostUserInfo(
            id="user-1",
            username="ada",
            display_name="Ada",
            roles="system_user",
        )
        self.membership_calls: list[tuple[str, str]] = []
        self.identity_calls: list[str] = []
        self.ephemeral_posts: list[dict[str, Any]] = []

    async def is_channel_member(self, *, channel_id: str, user_id: str) -> bool:
        self.membership_calls.append((channel_id, user_id))
        return self.memberships.pop(0)

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        self.identity_calls.append(user_id)
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    async def create_ephemeral_post(self, **kwargs: Any) -> object:
        self.ephemeral_posts.append(kwargs)
        return object()


def _payload(value: str, *, user_id: str = "user-1") -> dict[str, object]:
    return {
        "user_id": user_id,
        "channel_id": "channel-1",
        "post_id": "answer-post-1",
        "context": {"action_value": value},
    }


def _mattermost_action_payload(
    action_payload: dict[str, object], *, user_id: str = "user-1"
) -> dict[str, object]:
    integration = cast(dict[str, object], action_payload["integration"])
    context = cast(dict[str, object], integration["context"])
    assert context["action_value"]
    return {
        "user_id": user_id,
        "channel_id": "channel-1",
        "post_id": "answer-post-1",
        "context": context,
    }


def _action_value(
    action: MattermostInteractiveAction,
    *,
    user_id: str = "user-1",
    proposal_identity: str | None = None,
) -> str:
    props = build_mattermost_answer_action_props(
        signing_secret="secret",
        interactive_url="http://127.0.0.1:8091/interactive",
        channel_id="channel-1",
        root_post_id="root-1",
        answer_post_id="answer-post-1",
        answer_message_id=42,
        requester_user_id=user_id,
        sources=("[1] handbook - https://example.test/handbook",),
        mutation_command='@onyx-mutate {"confirmed":true}',
        mutation_proposal_identity=proposal_identity,
    )
    attachments = cast(list[dict[str, object]], props["attachments"])
    actions = cast(list[dict[str, object]], attachments[0]["actions"])
    for action_payload in actions:
        if action_payload["id"] == action.value:
            integration = cast(dict[str, object], action_payload["integration"])
            context = cast(dict[str, object], integration["context"])
            return cast(str, context["action_value"])
    raise AssertionError(f"missing {action}")


def test_signed_control_values_are_identity_bound_and_tamper_evident() -> None:
    value = _action_value(MattermostInteractiveAction.VIEW_SOURCES)

    parsed = parse_mattermost_interactive_payload(
        _payload(value),
        signing_secret="secret",
    )

    assert parsed.action is MattermostInteractiveAction.VIEW_SOURCES
    assert parsed.user_id == "user-1"
    assert parsed.channel_id == "channel-1"
    assert parsed.answer_message_id == 42

    tampered_value = value[:-1] + ("A" if value[-1] != "A" else "B")
    with pytest.raises(ValueError, match="signature"):
        parse_mattermost_interactive_payload(
            _payload(tampered_value),
            signing_secret="secret",
        )
    with pytest.raises(ValueError, match="identity"):
        parse_mattermost_interactive_payload(
            _payload(value, user_id="user-2"),
            signing_secret="secret",
        )


def test_confirm_mutation_control_binds_persisted_proposal_identity() -> None:
    value = _action_value(
        MattermostInteractiveAction.CONFIRM_MUTATION,
        proposal_identity="c" * 64,
    )

    parsed = parse_mattermost_interactive_payload(
        _payload(value),
        signing_secret="secret",
    )

    assert parsed.mutation_proposal_identity == "c" * 64
    assert parsed.dedupe_key == (
        "interactive:confirm_mutation:answer-post-1:user-1:" + "c" * 64
    )


def test_answer_action_buttons_are_wired_to_interactive_endpoint() -> None:
    props = build_mattermost_answer_action_props(
        signing_secret="secret",
        interactive_url="http://127.0.0.1:8091/interactive",
        channel_id="channel-1",
        root_post_id="root-1",
        answer_post_id="answer-post-1",
        answer_message_id=42,
        requester_user_id="user-1",
    )
    attachments = cast(list[dict[str, object]], props["attachments"])
    actions = cast(list[dict[str, object]], attachments[0]["actions"])

    assert actions != []
    for action in actions:
        assert "context" not in action
        integration = cast(dict[str, object], action["integration"])
        assert integration["url"] == "http://127.0.0.1:8091/interactive"
        context = cast(dict[str, object], integration["context"])
        assert context["action_value"]
        parsed = parse_mattermost_interactive_payload(
            _mattermost_action_payload(action),
            signing_secret="secret",
        )
        assert parsed.action.value == action["id"]
        assert parsed.user_id == "user-1"
        assert parsed.channel_id == "channel-1"
        assert parsed.answer_post_id == "answer-post-1"


@pytest.mark.asyncio
async def test_feedback_action_rechecks_membership_and_completes_once() -> None:
    value = _action_value(MattermostInteractiveAction.LIKE)
    client = InteractiveClient()
    calls: list[tuple[str, int, bool | None, bool | None]] = []

    def complete_feedback(
        _db_session: object,
        *,
        dedupe_key: str,
        chat_message_id: int,
        feedback_action: QAFeedbackType | None = None,
        required_followup: bool | None = None,
        feedback_text: str,
    ) -> bool:
        _ = feedback_text
        calls.append(
            (
                dedupe_key,
                chat_message_id,
                feedback_action == QAFeedbackType.LIKE,
                required_followup,
            )
        )
        return True

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        complete_feedback=complete_feedback,
    )

    assert result is MattermostInteractiveActionResult.COMPLETED
    assert client.membership_calls == [("channel-1", "bot-1"), ("channel-1", "user-1")]
    assert calls == [("interactive:like:answer-post-1:user-1", 42, True, None)]
    assert [post["message"] for post in client.ephemeral_posts] == [
        "Thanks for your feedback."
    ]


@pytest.mark.asyncio
async def test_authorization_denial_fails_closed_before_side_effect() -> None:
    value = _action_value(MattermostInteractiveAction.DISLIKE)
    client = InteractiveClient(memberships=[True, False])
    calls: list[object] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        complete_feedback=lambda *_args, **kwargs: calls.append(kwargs) or True,
    )

    assert result is MattermostInteractiveActionResult.UNAUTHORIZED
    assert calls == []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE
    ]


@pytest.mark.asyncio
async def test_replay_does_not_duplicate_feedback_or_visible_confirmation() -> None:
    value = _action_value(MattermostInteractiveAction.NEED_FOLLOWUP)
    client = InteractiveClient()
    calls: list[object] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        complete_feedback=lambda *_args, **kwargs: calls.append(kwargs) or False,
    )

    assert result is MattermostInteractiveActionResult.REPLAYED
    assert calls != []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_INTERACTIVE_REPLAY_MESSAGE
    ]


@pytest.mark.asyncio
async def test_primary_failure_lookup_failure_denies_before_admin_confirmation() -> (
    None
):
    value = _action_value(MattermostInteractiveAction.CONFIRM_MUTATION)
    client = InteractiveClient(identity=RuntimeError("mattermost unavailable"))
    mutation_calls: list[object] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        dispatch_mutation=lambda **kwargs: mutation_calls.append(kwargs) or True,
    )

    assert result is MattermostInteractiveActionResult.UNAUTHORIZED
    assert mutation_calls == []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_INTERACTIVE_UNAUTHORIZED_MESSAGE
    ]


@pytest.mark.asyncio
async def test_confirm_mutation_requires_current_system_admin_then_dispatches_command() -> (
    None
):
    value = _action_value(MattermostInteractiveAction.CONFIRM_MUTATION)
    client = InteractiveClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    mutation_calls: list[dict[str, object]] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        dispatch_mutation=lambda **kwargs: mutation_calls.append(kwargs) or True,
    )

    assert result is MattermostInteractiveActionResult.COMPLETED
    assert len(mutation_calls) == 1
    event = cast(Any, mutation_calls[0]["event"])
    assert event.text == '@onyx-mutate {"confirmed":true}'
    assert event.user_id == "user-1"


@pytest.mark.asyncio
async def test_confirm_mutation_replay_does_not_duplicate_gateway_dispatch() -> None:
    value = _action_value(MattermostInteractiveAction.CONFIRM_MUTATION)
    client = InteractiveClient(
        memberships=[True, True, True, True],
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        ),
    )
    claims = iter([(1, "owner-1"), None])
    completed_claims: list[object] = []
    mutation_calls: list[dict[str, object]] = []

    def claim_mutation(*_args: object, **_kwargs: object) -> tuple[int, object] | None:
        return next(claims)

    def complete_mutation(
        _db_session: object,
        *,
        event_id: int,
        claim_owner: object,
    ) -> bool:
        completed_claims.append((event_id, claim_owner))
        return True

    for expected in (
        MattermostInteractiveActionResult.COMPLETED,
        MattermostInteractiveActionResult.REPLAYED,
    ):
        result = await handle_mattermost_interactive_action(
            payload=_payload(value),
            signing_secret="secret",
            bot_user_id="bot-1",
            client=client,
            db_session=object(),
            dispatch_mutation=lambda **kwargs: mutation_calls.append(kwargs) or True,
            claim_mutation=claim_mutation,
            complete_mutation=complete_mutation,
        )

        assert result is expected

    assert len(mutation_calls) == 1
    assert completed_claims == [(1, "owner-1")]
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_INTERACTIVE_REPLAY_MESSAGE
    ]


@pytest.mark.asyncio
async def test_confirm_attachment_promotion_claims_persisted_proposal_before_dispatch() -> (
    None
):
    value = _action_value(
        MattermostInteractiveAction.CONFIRM_MUTATION,
        proposal_identity="d" * 64,
    )
    client = InteractiveClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    promotion_claims: list[tuple[str, str]] = []
    mutation_calls: list[dict[str, object]] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        dispatch_mutation=lambda **kwargs: mutation_calls.append(kwargs) or True,
        claim_mutation=lambda *_args, **_kwargs: (1, "owner-1"),
        complete_mutation=lambda *_args, **_kwargs: True,
        claim_promotion=lambda _db_session, *, proposal_identity, confirmer_user_id: (
            promotion_claims.append((proposal_identity, confirmer_user_id)) or object()
        ),
    )

    assert result is MattermostInteractiveActionResult.COMPLETED
    assert promotion_claims == [("d" * 64, "user-1")]
    assert len(mutation_calls) == 1


@pytest.mark.asyncio
async def test_confirm_attachment_promotion_dispatches_claimed_proposal_once() -> None:
    value = _action_value(
        MattermostInteractiveAction.CONFIRM_MUTATION,
        proposal_identity="f" * 64,
    )
    client = InteractiveClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    claimed_proposal = object()
    promotion_calls: list[dict[str, object]] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        dispatch_promotion=lambda **kwargs: promotion_calls.append(kwargs) or True,
        claim_mutation=lambda *_args, **_kwargs: (1, "owner-1"),
        complete_mutation=lambda *_args, **_kwargs: True,
        claim_promotion=lambda *_args, **_kwargs: claimed_proposal,
    )

    assert result is MattermostInteractiveActionResult.COMPLETED
    assert len(promotion_calls) == 1
    assert promotion_calls[0]["proposal"] is claimed_proposal
    event = cast(Any, promotion_calls[0]["event"])
    assert event.dedupe_key.endswith("f" * 64)


@pytest.mark.asyncio
async def test_confirm_attachment_promotion_replay_stops_before_dispatch() -> None:
    value = _action_value(
        MattermostInteractiveAction.CONFIRM_MUTATION,
        proposal_identity="e" * 64,
    )
    client = InteractiveClient(
        identity=MattermostUserInfo(
            id="user-1",
            username="admin",
            display_name="Admin",
            roles="system_user system_admin",
        )
    )
    mutation_calls: list[dict[str, object]] = []

    result = await handle_mattermost_interactive_action(
        payload=_payload(value),
        signing_secret="secret",
        bot_user_id="bot-1",
        client=client,
        db_session=object(),
        dispatch_mutation=lambda **kwargs: mutation_calls.append(kwargs) or True,
        claim_promotion=lambda *_args, **_kwargs: None,
    )

    assert result is MattermostInteractiveActionResult.REPLAYED
    assert mutation_calls == []
    assert [post["message"] for post in client.ephemeral_posts] == [
        MATTERMOST_INTERACTIVE_REPLAY_MESSAGE
    ]
