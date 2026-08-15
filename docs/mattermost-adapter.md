# Mattermost adapter contract and Slack parity matrix

For the v4.5.6-derived image, provenance, compatibility evidence, deployment gate, and rollback, see [Mattermost adapter release for Onyx v4.5.6](mattermost-v4.5.6-release.md).

## Scope

The Mattermost adapter gives Onyx a native bot interface for the OneQode proof of concept.
It keeps Onyx as the only agent brain. Mattermost supplies events, users, channels, posts,
threads, post updates, reactions, and file links.

This contract covers credential-independent behavior only. Production bot credentials,
production `@orka` usage, and production cutover are out of scope.

## Shared-knowledge-scope identity boundary

The PoC uses one configured Onyx persona and one shared knowledge scope for approved testers.
Every approved Mattermost tester uses the same Onyx persona and document-access scope.
The adapter must not try to mirror each Mattermost user's private Onyx permissions.

The user identity boundary is:

- Mattermost user identity is used for attribution, audit text, feedback attribution, and
  stable session keys.
- Onyx authorization uses the configured service identity for the shared PoC knowledge scope.
- The adapter must not treat a Mattermost user ID, username, nickname, or email as proof of
  extra Onyx document permission.
- The adapter must not request or store personal Onyx inference credentials for Mattermost users.
- The adapter must not use Hermes or Codex OAuth tokens as Onyx inference credentials.
- A Mattermost email can map to an Onyx user only when a later credentialed implementation
  explicitly verifies that mapping through Onyx-owned identity data.
- If per-user permissions become required, that is a new authority boundary and is out of
  this issue's scope.

### Controlled mutation boundary

Read/retrieval remains on the existing shared full-corpus path. The mutation
boundary must not add per-user, per-folder, or role-based retrieval filtering.
Ordinary members may continue to search, extract, summarize, compare, and use
thread attachments temporarily.

Every create, overwrite/update, rename/move, delete, or attachment promotion is
a mutation. The adapter performs a fresh server API lookup for every mutation
using the trusted sender ID from the Mattermost event. Only the exact current
Mattermost `system_admin` role authorizes routing. Channel admin and team admin
roles do not grant mutation authority. Prompt text, usernames, display names,
event claims, channel claims, and prior role observations never grant authority.
Lookup failure, identity mismatch, or immediate role removal fails closed before
tool execution with a clear permission response.

Authorized requests route only through the controlled platform gateway. The
adapter forwards the trusted Mattermost requester ID, server-resolved username,
channel, root thread, source post, action, source/destination, origin, explicit
confirmation, and expected revision. Overwrite, move, and delete require explicit
confirmation; existing-file mutations require the current expected revision.
Attachment promotion is a mutation and receives the same current-role check.
The platform gateway independently re-resolves authority and owns canonical
Seafile preconditions, mutation, read-back verification, and immutable audit.
Onyx exposes no direct Seafile mutation transport.

Production mutation commands use the explicit wire prefix
`!onyx-seafile-mutate ` followed by one JSON object containing exactly
`action`, `repo_id`, `path`, `expected_revision`, `content`, `destination_path`,
`confirmed`, and `scope_prefix`. The adapter derives the origin as `chat_command`
and derives requester and post/thread correlation from the normalized server
event. Supplying origin, requester, channel, post, root, or any other unknown
field fails closed before any identity lookup. Types are validated exactly; in
particular, booleans are not accepted as revisions or strings.

The Mattermost service enables mutation routing only when
`MATTERMOST_MUTATION_GATEWAY_FACTORY` names an installed factory as
`module:callable`. The factory returns the authoritative Orka platform gateway;
the Onyx bridge imports `actions.seafile` from that installed platform package
and constructs its exact `MattermostMutationContext`, `SeafileActionRequest`, and
enum instances at the call boundary. The platform package/process therefore
owns gateway assembly, Seafile transport, independent current-role resolution,
verification, and audit. If the factory or authoritative package is unavailable,
service startup fails closed. With the setting absent, ordinary reads and chat
remain enabled while explicit mutation commands receive a gateway-unavailable
denial.

The parity contract is that Slack user-specific behavior maps to shared-scope Mattermost behavior
unless this document says otherwise.

## Required Mattermost event classes

The adapter must accept only these event classes for the PoC:

| Event class | Mattermost primitive | Required behavior |
| --- | --- | --- |
| Direct message post | `posted` event where `channel_type` is `D` | Respond without requiring a bot mention. Use the DM session key. |
| Channel mention post | `posted` event where text mentions the bot in a public or private channel where both sender and bot are current members | Respond in the post thread. Remove the mention from the query text. Optional emergency channel/team/user restrictions may narrow access. |
| Root allowlisted post | `posted` event in a member-authorized channel when config permits root questions without mention | Respond in the post thread. This is optional per channel config. |
| Thread reply followup | `posted` event with a non-empty `root_id` in a thread already owned by the adapter | Continue the existing Onyx session without requiring a new mention. |
| Post update retry target | `post_edited` or an explicit retry action against an adapter-owned root post | Re-run the current thread request only when the action comes from an approved tester. |
| Reaction feedback | `reaction_added` or interactive feedback action on an adapter answer post | Record like/dislike feedback against the related Onyx chat message. |
| Post delete/tombstone | `post_deleted` for a user post or adapter answer | Do not delete Onyx chat history. Stop future thread ownership only if the adapter root is gone. |
| Bot/self post | `posted` from the Mattermost bot user or adapter integration user | Ignore to prevent loops. |
| Non-content/system event | Channel joins, leaves, preference changes, typing, status, and other non-post events | Ignore. |

All accepted event processing must be idempotent. The adapter must deduplicate at least by the
Mattermost event sequence or event ID when present. If it is absent, use `post_id` plus event type.

## Exact session keys

Mattermost IDs are opaque strings. Do not parse or timestamp-normalize them.

### DM session key

Use one Onyx chat session per Mattermost DM channel:

```text
mattermost:dm:{team_id}:{channel_id}
```

Rules:

- `team_id` is the Mattermost team ID when present.
- Use `team_id=global` when Mattermost omits a team for a DM event.
- `channel_id` is the Mattermost DM channel ID.
- Do not include user IDs in the key. The DM channel is the stable conversation identity.

### Channel root session key

Use one Onyx chat session per root post in an allowlisted channel:

```text
mattermost:channel:{team_id}:{channel_id}:{root_post_id}
```

Rules:

- For a root post, `root_post_id` is the post's own `id`.
- For a reply, `root_post_id` is the event post's `root_id`.
- Never substitute `parent_id`. Mattermost threads are rooted by `root_id`.

### Reply session key

A reply does not create a different session key. It resolves to its root session key:

```text
mattermost:channel:{team_id}:{channel_id}:{root_id}
```

If a reply has no `root_id`, treat it as a root post and use its `id` as `root_post_id`.

## Slack-to-Mattermost parity matrix

| Slack capability | Slack primitive | Mattermost primitive | Mattermost contract |
| --- | --- | --- | --- |
| Direct message | `message` with `channel_type=im` | `posted` in channel type `D` | Eligible when both sender and bot are current DM channel members. Session key is `mattermost:dm:{team_id}:{channel_id}`. |
| Explicit channel mention | `app_mention` or bot user tag in `message` | `posted` containing `@bot` or bot user mention token | Eligible when both sender and bot are current channel members. Strip the mention before sending text to Onyx. |
| Thread followup | `message` with `thread_ts` | `posted` with `root_id` | Continue the root session when the root is adapter-owned. |
| Root post answer | Slack channel config can permit non-mentions | Allowlisted channel config can permit root questions | Disabled by default. If enabled, respond to root posts only, not all thread chatter. |
| Socket/event dedupe | Slack event envelope and message timestamps | Mattermost event ID/sequence, then `post_id:event_type` fallback | Store enough dedupe state to avoid duplicate Onyx messages and duplicate posts. |
| Streaming answer | Slack `chat.update` on a message | Mattermost `UpdatePost` on one bot post | Create one placeholder post, then update it with answer chunks. |
| Citations/source links | Slack blocks and text links | Markdown links in post body | Preserve citation numbers and source URLs. Use plain Markdown when rich blocks are unavailable. |
| Actions | Slack block actions | Mattermost interactive message actions or reaction fallback | Support retry and feedback. Degrade to command/reaction fallback when interactive actions are unavailable. |
| Feedback | Slack like/dislike block actions | Reaction or interactive feedback action | Map to Onyx chat-message feedback with Mattermost user attribution. |
| Visible failures | Thread response or ephemeral message | Thread reply from bot | Post a concise failure in the thread when processing fails after acknowledgement. |
| Authorization | Slack channel config | Mattermost channel membership API plus optional channel/team/user restrictions | Membership lookup is the fail-closed boundary. Empty restrictions allow any channel/DM where both sender and bot are current members. |
| Bot-loop prevention | Ignore own Slack bot IDs | Ignore Mattermost bot user/integration user IDs | Never process the adapter's own posts. |

## Streaming model

1. Acknowledge the event quickly so Mattermost does not retry because of adapter latency.
2. Create one placeholder bot post in the target channel and thread.
3. Send the user text and conversation context to the configured Onyx persona.
4. Update the placeholder post with partial answer text as chunks arrive.
5. Replace the placeholder with the final answer plus citations.
6. If streaming fails after a partial answer, update the post with a visible failure note and keep the thread open.

The adapter must not create one Mattermost post per token or per chunk.

## Citations and source links

Citations must preserve Onyx citation numbers. Render sources as Markdown links when Mattermost
allows links:

```text
Answer text with [1] references.

Sources:
[1] Document title - https://example.test/doc
```

If a document has no link, show the semantic identifier or document title without making a link.
Never expose hidden connector credentials in source URLs.

## Actions and feedback

The adapter must support these logical actions:

- retry answer for this thread;
- like answer;
- dislike answer;
- open cited source link when Mattermost renders it.

Interactive buttons are preferred. If Mattermost interactive actions are not configured, reaction
or slash-command fallback is acceptable for retry and feedback. The fallback must still map to the
same Onyx chat message.

## Failure contract

Failures must be visible and bounded:

- Duplicate events must not create duplicate Onyx messages or duplicate Mattermost posts.
- Events must be ignored when the bot or sender is no longer a current channel member, or when the membership API cannot prove membership.
- Optional channel/team/user restrictions narrow access only when configured; empty restrictions do not reject valid members.
- Missing configured persona or shared service identity must fail loudly at startup or first use.
- Onyx API failure after event acknowledgement must post one thread failure message.
- Mattermost API rate limits must retry with bounded backoff.
- Repeated update failures must stop streaming and leave a final visible failure in the thread when possible.

## Credential and cutover boundary

No external credential is required to verify this contract. Tests use static fixtures only.

Credential-gated work starts only when a later issue needs a live Mattermost API call. That later
work must use a separate test bot and private test channel. It must not use the production `@orka`
account or production Mattermost runtime.

## Issue #8 verification tiers

Credential-independent verification runs the mocked Mattermost adapter end-to-end path in
`backend/tests/integration/tests/mattermost_bot/test_mattermost_bot_e2e.py`. It covers DM routing,
channel mention routing, thread follow-up continuity, citation rendering, single-post streaming
updates, reaction feedback mapping, and replay deduplication without contacting Mattermost.

Live disposable Mattermost verification is gated on separate test credentials. Required secret names
are `MATTERMOST_BOT_URL`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_BOT_PERSONA_ID`, and
`MATTERMOST_BOT_USER_ID`. `MATTERMOST_SLASH_COMMAND_TOKEN` is a bootstrap-only
input: the service imports it into `mattermost_slash_command_config`, then
steady-state `/orka` authorization uses the encrypted per-instance/bot DB row
managed through `/manage/admin/mattermost/slash-command`. Optional emergency narrowing controls are
`MATTERMOST_BOT_ALLOWED_CHANNEL_IDS`, `MATTERMOST_BOT_ALLOWED_TEAM_IDS`,
`MATTERMOST_BOT_APPROVED_USER_IDS`, and `MATTERMOST_BOT_ROOT_POST_CHANNEL_IDS`. Empty channel,
team, and user restrictions mean membership-based access. These values must point to a disposable
test bot and test channel only.
