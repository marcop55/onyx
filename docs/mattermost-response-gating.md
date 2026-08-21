# Mattermost response gating

For the adapter contract and the Slack parity matrix, see
[Mattermost adapter contract and Slack parity matrix](mattermost-adapter.md).

## Scope

This document defines when the Mattermost adapter is allowed to answer, and how the bot
reports whether it can still hear events. It covers channel opt-in, direct messages, thread
follow-ups, and the liveness signal the container health check depends on.

Retrieval behavior, persona selection, attachment placement, and answer formatting are out of
scope.

## Problem

Two defects let the bot answer where it was not wanted.

The first was mention detection. Channel gating inferred "the bot was mentioned" from
`self._strip_bot_mention(text) != text`. That strip also collapses whitespace, so any post
that was not already whitespace-canonical compared unequal and was classified as a mention.
The classification returned before `respond_tag_only` was ever read, so a channel configured
for mention-only answers replied to any post containing a newline, a double space, or leading
or trailing whitespace. In practice that is almost every multi-line or markdown post. This is
fixed separately in `fix: detect Mattermost mentions without relying on whitespace`.

The second is the subject of this document. Channel participation is implicit. An empty
`MATTERMOST_BOT_ALLOWED_CHANNEL_IDS` means allow-all, and a channel with no configuration row
of its own inherits the bot's default row through the fallback in
`fetch_mattermost_channel_config_for_bot_and_channel`. A channel therefore never has to be
configured to receive answers, and the default row silently grants participation everywhere the
bot is a member.

A third defect affects trust in the deployment rather than the answers. `ready_event` is set
when the listener object is constructed, not when a websocket connection succeeds, and
`normalized_events` catches every exception and reconnects with backoff. When credentials stop
working at runtime the listener loops forever, `/health` continues to return `200 ok`, and the
container reports healthy while the bot is deaf.

## Gating model

Channels are default-deny. Direct messages are default-allow.

| Surface | Behavior |
| --- | --- |
| Direct message | Always allowed. `approved_user_ids` still applies when configured. |
| Channel with its own enabled config row | Allowed. `respond_tag_only` decides mention-only versus all root posts. |
| Channel with no config row of its own | New interactions ignored: root posts, mentions, and thread replies. Follow-up events on work the adapter already owns are governed by ownership instead; see [Follow-up events](#follow-up-events). |
| Default config row | Template that new channel rows inherit. Grants no participation on its own. |

A channel is opted in when the bot has an enabled `mattermost_channel_config` row whose
`channel_id` matches the channel, and that row is not marked `disabled`. Opt-in is therefore
self-service in the admin UI, takes effect without a restart, and lives in the same record as
`respond_tag_only`.

`MATTERMOST_BOT_ALLOWED_CHANNEL_IDS` and `MATTERMOST_BOT_ALLOWED_TEAM_IDS` are retained as an
optional outer bound. When either is set, a channel must satisfy it *and* have a row. When both
are empty they impose no restriction, because the row requirement is now the gate.

Thread follow-ups keep Slack parity: once the bot owns a thread root, replies in that thread are
answered without a repeated mention. This is safe now that a thread can only become owned
through a real mention. Replies arrive as `posted` events, so channel opt-in is checked first: a
thread reply in a channel that was never opted in, or has since had its row removed, is ignored.

### Follow-up events

`post_edited`, `post_deleted`, and `reaction_added` are gated on adapter ownership rather than on
channel opt-in. Mattermost only sends `channel_type` on `posted` events — its websocket broadcast
omits it for the other three — so those events cannot tell a channel from a direct message, and
applying channel opt-in to them would silently break direct message edits and feedback reactions.

Ownership is already required on each of these paths: edits and deletions require the root post
to be in `owned_thread_root_ids`, and a reaction must target a post in
`owned_answer_post_root_ids`. The adapter can only have acquired that ownership somewhere it was
permitted to answer, so ownership is the meaningful gate.

The consequence is that removing a channel's row stops new interactions but does not retract
threads the adapter already owns: editing an already-answered post there can still trigger a
retry. Fully silencing a channel is therefore two steps — remove the row to stop new
interactions, and tombstone its thread mappings to stop follow-ups on existing ones. Tombstoned
roots are rejected before any other channel branch runs.

## Implementation

The allow-all behavior is the default-row fallback, so the fix is to remove that fallback from
the gating path rather than to layer a second gate on top of it.

Split the database lookup:

- A new fetch returns the channel-specific row only, with no fallback to the default row. The
  listener's managed channel config resolver uses it. A `None` result means the channel is not
  opted in.
- `fetch_mattermost_channel_config_for_bot_and_channel` keeps its fallback and its current
  callers, including the interactive action path in `run.py`, which handles button presses on
  messages the bot has already posted.

In the listener, `_is_allowed_channel` becomes `_channel_is_opted_in`, and the direct message
branch moves above it. The ordering matters: the channel check currently runs first, so
default-deny without that move would silence direct messages as well. The `post_edited`,
`post_deleted`, and `reaction_added` paths drop their channel check in favor of the ownership
checks they already perform, for the reasons in [Follow-up events](#follow-up-events).

Two alternatives were rejected. Keeping the fallback and adding a separate
`is_channel_configured` predicate leaves two lookups that can disagree about the same channel.
Hydrating an opted-in channel set at startup is the smallest listener change but goes stale
until the next restart, which is wrong when opt-in is edited in the admin UI.

## Liveness

The listener owns a status record covering `connected`, `last_connected_at`,
`consecutive_failures`, and `last_error`. It is updated when a connection succeeds and in the
reconnect exception branch.

- `ready_event` is set on the first successful connect rather than on listener construction.
- `/health` returns 503 once the listener has been disconnected for longer than a grace window,
  so a brief Mattermost interruption does not flap the container into a restart loop. The window
  is `MATTERMOST_BOT_UNHEALTHY_AFTER_SECONDS`, default 60.
- The health response reports `last_error` so the cause is visible without attaching a debugger.

## Testing

Unit tests follow the existing patterns in `backend/tests/unit/onyx/onyxbot/mattermost/`.

Gating, in `test_listener.py`:

- A channel with no row of its own is ignored for a root post, a mention, and a thread reply in
  an owned thread.
- A channel whose row sets `respond_tag_only: true` answers a mention and ignores an unmentioned
  root post.
- A channel whose row sets `respond_tag_only: false` answers an unmentioned root post.
- A direct message is answered with no channel row present, and with an env allowlist set that
  does not include the direct message channel.
- A channel with a row is ignored when a non-empty env allowlist excludes it.
- A reaction on an owned answer post and an edit of an owned root post are still accepted with no
  channel row present, covering the direct message case where `channel_type` is absent.
- A tombstoned thread root is rejected even when its channel is opted in.

Liveness, in `test_listener.py` and `test_run.py`:

- Status transitions across connect, failure, and reconnect.
- `/health` returns 503 after the grace window has elapsed while disconnected, and 200 once a
  reconnect succeeds.

## Rollout

Channel `5sy8bz6ewbgjprnycz8j1433je` has answer history and no configuration row. Create an
enabled row for it with `respond_tag_only: true` before deploying, so the people already using
the bot there keep tag-triggered answers.

This is instance-specific data rather than schema, so it belongs in the deploy runbook and not
in an Alembic migration.

## Non-goals

- Logging on the reconnect path. The health response exposes the last error, but nothing is
  written to logs, so there is still no history of repeated flapping.
- Re-validating credentials after startup. `client.get_me()` runs once at boot; credentials that
  stop working later surface through the health check rather than through a credential probe.
- Failing closed on malformed configuration, and logging the resolved gating decision at
  startup.
