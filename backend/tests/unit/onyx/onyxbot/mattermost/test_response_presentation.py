from onyx.chat.models import ChatBasicResponse
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.client import mattermost_event_from_payload
from onyx.onyxbot.mattermost.formatting import (
    MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR,
    format_mattermost_answer,
    format_mattermost_answer_parts,
    should_skip_mattermost_answer,
)
from onyx.onyxbot.mattermost.handler import _build_mattermost_context
from onyx.onyxbot.mattermost.listener import MattermostEventNormalizer
from onyx.onyxbot.mattermost.models import (
    MattermostListenerConfig,
    MattermostNormalizedEventType,
    NormalizedMattermostEvent,
)
from onyx.server.query_and_chat.streaming_models import CitationInfo


def test_success_formats_managed_citations_markdown_source_preview_and_split_once() -> (
    None
):
    rendered = format_mattermost_answer(
        _answer(
            answer="<b>Answer</b> with <https://example.test/path|source> [[1]]()",
            blurb="This source preview should be visible when enabled.",
        ),
        response_type="citations",
        include_source_previews=True,
        max_part_chars=130,
    )

    assert "<b>" not in rendered
    assert "[source](https://example.test/path)" in rendered
    assert rendered.count("Sources:") == 1
    assert "[1] Mattermost Doc - https://example.test/doc" in rendered
    assert "Preview: This source preview should be visible" in rendered
    assert MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR in rendered


def test_answer_only_when_sourced_filter_fails_closed_without_visible_answer() -> None:
    answer = _answer(answer="Unsourced answer", citations=[], top_documents=[])

    assert should_skip_mattermost_answer(
        answer,
        channel_config={"answer_filters": ["well_answered_postfilter"]},
        bypass_filters=False,
    )
    assert not should_skip_mattermost_answer(
        answer,
        channel_config={"answer_filters": ["well_answered_postfilter"]},
        bypass_filters=True,
    )


def test_authorization_denial_suppresses_unapproved_sender_response() -> None:
    envelope = mattermost_event_from_payload(
        {
            "event": "posted",
            "data": {
                "post": {
                    "id": "post-root-1",
                    "root_id": "",
                    "channel_id": "channel-1",
                    "user_id": "unknown-user",
                    "message": "@onyx what changed?",
                    "type": "",
                    "props": {},
                },
                "team_id": "team-1",
                "channel_type": "O",
            },
            "broadcast": {"channel_id": "channel-1", "team_id": "team-1"},
        }
    )

    event = MattermostEventNormalizer(_listener_config()).normalize(envelope)

    assert event is None


def test_replay_rendering_is_deterministic_and_emits_citations_once() -> None:
    answer = _answer(answer="Use this [[1]]()", blurb="Preview text")

    first = format_mattermost_answer(answer, response_type="quotes", max_part_chars=80)
    second = format_mattermost_answer(answer, response_type="quotes", max_part_chars=80)

    assert first == second
    assert first.count("Sources:") == 1
    assert first.count("> Preview text") == 1


def test_source_only_chunks_are_bounded_with_oversized_source_entry() -> None:
    max_part_chars = 72
    parts = format_mattermost_answer_parts(
        _answer(
            answer="Use this [[1]]().",
            top_documents=[
                _search_doc(
                    blurb="preview " * 20,
                    semantic_identifier="oversized-source-name-without-spaces" * 3,
                    link="https://example.test/" + ("path" * 20),
                )
            ],
        ),
        include_source_previews=True,
        max_part_chars=max_part_chars,
    )

    assert all(len(part) <= max_part_chars for part in parts)
    assert parts[0] == "Use this [[1]]()."
    assert "".join(parts[1:]).count("Sources:") == 1
    assert "oversized-source-name" in "".join(parts[1:])


def test_unsourced_answer_chunks_are_bounded_without_source_lines() -> None:
    max_part_chars = 72
    parts = format_mattermost_answer_parts(
        _answer(
            answer="unsourced paragraph " * 12,
            citations=[],
            top_documents=[],
        ),
        max_part_chars=max_part_chars,
    )

    assert len(parts) > 1
    assert all(len(part) <= max_part_chars for part in parts)
    assert "Sources:" not in "".join(parts)


def test_primary_failure_mode_preserves_agent_prompt_ownership_for_style_controls() -> (
    None
):
    context = _build_mattermost_context(_event(), response_style="detailed")

    assert (
        "selected Onyx Agent Instructions remain the only base personality source"
        in context
    )
    assert "system_prompt" not in context
    assert "duplicate system prompt" not in context
    assert "preserve citations plus safety-critical detail" in context


def _event() -> NormalizedMattermostEvent:
    return NormalizedMattermostEvent(
        event_type=MattermostNormalizedEventType.CHANNEL_MENTION,
        session_key="mattermost:channel:team-1:channel-1:post-root-1",
        team_id="team-1",
        channel_id="channel-1",
        post_id="post-root-1",
        root_post_id="post-root-1",
        user_id="user-1",
        text="What changed?",
        raw_event_type="posted",
        dedupe_key="event_id:post-root-1",
    )


def _listener_config() -> MattermostListenerConfig:
    return MattermostListenerConfig(
        bot_user_id="bot-user-1",
        bot_mentions=frozenset({"@onyx"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_team_ids=frozenset({"team-1"}),
        approved_user_ids=frozenset({"approved-user"}),
        root_post_channel_ids=frozenset(),
        owned_thread_root_ids=set(),
        owned_answer_post_root_ids={},
        owned_answer_post_message_ids={},
        initial_reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=2.0,
    )


def _answer(
    *,
    answer: str,
    citations: list[CitationInfo] | None = None,
    top_documents: list[SearchDoc] | None = None,
    blurb: str = "",
) -> ChatBasicResponse:
    return ChatBasicResponse(
        answer=answer,
        answer_citationless=answer,
        top_documents=top_documents
        if top_documents is not None
        else [_search_doc(blurb=blurb)],
        error_msg=None,
        message_id=22,
        citation_info=(
            citations
            if citations is not None
            else [CitationInfo(citation_number=1, document_id="doc-1")]
        ),
    )


def _search_doc(
    *,
    blurb: str,
    semantic_identifier: str = "Mattermost Doc",
    link: str = "https://example.test/doc",
) -> SearchDoc:
    return SearchDoc(
        document_id="doc-1",
        chunk_ind=0,
        semantic_identifier=semantic_identifier,
        link=link,
        blurb=blurb,
        source_type=DocumentSource.WEB,
        boost=0,
        hidden=False,
        metadata={},
        score=1.0,
        match_highlights=[],
        updated_at=None,
    )
