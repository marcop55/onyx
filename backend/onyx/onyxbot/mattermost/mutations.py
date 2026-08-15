"""Fail-closed Mattermost authorization and authoritative platform bridging."""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from types import ModuleType
from typing import Any, Protocol, cast

from onyx.onyxbot.mattermost.models import MattermostUserInfo, NormalizedMattermostEvent

MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE = (
    "Current Mattermost system_admin permission is required for this mutation."
)
MATTERMOST_MUTATION_COMMAND_PREFIX = "!onyx-seafile-mutate "
MATTERMOST_MUTATION_REJECTED_MESSAGE = "Seafile mutation request was rejected."
MATTERMOST_MUTATION_UNAVAILABLE_MESSAGE = (
    "Seafile mutation gateway is unavailable; mutation denied."
)
MATTERMOST_MUTATION_SUCCESS_MESSAGE = (
    "Seafile mutation completed through the controlled platform gateway."
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_REVISION_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,256}")
_REQUEST_FIELDS = frozenset(
    {
        "action",
        "repo_id",
        "path",
        "expected_revision",
        "content",
        "destination_path",
        "confirmed",
        "scope_prefix",
    }
)


class MattermostMutationPermissionError(PermissionError):
    """A mutation was rejected before controlled gateway execution."""


class SeafileActionType(str, Enum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    MOVE = "move"
    DELETE = "delete"


class SeafileActionOrigin(str, Enum):
    CHAT_COMMAND = "chat_command"
    TOOL_CALL = "tool_call"
    DOCUMENT_TEXT = "document_text"
    ATTACHMENT_PROMOTION = "attachment_promotion"


@dataclass(frozen=True)
class SeafileActionRequest:
    """Validated Onyx mutation intent; converted at the platform boundary."""

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


class AuthoritativePlatformGatewayBridge:
    """Convert validated Onyx intent into the installed platform package's exact types."""

    def __init__(self, gateway: object, platform_contract: ModuleType) -> None:
        mutate = getattr(gateway, "mutate", None)
        if not callable(mutate):
            raise ValueError("configured platform gateway must expose mutate")
        required = (
            "MattermostMutationContext",
            "SeafileActionRequest",
            "SeafileActionType",
            "SeafileActionOrigin",
        )
        if any(not hasattr(platform_contract, name) for name in required):
            raise ValueError("authoritative platform gateway contract is incomplete")
        self._gateway_mutate = mutate
        self._platform = platform_contract

    @classmethod
    def from_factory_spec(cls, factory_spec: str) -> AuthoritativePlatformGatewayBridge:
        if type(factory_spec) is not str or factory_spec.count(":") != 1:
            raise ValueError("gateway factory must use module:callable")
        module_name, attribute_name = factory_spec.split(":", 1)
        if not module_name or not attribute_name:
            raise ValueError("gateway factory must use module:callable")
        try:
            factory_module = importlib.import_module(module_name)
            factory = getattr(factory_module, attribute_name)
            platform_contract = importlib.import_module("actions.seafile")
        except (ImportError, AttributeError):
            raise ValueError(
                "configured platform gateway factory is unavailable"
            ) from None
        if not callable(factory):
            raise ValueError("configured platform gateway factory must be callable")
        return cls(factory(), platform_contract)

    def mutate(
        self,
        context: MattermostMutationContext,
        request: SeafileActionRequest,
    ) -> Any:
        platform_context = self._platform.MattermostMutationContext(
            requester_id=context.requester_id,
            channel_id=context.channel_id,
            post_id=context.post_id,
            root_post_id=context.root_post_id,
            claimed_username=context.claimed_username,
            claimed_roles=context.claimed_roles,
        )
        platform_request = self._platform.SeafileActionRequest(
            action=self._platform.SeafileActionType(request.action.value),
            repo_id=request.repo_id,
            path=request.path,
            requesting_user=request.requesting_user,
            origin=self._platform.SeafileActionOrigin(request.origin.value),
            expected_revision=request.expected_revision,
            content=request.content,
            destination_path=request.destination_path,
            confirmed=request.confirmed,
            scope_prefix=request.scope_prefix,
        )
        return self._gateway_mutate(platform_context, platform_request)


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
        self._validate_event(event)
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
            type(identity) is not MattermostUserInfo
            or type(identity.id) is not str
            or _IDENTIFIER_PATTERN.fullmatch(identity.id) is None
            or type(identity.username) is not str
            or not identity.username
            or len(identity.username) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in identity.username
            )
            or type(identity.roles) is not str
        ):
            raise MattermostMutationPermissionError(
                MATTERMOST_MUTATION_PERMISSION_DENIED_MESSAGE
            ) from None
        return identity

    @staticmethod
    def _validate_event(event: NormalizedMattermostEvent) -> None:
        if type(event) is not NormalizedMattermostEvent:
            raise MattermostMutationPermissionError(
                "A normalized Mattermost event is required."
            )
        identifiers = (
            event.user_id,
            event.channel_id,
            event.post_id,
            event.root_post_id,
        )
        if any(
            type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None
            for value in identifiers
        ):
            raise MattermostMutationPermissionError(
                "Valid Mattermost event correlation identifiers are required."
            )

    @staticmethod
    def _validate_pre_authorization_contract(request: SeafileActionRequest) -> None:
        if type(request) is not SeafileActionRequest:
            raise MattermostMutationPermissionError(
                "A typed mutation request is required."
            )
        if type(request.action) is not SeafileActionType:
            raise MattermostMutationPermissionError(
                "A typed mutation action is required before gateway routing."
            )
        if type(request.origin) is not SeafileActionOrigin:
            raise MattermostMutationPermissionError(
                "A typed mutation origin is required."
            )
        if type(request.confirmed) is not bool:
            raise MattermostMutationPermissionError(
                "A boolean confirmation is required."
            )
        required_strings = (
            request.repo_id,
            request.path,
            request.requesting_user,
            request.scope_prefix,
        )
        if any(type(value) is not str for value in required_strings):
            raise MattermostMutationPermissionError(
                "Mutation request strings are malformed."
            )
        optional_strings = (
            request.expected_revision,
            request.content,
            request.destination_path,
        )
        if any(
            value is not None and type(value) is not str for value in optional_strings
        ):
            raise MattermostMutationPermissionError(
                "Mutation request strings are malformed."
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
            if (
                type(request.expected_revision) is not str
                or _REVISION_PATTERN.fullmatch(request.expected_revision) is None
            ):
                raise MattermostMutationPermissionError(
                    f"{request.action.value} requires a valid expected revision."
                )
        if (
            request.action is SeafileActionType.CREATE
            and request.expected_revision is not None
        ):
            raise MattermostMutationPermissionError(
                "create does not accept an expected revision."
            )
        if (
            request.origin is SeafileActionOrigin.ATTACHMENT_PROMOTION
            and request.confirmed is not True
        ):
            raise MattermostMutationPermissionError(
                "Attachment promotion requires explicit confirmation."
            )
        if (
            not request.requesting_user
            or len(request.requesting_user) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in request.requesting_user
            )
        ):
            raise MattermostMutationPermissionError(
                "A valid requesting user is required for audit."
            )
        if _IDENTIFIER_PATTERN.fullmatch(request.repo_id) is None:
            raise MattermostMutationPermissionError(
                "A valid repo identifier is required."
            )
        MattermostMutationAdapter._validate_canonical_path(
            request.scope_prefix, "scope prefix", allow_root=True
        )
        MattermostMutationAdapter._validate_canonical_path(request.path, "path")
        if (
            request.scope_prefix != "/"
            and request.path != request.scope_prefix
            and not request.path.startswith(request.scope_prefix + "/")
        ):
            raise MattermostMutationPermissionError(
                "Mutation path is outside its scope."
            )
        if request.destination_path is not None:
            MattermostMutationAdapter._validate_canonical_path(
                request.destination_path, "destination path"
            )
            if (
                request.scope_prefix != "/"
                and request.destination_path != request.scope_prefix
                and not request.destination_path.startswith(request.scope_prefix + "/")
            ):
                raise MattermostMutationPermissionError(
                    "Mutation destination is outside its scope."
                )
        if request.action is SeafileActionType.MOVE:
            if not request.destination_path or request.destination_path == request.path:
                raise MattermostMutationPermissionError(
                    "move requires a distinct destination path."
                )
        elif request.destination_path is not None:
            raise MattermostMutationPermissionError(
                "A destination path is valid only for move."
            )
        if request.action in {SeafileActionType.CREATE, SeafileActionType.UPDATE}:
            if request.content is None:
                raise MattermostMutationPermissionError(
                    f"{request.action.value} requires content."
                )
        elif request.content is not None:
            raise MattermostMutationPermissionError(
                f"content is invalid for {request.action.value}."
            )

    @staticmethod
    def _validate_canonical_path(
        path: str,
        field: str,
        *,
        allow_root: bool = False,
    ) -> None:
        if (
            not path.startswith("/")
            or len(path) > 1024
            or (path == "/" and not allow_root)
            or (path != "/" and path.endswith("/"))
            or "//" in path
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(segment in {"", ".", ".."} for segment in path.split("/")[1:])
        ):
            raise MattermostMutationPermissionError(
                f"A canonical Seafile {field} is required."
            )


def parse_mattermost_mutation_command(text: str) -> SeafileActionRequest | None:
    """Parse the explicit JSON mutation wire contract; non-commands return None."""

    if type(text) is not str or not text.startswith(MATTERMOST_MUTATION_COMMAND_PREFIX):
        return None
    try:
        payload = json.loads(
            text[len(MATTERMOST_MUTATION_COMMAND_PREFIX) :],
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise MattermostMutationPermissionError(
            MATTERMOST_MUTATION_REJECTED_MESSAGE
        ) from None
    if type(payload) is not dict or frozenset(payload) != _REQUEST_FIELDS:
        raise MattermostMutationPermissionError(MATTERMOST_MUTATION_REJECTED_MESSAGE)
    try:
        request = SeafileActionRequest(
            action=SeafileActionType(payload["action"]),
            repo_id=payload["repo_id"],
            path=payload["path"],
            requesting_user="<unverified>",
            origin=SeafileActionOrigin.CHAT_COMMAND,
            expected_revision=payload["expected_revision"],
            content=payload["content"],
            destination_path=payload["destination_path"],
            confirmed=payload["confirmed"],
            scope_prefix=payload["scope_prefix"],
        )
        MattermostMutationAdapter._validate_pre_authorization_contract(request)
    except (KeyError, TypeError, ValueError, MattermostMutationPermissionError):
        raise MattermostMutationPermissionError(
            MATTERMOST_MUTATION_REJECTED_MESSAGE
        ) from None
    return request


def build_mattermost_confirmed_mutation_command(text: str) -> str | None:
    """Return a confirmed mutation command for a typed unconfirmed request."""

    payload = _mutation_payload_from_text(text)
    if payload is None:
        return None
    if payload.get("confirmed") is not False:
        return None
    confirmed_payload = cast(dict[str, Any], dict(payload))
    confirmed_payload["confirmed"] = True
    try:
        request = SeafileActionRequest(
            action=SeafileActionType(confirmed_payload["action"]),
            repo_id=confirmed_payload["repo_id"],
            path=confirmed_payload["path"],
            requesting_user="<unverified>",
            origin=SeafileActionOrigin.CHAT_COMMAND,
            expected_revision=confirmed_payload["expected_revision"],
            content=confirmed_payload["content"],
            destination_path=confirmed_payload["destination_path"],
            confirmed=confirmed_payload["confirmed"],
            scope_prefix=confirmed_payload["scope_prefix"],
        )
        MattermostMutationAdapter._validate_pre_authorization_contract(request)
    except (KeyError, TypeError, ValueError, MattermostMutationPermissionError):
        return None
    return MATTERMOST_MUTATION_COMMAND_PREFIX + json.dumps(
        confirmed_payload,
        sort_keys=True,
        separators=(",", ":"),
    )


def _mutation_payload_from_text(text: str) -> dict[str, object] | None:
    if type(text) is not str or not text.startswith(MATTERMOST_MUTATION_COMMAND_PREFIX):
        return None
    try:
        payload = json.loads(
            text[len(MATTERMOST_MUTATION_COMMAND_PREFIX) :],
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if type(payload) is not dict or frozenset(payload) != _REQUEST_FIELDS:
        return None
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload
