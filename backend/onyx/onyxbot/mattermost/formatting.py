"""Mattermost answer formatting helpers."""

from __future__ import annotations

import re
from html import unescape
from typing import TypedDict

from onyx.chat.models import ChatBasicResponse
from onyx.context.search.models import SearchDoc
from onyx.server.query_and_chat.streaming_models import CitationInfo

MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR = "\n\n---\n\n"
MATTERMOST_DEFAULT_MAX_PART_CHARS = 15_000
_MATTERMOST_RESPONSE_TYPE_CITATIONS = "citations"
_MATTERMOST_RESPONSE_TYPE_QUOTES = "quotes"

_HTML_NEWLINE_TAG_PATTERN = re.compile(
    r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|blockquote|section|article)>",
    re.IGNORECASE,
)
_HTML_TAG_PATTERN = re.compile(r"<(?!https?://|mailto:)/?[a-zA-Z][^>]*>")
_SLACK_LINK_PATTERN = re.compile(r"<((?:https?://|mailto:)[^|<>]+)\|([^<>]+)>")
_EMPTY_CITATION_LINK_PATTERN_TEMPLATE = r"\s*\[\[{citation_number}\]\]\(\)"


class MattermostPresentationConfig(TypedDict, total=False):
    response_type: str
    include_source_previews: bool
    answer_filters: list[str]


def format_mattermost_answer(
    answer: ChatBasicResponse,
    *,
    response_type: str = _MATTERMOST_RESPONSE_TYPE_CITATIONS,
    include_source_previews: bool = False,
    max_part_chars: int = MATTERMOST_DEFAULT_MAX_PART_CHARS,
) -> str:
    """Render an Onyx answer as Mattermost-safe Markdown."""

    rendered_answer = normalize_mattermost_markdown(answer.answer)
    if not answer.citation_info or not answer.top_documents:
        return rendered_answer

    cited_documents = _cited_documents(answer.citation_info, answer.top_documents)
    citation_lines: list[str] = []
    for citation, document in cited_documents:
        if document is None or not document.link:
            rendered_answer = _remove_empty_citation_link(
                rendered_answer, citation.citation_number
            )
            continue

        citation_lines.extend(
            _format_source_lines(
                citation_number=citation.citation_number,
                document=document,
                response_type=response_type,
                include_source_previews=include_source_previews,
            )
        )

    if not citation_lines:
        return rendered_answer

    sources = "Sources:\n" + "\n".join(citation_lines)
    return MATTERMOST_RESPONSE_PRESENTATION_SOURCE_ONCE_SEPARATOR.join(
        _split_answer_with_sources_once(
            answer=rendered_answer,
            sources=sources,
            max_part_chars=max_part_chars,
        )
    )


def normalize_mattermost_markdown(message: str) -> str:
    """Normalize generated text to Mattermost Markdown without hidden prompts."""

    normalized = _HTML_NEWLINE_TAG_PATTERN.sub("\n", message)
    normalized = _HTML_TAG_PATTERN.sub("", normalized)
    normalized = _SLACK_LINK_PATTERN.sub(r"[\2](\1)", normalized)
    return unescape(normalized).strip()


def should_skip_mattermost_answer(
    answer: ChatBasicResponse,
    *,
    channel_config: MattermostPresentationConfig | None,
    bypass_filters: bool,
) -> bool:
    if bypass_filters or channel_config is None:
        return False
    answer_filters = channel_config.get("answer_filters") or []
    return "well_answered_postfilter" in answer_filters and not answer.citation_info


def _cited_documents(
    citations: list[CitationInfo], top_documents: list[SearchDoc]
) -> list[tuple[CitationInfo, SearchDoc | None]]:
    cited_documents: list[tuple[CitationInfo, SearchDoc | None]] = []
    seen_document_ids: set[str] = set()
    for citation in sorted(citations, key=lambda item: item.citation_number):
        document = next(
            (
                candidate
                for candidate in top_documents
                if candidate.document_id == citation.document_id
            ),
            None,
        )
        if document is not None and document.document_id in seen_document_ids:
            continue
        if document is not None:
            seen_document_ids.add(document.document_id)
        cited_documents.append((citation, document))
    return cited_documents


def _remove_empty_citation_link(message: str, citation_number: int) -> str:
    return re.sub(
        _EMPTY_CITATION_LINK_PATTERN_TEMPLATE.format(citation_number=citation_number),
        "",
        message,
    )


def _format_source_lines(
    *,
    citation_number: int,
    document: SearchDoc,
    response_type: str,
    include_source_previews: bool,
) -> list[str]:
    source_name = normalize_mattermost_markdown(
        document.semantic_identifier or document.document_id
    ).replace("\n", " ")
    lines = [f"[{citation_number}] {source_name} - {document.link}"]
    preview = normalize_mattermost_markdown(document.blurb).replace("\n", " ").strip()
    if preview and response_type == _MATTERMOST_RESPONSE_TYPE_QUOTES:
        lines.append(f"> {preview}")
    elif preview and include_source_previews:
        lines.append(f"  Preview: {preview}")
    return lines


def _split_answer_with_sources_once(
    *,
    answer: str,
    sources: str,
    max_part_chars: int,
) -> list[str]:
    if max_part_chars <= len(sources) + 2:
        return [answer, sources]
    if len(answer) + len(sources) + 2 <= max_part_chars:
        return [answer + "\n\n" + sources]

    answer_parts = _split_text(answer, max_part_chars)
    final_answer_part = answer_parts[-1]
    if len(final_answer_part) + len(sources) + 2 <= max_part_chars:
        answer_parts[-1] = final_answer_part + "\n\n" + sources
        return answer_parts
    return answer_parts + [sources]


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts
