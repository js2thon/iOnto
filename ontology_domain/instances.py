"""Ontology instance and source-lineage envelopes for iOnto V5.

Instances are runtime data, not ontology schema. Lineage captures source evidence
at record and field level so query answers can be traced to their origin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import OntologySchema, utc_now


@dataclass
class SourceRecordRef:
    """Locator for one source record or document fragment."""

    source_id: str
    dataset: Optional[str] = None
    table: Optional[str] = None
    record_key: Optional[Dict[str, Any]] = None
    file_name: Optional[str] = None
    file_uri: Optional[str] = None
    sheet_name: Optional[str] = None
    row_number: Optional[int] = None
    page_number: Optional[int] = None
    document_locator: Optional[str] = None
    source_version: Optional[str] = None
    content_hash: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("SourceRecordRef.source_id must be non-empty")
        if self.row_number is not None and self.row_number < 1:
            raise ValueError("SourceRecordRef.row_number must be >= 1")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("SourceRecordRef.page_number must be >= 1")
        if not any((self.record_key, self.row_number, self.page_number, self.document_locator, self.file_uri)):
            raise ValueError("SourceRecordRef requires a record, row, page, or document locator")


@dataclass
class FieldLineage:
    """Trace for one target property value."""

    target_property_id: str
    source: SourceRecordRef
    source_field: Optional[str] = None
    source_value_hash: Optional[str] = None
    transform_ref: Optional[str] = None
    mapping_contract_id: Optional[str] = None
    mapping_contract_version: Optional[int] = None
    confidence: Optional[float] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.target_property_id:
            raise ValueError("FieldLineage.target_property_id is required")
        self.source.validate()
        if self.mapping_contract_version is not None and self.mapping_contract_version < 1:
            raise ValueError("FieldLineage.mapping_contract_version must be >= 1")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("FieldLineage.confidence must be between 0 and 1")


@dataclass
class RelationshipLineage:
    """Provenance for one materialized ontology relationship edge."""

    relationship_id: str
    source: SourceRecordRef
    target: SourceRecordRef
    mapping_contract_id: Optional[str] = None
    mapping_contract_version: Optional[int] = None
    join_key_hash: Optional[str] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.relationship_id:
            raise ValueError("RelationshipLineage.relationship_id is required")
        self.source.validate()
        self.target.validate()
        if self.mapping_contract_version is not None and self.mapping_contract_version < 1:
            raise ValueError("RelationshipLineage.mapping_contract_version must be >= 1")


@dataclass
class SourceLineage:
    """Record-level provenance for an ontology instance."""

    sources: List[SourceRecordRef] = field(default_factory=list)
    fields: List[FieldLineage] = field(default_factory=list)
    relationships: List[RelationshipLineage] = field(default_factory=list)
    ingestion_run_id: Optional[str] = None
    mapping_run_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for source in self.sources:
            source.validate()
        for item in self.fields:
            item.validate()
        for item in self.relationships:
            item.validate()


@dataclass
class OntologyInstance:
    """A business object instance bound to explicit ontology/mapping versions."""

    id: str
    object_type_id: str
    identity_values: Dict[str, Any]
    properties: Dict[str, Any] = field(default_factory=dict)
    relationship_refs: Dict[str, List[str]] = field(default_factory=dict)
    ontology_id: Optional[str] = None
    ontology_version: int = 1
    mapping_versions: Dict[str, int] = field(default_factory=dict)
    lineage: SourceLineage = field(default_factory=SourceLineage)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    observed_at: str = field(default_factory=utc_now)
    status: str = "active"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self, schema: Optional[OntologySchema] = None) -> None:
        if not self.id or not self.object_type_id:
            raise ValueError("OntologyInstance id and object_type_id are required")
        if self.ontology_version < 1:
            raise ValueError("OntologyInstance.ontology_version must be >= 1")
        if not self.identity_values:
            raise ValueError("OntologyInstance.identity_values cannot be empty")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("OntologyInstance.valid_from cannot be after valid_to")
        for contract_id, version in self.mapping_versions.items():
            if version < 1:
                raise ValueError(f"Invalid mapping version for {contract_id}")
        self.lineage.validate()
        if schema is None:
            return
        obj = schema.object_types.get(self.object_type_id)
        if obj is None:
            raise ValueError(f"Unknown ObjectType: {self.object_type_id}")
        known_properties = {
            prop_id for prop_id, prop in schema.properties.items()
            if prop.domain_object_type_id == self.object_type_id
        }
        for property_id in self.properties:
            if property_id not in known_properties:
                raise ValueError(f"Property {property_id} does not belong to {self.object_type_id}")
        known_relationships = {
            rel_id for rel_id, rel in schema.relationships.items()
            if rel.source_object_type_id == self.object_type_id
        }
        for relationship_id, targets in self.relationship_refs.items():
            if relationship_id not in known_relationships:
                raise ValueError(f"Relationship {relationship_id} is not outgoing from {self.object_type_id}")
            rel = schema.relationships[relationship_id]
            for target_id in targets:
                if not isinstance(target_id, str) or not target_id:
                    raise ValueError("Relationship target instance IDs must be non-empty strings")
        for relation_trace in self.lineage.relationships:
            if relation_trace.relationship_id not in known_relationships:
                raise ValueError(f"Lineage references unknown Relationship {relation_trace.relationship_id}")
            if relation_trace.mapping_contract_id and relation_trace.mapping_contract_id not in schema.mapping_contracts:
                raise ValueError(f"Lineage references unknown MappingContract {relation_trace.mapping_contract_id}")
        for field_trace in self.lineage.fields:
            if field_trace.target_property_id not in known_properties:
                raise ValueError(f"Lineage references unknown target Property {field_trace.target_property_id}")
            if field_trace.mapping_contract_id and field_trace.mapping_contract_id not in schema.mapping_contracts:
                raise ValueError(f"Lineage references unknown MappingContract {field_trace.mapping_contract_id}")
        for contract_id in self.mapping_versions:
            if contract_id not in schema.mapping_contracts:
                raise ValueError(f"Unknown MappingContract in mapping_versions: {contract_id}")
