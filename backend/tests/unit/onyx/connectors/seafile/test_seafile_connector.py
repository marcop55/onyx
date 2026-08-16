from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import pytest

import onyx.connectors.seafile.connector as seafile_connector_module
from onyx.configs.constants import DocumentSource
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.connectors.models import ConnectorFailure, Document, TextSection
from onyx.connectors.seafile.connector import SeafileApiClient, SeafileConnector
from onyx.connectors.seafile.models import SeafileLibrary, SeafileRemoteFile
from onyx.file_processing.extract_file_text import ExtractionResult


class FakeSeafileClient:
    def __init__(self) -> None:
        self.validated = False
        self.downloaded_paths: list[tuple[str, str]] = []

    def validate(self) -> None:
        self.validated = True

    def list_libraries(self) -> list[SeafileLibrary]:
        return [SeafileLibrary(id="lib-1", name="OneQode")]

    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]:
        assert library.id == "lib-1"
        yield seafile_file(library)

    def download_bytes(self, file: SeafileRemoteFile) -> bytes:
        self.downloaded_paths.append((file.library_id, file.path))
        return b"# Launch plan\n\nShip first-class Seafile knowledge."


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.content = b"downloaded"

    def json(self) -> Any:
        return self._payload


class FakePaginatedSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.requested_urls: list[str] = []

    def request(self, method: str, url: str, timeout: int) -> FakeResponse:
        self.requested_urls.append(url)
        assert method == "GET"
        assert timeout == 30
        if "p=/&start=0&limit=2" in url:
            return FakeResponse(
                [
                    {"type": "dir", "name": "Strategy", "id": "dir-1"},
                    {"type": "file", "name": "root.pdf", "id": "file-root"},
                ]
            )
        if "p=/&start=2&limit=2" in url:
            return FakeResponse(
                [{"type": "file", "name": "brief.docx", "id": "file-brief"}]
            )
        if "p=/Strategy&start=0&limit=2" in url:
            return FakeResponse(
                [{"type": "file", "name": "Launch Plan.md", "id": "file-launch"}]
            )
        if "/api/v2.1/" in url:
            return FakeResponse({"error": "not found"}, status_code=404)
        return FakeResponse([])

    def get(self, url: str, timeout: int) -> FakeResponse:
        self.requested_urls.append(url)
        assert timeout == 30
        return FakeResponse("https://download.example.com/file")


class FakeDuplicatePageSession(FakePaginatedSession):
    def request(self, method: str, url: str, timeout: int) -> FakeResponse:
        self.requested_urls.append(url)
        assert method == "GET"
        assert timeout == 30
        if "start=0" in url:
            return FakeResponse([{"type": "file", "name": "a.md", "id": "same-id"}])
        if "start=1" in url:
            return FakeResponse(
                [{"type": "file", "name": "a-copy.md", "id": "same-id"}]
            )
        return FakeResponse([])


class FakeUnsafePathSession(FakePaginatedSession):
    def request(self, method: str, url: str, timeout: int) -> FakeResponse:
        self.requested_urls.append(url)
        assert method == "GET"
        assert timeout == 30
        return FakeResponse([{"type": "file", "name": "../escape.md", "id": "escape"}])


class FakeFullPageForeverSession(FakePaginatedSession):
    def request(self, method: str, url: str, timeout: int) -> FakeResponse:
        self.requested_urls.append(url)
        assert method == "GET"
        assert timeout == 30
        return FakeResponse(
            [{"type": "file", "name": f"page-{len(self.requested_urls)}.md"}]
        )


class FakeMultiLibraryClient(FakeSeafileClient):
    def list_libraries(self) -> list[SeafileLibrary]:
        return [
            SeafileLibrary(id="lib-1", name="OneQode"),
            SeafileLibrary(id="lib-2", name="OneQode"),
        ]


class FakeSeafileImageClient(FakeSeafileClient):
    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]:
        yield SeafileRemoteFile(
            library_id=library.id,
            library_name=library.name,
            path="/Designs/mockup.png",
            id="file-image",
            name="mockup.png",
            size=256,
            mtime=datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc),
            content_type="image/png",
        )


class FakeUnsupportedFileClient(FakeSeafileClient):
    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]:
        yield SeafileRemoteFile(
            library_id=library.id,
            library_name=library.name,
            path="/Designs/archive.zip",
            id="file-zip",
            name="archive.zip",
            size=256,
        )


class FakeSeafilePdfClient(FakeSeafileClient):
    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]:
        yield SeafileRemoteFile(
            library_id=library.id,
            library_name=library.name,
            path="/Reports/board.pdf",
            id="file-pdf",
            name="board.pdf",
            content_type="application/pdf",
        )

    def download_bytes(self, file: SeafileRemoteFile) -> bytes:
        self.downloaded_paths.append((file.library_id, file.path))
        return b"%PDF-1.7\x00binary"


class FakeRichMediaClient(FakeSeafileClient):
    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]:
        for path, content_type in [
            ("/Designs/mockup.webp", "image/webp"),
            ("/Designs/diagram.svg", "image/svg+xml"),
            ("/Recordings/demo.mp3", "audio/mpeg"),
            ("/Recordings/demo.mp4", "video/mp4"),
        ]:
            yield SeafileRemoteFile(
                library_id=library.id,
                library_name=library.name,
                path=path,
                id=path.rsplit("/", 1)[-1],
                name=path.rsplit("/", 1)[-1],
                size=256,
                mtime=datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc),
                content_type=content_type,
            )

    def download_bytes(self, file: SeafileRemoteFile) -> bytes:
        self.downloaded_paths.append((file.library_id, file.path))
        return b"\x00\x01\x02not utf8\xff"


@pytest.fixture(autouse=True)
def default_rich_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_extract_text_and_images(
        file: Any,
        file_name: str,
        content_type: str | None = None,
        **_: Any,
    ) -> Any:
        assert file_name
        assert content_type is not None
        return ExtractionResult(
            text_content=file.read().decode("utf-8"),
            embedded_images=[],
            metadata={},
        )

    monkeypatch.setattr(
        seafile_connector_module,
        "extract_text_and_images",
        fake_extract_text_and_images,
    )


def seafile_file(library: SeafileLibrary) -> SeafileRemoteFile:
    return SeafileRemoteFile(
        library_id=library.id,
        library_name=library.name,
        path="/Strategy/Launch Plan.md",
        id="file-1",
        name="Launch Plan.md",
        size=128,
        mtime=datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc),
        modifier_email="reiss@example.com",
        modifier_name="Reiss",
        revision_id="rev-7",
        content_type="text/markdown",
        download_url="https://seafile.example.com/seafhttp/files/file-1",
    )


def _collect_documents(connector: SeafileConnector) -> list[Document]:
    return [
        item
        for batch in connector.load_from_state()
        for item in batch
        if isinstance(item, Document)
    ]


def _collect_failures(connector: SeafileConnector) -> list[ConnectorFailure]:
    return [
        item
        for batch in connector.load_from_state()
        for item in batch
        if isinstance(item, ConnectorFailure)
    ]


def test_seafile_connector_adopts_existing_ingestion_api_identity_without_dual_indexing() -> (
    None
):
    client = FakeSeafileClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com/",
        library_ids=["lib-1"],
        ingestion_api_document_id_mappings={
            "lib-1:/Strategy/Launch Plan.md": "oneqode:stable:launch-plan"
        },
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert client.validated is True
    assert client.downloaded_paths == [("lib-1", "/Strategy/Launch Plan.md")]
    assert len(documents) == 1
    document = documents[0]
    assert document.id == "oneqode:stable:launch-plan"
    assert document.source == DocumentSource.INGESTION_API
    assert document.from_ingestion_api is True
    assert document.semantic_identifier == "OneQode/Strategy/Launch Plan.md"
    assert (
        document.sections[0].link
        == "https://seafile.example.com/lib/lib-1/file/Strategy/Launch%20Plan.md"
    )
    assert (
        document.sections[0].text
        == "# Launch plan\n\nShip first-class Seafile knowledge."
    )
    assert document.doc_updated_at == datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc)
    assert document.metadata == {
        "library_id": "lib-1",
        "library_name": "OneQode",
        "path": "/Strategy/Launch Plan.md",
        "file_id": "file-1",
        "revision_id": "rev-7",
        "content_type": "text/markdown",
        "size": "128",
        "modifier_email": "reiss@example.com",
        "modifier_name": "Reiss",
    }
    assert connector.health_snapshot().as_metadata() == {
        "selected_library_count": "1",
        "indexed_count": "1",
        "excluded_count": "0",
        "skipped_count": "0",
        "error_count": "0",
        "adopted_ingestion_api_count": "1",
    }


def test_seafile_connector_refuses_to_create_second_identity_before_cutover() -> None:
    connector = SeafileConnector(
        base_url="https://seafile.example.com/",
        library_ids=["lib-1"],
        batch_size=10,
        client=FakeSeafileClient(),
    )

    documents = _collect_documents(connector)
    failures = _collect_failures(connector)

    assert documents == []
    assert len(failures) == 1
    assert "no existing document_id mapping" in failures[0].failure_message
    assert (
        "Refusing to create a second Seafile document identity"
        in failures[0].failure_message
    )
    assert connector.health_snapshot().error_count == 1


def test_seafile_connector_preserves_revision_link_exclusion_and_skip_counts() -> None:
    client = FakeSeafileClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["lib-1"],
        excluded_paths=["/Strategy/*"],
        ingestion_api_document_id_mappings={
            "lib-1:/Strategy/Launch Plan.md": "oneqode:stable:launch-plan"
        },
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert documents == []
    assert client.downloaded_paths == []
    assert connector.health_snapshot().excluded_count == 1
    assert connector.health_snapshot().indexed_count == 0


def test_seafile_connector_skips_unsupported_files_without_downloading() -> None:
    client = FakeUnsupportedFileClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["lib-1"],
        ingestion_api_document_id_mappings={"lib-1:/Designs/archive.zip": "stable-zip"},
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert documents == []
    assert client.downloaded_paths == []
    assert connector.health_snapshot().skipped_count == 1


def test_seafile_api_client_exhaustively_paginates_legacy_dirents_without_v21(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakePaginatedSession()
    monkeypatch.setattr(
        seafile_connector_module.requests, "Session", lambda: fake_session
    )
    client = SeafileApiClient(
        "https://seafile.example.com", "token", page_size=2, max_pages_per_directory=4
    )

    files = list(client.iter_files(SeafileLibrary(id="lib-1", name="OneQode")))

    assert [file.path for file in files] == [
        "/Strategy/Launch Plan.md",
        "/root.pdf",
        "/brief.docx",
    ]
    assert all("/api/v2.1/" not in url for url in fake_session.requested_urls)
    assert any("start=2&limit=2" in url for url in fake_session.requested_urls)


def test_seafile_api_client_fails_closed_on_duplicate_file_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seafile_connector_module.requests, "Session", lambda: FakeDuplicatePageSession()
    )
    client = SeafileApiClient(
        "https://seafile.example.com", "token", page_size=1, max_pages_per_directory=4
    )

    with pytest.raises(ConnectorValidationError) as exc_info:
        list(client.iter_files(SeafileLibrary(id="lib-1", name="OneQode")))

    assert "Duplicate Seafile file identity" in str(exc_info.value)


def test_seafile_api_client_fails_closed_on_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seafile_connector_module.requests, "Session", lambda: FakeUnsafePathSession()
    )
    client = SeafileApiClient("https://seafile.example.com", "token")

    with pytest.raises(ConnectorValidationError) as exc_info:
        list(client.iter_files(SeafileLibrary(id="lib-1", name="OneQode")))

    assert "unsafe" in str(exc_info.value)


def test_seafile_api_client_fails_closed_on_pagination_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seafile_connector_module.requests,
        "Session",
        lambda: FakeFullPageForeverSession(),
    )
    client = SeafileApiClient(
        "https://seafile.example.com", "token", page_size=1, max_pages_per_directory=2
    )

    with pytest.raises(ConnectorValidationError) as exc_info:
        list(client.iter_files(SeafileLibrary(id="lib-1", name="OneQode")))

    assert "pagination exceeded" in str(exc_info.value)


def test_seafile_connector_uses_rich_extraction_for_binary_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, str, str | None]] = []

    def fake_extract_text_and_images(
        file: Any,
        file_name: str,
        content_type: str | None = None,
        **_: Any,
    ) -> Any:
        raw = file.read()
        calls.append((raw, file_name, content_type))
        return ExtractionResult(
            text_content="rich pdf text",
            embedded_images=[],
            metadata={"Author": "OneQode"},
        )

    monkeypatch.setattr(
        seafile_connector_module,
        "extract_text_and_images",
        fake_extract_text_and_images,
    )

    client = FakeSeafilePdfClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["lib-1"],
        ingestion_api_document_id_mappings={"file-pdf": "stable-pdf"},
        client=client,
    )

    documents = _collect_documents(connector)

    assert calls == [(b"%PDF-1.7\x00binary", "board.pdf", "application/pdf")]
    assert len(documents) == 1
    assert isinstance(documents[0].sections[0], TextSection)
    assert documents[0].sections[0].text == "rich pdf text"
    assert documents[0].metadata["Author"] == "OneQode"


def test_seafile_connector_safely_handles_rich_media_without_utf8_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, str, str | None]] = []

    def fake_extract_text_and_images(
        file: Any,
        file_name: str,
        content_type: str | None = None,
        **_: Any,
    ) -> Any:
        raw = file.read()
        calls.append((raw, file_name, content_type))
        if file_name == "mockup.webp":
            return ExtractionResult(
                text_content="ocr text from webp",
                embedded_images=[],
                metadata={},
            )
        return ExtractionResult(text_content="", embedded_images=[], metadata={})

    monkeypatch.setattr(
        seafile_connector_module,
        "extract_text_and_images",
        fake_extract_text_and_images,
    )

    client = FakeRichMediaClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["lib-1"],
        indexable_extensions=[".webp", ".svg", ".mp3", ".mp4"],
        ingestion_api_document_id_mappings={
            "mockup.webp": "stable-webp",
            "diagram.svg": "stable-svg",
            "demo.mp3": "stable-audio",
            "demo.mp4": "stable-video",
        },
        client=client,
    )

    items = [item for batch in connector.load_from_state() for item in batch]
    documents = [item for item in items if isinstance(item, Document)]
    failures = [item for item in items if isinstance(item, ConnectorFailure)]

    assert [document.id for document in documents] == ["stable-webp"]
    assert documents[0].sections[0].text == "ocr text from webp"
    assert len(failures) == 3
    assert all(
        "no indexable content" in failure.failure_message for failure in failures
    )
    assert [call[1:] for call in calls] == [
        ("mockup.webp", "image/webp"),
        ("diagram.svg", "image/svg+xml"),
        ("demo.mp3", "audio/mpeg"),
        ("demo.mp4", "video/mp4"),
    ]


def test_seafile_connector_validates_exact_library_ids_and_rejects_missing_ids() -> (
    None
):
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["missing-lib"],
        client=FakeSeafileClient(),
    )

    with pytest.raises(ConnectorValidationError) as exc_info:
        connector.validate_connector_settings()

    assert "missing-lib" in str(exc_info.value)
    assert "OneQode (lib-1)" in str(exc_info.value)


def test_seafile_connector_rejects_mutable_library_names() -> None:
    with pytest.raises(ConnectorValidationError) as exc_info:
        SeafileConnector(
            base_url="https://seafile.example.com",
            library_names=["OneQode"],
            client=FakeMultiLibraryClient(),
        )

    assert "library_ids" in str(exc_info.value)
    assert "library_names" in str(exc_info.value)


def test_seafile_connector_rejects_duplicate_library_ids() -> None:
    with pytest.raises(ConnectorValidationError) as exc_info:
        SeafileConnector(
            base_url="https://seafile.example.com",
            library_ids=["lib-1", "lib-1"],
            client=FakeSeafileClient(),
        )

    assert "duplicates" in str(exc_info.value)
