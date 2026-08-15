from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MattermostChannel:
    id: str
    name: str
    team_id: str
    display_name: str | None = None


@dataclass(frozen=True)
class MattermostPost:
    id: str
    channel_id: str
    user_id: str
    message: str = ""
    root_id: str = ""
    parent_id: str = ""
    create_at: int | None = None
    update_at: int | None = None
    edit_at: int | None = None
    delete_at: int | None = None
    file_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MattermostFileMetadata:
    id: str
    post_id: str
    user_id: str
    filename: str
    mime_type: str
    size_bytes: int | None = None
    create_at: int | None = None
