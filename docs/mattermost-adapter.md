# Mattermost adapter contract and Slack parity matrix

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

The parity contract is that Slack user-specific behavior maps to shared-scope Mattermost behavior
unless this document says otherwise.

## Required Mattermost event classes

The adapter must accept only these event classes for the PoC:

| Event class | Mattermost primitive | Required behavior |
| --- | --- | --- |
| Direct message post | `posted` event where `channel_type` is `D` | Respond without requiring a bot mention. Use the DM session key. |
| Channel mention post | `posted` event where text mentions the bot in an allowlisted public or private channel | Respond in the post thread. Remove the mention from the query text. |
| Root allowlisted post | `posted` event in an allowlisted channel when config permits root questions without mention | Respond in the post thread. This is optional per channel config. |
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
| Direct message | `message` with `channel_type=im` | `posted` in channel type `D` | Always eligible for approved testers. Session key is `mattermost:dm:{team_id}:{channel_id}`. |
| Explicit channel mention | `app_mention` or bot user tag in `message` | `posted` containing `@bot` or bot user mention token | Eligible only in allowlisted channels. Strip the mention before sending text to Onyx. |
| Thread followup | `message` with `thread_ts` | `posted` with `root_id` | Continue the root session when the root is adapter-owned. |
| Root post answer | Slack channel config can permit non-mentions | Allowlisted channel config can permit root questions | Disabled by default. If enabled, respond to root posts only, not all thread chatter. |
| Socket/event dedupe | Slack event envelope and message timestamps | Mattermost event ID/sequence, then `post_id:event_type` fallback | Store enough dedupe state to avoid duplicate Onyx messages and duplicate posts. |
| Streaming answer | Slack `chat.update` on a message | Mattermost `UpdatePost` on one bot post | Create one placeholder post, then update it with answer chunks. |
| Citations/source links | Slack blocks and text links | Markdown links in post body | Preserve citation numbers and source URLs. Use plain Markdown when rich blocks are unavailable. |
| Actions | Slack block actions | Mattermost interactive message actions or reaction fallback | Support retry and feedback. Degrade to command/reaction fallback when interactive actions are unavailable. |
| Feedback | Slack like/dislike block actions | Reaction or interactive feedback action | Map to Onyx chat-message feedback with Mattermost user attribution. |
| Visible failures | Thread response or ephemeral message | Thread reply from bot | Post a concise failure in the thread when processing fails after acknowledgement. |
| Allowlist | Slack channel config | Mattermost channel/team allowlist config | Channel mention/root behavior must be deny-by-default outside configured channels. |
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
- Invalid or non-allowlisted channel events must be ignored.
- Missing configured persona or shared service identity must fail loudly at startup or first use.
- Onyx API failure after event acknowledgement must post one thread failure message.
- Mattermost API rate limits must retry with bounded backoff.
- Repeated update failures must stop streaming and leave a final visible failure in the thread when possible.

## Credential and cutover boundary

No external credential is required to verify this contract. Tests use static fixtures only.

Credential-gated work starts only when a later issue needs a live Mattermost API call. That later
work must use a separate test bot and private test channel. It must not use the production `@orka`
account or production Mattermost runtime.
