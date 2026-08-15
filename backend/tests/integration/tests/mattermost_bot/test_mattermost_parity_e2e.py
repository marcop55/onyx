from __future__ import annotations

from pathlib import Path

from onyx.onyxbot.mattermost.parity import (
    MATTERMOST_SLACK_PARITY_MATRIX,
    MattermostParityStatus,
    matrix_by_key,
)

_ACCEPTANCE_GUARANTEES = frozenset(
    {
        "membership_fail_closed",
        "shared_full_corpus",
        "replay_safe",
        "current_system_admin_only",
    }
)
_REPO_ROOT = Path(__file__).resolve().parents[5]
_DOC_PATH = _REPO_ROOT / "docs" / "mattermost-slack-parity.md"


def test_mattermost_parity_manifest_proves_release_acceptance_boundaries() -> None:
    matrix = matrix_by_key()

    assert matrix["membership_authorization"].status is (
        MattermostParityStatus.DIRECT_MATTERMOST_FEATURE
    )
    assert matrix["shared_full_corpus_retrieval"].status is (
        MattermostParityStatus.POLICY_DIFFERENCE
    )
    assert matrix["non_admin_seafile_mutation"].status is (
        MattermostParityStatus.POLICY_DIFFERENCE
    )
    assert matrix["health_delivery_observability"].status is (
        MattermostParityStatus.DIRECT_MATTERMOST_FEATURE
    )

    manifest_guarantees = frozenset(
        guarantee
        for entry in MATTERMOST_SLACK_PARITY_MATRIX
        for guarantee in entry.guarantees
    )
    assert _ACCEPTANCE_GUARANTEES <= manifest_guarantees

    for entry in MATTERMOST_SLACK_PARITY_MATRIX:
        if entry.status is MattermostParityStatus.PLATFORM_GAP:
            assert entry.fallback
        if "membership_fail_closed" in entry.guarantees:
            assert any(
                evidence
                in {
                    "backend/onyx/onyxbot/mattermost/listener.py:_authorize_and_attribute_event",
                    "backend/onyx/onyxbot/mattermost/channel_filters.py:resolve_mattermost_channel_filters",
                    "backend/onyx/connectors/mattermost/connector.py:MattermostConnector",
                }
                for evidence in entry.evidence
            )
        if "replay_safe" in entry.guarantees:
            assert any(
                evidence.startswith("backend/onyx/db/mattermost_bot.py")
                or evidence.startswith("backend/onyx/onyxbot/mattermost/listener.py")
                or evidence.startswith("backend/onyx/onyxbot/mattermost/handler.py")
                or evidence.startswith(
                    "backend/onyx/onyxbot/mattermost/channel_filters.py"
                )
                or evidence.startswith(
                    "backend/onyx/connectors/mattermost/connector.py"
                )
                for evidence in entry.evidence
            )


def test_human_parity_matrix_matches_executable_manifest() -> None:
    document = _DOC_PATH.read_text(encoding="utf-8")

    for entry in MATTERMOST_SLACK_PARITY_MATRIX:
        assert f"`{entry.key}`" in document
        assert f"| {entry.area.value} | {entry.status.value} |" in document
        assert entry.mattermost_contract in document
        for evidence in entry.evidence:
            assert f"`{evidence}`" in document
        if entry.fallback is not None:
            assert f"fallback: {entry.fallback}" in document
