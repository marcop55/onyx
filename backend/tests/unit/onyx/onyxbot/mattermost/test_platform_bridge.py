from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from onyx.onyxbot.mattermost.mutations import (
    AuthoritativePlatformGatewayBridge,
    MattermostMutationContext,
    SeafileActionOrigin,
    SeafileActionRequest,
    SeafileActionType,
)

PLATFORM_ROOT = Path(
    os.environ.get(
        "ORKA_PLATFORM_WORKTREE",
        "/Users/ai/code/orka-platform-worktrees/merge-pr26-main",
    )
)
PLATFORM_HEAD = "0392418a19ad1b93f078b4ad59a5d3fe1a303b53"
pytestmark = pytest.mark.skipif(
    not (PLATFORM_ROOT / "actions/seafile/gateway.py").is_file(),
    reason="authoritative Orka platform worktree is not available",
)


def _authoritative_contract() -> ModuleType:
    sys.path.insert(0, str(PLATFORM_ROOT))
    try:
        return importlib.import_module("actions.seafile")
    finally:
        sys.path.remove(str(PLATFORM_ROOT))


class ExactTypeGateway:
    def __init__(self, platform: ModuleType) -> None:
        self.platform = platform
        self.calls: list[tuple[Any, Any]] = []

    def mutate(self, context: object, request: object) -> str:
        assert type(context) is self.platform.MattermostMutationContext
        assert type(request) is self.platform.SeafileActionRequest
        authoritative_request = cast(Any, request)
        assert type(authoritative_request.action) is self.platform.SeafileActionType
        assert type(authoritative_request.origin) is self.platform.SeafileActionOrigin
        self.calls.append((context, authoritative_request))
        return "authoritative-result"


def local_context() -> MattermostMutationContext:
    return MattermostMutationContext(
        requester_id="mm-user-1",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="root-1",
        claimed_username="reiss",
        claimed_roles="system_user system_admin",
    )


def local_request() -> SeafileActionRequest:
    return SeafileActionRequest(
        action=SeafileActionType.UPDATE,
        repo_id="repo-1",
        path="/automation/note.md",
        requesting_user="reiss",
        origin=SeafileActionOrigin.CHAT_COMMAND,
        expected_revision="rev-1",
        content="new content",
        confirmed=True,
        scope_prefix="/automation",
    )


def test_bridge_constructs_exact_authoritative_platform_runtime_types() -> None:
    platform = _authoritative_contract()
    gateway = ExactTypeGateway(platform)
    bridge = AuthoritativePlatformGatewayBridge(gateway, platform)

    result = bridge.mutate(local_context(), local_request())

    assert result == "authoritative-result"
    assert len(gateway.calls) == 1
    authoritative_context, authoritative_request = gateway.calls[0]
    assert authoritative_context.requester_id == "mm-user-1"
    assert authoritative_request.expected_revision == "rev-1"
    assert platform.__file__ is not None
    assert Path(platform.__file__).resolve().is_relative_to(PLATFORM_ROOT)


def test_authoritative_gateway_rejects_duplicate_onyx_dataclasses() -> None:
    platform = _authoritative_contract()
    gateway = ExactTypeGateway(platform)

    with pytest.raises(AssertionError):
        gateway.mutate(local_context(), local_request())


def test_platform_contract_fixture_is_pinned_to_reviewed_commit() -> None:
    import subprocess

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PLATFORM_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == PLATFORM_HEAD


def test_configured_factory_builds_production_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _authoritative_contract()
    gateway = ExactTypeGateway(platform)
    factory_module = ModuleType("configured_platform_gateway")
    setattr(factory_module, "build_gateway", lambda: gateway)
    monkeypatch.setitem(sys.modules, factory_module.__name__, factory_module)

    bridge = AuthoritativePlatformGatewayBridge.from_factory_spec(
        "configured_platform_gateway:build_gateway"
    )

    assert bridge.mutate(local_context(), local_request()) == "authoritative-result"


@pytest.mark.parametrize("spec", ["", "missing_separator", "module:", ":factory"])
def test_malformed_factory_spec_fails_closed(spec: str) -> None:
    with pytest.raises(ValueError, match="gateway factory"):
        AuthoritativePlatformGatewayBridge.from_factory_spec(spec)
