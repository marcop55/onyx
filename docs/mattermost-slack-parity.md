# Mattermost Slack parity matrix

Generated from `backend/onyx/onyxbot/mattermost/parity.py`. Keep this human-readable matrix aligned with the executable manifest and its unit test.

## Contract boundaries

- Mattermost uses native posts, threads, reactions, files, user lookups, and membership APIs. It does not depend on Slack runtime APIs.
- Current bot-and-sender channel membership is the fail-closed admission boundary for channel, thread, and DM events.
- Admitted Mattermost members use the configured Onyx service identity and shared full-corpus PoC knowledge scope. The adapter must not infer private Onyx document permissions from Mattermost identity.
- Replay, retry, and ambiguous transport outcomes must not duplicate visible answers, files, feedback, or mutations.
- Non-admin Seafile mutation remains unreachable. Only a freshly verified Mattermost `system_admin` can route a typed mutation through the controlled platform gateway.

## Slack source-of-truth evidence

The executable manifest records a Slack owner contract for every key through `slack_owner_contracts_by_key()`. Each contract names exact release-line source paths for the shipped Slack code plus the public API/UI, storage, defaults, validation, authorization, and test evidence dimensions.

- Managed Slack bot/channel configuration: `backend/onyx/server/manage/slack_bot.py`, `backend/onyx/server/manage/models.py`, `backend/onyx/db/models.py:SlackBot`, `backend/onyx/db/models.py:SlackChannelConfig`, `web/src/app/admin/bots/SlackTokensForm.tsx`, and `web/src/app/admin/bots/[bot-id]/channels/SlackChannelConfigFormFields.tsx`.
- Slack runtime answers: `backend/onyx/onyxbot/slack/listener.py`, `backend/onyx/onyxbot/slack/handlers/handle_message.py`, and `backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py`.
- Slack actions and feedback: `backend/onyx/onyxbot/slack/handlers/handle_buttons.py`, `backend/onyx/onyxbot/slack/constants.py`, and `backend/onyx/db/feedback.py`.
- Slack federated search/channel references: `backend/onyx/context/search/federated/slack_search.py` and `backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py:resolve_channel_references`.
- Slack identity/bot-loop controls: `backend/onyx/onyxbot/slack/listener.py:SlackbotHandler`, `backend/onyx/onyxbot/slack/utils.py:get_onyx_bot_auth_ids`, and `backend/onyx/onyxbot/slack/handlers/handle_regular_answer.py`.

## Matrix

| Key | Area | Classification | Mattermost contract | Evidence / fallback |
| --- | --- | --- | --- | --- |
| `bot_instance_configuration` | bot | mattermost_native_equivalent | Mattermost slash command enablement and credentials are stored per Mattermost instance and bot user in encrypted Onyx DB config, with admin API upsert/get controls; environment values are bootstrap-only inputs. | `backend/onyx/db/models.py:MattermostSlashCommandConfig`; `backend/onyx/db/mattermost_bot.py:upsert_mattermost_slash_command_config`; `backend/onyx/server/manage/slack_bot.py:put_mattermost_slash_command_config` |
| `channel_agent_configuration` | channel | policy_difference | One configured persona and one shared PoC knowledge scope are used for admitted Mattermost users. | `backend/onyx/onyxbot/mattermost/handler.py:MattermostHandlerConfig`; fallback: Per-channel Mattermost agent controls are tracked by issue #32. |
| `channel_response_controls` | channel | mattermost_native_equivalent | Mention-only by default, optional root-post channels, optional team/channel/user narrowing, and visible thread replies. | `backend/onyx/onyxbot/mattermost/models.py:MattermostListenerConfig` |
| `direct_message_answer` | interaction | direct_mattermost_feature | Mattermost channel type D answers without requiring a mention and uses the DM session key. | `backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer`; durable event ledger |
| `explicit_channel_mention_answer` | interaction | direct_mattermost_feature | Mattermost public/private channel post with bot mention is stripped and answered in the root thread. | `backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer`; durable event ledger |
| `root_allowlisted_post_answer` | interaction | direct_mattermost_feature | Configured Mattermost root-post channels answer root posts only; thread chatter still needs ownership. | `backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer`; durable event ledger |
| `thread_followup_answer` | interaction | direct_mattermost_feature | Replies under an adapter-owned Mattermost root continue the same Onyx chat session. | `backend/onyx/onyxbot/mattermost/session.py:get_or_create_mattermost_chat_target`; durable event ledger |
| `slash_command_entrypoint` | interaction | direct_mattermost_feature | Mattermost-native `/orka` slash commands validate the managed per-instance/bot command token, re-check bot and sender channel membership, and route `ask`/`sources` through the durable Onyx handler while `help`/`status` stay ephemeral. | `backend/onyx/onyxbot/mattermost/commands.py:handle_mattermost_slash_command`; `backend/onyx/onyxbot/mattermost/commands.py:MattermostSlashCommandControl`; `backend/onyx/db/models.py:MattermostSlashCommandConfig`; durable event ledger |
| `ephemeral_or_private_answer` | interaction | mattermost_native_equivalent | Mattermost-native ephemeral posts deliver slash-command and configured private answers only to the originating sender, with durable delivery mode and terminal outcome checkpoints so replay cannot promote them to public posts or rerun the model. | `backend/onyx/onyxbot/mattermost/client.py:create_ephemeral_post`; `backend/onyx/onyxbot/mattermost/streaming.py:stream_mattermost_ephemeral_answer`; `backend/onyx/onyxbot/mattermost/handler.py:_resolve_delivery_mode`; `backend/onyx/db/mattermost_bot.py:checkpoint_mattermost_terminal_outcome`; `backend/tests/unit/onyx/onyxbot/mattermost/test_ephemeral_responses.py`; durable event ledger |
| `interactive_retry_control` | interaction | mattermost_native_equivalent | Post edits against adapter-owned roots act as replay-safe retry targets; interactive posts are tracked by issue #35. | fallback: Edit the owned root post, or use the later interactive action once configured. |
| `streaming_single_post_answer` | interaction | direct_mattermost_feature | Mattermost creates one placeholder post and updates that one post with chunks and final citations. | `backend/onyx/onyxbot/mattermost/streaming.py:stream_mattermost_answer` |
| `citations_and_source_links` | search | mattermost_native_equivalent | Mattermost renders answer citations and sources as Markdown links without hidden credentials. | `backend/onyx/onyxbot/mattermost/formatting.py:format_mattermost_answer` |
| `chat_feedback` | feedback | direct_mattermost_feature | Mattermost reactions on owned answer posts map to Onyx chat-message feedback with Mattermost attribution. | `backend/onyx/onyxbot/mattermost/listener.py:_normalize_reaction_feedback` |
| `standard_answer_workflow` | feedback | platform_gap | No direct release-line Mattermost standard-answer workflow is shipped yet. | fallback: Use regular Mattermost answers until issue #41 ships standard-answer parity. |
| `followup_workflow` | feedback | platform_gap | No direct release-line Mattermost follow-up workflow is shipped yet. | fallback: Use visible thread replies until issue #41 ships follow-up controls. |
| `bot_loop_prevention` | identity | direct_mattermost_feature | Mattermost ignores configured bot user and integration user posts before handling. | `backend/onyx/onyxbot/mattermost/listener.py:MattermostEventNormalizer` |
| `membership_authorization` | identity | direct_mattermost_feature | Every normalized event rechecks bot and sender channel membership and fails closed on lookup failure. | `backend/onyx/onyxbot/mattermost/listener.py:_authorize_and_attribute_event` |
| `shared_full_corpus_retrieval` | search | policy_difference | Admitted Mattermost members use the configured service identity with `allowed_tool_ids=None` and `bypass_acl=False`. | `backend/onyx/onyxbot/mattermost/handler.py:_stream_mattermost_answer_packets` |
| `per_user_private_onyx_permissions` | identity | platform_gap | Mattermost PoC must not infer Onyx private permissions from Mattermost identity. | fallback: Use the configured service identity and shared PoC knowledge scope until a credentialed per-user mapping is explicitly designed. |
| `mattermost_history_search_connector` | search | platform_gap | Mattermost history indexing is intentionally not faked in issue #30. | fallback: Use existing Onyx retrieval until issue #36 ships an authentic Mattermost connector. |
| `channel_reference_filtering` | search | platform_gap | Mattermost channel-reference filters require authentic indexed Mattermost history first. | fallback: Do not narrow retrieval by unindexed Mattermost channel claims until issue #37 ships. |
| `complete_thread_context` | search | platform_gap | Mattermost follows adapter-owned Onyx sessions now; complete channel-thread history loading is tracked separately. | fallback: Use current Onyx chat-session context until issue #38 ships full Mattermost thread context. |
| `attachment_handling` | search | direct_mattermost_feature | Mattermost file metadata, bytes, checksum, and stable user-file IDs are recorded once for event attachments. | `backend/onyx/onyxbot/mattermost/handler.py:_save_mattermost_attachments` |
| `admin_seafile_mutation` | administration | mattermost_native_equivalent | Only a freshly resolved Mattermost system_admin can route a typed Seafile mutation through the controlled platform gateway. | `backend/onyx/onyxbot/mattermost/mutations.py:MattermostMutationAdapter` |
| `non_admin_seafile_mutation` | administration | policy_difference | Ordinary Mattermost members can read and summarize but cannot create, overwrite, move, delete, or promote Seafile content. | fallback: Return a clear permission denial before gateway execution. |
| `health_delivery_observability` | administration | platform_gap | Release-line Mattermost records durable event states and bounded retries, but dashboard observability is not shipped yet. | fallback: Inspect durable event rows/logs until issue #42 ships operator health views. |

## Verification

Run:

```bash
uv run pytest -xq backend/tests/unit/onyx/onyxbot/mattermost/test_parity_contract.py
```
