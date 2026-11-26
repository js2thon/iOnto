"""Compatibility adapters for legacy iOnto V3/V4 snapshots."""
from __future__ import annotations

from typing import Any, Dict
import uuid

from .models import (
    Identity, ObjectType, OntologySchema, Property, Relationship, SemanticTerms,
)

_NAMESPACE = uuid.UUID("cc6d7f0a-5e1c-4c0e-bb3d-2f86a6df2f0a")


def _stable_id(kind: str, *parts: str) -> str:
    key = ":".join((kind,) + tuple(str(p) for p in parts))
    return f"{kind}:{uuid.uuid5(_NAMESPACE, key)}"


def legacy_snapshot_to_v5(snapshot: Dict[str, Any]) -> OntologySchema:
    """Convert the legacy dictionary snapshot without inventing field mappings.

    Legacy names become stable IDs in this adapter. Existing table/column metadata
    is retained as provenance; a MappingContract is only created in a later step
    when an explicit source-field -> ontology-property decision exists.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Legacy ontology snapshot must be a dictionary")

    schema = OntologySchema(
        ontology_id=str(snapshot.get("ontology_id") or _stable_id("ontology", "legacy")),
        version=int(snapshot.get("version") or 1),
        metadata={
            "migrated_from": str(snapshot.get("schema_version") or snapshot.get("version") or "legacy"),
            "migration_mode": "compatibility",
        },
    )
    old_types = snapshot.get("object_types") or {}
    old_links = snapshot.get("link_types") or snapshot.get("relationships") or {}
    old_mapping = snapshot.get("data_mapping") or {}

    type_id_by_name: Dict[str, str] = {}
    property_id_by_type_and_name: Dict[tuple, str] = {}

    for type_name, raw in old_types.items():
        raw = raw if isinstance(raw, dict) else {}
        object_id = _stable_id("object-type", type_name)
        type_id_by_name[type_name] = object_id
        prop_names = list(raw.get("properties") or [])
        pk_name = raw.get("primary_key")
        if pk_name and pk_name not in prop_names:
            prop_names.insert(0, pk_name)
        prop_ids = []
        for prop_name in prop_names:
            prop_id = _stable_id("property", type_name, prop_name)
            property_id_by_type_and_name[(type_name, prop_name)] = prop_id
            prop_ids.append(prop_id)
            schema.properties[prop_id] = Property(
                id=prop_id,
                name=str(prop_name),
                label=str(prop_name),
                domain_object_type_id=object_id,
                data_type="string",
                description="Migrated from legacy schema; source type should be profiled.",
                metadata={"legacy_property_name": str(prop_name)},
            )
        identity_ids = []
        if pk_name and (type_name, pk_name) in property_id_by_type_and_name:
            identity_id = _stable_id("identity", type_name, str(pk_name))
            identity_ids.append(identity_id)
            schema.identities[identity_id] = Identity(
                id=identity_id,
                object_type_id=object_id,
                name=f"{type_name} legacy primary key",
                property_refs=[property_id_by_type_and_name[(type_name, pk_name)]],
                identity_type="technical",
                metadata={"migrated_from": "legacy_primary_key"},
            )
        schema.object_types[object_id] = ObjectType(
            id=object_id,
            name=str(type_name),
            label=str(type_name),
            description=str(raw.get("description") or ""),
            object_category="master",
            semantic_terms=SemanticTerms(),
            property_ids=prop_ids,
            identity_ids=identity_ids,
            metadata={"legacy_primary_key": pk_name, "legacy_type_name": str(type_name)},
        )

    for rel_name, raw in old_links.items():
        raw = raw if isinstance(raw, dict) else {}
        source_name = raw.get("source") or raw.get("source_type")
        target_name = raw.get("target") or raw.get("target_type")
        if source_name not in type_id_by_name or target_name not in type_id_by_name:
            continue
        rel_id = _stable_id("relationship", rel_name, source_name, target_name)
        schema.relationships[rel_id] = Relationship(
            id=rel_id,
            name=str(rel_name),
            label=str(rel_name),
            source_object_type_id=type_id_by_name[source_name],
            target_object_type_id=type_id_by_name[target_name],
            description=str(raw.get("description") or ""),
            status="candidate",
            metadata={
                "migrated_from": "legacy_link_type",
                "legacy_source_property": raw.get("source_property"),
                "legacy_target_property": raw.get("target_property"),
            },
        )
        schema.object_types[type_id_by_name[source_name]].relationship_ids.append(rel_id)
        schema.object_types[type_id_by_name[target_name]].relationship_ids.append(rel_id)

    # Preserve source/table/column inventory, but do not claim that columns are
    # mapped to business properties merely because their names happen to match.
    schema.metadata["legacy_data_mapping"] = old_mapping
    errors = schema.validate()
    if errors:
        raise ValueError("Legacy migration produced an invalid V5 schema: " + "; ".join(errors))
    return schema
