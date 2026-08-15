from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from onyx.onyxbot.mattermost.handler import dispatch_mattermost_mutation
from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.mutations import (
    MATTERMOST_MUTATION_COMMAND_PREFIX,
    MattermostMutationAdapter,
    MattermostMutationContext,
    SeafileActionRequest,
)


class ControlledGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[MattermostMutationContext, SeafileActionRequest]] = []

    def mutate(
        self,
        context: MattermostMutationContext,
        request: SeafileActionRequest,
    ) -> str:
        self.calls.append((context, request))
        return "verified"


def identity(roles: str) -> MattermostUserInfo:
    return MattermostUserInfo(
        id="mm-user-1",
        username="reiss",
        display_name="Reiss",
        roles=roles,
    )


def event() -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.DIRECT_MESSAGE,
        session_key="mattermost:dm:global:channel-1",
        team_id="global",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="root-1",
        user_id="mm-user-1",
        text="ordinary chat",
    )


class DispatchClient:
    def __init__(self, identities: list[object]) -> None:
        self.identities = identities
        self.identity_calls: list[str] = []
        self.posts: list[dict[str, Any]] = []

    async def get_user_info(self, user_id: str) -> MattermostUserInfo:
        self.identity_calls.append(user_id)
        value = self.identities.pop(0)
        if isinstance(value, Exception):
            raise value
        return cast(MattermostUserInfo, value)

    async def create_post(self, **kwargs: Any) -> object:
        self.posts.append(kwargs)
        return object()


def command(**overrides: object) -> str:
    values: dict[str, object] = {
        "action": "update",
        "repo_id": "repo-1",
        "path": "/automation/note.md",
        "origin": "chat_command",
        "expected_revision": "rev-1",
        "content": "new content",
        "destination_path": None,
        "confirmed": True,
        "scope_prefix": "/automation",
    }
    values.update(overrides)
    import json

    return MATTERMOST_MUTATION_COMMAND_PREFIX + json.dumps(values)


@pytest.mark.asyncio
async def test_authorized_command_routes_once_and_posts_controlled_success() -> None:
    client = DispatchClient([identity("system_user system_admin")])
    gateway = ControlledGateway()
    mutation_event = replace(event(), text=command())

    handled = await dispatch_mattermost_mutation(
        event=mutation_event,
        client=cast(Any, client),
        adapter=MattermostMutationAdapter(client, gateway),
    )

    assert handled is True
    assert client.identity_calls == ["mm-user-1"]
    assert len(gateway.calls) == 1
    assert [post["message"] for post in client.posts] == [
        "Seafile mutation completed through the controlled platform gateway."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_value",
    [identity("system_user"), RuntimeError("private Mattermost API detail")],
)
async def test_non_admin_and_lookup_failure_are_denied_before_gateway(
    identity_value: object,
) -> None:
    client = DispatchClient([identity_value])
    gateway = ControlledGateway()

    handled = await dispatch_mattermost_mutation(
        event=replace(event(), text=command()),
        client=cast(Any, client),
        adapter=MattermostMutationAdapter(client, gateway),
    )

    assert handled is True
    assert gateway.calls == []
    assert [post["message"] for post in client.posts] == [
        "Current Mattermost system_admin permission is required for this mutation."
    ]


@pytest.mark.asyncio
async def test_ordinary_chat_is_not_consumed_or_routed() -> None:
    client = DispatchClient([identity("system_admin")])
    gateway = ControlledGateway()

    handled = await dispatch_mattermost_mutation(
        event=replace(event(), text="summarize the shared corpus"),
        client=cast(Any, client),
        adapter=MattermostMutationAdapter(client, gateway),
    )

    assert handled is False
    assert client.identity_calls == []
    assert gateway.calls == []
    assert client.posts == []


@pytest.mark.asyncio
async def test_disabled_gateway_denies_command_but_does_not_disable_chat() -> None:
    client = DispatchClient([])

    handled = await dispatch_mattermost_mutation(
        event=replace(event(), text=command()),
        client=cast(Any, client),
        adapter=None,
    )

    assert handled is True
    assert client.identity_calls == []
    assert [post["message"] for post in client.posts] == [
        "Seafile mutation gateway is unavailable; mutation denied."
    ]


@pytest.mark.asyncio
async def test_duplicate_wire_fields_are_rejected_before_lookup_or_gateway() -> None:
    client = DispatchClient([identity("system_admin")])
    gateway = ControlledGateway()
    duplicate = (
        MATTERMOST_MUTATION_COMMAND_PREFIX
        + '{"action":"update","action":"delete","repo_id":"repo-1",'
        '"path":"/automation/note.md","origin":"chat_command",'
        '"expected_revision":"rev-1","content":null,"destination_path":null,'
        '"confirmed":true,"scope_prefix":"/automation"}'
    )

    handled = await dispatch_mattermost_mutation(
        event=replace(event(), text=duplicate),
        client=cast(Any, client),
        adapter=MattermostMutationAdapter(client, gateway),
    )

    assert handled is True
    assert client.identity_calls == []
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_revision": True},
        {"confirmed": 1},
        {"repo_id": ["repo-1"]},
        {"path": None},
        {"origin": "document_text"},
        {"unexpected": "field"},
    ],
)
async def test_malformed_command_is_denied_before_lookup_or_gateway(
    overrides: dict[str, object],
) -> None:
    client = DispatchClient([identity("system_admin")])
    gateway = ControlledGateway()

    handled = await dispatch_mattermost_mutation(
        event=replace(event(), text=command(**overrides)),
        client=cast(Any, client),
        adapter=MattermostMutationAdapter(client, gateway),
    )

    assert handled is True
    assert client.identity_calls == []
    assert gateway.calls == []
    assert len(client.posts) == 1
    assert "rejected" in client.posts[0]["message"].lower()
