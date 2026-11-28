"""iOnto V5 ontology domain model package."""
from .models import (
    SCHEMA_VERSION, Constraint, Identity, MappingContract, ObjectType,
    OntologySchema, Property, Relationship, SemanticTerms, new_id,
)
from .legacy_adapter import legacy_snapshot_to_v5
from .instances import FieldLineage, OntologyInstance, RelationshipLineage, SourceLineage, SourceRecordRef
from .mapping_engine import MappingExecutionError, apply_transform, execute_mapping
from .sqlite_repository import RepositoryError, SQLiteOntologyRepository
from .runtime_bridge import (
    ensure_v5_schema,
    project_structure_to_legacy,
    publish_precheck,
    structure_diff,
)

__all__ = [
    "SCHEMA_VERSION", "Constraint", "Identity", "MappingContract", "ObjectType",
    "OntologySchema", "Property", "Relationship", "SemanticTerms", "new_id",
    "legacy_snapshot_to_v5", "FieldLineage", "OntologyInstance", "SourceLineage",
    "SourceRecordRef", "RelationshipLineage", "MappingExecutionError", "apply_transform", "execute_mapping",
    "RepositoryError", "SQLiteOntologyRepository",
    "ensure_v5_schema", "project_structure_to_legacy", "publish_precheck", "structure_diff",
]
