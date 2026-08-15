from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import pytest

from onyx.configs.constants import DocumentSource
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.connectors.models import ConnectorFailure, Document
from onyx.connectors.seafile.connector import SeafileConnector
from onyx.connectors.seafile.models import SeafileLibrary, SeafileRemoteFile


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

    def download_text(self, file: SeafileRemoteFile) -> str:
        self.downloaded_paths.append((file.library_id, file.path))
        return "# Launch plan\n\nShip first-class Seafile knowledge."


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


def test_seafile_connector_skips_non_index_eligible_files_without_downloading() -> None:
    client = FakeSeafileImageClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        library_ids=["lib-1"],
        ingestion_api_document_id_mappings={
            "lib-1:/Designs/mockup.png": "stable-image"
        },
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert documents == []
    assert client.downloaded_paths == []
    assert connector.health_snapshot().skipped_count == 1


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
