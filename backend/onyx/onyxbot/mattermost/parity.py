"""Executable Slack-to-Mattermost capability parity contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SlackCapabilityArea(StrEnum):
    BOT = "bot"
    CHANNEL = "channel"
    INTERACTION = "interaction"
    SEARCH = "search"
    IDENTITY = "identity"
    FEEDBACK = "feedback"
    ADMINISTRATION = "administration"


class MattermostParityStatus(StrEnum):
    DIRECT_MATTERMOST_FEATURE = "direct_mattermost_feature"
    MATTERMOST_NATIVE_EQUIVALENT = "mattermost_native_equivalent"
    POLICY_DIFFERENCE = "policy_difference"
    PLATFORM_GAP = "platform_gap"


@dataclass(frozen=True)
class SlackToMattermostCapability:
    key: str
    area: SlackCapabilityArea
    slack_capability: str
    mattermost_contract: str
    status: MattermostParityStatus
    evidence: tuple[str, ...]
    guarantees: frozenset[str]
    fallback: str | None = None


_MEMBERSHIP_EVIDENCE = (
    "backend/onyx/onyxbot/mattermost/listener.py:_authorize_and_attribute_event",
)
_LEDGER_EVIDENCE = (
    "backend/onyx/db/mattermost_bot.py:MattermostEventState",
    "backend/onyx/onyxbot/mattermost/handler.py:handle_normalized_mattermost_event",
)
_CHAT_EVIDENCE = (
    "backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer",
    *_MEMBERSHIP_EVIDENCE,
    *_LEDGER_EVIDENCE,
)
_CHAT_GUARANTEES = frozenset(
    {"membership_fail_closed", "shared_full_corpus", "replay_safe"}
)
_REPLAY_GUARANTEE = frozenset({"replay_safe"})

MATTERMOST_SLACK_PARITY_MATRIX: tuple[SlackToMattermostCapability, ...] = (
    SlackToMattermostCapability(
        key="bot_instance_configuration",
        area=SlackCapabilityArea.BOT,
        slack_capability="Dashboard-managed Slack bot tokens and enablement.",
        mattermost_contract="Deployment-file Mattermost service config for the release PoC; later dashboard CRUD is tracked by issue #31.",
        status=MattermostParityStatus.POLICY_DIFFERENCE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/config.py:MattermostBotConfig",
            "docs/mattermost-adapter.md:Credential and cutover boundary",
        ),
        guarantees=frozenset({"no_production_credentials", "provider_neutral"}),
        fallback="Operators configure the disposable test bot outside Onyx until Mattermost dashboard CRUD ships.",
    ),
    SlackToMattermostCapability(
        key="channel_agent_configuration",
        area=SlackCapabilityArea.CHANNEL,
        slack_capability="Per-channel Slack persona and document-set configuration.",
        mattermost_contract="One configured persona and one shared PoC knowledge scope are used for admitted Mattermost users.",
        status=MattermostParityStatus.POLICY_DIFFERENCE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/handler.py:MattermostHandlerConfig",
            "docs/mattermost-adapter.md:Shared-knowledge-scope identity boundary",
        ),
        guarantees=frozenset({"shared_full_corpus", "provider_neutral"}),
        fallback="Per-channel Mattermost agent controls are tracked by issue #32.",
    ),
    SlackToMattermostCapability(
        key="channel_response_controls",
        area=SlackCapabilityArea.CHANNEL,
        slack_capability="Slack respond-tag-only, root response, allowlist, and response visibility controls.",
        mattermost_contract="Mention-only by default, optional root-post channels, optional team/channel/user narrowing, and visible thread replies.",
        status=MattermostParityStatus.MATTERMOST_NATIVE_EQUIVALENT,
        evidence=(
            "backend/onyx/onyxbot/mattermost/models.py:MattermostListenerConfig",
            "backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer",
            *_MEMBERSHIP_EVIDENCE,
        ),
        guarantees=frozenset({"membership_fail_closed", "shared_full_corpus"}),
    ),
    SlackToMattermostCapability(
        key="direct_message_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack app DM answer.",
        mattermost_contract="Mattermost channel type D answers without requiring a mention and uses the DM session key.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=_CHAT_EVIDENCE,
        guarantees=_CHAT_GUARANTEES,
    ),
    SlackToMattermostCapability(
        key="explicit_channel_mention_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack app mention or bot tag in a channel.",
        mattermost_contract="Mattermost public/private channel post with bot mention is stripped and answered in the root thread.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=_CHAT_EVIDENCE,
        guarantees=_CHAT_GUARANTEES,
    ),
    SlackToMattermostCapability(
        key="root_allowlisted_post_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack channel config can permit non-mentioned root questions.",
        mattermost_contract="Configured Mattermost root-post channels answer root posts only; thread chatter still needs ownership.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=_CHAT_EVIDENCE,
        guarantees=_CHAT_GUARANTEES,
    ),
    SlackToMattermostCapability(
        key="thread_followup_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack thread follow-up continuity.",
        mattermost_contract="Replies under an adapter-owned Mattermost root continue the same Onyx chat session.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=_CHAT_EVIDENCE,
        guarantees=_CHAT_GUARANTEES,
    ),
    SlackToMattermostCapability(
        key="slash_command_entrypoint",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack /OnyxBot slash command.",
        mattermost_contract="Mattermost-native slash command UX is feasible but not shipped in the issue #30 release artifact.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "docs/mattermost-adapter.md:Actions and feedback",
            *_MEMBERSHIP_EVIDENCE,
        ),
        guarantees=frozenset({"membership_fail_closed"}),
        fallback="Use DM, mention, or root allowlisted post until issue #33 ships Mattermost slash commands.",
    ),
    SlackToMattermostCapability(
        key="ephemeral_or_private_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack ephemeral answers and private publication controls.",
        mattermost_contract="Mattermost ephemeral posts are a native equivalent, but this release answers visibly in-thread to avoid identity leaks.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "docs/mattermost-adapter.md:Failure contract",
            *_MEMBERSHIP_EVIDENCE,
        ),
        guarantees=frozenset({"membership_fail_closed", "shared_full_corpus"}),
        fallback="Use visible thread replies until issue #34 ships explicit ephemeral response handling.",
    ),
    SlackToMattermostCapability(
        key="interactive_retry_control",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack block action to regenerate an answer.",
        mattermost_contract="Post edits against adapter-owned roots act as replay-safe retry targets; interactive posts are tracked by issue #35.",
        status=MattermostParityStatus.MATTERMOST_NATIVE_EQUIVALENT,
        evidence=_CHAT_EVIDENCE,
        guarantees=_CHAT_GUARANTEES,
        fallback="Edit the owned root post, or use the later interactive action once configured.",
    ),
    SlackToMattermostCapability(
        key="streaming_single_post_answer",
        area=SlackCapabilityArea.INTERACTION,
        slack_capability="Slack chat.update streaming answer.",
        mattermost_contract="Mattermost creates one placeholder post and updates that one post with chunks and final citations.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/streaming.py:stream_mattermost_answer",
            *_LEDGER_EVIDENCE,
        ),
        guarantees=_REPLAY_GUARANTEE,
    ),
    SlackToMattermostCapability(
        key="citations_and_source_links",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack answer blocks with citations and source links.",
        mattermost_contract="Mattermost renders answer citations and sources as Markdown links without hidden credentials.",
        status=MattermostParityStatus.MATTERMOST_NATIVE_EQUIVALENT,
        evidence=(
            "backend/onyx/onyxbot/mattermost/formatting.py:format_mattermost_answer",
            "backend/onyx/onyxbot/mattermost/streaming.py:stream_mattermost_answer",
        ),
        guarantees=frozenset({"shared_full_corpus"}),
    ),
    SlackToMattermostCapability(
        key="chat_feedback",
        area=SlackCapabilityArea.FEEDBACK,
        slack_capability="Slack like/dislike answer feedback.",
        mattermost_contract="Mattermost reactions on owned answer posts map to Onyx chat-message feedback with Mattermost attribution.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/listener.py:_normalize_reaction_feedback",
            *_LEDGER_EVIDENCE,
        ),
        guarantees=_REPLAY_GUARANTEE,
    ),
    SlackToMattermostCapability(
        key="standard_answer_workflow",
        area=SlackCapabilityArea.FEEDBACK,
        slack_capability="Slack standard-answer workflow and source feedback blocks.",
        mattermost_contract="No direct release-line Mattermost standard-answer workflow is shipped yet.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=("backend/onyx/onyxbot/slack/handlers/handle_standard_answers.py",),
        guarantees=frozenset({"no_fake_connectors"}),
        fallback="Use regular Mattermost answers until issue #41 ships standard-answer parity.",
    ),
    SlackToMattermostCapability(
        key="followup_workflow",
        area=SlackCapabilityArea.FEEDBACK,
        slack_capability="Slack follow-up requested and resolved button workflow.",
        mattermost_contract="No direct release-line Mattermost follow-up workflow is shipped yet.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "backend/onyx/onyxbot/slack/handlers/handle_buttons.py:handle_followup_button",
        ),
        guarantees=frozenset({"no_fake_connectors"}),
        fallback="Use visible thread replies until issue #41 ships follow-up controls.",
    ),
    SlackToMattermostCapability(
        key="bot_loop_prevention",
        area=SlackCapabilityArea.IDENTITY,
        slack_capability="Slack ignores bot/self messages.",
        mattermost_contract="Mattermost ignores configured bot user and integration user posts before handling.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer",
        ),
        guarantees=frozenset({"replay_safe"}),
    ),
    SlackToMattermostCapability(
        key="membership_authorization",
        area=SlackCapabilityArea.IDENTITY,
        slack_capability="Slack channel/user access checks before answer visibility.",
        mattermost_contract="Every normalized event rechecks bot and sender channel membership and fails closed on lookup failure.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=_MEMBERSHIP_EVIDENCE,
        guarantees=frozenset({"membership_fail_closed", "shared_full_corpus"}),
    ),
    SlackToMattermostCapability(
        key="shared_full_corpus_retrieval",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack can answer with configured Onyx retrieval scope.",
        mattermost_contract="Admitted Mattermost members use the configured service identity with allowed_tool_ids=None and bypass_acl=False.",
        status=MattermostParityStatus.POLICY_DIFFERENCE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/handler.py:_stream_mattermost_answer_packets",
        ),
        guarantees=frozenset({"shared_full_corpus", "provider_neutral"}),
    ),
    SlackToMattermostCapability(
        key="per_user_private_onyx_permissions",
        area=SlackCapabilityArea.IDENTITY,
        slack_capability="Slack ephemeral/DM answers can use a resolved Onyx user's private permissions.",
        mattermost_contract="Mattermost PoC must not infer Onyx private permissions from Mattermost identity.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "docs/mattermost-adapter.md:Shared-knowledge-scope identity boundary",
        ),
        guarantees=frozenset({"shared_full_corpus", "provider_neutral"}),
        fallback="Use the configured service identity and shared PoC knowledge scope until a credentialed per-user mapping is explicitly designed.",
    ),
    SlackToMattermostCapability(
        key="mattermost_history_search_connector",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack federated/history search connector.",
        mattermost_contract="Mattermost history indexing is intentionally not faked in issue #30.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=("docs/mattermost-adapter.md:Issue #8 verification tiers",),
        guarantees=frozenset({"no_fake_connectors", "shared_full_corpus"}),
        fallback="Use existing Onyx retrieval until issue #36 ships an authentic Mattermost connector.",
    ),
    SlackToMattermostCapability(
        key="channel_reference_filtering",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack channel-reference search filtering.",
        mattermost_contract="Mattermost channel-reference filters require authentic indexed Mattermost history first.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "backend/onyx/context/search/federated/slack_search.py:resolve_channel_references",
        ),
        guarantees=frozenset({"no_fake_connectors", "shared_full_corpus"}),
        fallback="Do not narrow retrieval by unindexed Mattermost channel claims until issue #37 ships.",
    ),
    SlackToMattermostCapability(
        key="complete_thread_context",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack thread context fetch for answer grounding.",
        mattermost_contract="Mattermost follows adapter-owned Onyx sessions now; complete channel-thread history loading is tracked separately.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "backend/onyx/onyxbot/mattermost/session.py:get_or_create_mattermost_chat_target",
            "backend/onyx/db/mattermost_bot.py:get_or_create_mattermost_thread_mapping",
            "backend/onyx/context/search/federated/slack_search.py:fetch_thread_contexts_with_rate_limit_handling",
        ),
        guarantees=frozenset({"replay_safe", "shared_full_corpus"}),
        fallback="Use current Onyx chat-session context until issue #38 ships full Mattermost thread context.",
    ),
    SlackToMattermostCapability(
        key="attachment_handling",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack files attached to bot turns and cited sources.",
        mattermost_contract="Mattermost file metadata, bytes, checksum, and stable user-file IDs are recorded once for event attachments.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/client.py:get_file_info",
            "backend/onyx/onyxbot/mattermost/handler.py:_save_mattermost_attachments",
            "backend/onyx/db/mattermost_bot.py:record_mattermost_attachment",
        ),
        guarantees=frozenset({"replay_safe", "shared_full_corpus"}),
    ),
    SlackToMattermostCapability(
        key="admin_seafile_mutation",
        area=SlackCapabilityArea.ADMINISTRATION,
        slack_capability="Slack/admin action controls can mutate connected systems with explicit authority.",
        mattermost_contract="Only a freshly resolved Mattermost system_admin can route a typed Seafile mutation through the controlled platform gateway.",
        status=MattermostParityStatus.MATTERMOST_NATIVE_EQUIVALENT,
        evidence=(
            "backend/onyx/onyxbot/mattermost/mutations.py:MattermostMutationAdapter",
            "backend/onyx/onyxbot/mattermost/handler.py:dispatch_mattermost_mutation",
        ),
        guarantees=frozenset({"current_system_admin_only", "replay_safe"}),
    ),
    SlackToMattermostCapability(
        key="non_admin_seafile_mutation",
        area=SlackCapabilityArea.ADMINISTRATION,
        slack_capability="Slack users may trigger configured workflow buttons depending on app policy.",
        mattermost_contract="Ordinary Mattermost members can read and summarize but cannot create, overwrite, move, delete, or promote Seafile content.",
        status=MattermostParityStatus.POLICY_DIFFERENCE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/mutations.py:MattermostMutationAdapter",
            "docs/mattermost-adapter.md:Controlled mutation boundary",
        ),
        guarantees=frozenset({"current_system_admin_only", "shared_full_corpus"}),
        fallback="Return a clear permission denial before gateway execution.",
    ),
    SlackToMattermostCapability(
        key="health_delivery_observability",
        area=SlackCapabilityArea.ADMINISTRATION,
        slack_capability="Slack bot delivery, retry, and health observability.",
        mattermost_contract="Release-line Mattermost records durable event states and bounded retries, but dashboard observability is not shipped yet.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "backend/onyx/db/mattermost_bot.py:MattermostEventState",
            "backend/onyx/onyxbot/mattermost/client.py:_request_json",
        ),
        guarantees=frozenset({"replay_safe"}),
        fallback="Inspect durable event rows/logs until issue #42 ships operator health views.",
    ),
)


def matrix_by_key() -> dict[str, SlackToMattermostCapability]:
    return {entry.key: entry for entry in MATTERMOST_SLACK_PARITY_MATRIX}


def validate_mattermost_slack_parity_matrix() -> tuple[str, ...]:
    problems: list[str] = []
    seen_keys: set[str] = set()
    for entry in MATTERMOST_SLACK_PARITY_MATRIX:
        if entry.key in seen_keys:
            problems.append(f"duplicate key: {entry.key}")
        seen_keys.add(entry.key)
        if not entry.evidence:
            problems.append(f"{entry.key} has no evidence")
        if entry.status is MattermostParityStatus.PLATFORM_GAP and not entry.fallback:
            problems.append(f"{entry.key} platform gap needs fallback")
        if entry.status is MattermostParityStatus.POLICY_DIFFERENCE and not (
            entry.fallback or entry.guarantees
        ):
            problems.append(f"{entry.key} policy difference needs boundary")
    return tuple(problems)
