"""iOnto V5 ontology domain model package."""
from .models import (
    SCHEMA_VERSION, Constraint, Identity, MappingContract, ObjectType,
    OntologySchema, Property, Relationship, SemanticTerms, new_id,
)
from .legacy_adapter import legacy_snapshot_to_v5
from .instances import FieldLineage, OntologyInstance, SourceLineage, SourceRecordRef
from .mapping_engine import MappingExecutionError, apply_transform, execute_mapping

__all__ = [
    "SCHEMA_VERSION", "Constraint", "Identity", "MappingContract", "ObjectType",
    "OntologySchema", "Property", "Relationship", "SemanticTerms", "new_id",
    "legacy_snapshot_to_v5", "FieldLineage", "OntologyInstance", "SourceLineage",
    "SourceRecordRef", "MappingExecutionError", "apply_transform", "execute_mapping",
]
