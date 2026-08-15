from onyx.chat.models import ChatBasicResponse
from onyx.configs.constants import DocumentSource
from onyx.context.search.models import SearchDoc
from onyx.onyxbot.mattermost.formatting import format_mattermost_answer
from onyx.server.query_and_chat.streaming_models import CitationInfo


def test_format_mattermost_answer_preserves_citation_identity_and_links() -> None:
    answer = _answer(
        answer="Use the roadmap [2] and source export [1].",
        citation_info=[
            CitationInfo(citation_number=2, document_id="roadmap-doc"),
            CitationInfo(citation_number=1, document_id="source-export-doc"),
        ],
        top_documents=[
            _search_doc(
                document_id="source-export-doc",
                semantic_identifier="Source Export",
                link="https://example.test/export?doc=source-export-doc",
            ),
            _search_doc(
                document_id="roadmap-doc",
                semantic_identifier="Roadmap",
                link="https://example.test/roadmap?doc=roadmap-doc",
            ),
        ],
    )

    formatted = format_mattermost_answer(answer)

    assert formatted == (
        "Use the roadmap [2] and source export [1].\n\n"
        "Sources:\n"
        "[1] Source Export - https://example.test/export?doc=source-export-doc\n"
        "[2] Roadmap - https://example.test/roadmap?doc=roadmap-doc"
    )


def test_format_mattermost_answer_omits_unlinked_source_and_marker() -> None:
    answer = _answer(
        answer="Use internal notes [[3]]().",
        citation_info=[CitationInfo(citation_number=3, document_id="internal-doc")],
        top_documents=[
            _search_doc(
                document_id="internal-doc",
                semantic_identifier="Internal Notes",
                link=None,
            )
        ],
    )

    formatted = format_mattermost_answer(answer)

    assert formatted == "Use internal notes."


def _answer(
    *,
    answer: str,
    citation_info: list[CitationInfo],
    top_documents: list[SearchDoc],
) -> ChatBasicResponse:
    return ChatBasicResponse(
        answer=answer,
        answer_citationless=answer,
        top_documents=top_documents,
        error_msg=None,
        message_id=44,
        citation_info=citation_info,
    )


def _search_doc(
    *,
    document_id: str,
    semantic_identifier: str,
    link: str | None,
) -> SearchDoc:
    return SearchDoc(
        document_id=document_id,
        chunk_ind=0,
        semantic_identifier=semantic_identifier,
        link=link,
        blurb="",
        source_type=DocumentSource.WEB,
        boost=0,
        hidden=False,
        metadata={},
        score=1.0,
        match_highlights=[],
        updated_at=None,
    )
