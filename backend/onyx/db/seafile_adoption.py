from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.connectors.models import InputType
from onyx.db.enums import ConnectorCredentialPairStatus
from onyx.db.models import (
    Connector,
    ConnectorCredentialPair,
    Credential,
    DocumentByConnectorCredentialPair,
)

_SEAFILE_ADOPTION_STATE_KEY = "seafile_managed_adoption"
_SEAFILE_ROLLBACK_STATE_KEY = "previous_ingestion_api_state"
_SEAFILE_ROLLBACK_CREDENTIAL_STATE_KEY = (
    "onyx_seafile_previous_ingestion_api_credential_json"
)


@dataclass(frozen=True)
class SeafileAdoptionResult:
    cc_pair_id: int
    connector_id: int
    credential_id: int
    adopted_document_count: int
    was_already_adopted: bool


def adopt_ingestion_api_cc_pair_as_managed_seafile(
    db_session: Session,
    *,
    cc_pair_id: int,
    expected_document_ids: set[str],
    seafile_connector_config: dict[str, Any],
    seafile_credential_updates: dict[str, Any],
    connector_name: str = "Managed Seafile",
) -> SeafileAdoptionResult:
    """Convert one existing Ingestion API connector/credential pair in place.

    This intentionally updates the existing connector, credential and cc-pair rows
    instead of creating replacements, so document-set, Agent and index-attempt
    references that point at the cc-pair id remain stable. It fails closed if the
    current document association inventory differs from the caller's preflight.
    """

    if not expected_document_ids:
        raise ValueError("Seafile adoption requires a non-empty expected inventory")
    if not seafile_credential_updates:
        raise ValueError("Seafile adoption requires credential updates")

    cc_pair = (
        db_session.execute(
            select(ConnectorCredentialPair)
            .where(ConnectorCredentialPair.id == cc_pair_id)
            .with_for_update(of=ConnectorCredentialPair)
        )
        .unique()
        .scalar_one_or_none()
    )
    if cc_pair is None:
        raise ValueError(f"Connector credential pair {cc_pair_id} was not found")

    connector = cc_pair.connector
    credential = cc_pair.credential
    if _is_adopted(connector, credential):
        _validate_existing_adoption(
            db_session, cc_pair, connector, credential, expected_document_ids
        )
        return SeafileAdoptionResult(
            cc_pair_id=cc_pair.id,
            connector_id=connector.id,
            credential_id=credential.id,
            adopted_document_count=len(expected_document_ids),
            was_already_adopted=True,
        )

    if connector.source is not DocumentSource.INGESTION_API:
        raise ValueError("Only Ingestion API connectors can be adopted as Seafile")
    if credential.source is not DocumentSource.INGESTION_API:
        raise ValueError("Only Ingestion API credentials can be adopted as Seafile")
    if cc_pair.status not in ConnectorCredentialPairStatus.indexable_statuses():
        raise ValueError(
            f"CC pair {cc_pair.id} is not indexable: {cc_pair.status.value}"
        )

    actual_document_ids = _document_ids_for_pair(db_session, cc_pair)
    if actual_document_ids != expected_document_ids:
        raise ValueError(
            "Seafile adoption inventory mismatch: expected "
            f"{len(expected_document_ids)} docs, found {len(actual_document_ids)} docs"
        )

    _assert_no_second_seafile_pair(db_session, cc_pair.id)

    previous_connector_config = dict(connector.connector_specific_config or {})
    previous_credential_json = (
        credential.credential_json.get_value(apply_mask=False)
        if credential.credential_json is not None
        else {}
    )
    previous_state = {
        "connector_name": connector.name,
        "connector_source": connector.source.value,
        "connector_input_type": connector.input_type.value,
        "connector_specific_config": previous_connector_config,
        "connector_refresh_freq": connector.refresh_freq,
        "connector_prune_freq": connector.prune_freq,
        "credential_source": credential.source.value,
        "credential_json_sha256": _credential_json_sha256(previous_credential_json),
        "document_ids": sorted(actual_document_ids),
    }

    connector.name = connector_name
    connector.source = DocumentSource.SEAFILE
    connector.input_type = InputType.LOAD_STATE
    connector.connector_specific_config = {
        **seafile_connector_config,
        _SEAFILE_ADOPTION_STATE_KEY: {
            "cc_pair_id": cc_pair.id,
            "document_count": len(actual_document_ids),
            "document_ids": sorted(actual_document_ids),
        },
        _SEAFILE_ROLLBACK_STATE_KEY: previous_state,
    }
    credential.source = DocumentSource.SEAFILE
    cast(Any, credential).credential_json = {
        **previous_credential_json,
        **seafile_credential_updates,
        _SEAFILE_ROLLBACK_CREDENTIAL_STATE_KEY: previous_credential_json,
    }

    db_session.commit()
    return SeafileAdoptionResult(
        cc_pair_id=cc_pair.id,
        connector_id=connector.id,
        credential_id=credential.id,
        adopted_document_count=len(actual_document_ids),
        was_already_adopted=False,
    )


def rollback_managed_seafile_adoption(
    db_session: Session,
    *,
    cc_pair_id: int,
) -> SeafileAdoptionResult:
    cc_pair = (
        db_session.execute(
            select(ConnectorCredentialPair)
            .where(ConnectorCredentialPair.id == cc_pair_id)
            .with_for_update(of=ConnectorCredentialPair)
        )
        .unique()
        .scalar_one_or_none()
    )
    if cc_pair is None:
        raise ValueError(f"Connector credential pair {cc_pair_id} was not found")

    connector = cc_pair.connector
    credential = cc_pair.credential
    state = (connector.connector_specific_config or {}).get(_SEAFILE_ROLLBACK_STATE_KEY)
    if not isinstance(state, dict):
        raise ValueError("Managed Seafile rollback state is missing")

    expected_document_ids = set(_string_list(state.get("document_ids")))
    actual_document_ids = _document_ids_for_pair(db_session, cc_pair)
    if actual_document_ids != expected_document_ids:
        raise ValueError("Managed Seafile rollback inventory mismatch")
    if state.get("rollback_completed") is True:
        _validate_completed_rollback(cc_pair, connector, credential, state)
        return SeafileAdoptionResult(
            cc_pair_id=cc_pair.id,
            connector_id=connector.id,
            credential_id=credential.id,
            adopted_document_count=len(actual_document_ids),
            was_already_adopted=False,
        )

    connector.name = _required_str(state, "connector_name")
    connector.source = DocumentSource(_required_str(state, "connector_source"))
    connector.input_type = InputType(_required_str(state, "connector_input_type"))
    restored_connector_config = dict(state.get("connector_specific_config") or {})
    restored_connector_config[_SEAFILE_ROLLBACK_STATE_KEY] = {
        **state,
        "rollback_completed": True,
    }
    connector.connector_specific_config = restored_connector_config
    connector.refresh_freq = state.get("connector_refresh_freq")
    connector.prune_freq = state.get("connector_prune_freq")
    credential.source = DocumentSource(_required_str(state, "credential_source"))
    current_credential_json = (
        credential.credential_json.get_value(apply_mask=False)
        if credential.credential_json is not None
        else {}
    )
    previous_credential_json = current_credential_json.get(
        _SEAFILE_ROLLBACK_CREDENTIAL_STATE_KEY
    )
    if not isinstance(previous_credential_json, dict):
        raise ValueError("Managed Seafile rollback credential state is missing")
    cast(Any, credential).credential_json = dict(previous_credential_json)

    db_session.commit()
    return SeafileAdoptionResult(
        cc_pair_id=cc_pair.id,
        connector_id=connector.id,
        credential_id=credential.id,
        adopted_document_count=len(actual_document_ids),
        was_already_adopted=False,
    )


def _is_adopted(connector: Connector, credential: Credential) -> bool:
    return (
        connector.source is DocumentSource.SEAFILE
        and credential.source is DocumentSource.SEAFILE
        and isinstance(connector.connector_specific_config, dict)
        and _SEAFILE_ADOPTION_STATE_KEY in connector.connector_specific_config
    )


def _validate_existing_adoption(
    db_session: Session,
    cc_pair: ConnectorCredentialPair,
    connector: Connector,
    credential: Credential,
    expected_document_ids: set[str],
) -> None:
    adoption_state = connector.connector_specific_config.get(
        _SEAFILE_ADOPTION_STATE_KEY
    )
    if not isinstance(adoption_state, dict):
        raise ValueError("Managed Seafile adoption state is invalid")
    if adoption_state.get("cc_pair_id") != cc_pair.id:
        raise ValueError("Managed Seafile adoption state points at a different pair")
    if credential.id != cc_pair.credential_id or connector.id != cc_pair.connector_id:
        raise ValueError("Managed Seafile pair identity changed")
    if set(_string_list(adoption_state.get("document_ids"))) != expected_document_ids:
        raise ValueError("Managed Seafile adoption inventory changed")
    actual_document_ids = _document_ids_for_pair(db_session, cc_pair)
    if actual_document_ids != expected_document_ids:
        raise ValueError("Managed Seafile live document associations changed")


def _validate_completed_rollback(
    cc_pair: ConnectorCredentialPair,
    connector: Connector,
    credential: Credential,
    state: dict[str, Any],
) -> None:
    if credential.id != cc_pair.credential_id or connector.id != cc_pair.connector_id:
        raise ValueError("Managed Seafile rollback pair identity changed")
    if connector.name != _required_str(state, "connector_name"):
        raise ValueError("Managed Seafile rollback connector name changed")
    if connector.source != DocumentSource(_required_str(state, "connector_source")):
        raise ValueError("Managed Seafile rollback connector source changed")
    if connector.input_type != InputType(_required_str(state, "connector_input_type")):
        raise ValueError("Managed Seafile rollback connector input type changed")
    if connector.refresh_freq != state.get("connector_refresh_freq"):
        raise ValueError("Managed Seafile rollback refresh frequency changed")
    if connector.prune_freq != state.get("connector_prune_freq"):
        raise ValueError("Managed Seafile rollback prune frequency changed")
    if credential.source != DocumentSource(_required_str(state, "credential_source")):
        raise ValueError("Managed Seafile rollback credential source changed")

    connector_config = dict(connector.connector_specific_config or {})
    connector_config.pop(_SEAFILE_ROLLBACK_STATE_KEY, None)
    if connector_config != dict(state.get("connector_specific_config") or {}):
        raise ValueError("Managed Seafile rollback connector config changed")

    credential_json = (
        credential.credential_json.get_value(apply_mask=False)
        if credential.credential_json is not None
        else {}
    )
    if _credential_json_sha256(credential_json) != _required_str(
        state, "credential_json_sha256"
    ):
        raise ValueError("Managed Seafile rollback credential state changed")


def _document_ids_for_pair(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> set[str]:
    return set(
        db_session.execute(
            select(DocumentByConnectorCredentialPair.id).where(
                DocumentByConnectorCredentialPair.connector_id == cc_pair.connector_id,
                DocumentByConnectorCredentialPair.credential_id
                == cc_pair.credential_id,
            )
        ).scalars()
    )


def _assert_no_second_seafile_pair(db_session: Session, cc_pair_id: int) -> None:
    count = db_session.execute(
        select(func.count())
        .select_from(ConnectorCredentialPair)
        .join(ConnectorCredentialPair.connector)
        .join(ConnectorCredentialPair.credential)
        .where(Connector.source == DocumentSource.SEAFILE)
        .where(ConnectorCredentialPair.id != cc_pair_id)
    ).scalar_one()
    if count:
        raise ValueError("A second managed Seafile connector/pair already exists")


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Managed Seafile rollback field is invalid: {key}")
    return value


def _credential_json_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Managed Seafile adoption document_ids are invalid")
    return value
