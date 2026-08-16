from __future__ import annotations

import io
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Protocol, cast
from urllib.parse import quote

import requests

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.exceptions import ConnectorValidationError, CredentialExpiredError
from onyx.connectors.interfaces import (
    GenerateDocumentsOutput,
    LoadConnector,
    PollConnector,
)
from onyx.connectors.models import (
    ConnectorFailure,
    ConnectorMissingCredentialError,
    Document,
    DocumentFailure,
    HierarchyNode,
    TextSection,
)
from onyx.connectors.seafile.models import SeafileLibrary, SeafileRemoteFile
from onyx.file_processing.extract_file_text import (
    ExtractionResult,
    extract_text_and_images,
)
from onyx.file_processing.file_types import OnyxFileExtensions

_DEFAULT_INDEXABLE_EXTENSIONS = OnyxFileExtensions.ALL_ALLOWED_EXTENSIONS
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_MAX_PAGES_PER_DIRECTORY = 10_000


class SeafileClient(Protocol):
    def validate(self) -> None: ...

    def list_libraries(self) -> list[SeafileLibrary]: ...

    def iter_files(self, library: SeafileLibrary) -> Iterable[SeafileRemoteFile]: ...

    def download_bytes(self, file: SeafileRemoteFile) -> bytes: ...


@dataclass(frozen=True)
class SeafileAdoptionConfig:
    adopt_existing_ingestion_api: bool = True
    document_id_mappings: Mapping[str, str] | None = None


@dataclass(frozen=True)
class SeafileHealthSnapshot:
    selected_library_count: int
    indexed_count: int
    excluded_count: int
    skipped_count: int
    error_count: int
    adopted_ingestion_api_count: int

    def as_metadata(self) -> dict[str, str]:
        return {
            "selected_library_count": str(self.selected_library_count),
            "indexed_count": str(self.indexed_count),
            "excluded_count": str(self.excluded_count),
            "skipped_count": str(self.skipped_count),
            "error_count": str(self.error_count),
            "adopted_ingestion_api_count": str(self.adopted_ingestion_api_count),
        }


class SeafileApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int = 30,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages_per_directory: int = _DEFAULT_MAX_PAGES_PER_DIRECTORY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        if page_size <= 0:
            raise ConnectorValidationError("Seafile page_size must be positive")
        if max_pages_per_directory <= 0:
            raise ConnectorValidationError(
                "Seafile max_pages_per_directory must be positive"
            )
        self._page_size = page_size
        self._max_pages_per_directory = max_pages_per_directory
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Token {token}"})

    def validate(self) -> None:
        payload = self._request_object("GET", "/api2/account/info/")
        if not payload:
            raise ConnectorValidationError(
                "Seafile account validation returned no data"
            )

    def list_libraries(self) -> list[SeafileLibrary]:
        payload = self._request_json("GET", "/api2/repos/")
        if not isinstance(payload, list):
            raise ConnectorValidationError("Seafile libraries payload is invalid")
        libraries: list[SeafileLibrary] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            library_id = _string_value(item.get("id")) or _string_value(
                item.get("repo_id")
            )
            name = _string_value(item.get("name"))
            if library_id and name:
                libraries.append(SeafileLibrary(id=library_id, name=name))
        return libraries

    def iter_files(self, library: SeafileLibrary) -> Iterator[SeafileRemoteFile]:
        seen_directory_paths: set[str] = set()
        seen_file_identities: set[str] = set()
        yield from self._iter_files_at_path(
            library,
            "/",
            seen_directory_paths=seen_directory_paths,
            seen_file_identities=seen_file_identities,
        )

    def download_bytes(self, file: SeafileRemoteFile) -> bytes:
        download_url = file.download_url or self._get_download_url(file)
        try:
            response = self._session.get(download_url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ConnectorValidationError(
                f"Seafile download failed for {file.path}: {exc}"
            ) from exc
        self._raise_for_response(response, "GET", download_url)
        return response.content

    def _iter_files_at_path(
        self,
        library: SeafileLibrary,
        directory_path: str,
        *,
        seen_directory_paths: set[str],
        seen_file_identities: set[str],
    ) -> Iterator[SeafileRemoteFile]:
        if directory_path in seen_directory_paths:
            raise ConnectorValidationError(
                f"Duplicate Seafile directory traversal path: {directory_path}"
            )
        seen_directory_paths.add(directory_path)

        start = 0
        pages_seen = 0
        while True:
            if pages_seen >= self._max_pages_per_directory:
                raise ConnectorValidationError(
                    "Seafile directory pagination exceeded the configured safety "
                    f"limit for {directory_path}"
                )
            payload = self._request_json(
                "GET",
                f"/api2/repos/{quote(library.id, safe='')}/dir/?"
                f"p={quote(directory_path)}&start={start}&limit={self._page_size}",
            )
            pages_seen += 1
            if not isinstance(payload, list):
                raise ConnectorValidationError("Seafile directory payload is invalid")

            page_item_count = 0
            for item in payload:
                if not isinstance(item, dict):
                    raise ConnectorValidationError(
                        "Seafile directory payload contains a non-object entry"
                    )
                item_type = _string_value(item.get("type"))
                name = _string_value(item.get("name"))
                if not name or item_type not in {"dir", "file"}:
                    raise ConnectorValidationError(
                        "Seafile directory payload contains an ambiguous entry"
                    )
                page_item_count += 1
                child_path = _join_seafile_path(directory_path, name)
                if item_type == "dir":
                    yield from self._iter_files_at_path(
                        library,
                        child_path,
                        seen_directory_paths=seen_directory_paths,
                        seen_file_identities=seen_file_identities,
                    )
                elif item_type == "file":
                    file = self._file_from_payload(library, child_path, item)
                    identity = f"{file.library_id}:{file.id}"
                    if identity in seen_file_identities:
                        raise ConnectorValidationError(
                            f"Duplicate Seafile file identity encountered: {identity}"
                        )
                    seen_file_identities.add(identity)
                    yield file

            if page_item_count < self._page_size:
                break
            start += self._page_size

    def _file_from_payload(
        self, library: SeafileLibrary, path: str, payload: dict[str, Any]
    ) -> SeafileRemoteFile:
        file_id = _string_value(payload.get("id")) or path
        return SeafileRemoteFile(
            library_id=library.id,
            library_name=library.name,
            path=path,
            id=file_id,
            name=path.rsplit("/", 1)[-1],
            size=_int_value(payload.get("size")),
            mtime=_datetime_value(payload.get("mtime") or payload.get("last_modified")),
            modifier_email=_string_value(payload.get("modifier_email")),
            modifier_name=_string_value(payload.get("modifier_name")),
            revision_id=_string_value(
                payload.get("commit_id") or payload.get("revision_id")
            ),
            content_type=_string_value(payload.get("content_type")),
            download_url=_string_value(payload.get("download_url")),
        )

    def _get_download_url(self, file: SeafileRemoteFile) -> str:
        payload = self._request_json(
            "GET",
            f"/api2/repos/{quote(file.library_id, safe='')}/file/?p={quote(file.path)}",
        )
        if not isinstance(payload, str) or not payload:
            raise ConnectorValidationError(
                f"Seafile download URL payload is invalid for {file.path}"
            )
        return payload

    def _request_object(self, method: str, path: str) -> dict[str, Any]:
        payload = self._request_json(method, path)
        if not isinstance(payload, dict):
            raise ConnectorValidationError("Seafile returned a non-object payload")
        return payload

    def _request_json(self, method: str, path: str) -> dict[str, Any] | list[Any] | str:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(method, url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ConnectorValidationError(
                f"Seafile {method} {path} transport failed: {exc}"
            ) from exc
        self._raise_for_response(response, method, path)
        payload = response.json()
        if not isinstance(payload, (dict, list, str)):
            raise ConnectorValidationError("Seafile returned an invalid JSON payload")
        return cast(dict[str, Any] | list[Any] | str, payload)

    @staticmethod
    def _raise_for_response(
        response: requests.Response, method: str, path: str
    ) -> None:
        if response.status_code in {401, 403}:
            raise CredentialExpiredError(
                f"Seafile token rejected with {response.status_code}"
            )
        if response.status_code >= 400:
            raise ConnectorValidationError(
                f"Seafile {method} {path} failed with {response.status_code}: {response.text}"
            )


class SeafileConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        *,
        base_url: str,
        library_ids: list[str] | None = None,
        library_names: list[str] | None = None,
        excluded_paths: list[str] | None = None,
        indexable_extensions: list[str] | None = None,
        adopt_existing_ingestion_api: bool = True,
        ingestion_api_document_id_mappings: dict[str, str] | list[str] | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
        client: SeafileClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if library_names:
            raise ConnectorValidationError(
                "Seafile library selection must use exact library_ids, not mutable "
                "library_names. Discover libraries from the server, then save the "
                "stable library IDs."
            )
        self.library_ids = _validate_unique_ids(library_ids or [])
        self.excluded_paths = excluded_paths or []
        self.indexable_extensions = {
            extension.lower()
            for extension in (
                indexable_extensions or sorted(_DEFAULT_INDEXABLE_EXTENSIONS)
            )
        }
        self.adoption = SeafileAdoptionConfig(
            adopt_existing_ingestion_api=adopt_existing_ingestion_api,
            document_id_mappings=_normalize_document_id_mappings(
                ingestion_api_document_id_mappings
            ),
        )
        self.batch_size = batch_size
        self.client = client
        self._last_health = SeafileHealthSnapshot(
            selected_library_count=0,
            indexed_count=0,
            excluded_count=0,
            skipped_count=0,
            error_count=0,
            adopted_ingestion_api_count=0,
        )

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        token = credentials.get("seafile_api_token") or credentials.get("api_token")
        base_url = credentials.get("seafile_base_url") or credentials.get("base_url")
        if not isinstance(token, str) or not token:
            raise ConnectorMissingCredentialError("Seafile")
        if isinstance(base_url, str) and base_url:
            self.base_url = base_url.rstrip("/")
        self.client = SeafileApiClient(self.base_url, token)
        return None

    def validate_connector_settings(self) -> None:
        if self.client is None:
            raise ConnectorMissingCredentialError("Seafile")
        self.client.validate()
        self._selected_libraries(self.client.list_libraries())

    def health_snapshot(self) -> SeafileHealthSnapshot:
        return self._last_health

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._poll_source(start=None, end=None)

    def poll_source(self, start: float, end: float) -> GenerateDocumentsOutput:
        return self._poll_source(start=start, end=end)

    def _poll_source(
        self, start: float | None, end: float | None
    ) -> GenerateDocumentsOutput:
        if self.client is None:
            raise ConnectorMissingCredentialError("Seafile")
        self.client.validate()
        batch: list[Document | HierarchyNode] = []
        counts = _SeafileRunCounts()
        for document in self._iter_documents(start=start, end=end, counts=counts):
            batch.append(document)
            if len(batch) == self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
        self._last_health = counts.snapshot()

    def _iter_documents(
        self, *, start: float | None, end: float | None, counts: "_SeafileRunCounts"
    ) -> Iterator[Document]:
        assert self.client is not None
        selected_libraries = self._selected_libraries(self.client.list_libraries())
        counts.selected_library_count = len(selected_libraries)
        for library in selected_libraries:
            for file in self.client.iter_files(library):
                if self._is_excluded(file.path):
                    counts.excluded_count += 1
                    continue
                if not self._is_indexable(file):
                    counts.skipped_count += 1
                    continue
                if not _is_within_poll_window(file.mtime, start=start, end=end):
                    counts.skipped_count += 1
                    continue
                try:
                    _document_id(file, adoption=self.adoption, base_url=self.base_url)
                    raw_bytes = self.client.download_bytes(file)
                    extraction_result = extract_text_and_images(
                        io.BytesIO(raw_bytes),
                        file_name=file.name,
                        content_type=file.content_type,
                    )
                    document = _document_from_file(
                        self.base_url,
                        file,
                        extraction_result,
                        adoption=self.adoption,
                    )
                    counts.indexed_count += 1
                    if document.from_ingestion_api:
                        counts.adopted_ingestion_api_count += 1
                    yield document
                except Exception as exc:
                    counts.error_count += 1
                    yield cast(
                        Document,
                        ConnectorFailure(
                            failed_document=DocumentFailure(
                                document_id=_document_id(file),
                                document_link=_canonical_link(self.base_url, file),
                            ),
                            failure_message=f"Failed to index Seafile file {file.path}: {exc}",
                            exception=exc,
                        ),
                    )

    def _selected_libraries(
        self, libraries: list[SeafileLibrary]
    ) -> list[SeafileLibrary]:
        if not self.library_ids:
            return libraries

        libraries_by_id = {library.id: library for library in libraries}
        missing_library_ids = sorted(
            library_id
            for library_id in self.library_ids
            if library_id not in libraries_by_id
        )
        if missing_library_ids:
            available = (
                ", ".join(f"{library.name} ({library.id})" for library in libraries)
                or "none visible to this token"
            )
            raise ConnectorValidationError(
                "Configured Seafile library_ids are not visible to this token: "
                f"{', '.join(missing_library_ids)}. Available libraries: {available}."
            )
        return [libraries_by_id[library_id] for library_id in self.library_ids]

    def _is_excluded(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.excluded_paths)

    def _is_indexable(self, file: SeafileRemoteFile) -> bool:
        extension = _file_extension(file.name or file.path)
        return extension in self.indexable_extensions


def _document_from_file(
    base_url: str,
    file: SeafileRemoteFile,
    extraction_result: ExtractionResult,
    *,
    adoption: SeafileAdoptionConfig,
) -> Document:
    metadata: dict[str, str | list[str]] = {
        "library_id": file.library_id,
        "library_name": file.library_name,
        "path": file.path,
        "file_id": file.id,
    }
    optional_metadata = {
        "revision_id": file.revision_id,
        "content_type": file.content_type,
        "size": str(file.size) if file.size is not None else None,
        "modifier_email": file.modifier_email,
        "modifier_name": file.modifier_name,
    }
    metadata.update({key: value for key, value in optional_metadata.items() if value})
    metadata.update(
        {
            key: str(value)
            for key, value in extraction_result.metadata.items()
            if value is not None and str(value)
        }
    )
    document_id = _document_id(file, adoption=adoption, base_url=base_url)
    source = (
        DocumentSource.INGESTION_API
        if adoption.adopt_existing_ingestion_api
        else DocumentSource.SEAFILE
    )
    if not extraction_result.text_content.strip():
        raise ConnectorValidationError(
            f"Seafile rich extraction produced no indexable content for {file.path}"
        )
    return Document(
        id=document_id,
        source=source,
        semantic_identifier=f"{file.library_name}{file.path}",
        title=file.name,
        sections=[
            TextSection(
                link=_canonical_link(base_url, file),
                text=extraction_result.text_content.strip(),
            )
        ],
        metadata=metadata,
        doc_updated_at=file.mtime,
        from_ingestion_api=adoption.adopt_existing_ingestion_api,
    )


def _document_id(
    file: SeafileRemoteFile,
    *,
    adoption: SeafileAdoptionConfig | None = None,
    base_url: str | None = None,
) -> str:
    if adoption and adoption.adopt_existing_ingestion_api:
        if base_url is None:
            raise ConnectorValidationError(
                "Seafile Ingestion API adoption requires base_url to resolve document IDs"
            )
        document_id = _adopted_document_id(
            base_url, file, adoption.document_id_mappings
        )
        if document_id is None:
            raise ConnectorValidationError(
                "Seafile Ingestion API adoption is enabled, but no existing "
                f"document_id mapping was found for {file.library_id}:{file.path}. "
                "Refusing to create a second Seafile document identity before cutover."
            )
        return document_id
    return f"seafile:{file.library_id}:{file.path}"


def _adopted_document_id(
    base_url: str,
    file: SeafileRemoteFile,
    mappings: Mapping[str, str] | None,
) -> str | None:
    if not mappings:
        return None
    candidates = (
        f"{file.library_id}:{file.path}",
        _canonical_link(base_url, file),
        file.path,
        file.id,
    )
    for candidate in candidates:
        document_id = mappings.get(candidate)
        if document_id:
            return document_id
    return None


@dataclass
class _SeafileRunCounts:
    selected_library_count: int = 0
    indexed_count: int = 0
    excluded_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    adopted_ingestion_api_count: int = 0

    def snapshot(self) -> SeafileHealthSnapshot:
        return SeafileHealthSnapshot(
            selected_library_count=self.selected_library_count,
            indexed_count=self.indexed_count,
            excluded_count=self.excluded_count,
            skipped_count=self.skipped_count,
            error_count=self.error_count,
            adopted_ingestion_api_count=self.adopted_ingestion_api_count,
        )


def _canonical_link(base_url: str, file: SeafileRemoteFile) -> str:
    quoted_path = quote(file.path.lstrip("/"))
    return f"{base_url.rstrip('/')}/lib/{quote(file.library_id, safe='')}/file/{quoted_path}"


def _join_seafile_path(directory_path: str, name: str) -> str:
    _validate_safe_seafile_path_segment(name)
    if directory_path == "/":
        return f"/{name}"
    return f"{directory_path.rstrip('/')}/{name}"


def _validate_safe_seafile_path_segment(name: str) -> None:
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ConnectorValidationError(
            f"Seafile directory payload contains unsafe path segment: {name}"
        )
    if any(part in {".", ".."} for part in name.split("/")):
        raise ConnectorValidationError(
            f"Seafile directory payload contains unsafe path segment: {name}"
        )


def _file_extension(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _validate_unique_ids(library_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    cleaned: list[str] = []
    for library_id in library_ids:
        if not library_id:
            continue
        if library_id in seen:
            duplicates.add(library_id)
        seen.add(library_id)
        cleaned.append(library_id)
    if duplicates:
        raise ConnectorValidationError(
            "Seafile library_ids contains duplicates: " + ", ".join(sorted(duplicates))
        )
    return cleaned


def _normalize_document_id_mappings(
    mappings: dict[str, str] | list[str] | None,
) -> dict[str, str]:
    if mappings is None:
        return {}
    if isinstance(mappings, dict):
        return {key: value for key, value in mappings.items() if key and value}
    normalized: dict[str, str] = {}
    for item in mappings:
        if "=" not in item:
            raise ConnectorValidationError(
                "Seafile ingestion_api_document_id_mappings entries must use "
                "key=document_id format."
            )
        key, document_id = item.split("=", 1)
        key = key.strip()
        document_id = document_id.strip()
        if key and document_id:
            normalized[key] = document_id
    return normalized


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_value(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _is_within_poll_window(
    value: datetime | None, *, start: float | None, end: float | None
) -> bool:
    if value is None or (start is None and end is None):
        return True
    timestamp = value.timestamp()
    if start is not None and timestamp < start:
        return False
    if end is not None and timestamp > end:
        return False
    return True
