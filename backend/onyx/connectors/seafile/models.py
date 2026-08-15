from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SeafileLibrary(BaseModel):
    id: str
    name: str


class SeafileRemoteFile(BaseModel):
    library_id: str
    library_name: str
    path: str
    id: str
    name: str
    size: int | None = None
    mtime: datetime | None = None
    modifier_email: str | None = None
    modifier_name: str | None = None
    revision_id: str | None = None
    content_type: str | None = None
    download_url: str | None = None
