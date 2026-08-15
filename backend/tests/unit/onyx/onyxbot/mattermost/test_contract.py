from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[6]
_CONTRACT_PATH = _REPO_ROOT / "docs" / "mattermost-adapter.md"


REQUIRED_EVENT_FIXTURES: dict[str, dict[str, Any]] = {
    "direct_message_post": {
        "event": "posted",
        "team_id": "global",
        "channel_id": "dm_channel_1",
        "channel_type": "D",
        "post": {"id": "post_dm_1", "root_id": "", "message": "help"},
        "expected_session_key": "mattermost:dm:global:dm_channel_1",
    },
    "channel_mention_post": {
        "event": "posted",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": {
            "id": "post_root_1",
            "root_id": "",
            "message": "@onyx what changed?",
        },
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "root_allowlisted_post": {
        "event": "posted",
        "team_id": "team_1",
        "channel_id": "channel_2",
        "channel_type": "P",
        "post": {"id": "post_root_2", "root_id": "", "message": "summarize this"},
        "expected_session_key": "mattermost:channel:team_1:channel_2:post_root_2",
    },
    "thread_reply_followup": {
        "event": "posted",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": {
            "id": "post_reply_1",
            "root_id": "post_root_1",
            "message": "can you expand?",
        },
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "post_update_retry_target": {
        "event": "post_edited",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": {
            "id": "post_root_1",
            "root_id": "",
            "message": "@onyx retry with more detail",
        },
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "reaction_feedback": {
        "event": "reaction_added",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": {"id": "bot_answer_1", "root_id": "post_root_1", "message": "+1"},
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "post_delete_tombstone": {
        "event": "post_deleted",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": {"id": "post_root_1", "root_id": "", "message": ""},
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "bot_self_post": {
        "event": "posted",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "user_id": "bot_user_1",
        "post": {"id": "bot_post_1", "root_id": "post_root_1", "message": "answer"},
        "expected_session_key": "mattermost:channel:team_1:channel_1:post_root_1",
    },
    "non_content_system_event": {
        "event": "user_added",
        "team_id": "team_1",
        "channel_id": "channel_1",
        "channel_type": "O",
        "post": None,
        "expected_session_key": None,
    },
}


def _mattermost_session_key(event_fixture: dict[str, Any]) -> str | None:
    post = event_fixture["post"]
    if post is None:
        return None

    team_id = event_fixture.get("team_id") or "global"
    channel_id = event_fixture["channel_id"]
    channel_type = event_fixture["channel_type"]

    if channel_type == "D":
        return f"mattermost:dm:{team_id}:{channel_id}"

    root_post_id = post.get("root_id") or post["id"]
    return f"mattermost:channel:{team_id}:{channel_id}:{root_post_id}"


def _read_contract() -> str:
    return _CONTRACT_PATH.read_text(encoding="utf-8")


def _normalize_contract_text(text: str) -> str:
    normalized_text = text.lower()
    for punctuation in ("-", "/"):
        normalized_text = normalized_text.replace(punctuation, " ")
    return normalized_text


def test_contract_document_exists() -> None:
    assert _CONTRACT_PATH.exists()


def test_contract_defines_shared_knowledge_scope_identity_boundary() -> None:
    contract = _read_contract()

    required_phrases = [
        "one shared knowledge scope",
        "configured service identity",
        "must not try to mirror each Mattermost user's private Onyx permissions",
        "must not use Hermes or Codex OAuth tokens as Onyx inference credentials",
        "per-user permissions become required",
    ]

    for phrase in required_phrases:
        assert phrase in contract


@pytest.mark.parametrize("event_name", REQUIRED_EVENT_FIXTURES)
def test_contract_names_every_required_event_class(event_name: str) -> None:
    contract = _normalize_contract_text(_read_contract())
    readable_event_name = event_name.replace("_", " ")

    assert readable_event_name in contract


@pytest.mark.parametrize("event_fixture", REQUIRED_EVENT_FIXTURES.values())
def test_session_key_contract_for_required_event_fixtures(
    event_fixture: dict[str, Any],
) -> None:
    assert (
        _mattermost_session_key(event_fixture) == event_fixture["expected_session_key"]
    )


def test_contract_includes_exact_session_key_templates() -> None:
    contract = _read_contract()

    assert "mattermost:dm:{team_id}:{channel_id}" in contract
    assert "mattermost:channel:{team_id}:{channel_id}:{root_post_id}" in contract
    assert "mattermost:channel:{team_id}:{channel_id}:{root_id}" in contract


def test_contract_maps_required_slack_parity_targets() -> None:
    contract = _read_contract()

    required_targets = [
        "Direct message",
        "Explicit channel mention",
        "Thread followup",
        "Root post answer",
        "Socket/event dedupe",
        "Streaming answer",
        "Citations/source links",
        "Actions",
        "Feedback",
        "Visible failures",
        "Allowlist",
        "Bot-loop prevention",
    ]

    for target in required_targets:
        assert target in contract


def test_contract_defines_credential_and_production_cutover_boundary() -> None:
    contract = _read_contract()

    assert "No external credential is required to verify this contract" in contract
    assert "must not use the production `@orka`" in contract
    assert "Production bot credentials" in contract


def test_contract_defines_current_system_admin_mutation_boundary() -> None:
    contract = " ".join(_read_contract().split()).lower()

    required_phrases = [
        "current mattermost `system_admin`",
        "fresh server api lookup for every mutation",
        "controlled platform gateway",
        "channel admin and team admin roles do not grant mutation authority",
        "attachment promotion is a mutation",
        "read/retrieval remains on the existing shared full-corpus path",
    ]
    for phrase in required_phrases:
        assert phrase in contract
