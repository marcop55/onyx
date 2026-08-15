from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from onyx.db.models import MattermostEventState
from onyx.onyxbot.mattermost.attachments import save_mattermost_attachments
from onyx.onyxbot.mattermost.client import MattermostClientError
from onyx.onyxbot.mattermost.models import (
    MattermostFileInfo,
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)


def _event(*, file_ids: tuple[str, ...] = ("file-1",)) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-1",
        root_post_id="post-1",
        user_id="user-1",
        text="summarize this",
        raw_event_type="posted",
        file_ids=file_ids,
        dedupe_key="event_id:post-1",
    )


def _ledger_event() -> MattermostEventState:
    return MattermostEventState(
        id=1,
        instance_id="instance-1",
        channel_id="channel-1",
        dedupe_key="event_id:post-1",
        event_type="channel_mention",
        source_post_id="post-1",
        mattermost_pending_post_id="pending-1",
        state="claimed",
    )


@pytest.mark.asyncio
async def test_saves_accepted_mattermost_attachment_with_provenance() -> None:
    db_session = MagicMock()
    db_session.scalar.side_effect = [None, None]
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-2",
            post_id="post-1",
            filename="brief.txt",
            mime_type="text/plain; charset=utf-8",
            size_bytes=11,
            create_at=1786720000123,
        )
    )
    client.download_file = AsyncMock(return_value=b"hello world")
    file_store = MagicMock()
    file_store.save_file.return_value = "stored-file-1"

    with (
        patch(
            "onyx.onyxbot.mattermost.attachments.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.attachments.record_mattermost_attachment"
        ) as mock_record_attachment,
    ):
        descriptors = await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    user_file = db_session.add.call_args.args[0]
    assert descriptors == [
        {
            "id": "stored-file-1",
            "type": "plain_text",
            "name": "brief.txt",
            "user_file_id": str(user_file.id),
        }
    ]
    file_store.save_file.assert_called_once()
    assert file_store.save_file.call_args.kwargs["file_id"] == (
        "mattermost/instance-1/channel-1/file-1"
    )
    assert file_store.save_file.call_args.kwargs["file_type"] == "text/plain"
    mock_record_attachment.assert_called_once()
    assert mock_record_attachment.call_args.kwargs == {
        "event_id": 1,
        "mattermost_file_id": "file-1",
        "source_post_id": "post-1",
        "uploader_user_id": "user-2",
        "filename": "brief.txt",
        "mime_type": "text/plain",
        "size_bytes": 11,
        "sha256": "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        "channel_id": "channel-1",
        "root_post_id": "post-1",
        "create_at": 1786720000123,
        "file_store_id": "stored-file-1",
        "user_file_id": user_file.id,
    }


@pytest.mark.asyncio
async def test_denies_attachment_metadata_for_a_different_mattermost_post() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-1",
            post_id="other-post",
            filename="brief.txt",
            mime_type="text/plain",
            size_bytes=11,
        )
    )
    client.download_file = AsyncMock(return_value=b"hello world")

    with pytest.raises(MattermostClientError, match="does not belong to source post"):
        await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    client.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_reuses_recorded_attachment_without_downloading_again() -> None:
    existing_attachment = MagicMock()
    existing_attachment.file_store_id = "stored-file-1"
    existing_attachment.user_file_id = UUID("00000000-0000-0000-0000-000000000789")
    existing_attachment.mime_type = "text/plain"
    existing_attachment.filename = "brief.txt"
    db_session = MagicMock()
    db_session.scalar.return_value = existing_attachment
    client = MagicMock()
    client.get_file_info = AsyncMock()
    client.download_file = AsyncMock()
    file_store = MagicMock()

    with (
        patch(
            "onyx.onyxbot.mattermost.attachments.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.attachments.record_mattermost_attachment"
        ) as mock_record_attachment,
    ):
        descriptors = await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    assert descriptors == [
        {
            "id": "stored-file-1",
            "type": "plain_text",
            "name": "brief.txt",
            "user_file_id": "00000000-0000-0000-0000-000000000789",
        }
    ]
    client.get_file_info.assert_not_awaited()
    client.download_file.assert_not_awaited()
    file_store.save_file.assert_not_called()
    mock_record_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_unsupported_mattermost_attachment_type_before_download() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-1",
            post_id="post-1",
            filename="installer.exe",
            mime_type="application/x-msdownload",
            size_bytes=4,
        )
    )
    client.download_file = AsyncMock(return_value=b"MZ..")

    with pytest.raises(MattermostClientError, match="unsupported Mattermost file type"):
        await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    client.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_mattermost_attachment_when_downloaded_size_changes() -> None:
    db_session = MagicMock()
    db_session.scalar.return_value = None
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-1",
            post_id="post-1",
            filename="brief.txt",
            mime_type="text/plain",
            size_bytes=12,
        )
    )
    client.download_file = AsyncMock(return_value=b"hello world")
    file_store = MagicMock()

    with (
        patch(
            "onyx.onyxbot.mattermost.attachments.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.attachments.record_mattermost_attachment"
        ) as mock_record_attachment,
        pytest.raises(MattermostClientError, match="file size mismatch"),
    ):
        await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    file_store.save_file.assert_not_called()
    mock_record_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_mattermost_attachment_without_size_metadata_before_download() -> (
    None
):
    db_session = MagicMock()
    db_session.scalar.return_value = None
    client = MagicMock()
    client.get_file_info = AsyncMock(
        return_value=MattermostFileInfo(
            id="file-1",
            uploader_user_id="user-1",
            post_id="post-1",
            filename="brief.txt",
            mime_type="text/plain",
            size_bytes=None,
        )
    )
    client.download_file = AsyncMock(return_value=b"hello world")
    file_store = MagicMock()

    with (
        patch(
            "onyx.onyxbot.mattermost.attachments.get_default_file_store",
            return_value=file_store,
        ),
        patch(
            "onyx.onyxbot.mattermost.attachments.record_mattermost_attachment"
        ) as mock_record_attachment,
        pytest.raises(MattermostClientError, match="file size metadata is required"),
    ):
        await save_mattermost_attachments(
            client=client,
            db_session=db_session,
            event=_event(),
            ledger_event=_ledger_event(),
            service_user_id=UUID("00000000-0000-0000-0000-000000000456"),
        )

    client.download_file.assert_not_awaited()
    file_store.save_file.assert_not_called()
    mock_record_attachment.assert_not_called()
