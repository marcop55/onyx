from pathlib import Path

from onyx.onyxbot.mattermost.parity import (
    MATTERMOST_SLACK_PARITY_MATRIX,
    MattermostParityStatus,
    SlackCapabilityArea,
    matrix_by_key,
    validate_mattermost_slack_parity_matrix,
)

_REPO_ROOT = Path(__file__).resolve().parents[6]
_DOC_PATH = _REPO_ROOT / "docs" / "mattermost-slack-parity.md"

_EXPECTED_KEYS = frozenset(
    {
        "bot_instance_configuration",
        "channel_agent_configuration",
        "channel_response_controls",
        "direct_message_answer",
        "explicit_channel_mention_answer",
        "root_allowlisted_post_answer",
        "thread_followup_answer",
        "slash_command_entrypoint",
        "ephemeral_or_private_answer",
        "interactive_retry_control",
        "streaming_single_post_answer",
        "citations_and_source_links",
        "chat_feedback",
        "standard_answer_workflow",
        "followup_workflow",
        "bot_loop_prevention",
        "membership_authorization",
        "shared_full_corpus_retrieval",
        "per_user_private_onyx_permissions",
        "mattermost_history_search_connector",
        "channel_reference_filtering",
        "complete_thread_context",
        "attachment_handling",
        "admin_seafile_mutation",
        "non_admin_seafile_mutation",
        "health_delivery_observability",
    }
)


def test_parity_manifest_is_executable_and_complete() -> None:
    problems = validate_mattermost_slack_parity_matrix()

    assert problems == ()
    assert frozenset(matrix_by_key()) == _EXPECTED_KEYS
    assert {entry.area for entry in MATTERMOST_SLACK_PARITY_MATRIX} == set(
        SlackCapabilityArea
    )


def test_successful_chat_capabilities_pin_mattermost_native_evidence() -> None:
    matrix = matrix_by_key()

    for key in (
        "direct_message_answer",
        "explicit_channel_mention_answer",
        "root_allowlisted_post_answer",
        "thread_followup_answer",
    ):
        entry = matrix[key]
        assert entry.status is MattermostParityStatus.DIRECT_MATTERMOST_FEATURE
        assert "membership_fail_closed" in entry.guarantees
        assert "shared_full_corpus" in entry.guarantees
        assert "replay_safe" in entry.guarantees
        assert any("mattermost/listener.py" in evidence for evidence in entry.evidence)
        assert any("mattermost/handler.py" in evidence for evidence in entry.evidence)


def test_authorization_denial_is_manifested_for_admitted_runtime_paths() -> None:
    admitted_runtime_entries = [
        entry
        for entry in MATTERMOST_SLACK_PARITY_MATRIX
        if "membership_fail_closed" in entry.guarantees
    ]

    assert admitted_runtime_entries
    assert all(
        "backend/onyx/onyxbot/mattermost/listener.py:_authorize_and_attribute_event"
        in entry.evidence
        for entry in admitted_runtime_entries
    )
    assert matrix_by_key()["non_admin_seafile_mutation"].status is (
        MattermostParityStatus.POLICY_DIFFERENCE
    )


def test_replay_safe_entries_have_durable_or_idempotent_evidence() -> None:
    replay_safe_entries = [
        entry
        for entry in MATTERMOST_SLACK_PARITY_MATRIX
        if "replay_safe" in entry.guarantees
    ]

    assert replay_safe_entries
    for entry in replay_safe_entries:
        assert any(
            evidence.startswith("backend/onyx/db/mattermost_bot.py")
            or evidence.startswith("backend/onyx/onyxbot/mattermost/listener.py")
            or evidence.startswith("backend/onyx/onyxbot/mattermost/handler.py")
            for evidence in entry.evidence
        )


def test_primary_platform_gap_records_fallback_without_weakening_retrieval() -> None:
    matrix = matrix_by_key()
    private_permissions = matrix["per_user_private_onyx_permissions"]
    history_connector = matrix["mattermost_history_search_connector"]

    assert private_permissions.status is MattermostParityStatus.PLATFORM_GAP
    assert (
        private_permissions.fallback
        == "Use the configured service identity and shared PoC knowledge scope until a credentialed per-user mapping is explicitly designed."
    )
    assert "shared_full_corpus" in private_permissions.guarantees
    assert history_connector.status is MattermostParityStatus.PLATFORM_GAP
    assert "no_fake_connectors" in history_connector.guarantees


def test_human_matrix_lists_every_executable_manifest_key() -> None:
    document = _DOC_PATH.read_text(encoding="utf-8")

    for key in _EXPECTED_KEYS:
        assert f"`{key}`" in document
    assert "Generated from `backend/onyx/onyxbot/mattermost/parity.py`" in document
