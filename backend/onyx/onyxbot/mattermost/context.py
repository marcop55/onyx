"""Mattermost thread context loading for bot turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import (
    MattermostPost,
    MattermostUserInfo,
    NormalizedMattermostEvent,
)

MATTERMOST_THREAD_CONTEXT_HEADER = (
    "The following is the current Mattermost thread context before this turn. "
    "Posts are chronological, use latest edits, omit deleted posts, and preserve "
    "sender attribution.\n===================="
)
DEFAULT_MAX_MATTERMOST_THREAD_CONTEXT_POSTS = 50


class MattermostThreadContextFetchError(RuntimeError):
    """Raised when thread context cannot be loaded safely."""


class MattermostThreadContextClient(Protocol):
    async def get_thread_posts(self, root_post_id: str) -> list[MattermostPost]: ...

    async def get_user_info(self, user_id: str) -> MattermostUserInfo: ...


@dataclass(frozen=True)
class MattermostThreadContextPost:
    post_id: str
    user_id: str
    sender: str
    message: str
    create_at: int | None = None
    update_at: int | None = None


@dataclass(frozen=True)
class LoadedMattermostThreadContext:
    text: str
    post_ids: frozenset[str]


async def build_mattermost_turn_context(
    *,
    client: MattermostThreadContextClient,
    event: NormalizedMattermostEvent,
    previously_loaded_post_ids: frozenset[str],
    max_posts: int = DEFAULT_MAX_MATTERMOST_THREAD_CONTEXT_POSTS,
) -> LoadedMattermostThreadContext | None:
    """Return unseen Mattermost thread context to inject before this turn.

    The current event message is sent as the actual user message, so it is not
    repeated in additional context. Previously handled posts are also skipped so
    subsequent turns inject only unseen external thread deltas.
    """

    try:
        posts = await client.get_thread_posts(event.root_post_id)
        context_posts = await _context_posts(
            client=client,
            posts=posts,
            current_post_id=event.post_id,
            previously_loaded_post_ids=previously_loaded_post_ids,
            max_posts=max_posts,
        )
    except MattermostClientError as exc:
        raise MattermostThreadContextFetchError(
            "Mattermost thread context lookup failed"
        ) from exc

    if not context_posts:
        return None

    rendered_posts = [
        f"{post.sender}:\n{post.message}"
        for post in context_posts
        if post.message.strip()
    ]
    if not rendered_posts:
        return None
    return LoadedMattermostThreadContext(
        text=MATTERMOST_THREAD_CONTEXT_HEADER + "\n" + "\n\n".join(rendered_posts),
        post_ids=frozenset(post.post_id for post in context_posts),
    )


async def _context_posts(
    *,
    client: MattermostThreadContextClient,
    posts: list[MattermostPost],
    current_post_id: str,
    previously_loaded_post_ids: frozenset[str],
    max_posts: int,
) -> list[MattermostThreadContextPost]:
    eligible_posts = [
        post
        for post in posts
        if post.id
        and post.id != current_post_id
        and post.id not in previously_loaded_post_ids
        and not post.delete_at
        and post.message.strip()
    ]
    eligible_posts.sort(
        key=lambda post: (post.create_at is None, post.create_at or 0, post.id)
    )
    if max_posts > 0:
        eligible_posts = eligible_posts[-max_posts:]

    user_cache: dict[str, MattermostUserInfo] = {}
    context_posts: list[MattermostThreadContextPost] = []
    for post in eligible_posts:
        user_info = user_cache.get(post.user_id)
        if user_info is None:
            try:
                user_info = await client.get_user_info(post.user_id)
            except MattermostClientError as exc:
                raise MattermostThreadContextFetchError(
                    "Mattermost thread sender attribution lookup failed"
                ) from exc
            user_cache[post.user_id] = user_info
        context_posts.append(
            MattermostThreadContextPost(
                post_id=post.id,
                user_id=post.user_id,
                sender=_render_sender(user_info),
                message=post.message.strip(),
                create_at=post.create_at,
                update_at=post.update_at,
            )
        )
    return context_posts


def _render_sender(user_info: MattermostUserInfo) -> str:
    identity_parts = [part for part in (user_info.username, user_info.id) if part]
    identity = f" ({', '.join(identity_parts)})" if identity_parts else ""
    return f"{user_info.display_name or user_info.username or user_info.id}{identity}"
