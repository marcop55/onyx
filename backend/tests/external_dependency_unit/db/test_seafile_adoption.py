from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.db.enums import IndexingStatus
from onyx.db.models import Document as DBDocument
from onyx.db.models import (
    DocumentByConnectorCredentialPair,
    DocumentSet,
    DocumentSet__ConnectorCredentialPair,
    IndexAttempt,
    Persona,
    Persona__DocumentSet,
)
from onyx.db.seafile_adoption import (
    adopt_ingestion_api_cc_pair_as_managed_seafile,
    rollback_managed_seafile_adoption,
)
from onyx.db.search_settings import get_current_search_settings
from onyx.kg.models import KGStage
from tests.external_dependency_unit.indexing_helpers import (
    cleanup_cc_pair,
    make_cc_pair,
)


def _attach_docs(
    db_session: Session, connector_id: int, credential_id: int, *, count: int = 3
) -> set[str]:
    doc_ids = {f"seafile-adopt-test-{uuid4().hex}-{idx}" for idx in range(count)}
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


def _cleanup_document_set_and_persona(
    db_session: Session, document_set: DocumentSet, persona: Persona
) -> None:
    db_session.query(Persona__DocumentSet).filter(
        Persona__DocumentSet.persona_id == persona.id,
        Persona__DocumentSet.document_set_id == document_set.id,
    ).delete(synchronize_session="fetch")
    db_session.query(DocumentSet__ConnectorCredentialPair).filter(
        DocumentSet__ConnectorCredentialPair.document_set_id == document_set.id,
    ).delete(synchronize_session="fetch")
    db_session.delete(persona)
    db_session.delete(document_set)
    db_session.commit()


def _contains_value(value: Any, needle: str) -> bool:
    if value == needle:
        return True
    if isinstance(value, dict):
        return any(_contains_value(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, needle) for item in value)
    return False


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


def test_managed_seafile_adoption_keeps_rollback_credentials_encrypted(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    original_token = f"ingestion-secret-{uuid4().hex}"
    seafile_token = f"seafile-secret-{uuid4().hex}"
    pair.credential.credential_json = cast(Any, {"api_token": original_token})
    db_session.commit()
    doc_ids = _attach_docs(db_session, pair.connector_id, pair.credential_id)
    try:
        adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={"base_url": "https://seafile.example.com"},
            seafile_credential_updates={"seafile_api_token": seafile_token},
        )

        db_session.expire_all()
        connector_config = pair.connector.connector_specific_config
        assert not _contains_value(connector_config, original_token)
        assert not _contains_value(connector_config, seafile_token)

        rollback_managed_seafile_adoption(db_session, cc_pair_id=pair.id)

        db_session.expire_all()
        credential_json = pair.credential.credential_json
        assert credential_json is not None
        assert credential_json.get_value(apply_mask=False) == {
            "api_token": original_token
        }
    finally:
        cleanup_cc_pair(db_session, pair)


def test_seafile_adoption_preserves_document_set_agent_and_index_identity(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    doc_ids = _attach_docs(db_session, pair.connector_id, pair.credential_id)
    document_set = DocumentSet(
        name=f"seafile-adoption-document-set-{uuid4().hex}",
        description="managed seafile adoption fixture",
        is_public=True,
        is_up_to_date=True,
    )
    persona = Persona(
        name=f"seafile-adoption-agent-{uuid4().hex}",
        description="managed seafile adoption fixture",
        is_public=True,
    )
    try:
        db_session.add(document_set)
        db_session.add(persona)
        db_session.flush()
        db_session.add(
            DocumentSet__ConnectorCredentialPair(
                document_set_id=document_set.id,
                connector_credential_pair_id=pair.id,
                is_current=True,
            )
        )
        db_session.add(
            Persona__DocumentSet(
                persona_id=persona.id,
                document_set_id=document_set.id,
            )
        )
        search_settings = get_current_search_settings(db_session)
        index_attempt = IndexAttempt(
            connector_credential_pair_id=pair.id,
            from_beginning=True,
            status=IndexingStatus.SUCCESS,
            search_settings_id=search_settings.id,
        )
        db_session.add(index_attempt)
        db_session.commit()

        result = adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={"base_url": "https://seafile.example.com"},
            seafile_credential_updates={"seafile_api_token": "token"},
        )

        db_session.expire_all()
        assert result.cc_pair_id == pair.id
        assert (
            db_session.query(DocumentSet__ConnectorCredentialPair)
            .filter(
                DocumentSet__ConnectorCredentialPair.document_set_id == document_set.id,
                DocumentSet__ConnectorCredentialPair.connector_credential_pair_id
                == pair.id,
                DocumentSet__ConnectorCredentialPair.is_current.is_(True),
            )
            .count()
            == 1
        )
        assert (
            db_session.query(Persona__DocumentSet)
            .filter(
                Persona__DocumentSet.persona_id == persona.id,
                Persona__DocumentSet.document_set_id == document_set.id,
            )
            .count()
            == 1
        )
        persisted_index_attempt = db_session.get(IndexAttempt, index_attempt.id)
        assert persisted_index_attempt is not None
        assert persisted_index_attempt.connector_credential_pair_id == pair.id
    finally:
        db_session.rollback()
        db_session.query(IndexAttempt).filter(
            IndexAttempt.connector_credential_pair_id == pair.id
        ).delete(synchronize_session="fetch")
        db_session.commit()
        _cleanup_document_set_and_persona(db_session, document_set, persona)
        cleanup_cc_pair(db_session, pair)


def test_seafile_adoption_uses_deterministic_482_to_278_inventory(
    db_session: Session,
) -> None:
    pair = make_cc_pair(db_session, source=DocumentSource.INGESTION_API)
    admitted_paths = {f"/Admitted/doc-{idx:03}.pdf" for idx in range(278)}
    rejected_paths = {f"/Archive/rejected-{idx:03}.pdf" for idx in range(204)}
    assert len(admitted_paths | rejected_paths) == 482
    doc_ids = _attach_docs(
        db_session, pair.connector_id, pair.credential_id, count=len(admitted_paths)
    )
    try:
        mappings = {
            f"lib-1:{path}": doc_id
            for path, doc_id in zip(
                sorted(admitted_paths), sorted(doc_ids), strict=True
            )
        }

        result = adopt_ingestion_api_cc_pair_as_managed_seafile(
            db_session,
            cc_pair_id=pair.id,
            expected_document_ids=doc_ids,
            seafile_connector_config={
                "base_url": "https://seafile.example.com",
                "library_ids": ["lib-1"],
                "excluded_paths": sorted(rejected_paths),
                "ingestion_api_document_id_mappings": mappings,
            },
            seafile_credential_updates={"seafile_api_token": "token"},
        )

        db_session.refresh(pair)
        assert result.adopted_document_count == 278
        assert pair.connector.connector_specific_config["seafile_managed_adoption"][
            "document_ids"
        ] == sorted(doc_ids)
        assert (
            pair.connector.connector_specific_config[
                "ingestion_api_document_id_mappings"
            ]
            == mappings
        )
    finally:
        cleanup_cc_pair(db_session, pair)
