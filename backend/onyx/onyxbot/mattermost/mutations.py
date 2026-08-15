"""Fail-closed Mattermost authorization for controlled Seafile mutations.

This adapter boundary deliberately exposes no Seafile transport. It performs a
fresh Mattermost identity lookup for each mutation and can route an authorized
request only to the platform's controlled mutation gateway. Shared-corpus reads
and temporary attachment use remain on the existing chat/retrieval path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from onyx.onyxbot.mattermost.models import MattermostUserInfo, NormalizedMattermostEvent

MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE = (
    "Current Mattermost system_admin permission is required for this mutation."
)


class MattermostMutationPermissionError(PermissionError):
    """A mutation was rejected before controlled gateway execution."""


class SeafileActionType(str, Enum):
    """Actions in the merged controlled-gateway request contract."""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    MOVE = "move"
    DELETE = "delete"


class SeafileActionOrigin(str, Enum):
    """Trusted origin classifications in the controlled-gateway contract."""

    CHAT_COMMAND = "chat_command"
    TOOL_CALL = "tool_call"
    DOCUMENT_TEXT = "document_text"
    ATTACHMENT_PROMOTION = "attachment_promotion"


@dataclass(frozen=True)
class SeafileActionRequest:
    """Exact adapter-side shape accepted by the platform mutation gateway."""

    action: SeafileActionType
    repo_id: str
    path: str
    requesting_user: str
    origin: SeafileActionOrigin
    expected_revision: str | None = None
    content: str | None = None
    destination_path: str | None = None
    confirmed: bool = False
    scope_prefix: str = "/automation"


@dataclass(frozen=True)
class MattermostMutationContext:
    """Trusted event correlation plus explicitly untrusted role claims."""

    requester_id: str
    channel_id: str
    post_id: str
    root_post_id: str
    claimed_username: str | None = None
    claimed_roles: str | None = None


class MattermostIdentityLookup(Protocol):
    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...


class ControlledSeafileMutationGateway(Protocol):
    def mutate(
        self,
        context: MattermostMutationContext,
        request: SeafileActionRequest,
    ) -> Any: ...


class MattermostMutationAdapter:
    """Resolve current authority and route mutations only to the controlled gateway."""

    def __init__(
        self,
        mattermost: MattermostIdentityLookup,
        gateway: ControlledSeafileMutationGateway,
    ) -> None:
        self._mattermost = mattermost
        self._gateway = gateway

    async def route(
        self,
        event: NormalizedMattermostEvent,
        request: SeafileActionRequest,
    ) -> Any:
        """Authorize one mutation from fresh server state and route it once."""

        self._validate_pre_authorization_contract(request)
        identity = await self._get_current_identity(event.user_id)
        if identity.id != event.user_id or "system_admin" not in identity.roles.split():
            raise MattermostMutationPermissionError(
                MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE
            )

        context = MattermostMutationContext(
            requester_id=event.user_id,
            channel_id=event.channel_id,
            post_id=event.post_id,
            root_post_id=event.root_post_id,
            claimed_username=identity.username,
            claimed_roles=identity.roles,
        )
        trusted_request = replace(request, requesting_user=identity.username)
        return self._gateway.mutate(context, trusted_request)

    async def _get_current_identity(self, user_id: str) -> MattermostUserInfo:
        try:
            identity = await self._mattermost.get_user_info(user_id)
        except Exception:
            raise MattermostMutationPermissionError(
                MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE
            ) from None
        if (
            not isinstance(identity, MattermostUserInfo)
            or type(identity.id) is not str
            or not identity.id
            or type(identity.username) is not str
            or not identity.username
            or type(identity.roles) is not str
        ):
            raise MattermostMutationPermissionError(
                MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE
            ) from None
        return identity

    @staticmethod
    def _validate_pre_authorization_contract(request: SeafileActionRequest) -> None:
        if (
            type(request) is not SeafileActionRequest
            or type(request.action) is not SeafileActionType
        ):
            raise MattermostMutationPermissionError(
                "A typed mutation action is required before gateway routing."
            )
        if (
            type(request.origin) is not SeafileActionOrigin
            or type(request.confirmed) is not bool
        ):
            raise MattermostMutationPermissionError(
                "A typed mutation origin and boolean confirmation are required."
            )
        if request.action is SeafileActionType.READ:
            raise MattermostMutationPermissionError(
                "The controlled gateway accepts mutation actions only; reads stay on the shared retrieval path."
            )
        if request.origin is SeafileActionOrigin.DOCUMENT_TEXT:
            raise MattermostMutationPermissionError(
                "Retrieved document text cannot authorize a mutation."
            )
        if request.action in {
            SeafileActionType.UPDATE,
            SeafileActionType.MOVE,
            SeafileActionType.DELETE,
        }:
            if request.confirmed is not True:
                raise MattermostMutationPermissionError(
                    f"{request.action.value} requires explicit confirmation."
                )
            if not request.expected_revision:
                raise MattermostMutationPermissionError(
                    f"{request.action.value} requires the current expected revision."
                )
        if (
            request.origin is SeafileActionOrigin.ATTACHMENT_PROMOTION
            and request.confirmed is not True
        ):
            raise MattermostMutationPermissionError(
                "Attachment promotion requires explicit confirmation."
            )
