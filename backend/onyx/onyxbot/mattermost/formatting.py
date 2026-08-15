"""Mattermost answer formatting helpers."""

from __future__ import annotations

import re

from onyx.chat.models import ChatBasicResponse


def format_mattermost_answer(answer: ChatBasicResponse) -> str:
    """Render an Onyx answer as Mattermost-safe Markdown."""

    if not answer.citation_info or not answer.top_documents:
        return answer.answer

    rendered_answer = answer.answer
    citation_lines: list[str] = []
    for citation in sorted(answer.citation_info, key=lambda item: item.citation_number):
        document = next(
            (
                candidate
                for candidate in answer.top_documents
                if candidate.document_id == citation.document_id
            ),
            None,
        )
        if document is None or not document.link:
            rendered_answer = re.sub(
                rf"\s*\[\[{citation.citation_number}\]\]\(\)",
                "",
                rendered_answer,
            )
            continue

        source_name = document.semantic_identifier or document.document_id
        citation_lines.append(
            f"[{citation.citation_number}] {source_name} - {document.link}"
        )

    if not citation_lines:
        return rendered_answer
    return rendered_answer + "\n\nSources:\n" + "\n".join(citation_lines)
