from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.db.models import Document as DBDocument
from onyx.db.models import DocumentByConnectorCredentialPair
from onyx.db.seafile_adoption import (
    adopt_ingestion_api_cc_pair_as_managed_seafile,
    rollback_managed_seafile_adoption,
)
from onyx.kg.models import KGStage
from tests.external_dependency_unit.indexing_helpers import (
    cleanup_cc_pair,
    make_cc_pair,
)


def _attach_docs(
    db_session: Session, connector_id: int, credential_id: int
) -> set[str]:
    doc_ids = {f"seafile-adopt-test-{uuid4().hex}-{idx}" for idx in range(3)}
    for doc_id in doc_ids:
        db_session.add(
            DBDocument(
                id=doc_id,
                from_ingestion_api=True,
                boost=0,
                hidden=False,
                semantic_id=f"semantic-{doc_id}",
                link=f"https://seafile.example.com/lib/lib-1/file/{doc_id}.pdf",
                kg_stage=KGStage.NOT_STARTED,
            )
        )
        db_session.add(
            DocumentByConnectorCredentialPair(
                id=doc_id,
                connector_id=connector_id,
                credential_id=credential_id,
                has_been_indexed=True,
            )
        )
    db_session.commit()
    return doc_ids


def test_seafile_adoption_preserves_pair_and_document_associations(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    doc_ids = _attach_docs(db_session, pair.connector_id, pair.credential_id)
    try:
        result = adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={
                "base_url": "https://seafile.example.com",
                "library_ids": ["lib-1"],
                "excluded_paths": ["/Archive/*", "/Inbox/*"],
                "ingestion_api_document_id_mappings": {
                    f"file-{idx}": doc_id for idx, doc_id in enumerate(sorted(doc_ids))
                },
            },
            seafile_credential_updates={"seafile_api_token": "token-redacted-in-api"},
            connector_name="OneQode Managed Seafile",
        )

        db_session.refresh(pair)
        assert result.connector_id == pair.connector_id
        assert result.credential_id == pair.credential_id
        assert result.cc_pair_id == pair.id
        assert result.adopted_document_count == 3
        assert pair.connector.source is DocumentSource.SEAFILE
        assert pair.credential.source is DocumentSource.SEAFILE
        assert pair.connector.name == "OneQode Managed Seafile"
        assert pair.connector.connector_specific_config["seafile_managed_adoption"][
            "document_ids"
        ] == sorted(doc_ids)
        assert (
            db_session.query(DocumentByConnectorCredentialPair)
            .filter(
                DocumentByConnectorCredentialPair.connector_id == pair.connector_id,
                DocumentByConnectorCredentialPair.credential_id == pair.credential_id,
            )
            .count()
            == 3
        )

        again = adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={},
            seafile_credential_updates={"seafile_api_token": "unused"},
        )
        assert again.was_already_adopted is True
    finally:
        cleanup_cc_pair(db_session, pair)


def test_seafile_adoption_fails_closed_on_inventory_mismatch(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    doc_ids = _attach_docs(db_session, pair.connector_id, pair.credential_id)
    try:
        missing_one_doc = set(sorted(doc_ids)[1:])
        try:
            adopt_ingestion_api_cc_pair_as_managed_seafile(
                db_session,
                cc_pair_id=pair.id,
                expected_document_ids=missing_one_doc,
                seafile_connector_config={"base_url": "https://seafile.example.com"},
                seafile_credential_updates={"seafile_api_token": "token"},
            )
        except ValueError as exc:
            assert "inventory mismatch" in str(exc)
        else:
            raise AssertionError("adoption should fail closed on incomplete inventory")

        db_session.refresh(pair)
        assert pair.connector.source is DocumentSource.INGESTION_API
        assert pair.credential.source is DocumentSource.INGESTION_API
    finally:
        cleanup_cc_pair(db_session, pair)


def test_managed_seafile_adoption_rolls_back_without_changing_stable_ids(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    doc_ids = _attach_docs(db_session, pair.connector_id, pair.credential_id)
    original_connector_id = pair.connector_id
    original_credential_id = pair.credential_id
    try:
        adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={"base_url": "https://seafile.example.com"},
            seafile_credential_updates={"seafile_api_token": "token"},
        )

        rollback_managed_seafile_adoption(db_session, cc_pair_id=pair.id)

        db_session.refresh(pair)
        assert pair.connector_id == original_connector_id
        assert pair.credential_id == original_credential_id
        assert pair.connector.source is DocumentSource.INGESTION_API
        assert pair.credential.source is DocumentSource.INGESTION_API
        assert {
            row[0]
            for row in db_session.query(DocumentByConnectorCredentialPair.id)
            .filter(
                DocumentByConnectorCredentialPair.connector_id == pair.connector_id,
                DocumentByConnectorCredentialPair.credential_id == pair.credential_id,
            )
            .all()
        } == doc_ids
    finally:
        cleanup_cc_pair(db_session, pair)
