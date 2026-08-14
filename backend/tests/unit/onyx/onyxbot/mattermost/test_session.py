from onyx.onyxbot.mattermost.models import (
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.onyxbot.mattermost.session import get_mattermost_mapping_root_id


def test_dm_roots_get_fresh_mappings_and_replies_reuse_the_root() -> None:
    first_root = _dm_event(post_id="root-1", root_post_id="root-1")
    second_root = _dm_event(post_id="root-2", root_post_id="root-2")
    first_reply = _dm_event(post_id="reply-1", root_post_id="root-1")

    assert get_mattermost_mapping_root_id(first_root) == "root-1"
    assert get_mattermost_mapping_root_id(second_root) == "root-2"
    assert get_mattermost_mapping_root_id(first_reply) == "root-1"


def _dm_event(*, post_id: str, root_post_id: str) -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.DIRECT_MESSAGE,
        session_key="mattermost:dm:global:dm-channel",
        team_id="global",
        channel_id="dm-channel",
        post_id=post_id,
        root_post_id=root_post_id,
        user_id="user-1",
    )
