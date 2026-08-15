from collections.abc import Iterable
from datetime import datetime, timezone

from onyx.configs.constants import DocumentSource
from onyx.connectors.mattermost.connector import MattermostConnector
from onyx.connectors.mattermost.models import (
    MattermostChannel,
    MattermostFileMetadata,
    MattermostPost,
)
from onyx.connectors.models import ConnectorFailure, Document, HierarchyNode


class FakeMattermostHistoryClient:
    def __init__(
        self,
        *,
        channels: list[MattermostChannel],
        posts_by_channel: dict[str, list[MattermostPost]],
        member_pairs: set[tuple[str, str]],
        files_by_id: dict[str, MattermostFileMetadata] | None = None,
        failing_channels: set[str] | None = None,
    ) -> None:
        self.bot_user_id = "bot-user"
        self.channels = channels
        self.posts_by_channel = posts_by_channel
        self.member_pairs = member_pairs
        self.files_by_id = files_by_id or {}
        self.failing_channels = failing_channels or set()
        self.membership_checks: list[tuple[str, str]] = []

    def validate(self) -> None:
        return None

    def list_channels_for_bot(self) -> list[MattermostChannel]:
        return self.channels

    def is_channel_member(self, channel_id: str, user_id: str) -> bool:
        self.membership_checks.append((channel_id, user_id))
        return (channel_id, user_id) in self.member_pairs

    def get_channel_posts(
        self, channel_id: str, *, start: float | None, end: float | None
    ) -> Iterable[MattermostPost]:
        _ = (start, end)
        if channel_id in self.failing_channels:
            raise RuntimeError("mattermost history unavailable")
        for post in self.posts_by_channel[channel_id]:
            yield post

    def get_file_metadata(self, file_id: str) -> MattermostFileMetadata:
        return self.files_by_id[file_id]


def _collect_documents(
    connector: MattermostConnector, start: float = 0, end: float = 2_000
) -> list[Document | HierarchyNode | ConnectorFailure]:
    return [item for batch in connector.poll_source(start, end) for item in batch]


def test_mattermost_connector_indexes_channel_history_with_threads_and_file_metadata() -> (
    None
):
    connector = MattermostConnector(
        batch_size=10,
        client=FakeMattermostHistoryClient(
            channels=[
                MattermostChannel(id="chan-1", name="town-square", team_id="team-1")
            ],
            posts_by_channel={
                "chan-1": [
                    MattermostPost(
                        id="root-1",
                        channel_id="chan-1",
                        user_id="alice",
                        message="launch plan",
                        create_at=1_000_000,
                        update_at=1_000_500,
                        file_ids=("file-1",),
                    ),
                    MattermostPost(
                        id="reply-1",
                        channel_id="chan-1",
                        user_id="bob",
                        message="approved",
                        root_id="root-1",
                        create_at=1_001_000,
                    ),
                ]
            },
            files_by_id={
                "file-1": MattermostFileMetadata(
                    id="file-1",
                    post_id="root-1",
                    user_id="alice",
                    filename="brief.pdf",
                    mime_type="application/pdf",
                    size_bytes=123,
                    create_at=999_000,
                )
            },
            member_pairs={
                ("chan-1", "bot-user"),
                ("chan-1", "alice"),
                ("chan-1", "bob"),
            },
        ),
    )

    results = _collect_documents(connector)

    hierarchy_nodes = [item for item in results if isinstance(item, HierarchyNode)]
    documents = [item for item in results if isinstance(item, Document)]
    assert [node.raw_node_id for node in hierarchy_nodes] == ["chan-1"]
    assert len(documents) == 1
    doc = documents[0]
    assert doc.id == "mattermost:team-1:chan-1:root-1"
    assert doc.source == DocumentSource.MATTERMOST
    assert doc.semantic_identifier == "#town-square: launch plan"
    assert doc.sections[0].link == "https://mattermost.example.com/team-1/pl/root-1"
    assert doc.sections[0].text == "alice: launch plan\nbob: approved"
    assert doc.metadata == {
        "team_id": "team-1",
        "channel_id": "chan-1",
        "channel_name": "town-square",
        "root_post_id": "root-1",
        "post_ids": ["root-1", "reply-1"],
        "sender_user_ids": ["alice", "bob"],
        "file_ids": ["file-1"],
        "file_names": ["brief.pdf"],
        "file_mime_types": ["application/pdf"],
        "file_sizes": ["123"],
    }
    assert doc.doc_created_at == datetime.fromtimestamp(1_000, tz=timezone.utc)
    assert doc.doc_updated_at == datetime.fromtimestamp(1_001, tz=timezone.utc)
    assert doc.parent_hierarchy_raw_node_id == "chan-1"


def test_mattermost_connector_fails_closed_when_sender_is_not_current_channel_member() -> (
    None
):
    fake_client = FakeMattermostHistoryClient(
        channels=[MattermostChannel(id="chan-1", name="private", team_id="team-1")],
        posts_by_channel={
            "chan-1": [
                MattermostPost(
                    id="root-1",
                    channel_id="chan-1",
                    user_id="former-member",
                    message="should not index",
                    create_at=1_000,
                )
            ]
        },
        member_pairs={("chan-1", "bot-user")},
    )
    connector = MattermostConnector(batch_size=10, client=fake_client)

    results = _collect_documents(connector)

    assert [item for item in results if isinstance(item, Document)] == []
    assert ("chan-1", "bot-user") in fake_client.membership_checks
    assert ("chan-1", "former-member") in fake_client.membership_checks


def test_mattermost_connector_uses_stable_root_ids_to_make_replay_idempotent() -> None:
    duplicate_post = MattermostPost(
        id="root-1",
        channel_id="chan-1",
        user_id="alice",
        message="same visible post delivered twice",
        create_at=1_000,
    )
    connector = MattermostConnector(
        batch_size=10,
        client=FakeMattermostHistoryClient(
            channels=[
                MattermostChannel(id="chan-1", name="town-square", team_id="team-1")
            ],
            posts_by_channel={"chan-1": [duplicate_post, duplicate_post]},
            member_pairs={("chan-1", "bot-user"), ("chan-1", "alice")},
        ),
    )

    documents = [
        item for item in _collect_documents(connector) if isinstance(item, Document)
    ]

    assert [doc.id for doc in documents] == ["mattermost:team-1:chan-1:root-1"]


def test_mattermost_connector_indexes_deleted_posts_as_stable_tombstones() -> None:
    connector = MattermostConnector(
        batch_size=10,
        client=FakeMattermostHistoryClient(
            channels=[
                MattermostChannel(id="chan-1", name="town-square", team_id="team-1")
            ],
            posts_by_channel={
                "chan-1": [
                    MattermostPost(
                        id="root-1",
                        channel_id="chan-1",
                        user_id="alice",
                        message="removed content",
                        create_at=1_000,
                        update_at=1_500,
                        delete_at=2_000,
                    )
                ]
            },
            member_pairs={("chan-1", "bot-user"), ("chan-1", "alice")},
        ),
    )

    documents = [
        item for item in _collect_documents(connector) if isinstance(item, Document)
    ]

    assert len(documents) == 1
    doc = documents[0]
    assert doc.id == "mattermost:team-1:chan-1:root-1"
    assert doc.sections[0].text == "alice: [deleted Mattermost post root-1]"
    assert doc.doc_updated_at == datetime.fromtimestamp(2, tz=timezone.utc)


def test_mattermost_connector_emits_failure_for_history_fetch_errors() -> None:
    connector = MattermostConnector(
        batch_size=10,
        client=FakeMattermostHistoryClient(
            channels=[
                MattermostChannel(id="chan-1", name="town-square", team_id="team-1")
            ],
            posts_by_channel={"chan-1": []},
            member_pairs={("chan-1", "bot-user")},
            failing_channels={"chan-1"},
        ),
    )

    results = _collect_documents(connector)

    failures = [item for item in results if isinstance(item, ConnectorFailure)]
    assert len(failures) == 1
    assert failures[0].failed_entity is not None
    assert failures[0].failed_entity.entity_id == "chan-1"
    assert "mattermost history unavailable" in failures[0].failure_message
