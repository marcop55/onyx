from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from onyx.configs.constants import DocumentSource
from onyx.connectors.models import Document
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
        yield SeafileRemoteFile(
            library_id="lib-1",
            library_name="OneQode",
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

    def download_text(self, file: SeafileRemoteFile) -> str:
        self.downloaded_paths.append((file.library_id, file.path))
        return "# Launch plan\n\nShip first-class Seafile knowledge."


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


def _collect_documents(connector: SeafileConnector) -> list[Document]:
    return [
        item
        for batch in connector.load_from_state()
        for item in batch
        if isinstance(item, Document)
    ]


def test_seafile_connector_indexes_files_with_stable_identity_revision_and_canonical_link() -> (
    None
):
    client = FakeSeafileClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com/",
        library_names=["OneQode"],
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert client.validated is True
    assert client.downloaded_paths == [("lib-1", "/Strategy/Launch Plan.md")]
    assert len(documents) == 1
    document = documents[0]
    assert document.id == "seafile:lib-1:/Strategy/Launch Plan.md"
    assert document.source == DocumentSource.SEAFILE
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


def test_seafile_connector_skips_non_index_eligible_files_without_downloading() -> None:
    client = FakeSeafileImageClient()
    connector = SeafileConnector(
        base_url="https://seafile.example.com",
        batch_size=10,
        client=client,
    )

    documents = _collect_documents(connector)

    assert documents == []
    assert client.downloaded_paths == []
