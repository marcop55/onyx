"""Replay-safe Mattermost attachment ingestion for Onyx chat turns."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from io import BytesIO
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.configs.app_configs import MAX_ALLOWED_UPLOAD_SIZE_MB
from onyx.configs.constants import FileOrigin
from onyx.db.enums import UserFileStatus
from onyx.db.mattermost_bot import record_mattermost_attachment
from onyx.db.models import MattermostAttachment, MattermostEventState, UserFile
from onyx.file_processing.file_types import OnyxMimeTypes
from onyx.file_store.file_store import get_default_file_store
from onyx.file_store.models import FileDescriptor
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import NormalizedMattermostEvent
from onyx.onyxbot.mattermost.streaming import MattermostStreamingClient
from onyx.server.query_and_chat.chat_utils import mime_type_to_chat_file_type

_MATTERMOST_FALLBACK_MIME_TYPE = "application/octet-stream"


def _normalize_mime_type(mime_type: str | None) -> str:
    if not mime_type:
        return _MATTERMOST_FALLBACK_MIME_TYPE
    return mime_type.split(";", 1)[0].strip().lower()


def _validate_mattermost_attachment_metadata(
    *,
    file_id: str,
    source_post_id: str,
    metadata_file_id: str,
    metadata_post_id: str,
    mime_type: str,
    size_bytes: int | None,
) -> None:
    if metadata_file_id != file_id or metadata_post_id != source_post_id:
        raise MattermostClientError(
            f"Mattermost file {file_id} does not belong to source post {source_post_id}"
        )
    if mime_type not in OnyxMimeTypes.ALLOWED_MIME_TYPES:
        raise MattermostClientError(f"unsupported Mattermost file type: {mime_type}")
    if size_bytes is None:
        return
    if size_bytes < 0:
        raise MattermostClientError("Mattermost file size cannot be negative")
    max_size_bytes = MAX_ALLOWED_UPLOAD_SIZE_MB * 1024 * 1024
    if max_size_bytes > 0 and size_bytes > max_size_bytes:
        raise MattermostClientError(
            f"Mattermost file exceeds {MAX_ALLOWED_UPLOAD_SIZE_MB} MB upload limit"
        )


def _validate_mattermost_attachment_bytes(
    *,
    file_id: str,
    expected_size_bytes: int | None,
    content: bytes,
) -> None:
    if expected_size_bytes is None:
        return
    actual_size_bytes = len(content)
    if actual_size_bytes != expected_size_bytes:
        raise MattermostClientError(
            "Mattermost file size mismatch for "
            f"{file_id}: expected {expected_size_bytes}, got {actual_size_bytes}"
        )


async def save_mattermost_attachments(
    *,
    client: MattermostStreamingClient,
    db_session: Session,
    event: NormalizedMattermostEvent,
    ledger_event: MattermostEventState,
    service_user_id: UUID,
    get_file_store: Callable[[], Any] | None = None,
    record_attachment: Callable[..., MattermostAttachment] | None = None,
) -> list[FileDescriptor]:
    descriptors: list[FileDescriptor] = []
    if not event.file_ids:
        return descriptors
    file_store = (get_file_store or get_default_file_store)()
    record_attachment_fn = record_attachment or record_mattermost_attachment
    for file_id in event.file_ids:
        existing_attachment = db_session.scalar(
            select(MattermostAttachment).where(
                MattermostAttachment.event_id == ledger_event.id,
                MattermostAttachment.mattermost_file_id == file_id,
                MattermostAttachment.file_store_id.is_not(None),
                MattermostAttachment.user_file_id.is_not(None),
            )
        )
        if existing_attachment is not None:
            descriptors.append(
                {
                    "id": existing_attachment.file_store_id or "",
                    "type": mime_type_to_chat_file_type(existing_attachment.mime_type),
                    "name": existing_attachment.filename,
                    "user_file_id": str(existing_attachment.user_file_id),
                }
            )
            continue
        info = await client.get_file_info(file_id)
        mime_type = _normalize_mime_type(info.mime_type)
        _validate_mattermost_attachment_metadata(
            file_id=file_id,
            source_post_id=event.post_id,
            metadata_file_id=info.id,
            metadata_post_id=info.post_id,
            mime_type=mime_type,
            size_bytes=info.size_bytes,
        )
        content = await client.download_file(file_id)
        _validate_mattermost_attachment_bytes(
            file_id=file_id,
            expected_size_bytes=info.size_bytes,
            content=content,
        )
        checksum = hashlib.sha256(content).hexdigest()
        stable_file_store_id = (
            f"mattermost/{ledger_event.instance_id}/{event.channel_id}/{file_id}"
        )
        stored_file_id = file_store.save_file(
            content=BytesIO(content),
            display_name=info.filename,
            file_origin=FileOrigin.USER_FILE,
            file_type=mime_type,
            file_id=stable_file_store_id,
        )
        stable_user_file_id = uuid5(NAMESPACE_URL, stable_file_store_id)
        user_file = db_session.scalar(
            select(UserFile).where(UserFile.id == stable_user_file_id)
        )
        if user_file is None:
            user_file = UserFile(
                id=stable_user_file_id,
                user_id=service_user_id,
                file_id=stored_file_id,
                name=info.filename,
                token_count=0,
                file_type=mime_type,
                status=UserFileStatus.COMPLETED,
                content_type=mime_type,
            )
            db_session.add(user_file)
            db_session.commit()
        record_attachment_fn(
            db_session,
            event_id=ledger_event.id,
            mattermost_file_id=info.id,
            source_post_id=info.post_id or event.post_id,
            uploader_user_id=info.uploader_user_id,
            filename=info.filename,
            mime_type=mime_type,
            size_bytes=info.size_bytes,
            sha256=checksum,
            channel_id=event.channel_id,
            root_post_id=event.root_post_id,
            create_at=info.create_at,
            file_store_id=stored_file_id,
            user_file_id=user_file.id,
        )
        descriptors.append(
            {
                "id": stored_file_id,
                "type": mime_type_to_chat_file_type(mime_type),
                "name": info.filename,
                "user_file_id": str(user_file.id),
            }
        )
    return descriptors
