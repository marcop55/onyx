from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any, Protocol, cast

import requests

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.exceptions import ConnectorValidationError, CredentialExpiredError
from onyx.connectors.interfaces import (
    GenerateDocumentsOutput,
    LoadConnector,
    PollConnector,
    SecondsSinceUnixEpoch,
)
from onyx.connectors.mattermost.models import (
    MattermostChannel,
    MattermostFileMetadata,
    MattermostPost,
)
from onyx.connectors.mattermost.utils import (
    build_mattermost_document_id,
    build_mattermost_post_link,
    canonical_root_post_id,
    mattermost_millis_to_datetime,
    post_updated_timestamp,
)
from onyx.connectors.models import (
    ConnectorFailure,
    ConnectorMissingCredentialError,
    Document,
    EntityFailure,
    HierarchyNode,
    TextSection,
)
from onyx.db.enums import HierarchyNodeType
from onyx.utils.logger import setup_logger

logger = setup_logger()

_DEFAULT_BASE_URL = "https://mattermost.example.com"


class MattermostHistoryClient(Protocol):
    bot_user_id: str

    def validate(self) -> None: ...

    def list_channels_for_bot(self) -> list[MattermostChannel]: ...

    def is_channel_member(self, channel_id: str, user_id: str) -> bool: ...

    def get_channel_posts(
        self, channel_id: str, *, start: float | None, end: float | None
    ) -> Iterable[MattermostPost]: ...

    def get_file_metadata(self, file_id: str) -> MattermostFileMetadata: ...


class MattermostApiClient:
    def __init__(self, base_url: str, token: str, *, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self.bot_user_id = ""

    def validate(self) -> None:
        payload = self._request_object("GET", "/api/v4/users/me")
        user_id = _string_value(payload.get("id"))
        if not user_id:
            raise ConnectorValidationError(
                "Mattermost auth validation did not return a user id"
            )
        self.bot_user_id = user_id

    def list_channels_for_bot(self) -> list[MattermostChannel]:
        if not self.bot_user_id:
            self.validate()
        payload = self._request_json(
            "GET", f"/api/v4/users/{self.bot_user_id}/channels"
        )
        if not isinstance(payload, list):
            raise ConnectorValidationError("Mattermost channel list payload is invalid")
        return [
            _channel_from_payload(item) for item in payload if isinstance(item, dict)
        ]

    def is_channel_member(self, channel_id: str, user_id: str) -> bool:
        try:
            payload = self._request_object(
                "GET", f"/api/v4/channels/{channel_id}/members/{user_id}"
            )
        except CredentialExpiredError:
            raise
        except ConnectorValidationError as exc:
            if "404" in str(exc):
                return False
            raise
        return (
            payload.get("channel_id") == channel_id
            and payload.get("user_id") == user_id
        )

    def get_channel_posts(
        self, channel_id: str, *, start: float | None, end: float | None
    ) -> Iterator[MattermostPost]:
        since = int(start * 1000) if start is not None else 0
        page = 0
        seen_ids: set[str] = set()
        while True:
            payload = self._request_object(
                "GET",
                f"/api/v4/channels/{channel_id}/posts?since={since}&page={page}&per_page=200",
            )
            posts_payload = payload.get("posts")
            order_payload = payload.get("order")
            if not isinstance(posts_payload, dict) or not isinstance(
                order_payload, list
            ):
                raise ConnectorValidationError("Mattermost posts payload is invalid")
            if not order_payload:
                return
            yielded = False
            for post_id in reversed(
                [item for item in order_payload if isinstance(item, str)]
            ):
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                raw_post = posts_payload.get(post_id)
                if not isinstance(raw_post, dict):
                    continue
                post = _post_from_payload(raw_post)
                updated_at = post_updated_timestamp(post)
                if (
                    end is not None
                    and updated_at is not None
                    and updated_at > end * 1000
                ):
                    continue
                yielded = True
                yield post
            if len(order_payload) < 200 or not yielded:
                return
            page += 1

    def get_file_metadata(self, file_id: str) -> MattermostFileMetadata:
        payload = self._request_object("GET", f"/api/v4/files/{file_id}/info")
        return _file_metadata_from_payload(payload)

    def _request_object(self, method: str, path: str) -> dict[str, Any]:
        payload = self._request_json(method, path)
        if not isinstance(payload, dict):
            raise ConnectorValidationError("Mattermost returned a non-object payload")
        return payload

    def _request_json(self, method: str, path: str) -> dict[str, Any] | list[Any]:
        try:
            response = self._session.request(
                method, f"{self.base_url}{path}", timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise ConnectorValidationError(
                f"Mattermost {method} {path} transport failed: {exc}"
            ) from exc
        if response.status_code in {401, 403}:
            raise CredentialExpiredError(
                f"Mattermost token rejected with {response.status_code}"
            )
        if response.status_code >= 400:
            raise ConnectorValidationError(
                f"Mattermost {method} {path} failed with {response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ConnectorValidationError("Mattermost returned a non-object payload")
        return cast(dict[str, Any] | list[Any], payload)


class MattermostConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        channels: list[str] | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
        client: MattermostHistoryClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.channels = [channel.removeprefix("#") for channel in channels or []]
        self.batch_size = batch_size
        self.client = client

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        token = credentials.get("mattermost_token") or credentials.get("token")
        base_url = credentials.get("mattermost_base_url") or credentials.get("base_url")
        if not isinstance(token, str) or not token:
            raise ConnectorMissingCredentialError("Mattermost")
        if isinstance(base_url, str) and base_url:
            self.base_url = base_url.rstrip("/")
        self.client = MattermostApiClient(self.base_url, token)
        return None

    def validate_connector_settings(self) -> None:
        if self.client is None:
            raise ConnectorMissingCredentialError("Mattermost")
        self.client.validate()

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._poll_source(start=None, end=None)

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        return self._poll_source(start=start, end=end)

    def _poll_source(
        self, start: float | None, end: float | None
    ) -> GenerateDocumentsOutput:
        if self.client is None:
            raise ConnectorMissingCredentialError("Mattermost")
        batch: list[Document | HierarchyNode] = []
        for item in self._iter_documents(start=start, end=end):
            batch.append(cast(Document | HierarchyNode, item))
            if len(batch) == self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _iter_documents(
        self, *, start: float | None, end: float | None
    ) -> Iterator[Document | HierarchyNode | ConnectorFailure]:
        assert self.client is not None
        for channel in self._filtered_channels(self.client.list_channels_for_bot()):
            try:
                if not self.client.is_channel_member(
                    channel.id, self.client.bot_user_id
                ):
                    logger.warning(
                        "Mattermost bot is not a current member of %s", channel.id
                    )
                    continue
                yield _channel_to_hierarchy_node(self.base_url, channel)
                posts = self.client.get_channel_posts(channel.id, start=start, end=end)
                yield from self._channel_posts_to_documents(channel, posts)
            except Exception as exc:
                logger.exception(
                    "Mattermost history indexing failed for channel %s", channel.id
                )
                yield ConnectorFailure(
                    failed_entity=EntityFailure(
                        entity_id=channel.id,
                        missed_time_range=_missed_time_range(start, end),
                    ),
                    failure_message=str(exc),
                    exception=exc,
                )

    def _filtered_channels(
        self, channels: list[MattermostChannel]
    ) -> list[MattermostChannel]:
        if not self.channels:
            return channels
        channel_names = set(self.channels)
        channel_ids = set(self.channels)
        return [
            channel
            for channel in channels
            if channel.name in channel_names or channel.id in channel_ids
        ]

    def _channel_posts_to_documents(
        self, channel: MattermostChannel, posts: Iterable[MattermostPost]
    ) -> Iterator[Document]:
        assert self.client is not None
        grouped_posts: dict[str, list[MattermostPost]] = {}
        seen_post_ids: set[str] = set()
        for post in posts:
            if post.id in seen_post_ids:
                continue
            seen_post_ids.add(post.id)
            if not self.client.is_channel_member(channel.id, post.user_id):
                logger.warning(
                    "Skipping Mattermost post %s because sender %s is not a current member of channel %s",
                    post.id,
                    post.user_id,
                    channel.id,
                )
                continue
            grouped_posts.setdefault(canonical_root_post_id(post), []).append(post)

        for root_post_id, thread_posts in grouped_posts.items():
            ordered_posts = sorted(
                thread_posts,
                key=lambda post: (
                    post.create_at or post.update_at or post.delete_at or 0
                ),
            )
            yield _thread_posts_to_document(
                base_url=self.base_url,
                channel=channel,
                root_post_id=root_post_id,
                posts=ordered_posts,
                client=self.client,
            )


def _channel_to_hierarchy_node(
    base_url: str, channel: MattermostChannel
) -> HierarchyNode:
    return HierarchyNode(
        raw_node_id=channel.id,
        raw_parent_id=None,
        display_name=f"#{channel.name}",
        link=f"{base_url.rstrip('/')}/{channel.team_id}/channels/{channel.name}",
        node_type=HierarchyNodeType.CHANNEL,
    )


def _thread_posts_to_document(
    *,
    base_url: str,
    channel: MattermostChannel,
    root_post_id: str,
    posts: list[MattermostPost],
    client: MattermostHistoryClient,
) -> Document:
    root_post = posts[0]
    text_lines = [_post_text_line(post) for post in posts]
    file_metadata = _file_metadata_for_posts(posts, client)
    first_text = root_post.message.strip() or "Mattermost thread"
    snippet = first_text[:50].rstrip()
    post_ids = [post.id for post in posts]
    sender_user_ids = _ordered_unique(post.user_id for post in posts if post.user_id)
    updated_at = max((post_updated_timestamp(post) or 0) for post in posts)
    metadata: dict[str, str | list[str]] = {
        "team_id": channel.team_id,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "root_post_id": root_post_id,
        "post_ids": post_ids,
        "sender_user_ids": sender_user_ids,
    }
    if file_metadata:
        metadata.update(
            {
                "file_ids": [file.id for file in file_metadata],
                "file_names": [file.filename for file in file_metadata],
                "file_mime_types": [file.mime_type for file in file_metadata],
                "file_sizes": [str(file.size_bytes or 0) for file in file_metadata],
            }
        )
    return Document(
        id=build_mattermost_document_id(channel.team_id, channel.id, root_post_id),
        sections=[
            TextSection(
                link=build_mattermost_post_link(
                    base_url, channel.team_id, root_post_id
                ),
                text="\n".join(text_lines),
            )
        ],
        source=DocumentSource.MATTERMOST,
        semantic_identifier=f"#{channel.name}: {snippet}",
        metadata=metadata,
        doc_created_at=mattermost_millis_to_datetime(root_post.create_at),
        doc_updated_at=mattermost_millis_to_datetime(updated_at),
        doc_metadata={
            "hierarchy": {
                "source_path": [channel.name],
                "channel_name": channel.name,
                "channel_id": channel.id,
                "team_id": channel.team_id,
            }
        },
        parent_hierarchy_raw_node_id=channel.id,
    )


def _post_text_line(post: MattermostPost) -> str:
    prefix = f"{post.user_id}: " if post.user_id else ""
    if post.delete_at:
        return f"{prefix}[deleted Mattermost post {post.id}]"
    text = post.message.strip() or "[empty Mattermost post]"
    return f"{prefix}{text}"


def _file_metadata_for_posts(
    posts: list[MattermostPost], client: MattermostHistoryClient
) -> list[MattermostFileMetadata]:
    file_metadata: list[MattermostFileMetadata] = []
    seen_file_ids: set[str] = set()
    for post in posts:
        for file_id in post.file_ids:
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            file_metadata.append(client.get_file_metadata(file_id))
    return file_metadata


def _ordered_unique(values: Iterable[str]) -> list[str]:
    unique_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        if value in seen_values:
            continue
        seen_values.add(value)
        unique_values.append(value)
    return unique_values


def _missed_time_range(
    start: float | None, end: float | None
) -> tuple[datetime, datetime] | None:
    if start is None or end is None:
        return None
    return (
        datetime.fromtimestamp(start, tz=timezone.utc),
        datetime.fromtimestamp(end, tz=timezone.utc),
    )


def _channel_from_payload(payload: dict[str, Any]) -> MattermostChannel:
    channel_id = _string_value(payload.get("id"))
    name = _string_value(payload.get("name"))
    team_id = _string_value(payload.get("team_id")) or "global"
    return MattermostChannel(
        id=channel_id,
        name=name,
        team_id=team_id,
        display_name=_string_value(payload.get("display_name")) or None,
    )


def _post_from_payload(payload: dict[str, Any]) -> MattermostPost:
    return MattermostPost(
        id=_string_value(payload.get("id")),
        channel_id=_string_value(payload.get("channel_id")),
        user_id=_string_value(payload.get("user_id")),
        message=_string_value(payload.get("message")),
        root_id=_string_value(payload.get("root_id")),
        parent_id=_string_value(payload.get("parent_id")),
        create_at=_int_value(payload.get("create_at")),
        update_at=_int_value(payload.get("update_at")),
        edit_at=_int_value(payload.get("edit_at")),
        delete_at=_int_value(payload.get("delete_at")),
        file_ids=_string_tuple_value(payload.get("file_ids")),
    )


def _file_metadata_from_payload(
    payload: dict[str, Any] | list[Any],
) -> MattermostFileMetadata:
    if not isinstance(payload, dict):
        raise ConnectorValidationError("Mattermost file metadata payload is invalid")
    return MattermostFileMetadata(
        id=_string_value(payload.get("id")),
        post_id=_string_value(payload.get("post_id")),
        user_id=_string_value(payload.get("user_id")),
        filename=_string_value(payload.get("name")),
        mime_type=_string_value(payload.get("mime_type")),
        size_bytes=_int_value(payload.get("size")),
        create_at=_int_value(payload.get("create_at")),
    )


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int_value(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _string_tuple_value(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
