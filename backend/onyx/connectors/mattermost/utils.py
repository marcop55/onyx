from __future__ import annotations

from datetime import datetime, timezone

from onyx.connectors.mattermost.models import MattermostPost


def mattermost_millis_to_datetime(timestamp_millis: int | None) -> datetime | None:
    if timestamp_millis is None:
        return None
    return datetime.fromtimestamp(timestamp_millis / 1000, tz=timezone.utc)


def canonical_root_post_id(post: MattermostPost) -> str:
    return post.root_id or post.id


def build_mattermost_document_id(
    team_id: str, channel_id: str, root_post_id: str
) -> str:
    return f"mattermost:{team_id}:{channel_id}:{root_post_id}"


def build_mattermost_post_link(base_url: str, team_id: str, post_id: str) -> str:
    return f"{base_url.rstrip('/')}/{team_id}/pl/{post_id}"


def post_updated_timestamp(post: MattermostPost) -> int | None:
    timestamps = [
        timestamp
        for timestamp in (post.delete_at, post.edit_at, post.update_at, post.create_at)
        if timestamp is not None
    ]
    return max(timestamps) if timestamps else None
