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
        mattermost_contract="Mattermost slash command enablement and credentials are stored per Mattermost instance and bot user in encrypted Onyx DB config, with admin API upsert/get controls; environment values are bootstrap-only inputs.",
        status=MattermostParityStatus.MATTERMOST_NATIVE_EQUIVALENT,
        evidence=(
            "backend/onyx/db/models.py:MattermostSlashCommandConfig",
            "backend/onyx/db/mattermost_bot.py:upsert_mattermost_slash_command_config",
            "backend/onyx/server/manage/slack_bot.py:put_mattermost_slash_command_config",
            "backend/onyx/onyxbot/mattermost/run.py:get_or_bootstrap_mattermost_slash_command_config",
        ),
        guarantees=frozenset({"managed_credentials", "provider_neutral"}),
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
        mattermost_contract="Mattermost-native /orka slash commands validate the managed per-instance/bot command token, re-check bot and sender channel membership, and route ask/sources through the durable Onyx handler while help/status stay ephemeral.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/commands.py:handle_mattermost_slash_command",
            "backend/onyx/onyxbot/mattermost/commands.py:MattermostSlashCommandControl",
            "backend/onyx/db/models.py:MattermostSlashCommandConfig",
            "backend/onyx/onyxbot/mattermost/run.py:_handle_slash_command_request",
            "backend/tests/unit/onyx/onyxbot/mattermost/test_commands.py",
            *_MEMBERSHIP_EVIDENCE,
            *_LEDGER_EVIDENCE,
        ),
        guarantees=_CHAT_GUARANTEES,
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
        mattermost_contract="Mattermost history indexing uses a first-class Onyx connector with stable post/thread IDs, bot-and-sender membership checks, tombstone text for deletions, hierarchy nodes, canonical post links and file metadata.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/connectors/mattermost/connector.py:MattermostConnector",
            "backend/onyx/connectors/mattermost/models.py:MattermostPost",
            "backend/tests/unit/onyx/connectors/mattermost/test_mattermost_connector.py",
        ),
        guarantees=frozenset(
            {
                "no_fake_connectors",
                "membership_fail_closed",
                "shared_full_corpus",
                "replay_safe",
            }
        ),
    ),
    SlackToMattermostCapability(
        key="channel_reference_filtering",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack channel-reference search filtering.",
        mattermost_contract="Mattermost channel-reference filters require authentic indexed Mattermost history first.",
        status=MattermostParityStatus.PLATFORM_GAP,
        evidence=(
            "backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:resolve_channel_references",
        ),
        guarantees=frozenset({"no_fake_connectors", "shared_full_corpus"}),
        fallback="Do not narrow retrieval by unindexed Mattermost channel claims until issue #37 ships.",
    ),
    SlackToMattermostCapability(
        key="complete_thread_context",
        area=SlackCapabilityArea.SEARCH,
        slack_capability="Slack thread context fetch for answer grounding.",
        mattermost_contract="Before each Mattermost answer, the adapter fetches the current native thread, injects bounded chronological unseen non-deleted posts with sender attribution, and keeps current/replayed posts out of additional context.",
        status=MattermostParityStatus.DIRECT_MATTERMOST_FEATURE,
        evidence=(
            "backend/onyx/onyxbot/mattermost/context.py:build_mattermost_turn_context",
            "backend/onyx/onyxbot/mattermost/client.py:get_thread_posts",
            "backend/onyx/onyxbot/mattermost/handler.py:_stream_mattermost_answer_packets",
            "backend/onyx/db/mattermost_bot.py:get_loaded_mattermost_context_post_ids",
            "backend/onyx/context/search/federated/slack_search.py:fetch_thread_contexts_with_rate_limit_handling",
        ),
        guarantees=frozenset({"replay_safe", "shared_full_corpus"}),
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


_SLACK_MANAGED_CONFIG_CONTRACT = (
    "source: backend/onyx/server/manage/slack_bot.py:create_bot, patch_bot, _form_channel_config; web/src/app/admin/bots/SlackTokensForm.tsx; web/src/app/admin/bots/[bot-id]/channels/SlackChannelConfigFormFields.tsx",
    "public: backend/onyx/server/manage/slack_bot.py exposes /api/manage/admin/slack-app/bots and /api/manage/admin/slack-app/channel through the manage router",
    "storage: backend/onyx/db/models.py:SlackBot stores encrypted bot/app/user tokens; SlackChannelConfig stores JSONB channel_config, persona_id, standard_answer_categories, enable_auto_filters, is_default",
    "defaults: backend/onyx/server/manage/slack_bot.py:create_bot creates a default channel config with channel_name=None, respond_tag_only=True, enable_auto_filters=False, is_default=True; backend/onyx/db/models.py:ChannelConfig defaults optional flags false",
    "validation: backend/onyx/server/manage/slack_bot.py validates Slack tokens, channel-name uniqueness, answer_filters and document_sets/persona_id exclusivity",
    "authorization: backend/onyx/server/manage/slack_bot.py:create_slack_channel_config, patch_slack_channel_config, delete_slack_channel_config, list_slack_channel_configs, create_bot, patch_bot require onyx.auth.permissions.require_permission with backend/onyx/db/enums.py:Permission.FULL_ADMIN_PANEL_ACCESS for Slack bot and channel CRUD",
    "tests: backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py pins this contract; backend/tests/unit/onyx/onyxbot/test_handle_regular_answer.py pins shipped Slack answer/filter behavior",
)

_SLACK_RUNTIME_ANSWER_CONTRACT = (
    "source: backend/onyx/onyxbot/slack/listener.py:SlackbotHandler; backend/onyx/onyxbot/slack/handlers/handle_message.py:handle_message; backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:handle_regular_answer",
    "public: backend/onyx/onyxbot/slack/listener.py consumes Slack Socket Mode events and slash/block-action payloads, then responds through Slack WebClient posts, updates and ephemeral posts",
    "storage: backend/onyx/db/models.py:SlackChannelConfig stores channel JSONB and persona_id; backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py sends ChatSessionCreationRequest and SendMessageRequest into Onyx chat storage",
    "defaults: backend/onyx/server/manage/models.py:SlackChannelConfigCreationRequest defaults respond_tag_only/respond_to_bots/is_ephemeral/show_continue_in_web_ui/enable_auto_filters/disabled false; backend/onyx/server/manage/slack_bot.py:create_bot default respond_tag_only=True",
    "validation: backend/onyx/onyxbot/slack/config.py:validate_channel_name and backend/onyx/server/manage/models.py validators constrain managed config before runtime use",
    "authorization: backend/onyx/onyxbot/slack/listener.py:build_request_details resolves Slack sender email through onyx.connectors.slack.utils:expert_info_from_slack_id; backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:handle_regular_answer maps that email with onyx.db.users:get_user_by_email, falls back through onyx.auth.users:get_anonymous_user, checks persona access with onyx.db.persona:get_persona_by_id, and calls handle_stream_message_objects with bypass_acl=False",
    "tests: backend/tests/unit/onyx/onyxbot/test_handle_regular_answer.py pins Slack answer, channel-reference and persona-denial behavior; backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py maps the release contract",
)

_SLACK_ACTION_FEEDBACK_CONTRACT = (
    "source: backend/onyx/onyxbot/slack/handlers/handle_buttons.py:handle_generate_answer_button, handle_publish_ephemeral_message_button, handle_slack_feedback, handle_followup_button, handle_followup_resolved_button",
    "public: backend/onyx/onyxbot/slack/listener.py routes Slack block actions and view submissions to button handlers using action IDs from backend/onyx/onyxbot/slack/constants.py",
    "storage: backend/onyx/onyxbot/slack/handlers/handle_buttons.py writes chat-message feedback and doc-retrieval feedback through backend/onyx/db/feedback.py",
    "defaults: backend/onyx/configs/onyxbot_configs.py controls feedback/follow-up emoji and visibility defaults consumed by backend/onyx/onyxbot/slack/handlers/handle_buttons.py",
    "validation: backend/onyx/onyxbot/slack/handlers/handle_buttons.py validates payload action IDs, message IDs, doc IDs, ranks, metadata and Slack user identity before side effects",
    "authorization: backend/onyx/onyxbot/slack/listener.py:process_feedback routes Slack feedback actions; backend/onyx/onyxbot/slack/handlers/handle_buttons.py:handle_slack_feedback resolves the clicking Slack user through onyx.connectors.slack.utils:expert_info_from_slack_id and onyx.db.users:get_user_by_email before feedback attribution; backend/onyx/onyxbot/slack/handlers/handle_buttons.py:handle_followup_button loads configured follow_up_tags/groups through get_slack_channel_config_for_bot_and_channel",
    "tests: backend/tests/unit/onyx/onyxbot/test_handle_regular_answer.py covers adjacent Slack answer contracts; backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py requires this mapped action/feedback evidence",
)

_SLACK_SEARCH_CONNECTOR_CONTRACT = (
    "source: backend/onyx/context/search/federated/slack_search.py; backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:resolve_channel_references",
    "public: backend/onyx/context/search/federated/slack_search.py performs Slack federated search and thread context fetches, while Slack bot channel references become Onyx search tags",
    "storage: backend/onyx/context/search/federated/slack_search.py builds transient federated SearchDoc results; backend/onyx/db/models.py:SlackChannelConfig stores answer_filters and enable_auto_filters",
    "defaults: backend/onyx/configs/app_configs.py supplies Slack thread context limits; backend/onyx/server/manage/models.py defaults answer_filters to [] and enable_auto_filters to False",
    "validation: backend/onyx/server/manage/models.py validates answer_filters against backend/onyx/onyxbot/slack/config.py:VALID_SLACK_FILTERS; resolve_channel_references ignores unresolved channel IDs",
    "authorization: backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:handle_regular_answer checks persona/user access with onyx.db.persona:get_persona_by_id, builds BaseFilters from Slack channel tags, and calls handle_stream_message_objects with bypass_acl=False before Slack search/filter context reaches chat; backend/onyx/context/search/federated/slack_search.py:slack_retrieval runs only from the provided SlackContext and OAuth access_token",
    "tests: backend/tests/unit/onyx/onyxbot/test_handle_regular_answer.py covers channel-reference filters; backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py pins Mattermost fallback boundaries",
)

_SLACK_BOT_IDENTITY_CONTRACT = (
    "source: backend/onyx/onyxbot/slack/listener.py:SlackbotHandler; backend/onyx/onyxbot/slack/utils.py:get_onyx_bot_auth_ids; backend/onyx/onyxbot/slack/handlers/handle_message.py:handle_message",
    "public: backend/onyx/onyxbot/slack/listener.py ignores bot/self events and routes only eligible Slack user events into managed channel configs",
    "storage: backend/onyx/db/models.py:SlackBot stores bot/app/user tokens and enabled state; backend/onyx/db/models.py:SlackChannelConfig stores per-channel persona and response controls",
    "defaults: backend/onyx/server/manage/slack_bot.py:create_bot creates an enabled bot with a default respond_tag_only channel config for all channels and DMs",
    "validation: backend/onyx/server/manage/slack_bot.py validates Slack tokens before storage and backend/onyx/onyxbot/slack/config.py validates duplicate channel names",
    "authorization: backend/onyx/onyxbot/slack/listener.py:prefilter_requests ignores bot/self events using backend/onyx/onyxbot/slack/utils.py:get_onyx_bot_auth_ids; backend/onyx/onyxbot/slack/listener.py:build_request_details resolves Slack user email; backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:handle_regular_answer enforces Onyx persona access for resolved Slack users and falls back to anonymous/service users where public-channel behavior requires it",
    "tests: backend/tests/unit/onyx/onyxbot/test_handle_regular_answer.py pins persona access denial and channel-reference behavior; backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py pins Mattermost identity mapping",
)

_SLACK_OWNER_CONTRACTS_BY_KEY: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        (
            "bot_instance_configuration",
            "channel_agent_configuration",
            "channel_response_controls",
        ),
        _SLACK_MANAGED_CONFIG_CONTRACT,
    ),
    **dict.fromkeys(
        (
            "direct_message_answer",
            "explicit_channel_mention_answer",
            "root_allowlisted_post_answer",
            "thread_followup_answer",
            "slash_command_entrypoint",
            "ephemeral_or_private_answer",
            "interactive_retry_control",
            "streaming_single_post_answer",
            "citations_and_source_links",
            "complete_thread_context",
            "attachment_handling",
            "health_delivery_observability",
        ),
        _SLACK_RUNTIME_ANSWER_CONTRACT,
    ),
    **dict.fromkeys(
        (
            "chat_feedback",
            "standard_answer_workflow",
            "followup_workflow",
        ),
        _SLACK_ACTION_FEEDBACK_CONTRACT,
    ),
    **dict.fromkeys(
        (
            "shared_full_corpus_retrieval",
            "mattermost_history_search_connector",
            "channel_reference_filtering",
        ),
        _SLACK_SEARCH_CONNECTOR_CONTRACT,
    ),
    **dict.fromkeys(
        (
            "bot_loop_prevention",
            "membership_authorization",
            "per_user_private_onyx_permissions",
            "admin_seafile_mutation",
            "non_admin_seafile_mutation",
        ),
        _SLACK_BOT_IDENTITY_CONTRACT,
    ),
}


def matrix_by_key() -> dict[str, SlackToMattermostCapability]:
    return {entry.key: entry for entry in MATTERMOST_SLACK_PARITY_MATRIX}


def slack_owner_contracts_by_key() -> dict[str, tuple[str, ...]]:
    return _SLACK_OWNER_CONTRACTS_BY_KEY.copy()


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
        if entry.key not in _SLACK_OWNER_CONTRACTS_BY_KEY:
            problems.append(f"{entry.key} has no Slack owner contract")
    unknown_owner_contracts = set(_SLACK_OWNER_CONTRACTS_BY_KEY) - seen_keys
    for key in sorted(unknown_owner_contracts):
        problems.append(f"unknown Slack owner contract key: {key}")
    return tuple(problems)
