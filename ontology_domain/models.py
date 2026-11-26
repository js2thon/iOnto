"""Versioned domain model for iOnto V5.

This package deliberately uses the Python standard library so local deployments
do not need an additional validation/runtime dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import uuid

SCHEMA_VERSION = "5.0"


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _json_value(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


@dataclass
class SemanticTerms:
    business_definition: str = ""
    aliases: List[str] = field(default_factory=list)
    synonyms: List[str] = field(default_factory=list)


@dataclass
class ObjectType:
    id: str
    name: str
    label: str
    description: str = ""
    object_category: str = "master"
    semantic_terms: SemanticTerms = field(default_factory=SemanticTerms)
    property_ids: List[str] = field(default_factory=list)
    identity_ids: List[str] = field(default_factory=list)
    relationship_ids: List[str] = field(default_factory=list)
    status: str = "draft"
    version: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _non_empty(self.id, "ObjectType.id")
        _non_empty(self.name, "ObjectType.name")
        _non_empty(self.label, "ObjectType.label")
        if self.version < 1:
            raise ValueError("ObjectType.version must be >= 1")
        if self.object_category not in {"master", "transaction", "organization", "process", "reference", "event", "other"}:
            raise ValueError(f"Unsupported object_category: {self.object_category}")


@dataclass
class Property:
    id: str
    name: str
    label: str
    domain_object_type_id: str
    data_type: str = "string"
    description: str = ""
    required: bool = False
    nullable: bool = True
    min_cardinality: int = 0
    max_cardinality: Optional[int] = 1
    unit: Optional[str] = None
    format: Optional[str] = None
    enum_values: List[Any] = field(default_factory=list)
    semantic_terms: SemanticTerms = field(default_factory=SemanticTerms)
    classification: str = "internal"
    constraints: List[str] = field(default_factory=list)
    derived: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for name, value in (("id", self.id), ("name", self.name), ("label", self.label),
                            ("domain_object_type_id", self.domain_object_type_id)):
            _non_empty(value, f"Property.{name}")
        if self.data_type not in {"string", "integer", "decimal", "number", "boolean", "date", "datetime", "enum", "object_ref", "json"}:
            raise ValueError(f"Unsupported property data_type: {self.data_type}")
        if self.min_cardinality < 0 or (self.max_cardinality is not None and self.max_cardinality < self.min_cardinality):
            raise ValueError("Invalid property cardinality")
        if self.required and self.nullable:
            raise ValueError("A required property cannot be nullable")


@dataclass
class Relationship:
    id: str
    name: str
    label: str
    source_object_type_id: str
    target_object_type_id: str
    description: str = ""
    relationship_kind: str = "association"
    # For A -> B, source cardinality is the number of B instances per A;
    # target cardinality is the number of A instances per B.
    source_min_cardinality: int = 0
    source_max_cardinality: Optional[int] = 1
    target_min_cardinality: int = 0
    target_max_cardinality: Optional[int] = None
    inverse_relationship_id: Optional[str] = None
    mapping_ref: Optional[str] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    confidence: Optional[float] = None
    status: str = "candidate"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for name, value in (("id", self.id), ("name", self.name), ("label", self.label),
                            ("source_object_type_id", self.source_object_type_id),
                            ("target_object_type_id", self.target_object_type_id)):
            _non_empty(value, f"Relationship.{name}")
        if self.relationship_kind not in {"association", "aggregation", "composition", "temporal", "role"}:
            raise ValueError(f"Unsupported relationship_kind: {self.relationship_kind}")
        for minimum, maximum in ((self.source_min_cardinality, self.source_max_cardinality),
                                 (self.target_min_cardinality, self.target_max_cardinality)):
            if minimum < 0 or (maximum is not None and maximum < minimum):
                raise ValueError("Invalid relationship cardinality")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("Relationship.confidence must be between 0 and 1")


@dataclass
class Identity:
    id: str
    object_type_id: str
    name: str
    property_refs: List[str]
    identity_type: str = "business"
    normalization: List[str] = field(default_factory=list)
    case_sensitive: bool = True
    null_policy: str = "reject"
    unique_scope: str = "global"
    source_priority: List[str] = field(default_factory=list)
    status: str = "draft"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for name, value in (("id", self.id), ("object_type_id", self.object_type_id), ("name", self.name)):
            _non_empty(value, f"Identity.{name}")
        if not self.property_refs:
            raise ValueError("Identity.property_refs cannot be empty")
        if self.identity_type not in {"technical", "business", "natural", "composite", "legacy_synthetic"}:
            raise ValueError(f"Unsupported identity_type: {self.identity_type}")
        if self.null_policy not in {"reject", "ignore", "allow"}:
            raise ValueError(f"Unsupported null_policy: {self.null_policy}")


@dataclass
class Constraint:
    id: str
    name: str
    target_ref: str
    constraint_type: str
    severity: str = "error"
    expression: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    validation_engine: str = "native"
    auto_fix_policy: str = "none"
    message: str = ""
    source: str = "user"
    status: str = "active"

    def validate(self) -> None:
        for name, value in (("id", self.id), ("name", self.name), ("target_ref", self.target_ref),
                            ("constraint_type", self.constraint_type)):
            _non_empty(value, f"Constraint.{name}")
        if self.severity not in {"block", "error", "warning", "info"}:
            raise ValueError(f"Unsupported constraint severity: {self.severity}")
        if self.auto_fix_policy not in {"safe", "assist", "manual", "none"}:
            raise ValueError(f"Unsupported auto_fix_policy: {self.auto_fix_policy}")
        if self.validation_engine not in {"native", "shacl", "custom"}:
            raise ValueError(f"Unsupported validation_engine: {self.validation_engine}")


@dataclass
class MappingContract:
    id: str
    name: str
    source_ref: Dict[str, Any]
    target_ref: Dict[str, Any]
    mapping_kind: str = "direct"
    transform: Optional[Dict[str, Any]] = None
    null_policy: str = "preserve"
    default_value: Any = None
    identity_refs: List[str] = field(default_factory=list)
    join_keys: List[Dict[str, str]] = field(default_factory=list)
    quality_expectations: List[Dict[str, Any]] = field(default_factory=list)
    confidence: Optional[float] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    review_status: str = "pending"
    status: str = "draft"
    version: int = 1
    source_schema_hash: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _non_empty(self.id, "MappingContract.id")
        _non_empty(self.name, "MappingContract.name")
        if not self.source_ref or not self.target_ref:
            raise ValueError("MappingContract source_ref and target_ref are required")
        if self.mapping_kind not in {"direct", "transform", "join", "lookup", "derived"}:
            raise ValueError(f"Unsupported mapping_kind: {self.mapping_kind}")
        if self.null_policy not in {"preserve", "drop", "default", "reject"}:
            raise ValueError(f"Unsupported mapping null_policy: {self.null_policy}")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("MappingContract.confidence must be between 0 and 1")
        if self.version < 1:
            raise ValueError("MappingContract.version must be >= 1")
        # Transform is a declarative specification, never executable Python/code.
        if self.transform and any(k in self.transform for k in ("python", "code", "script", "eval")):
            raise ValueError("Executable code is forbidden in MappingContract.transform")


@dataclass
class OntologySchema:
    schema_version: str = SCHEMA_VERSION
    ontology_id: str = field(default_factory=lambda: new_id("ontology"))
    version: int = 1
    object_types: Dict[str, ObjectType] = field(default_factory=dict)
    properties: Dict[str, Property] = field(default_factory=dict)
    relationships: Dict[str, Relationship] = field(default_factory=dict)
    identities: Dict[str, Identity] = field(default_factory=dict)
    constraints: Dict[str, Constraint] = field(default_factory=dict)
    mapping_contracts: Dict[str, MappingContract] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        errors: List[str] = []
        collections = (
            ("object_types", self.object_types, ObjectType),
            ("properties", self.properties, Property),
            ("relationships", self.relationships, Relationship),
            ("identities", self.identities, Identity),
            ("constraints", self.constraints, Constraint),
            ("mapping_contracts", self.mapping_contracts, MappingContract),
        )
        for collection_name, values, _ in collections:
            for key, item in values.items():
                if key != item.id:
                    errors.append(f"{collection_name}[{key}] key does not match item.id")
                try:
                    item.validate()
                except ValueError as exc:
                    errors.append(str(exc))
        for prop in self.properties.values():
            if prop.domain_object_type_id not in self.object_types:
                errors.append(f"Property {prop.id} references missing ObjectType {prop.domain_object_type_id}")
        for rel in self.relationships.values():
            if rel.source_object_type_id not in self.object_types or rel.target_object_type_id not in self.object_types:
                errors.append(f"Relationship {rel.id} references a missing ObjectType")
            if rel.inverse_relationship_id:
                inverse = self.relationships.get(rel.inverse_relationship_id)
                if inverse is None:
                    errors.append(f"Relationship {rel.id} references missing inverse Relationship {rel.inverse_relationship_id}")
                elif (
                    inverse.source_object_type_id != rel.target_object_type_id
                    or inverse.target_object_type_id != rel.source_object_type_id
                ):
                    errors.append(f"Relationship {rel.id} inverse endpoints are not reversed")
        for identity in self.identities.values():
            if identity.object_type_id not in self.object_types:
                errors.append(f"Identity {identity.id} references a missing ObjectType")
            for prop_id in identity.property_refs:
                prop = self.properties.get(prop_id)
                if prop is None:
                    errors.append(f"Identity {identity.id} references missing Property {prop_id}")
                elif prop.domain_object_type_id != identity.object_type_id:
                    errors.append(f"Identity {identity.id} references Property {prop_id} from another ObjectType")
        for obj in self.object_types.values():
            for prop_id in obj.property_ids:
                prop = self.properties.get(prop_id)
                if prop is None:
                    errors.append(f"ObjectType {obj.id} references missing Property {prop_id}")
                elif prop.domain_object_type_id != obj.id:
                    errors.append(f"ObjectType {obj.id} references Property {prop_id} owned by another ObjectType")
            for identity_id in obj.identity_ids:
                identity = self.identities.get(identity_id)
                if identity is None:
                    errors.append(f"ObjectType {obj.id} references missing Identity {identity_id}")
                elif identity.object_type_id != obj.id:
                    errors.append(f"ObjectType {obj.id} references Identity {identity_id} owned by another ObjectType")
            for relationship_id in obj.relationship_ids:
                rel = self.relationships.get(relationship_id)
                if rel is None:
                    errors.append(f"ObjectType {obj.id} references missing Relationship {relationship_id}")
                elif obj.id not in {rel.source_object_type_id, rel.target_object_type_id}:
                    errors.append(f"ObjectType {obj.id} is not an endpoint of Relationship {relationship_id}")
        known_targets = set(self.object_types) | set(self.properties) | set(self.relationships) | set(self.identities) | set(self.mapping_contracts)
        for constraint in self.constraints.values():
            if constraint.target_ref not in known_targets:
                errors.append(f"Constraint {constraint.id} references unknown target {constraint.target_ref}")
        for contract in self.mapping_contracts.values():
            for identity_id in contract.identity_refs:
                identity = self.identities.get(identity_id)
                if identity is None:
                    errors.append(f"MappingContract {contract.id} references missing Identity {identity_id}")
                elif contract.target_ref.get("object_type_id") and identity.object_type_id != contract.target_ref.get("object_type_id"):
                    errors.append(f"MappingContract {contract.id} references Identity {identity_id} from another ObjectType")
            target = contract.target_ref
            for key, collection in (
                ("object_type_id", self.object_types),
                ("property_id", self.properties),
                ("relationship_id", self.relationships),
                ("identity_id", self.identities),
            ):
                target_id = target.get(key)
                if target_id and target_id not in collection:
                    errors.append(f"MappingContract {contract.id} references missing {key} {target_id}")
            object_type_id = target.get("object_type_id")
            property_id = target.get("property_id")
            if object_type_id and property_id in self.properties:
                if self.properties[property_id].domain_object_type_id != object_type_id:
                    errors.append(f"MappingContract {contract.id} target Property does not belong to ObjectType {object_type_id}")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        return _json_value(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OntologySchema":
        def build_map(key: str, typ: Any) -> Dict[str, Any]:
            result = {}
            for item_id, raw in (data.get(key) or {}).items():
                item = dict(raw)
                if "semantic_terms" in item and isinstance(item["semantic_terms"], dict):
                    item["semantic_terms"] = SemanticTerms(**item["semantic_terms"])
                result[item_id] = typ(**item)
            return result
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            ontology_id=data.get("ontology_id") or new_id("ontology"),
            version=int(data.get("version", 1)),
            object_types=build_map("object_types", ObjectType),
            properties=build_map("properties", Property),
            relationships=build_map("relationships", Relationship),
            identities=build_map("identities", Identity),
            constraints=build_map("constraints", Constraint),
            mapping_contracts=build_map("mapping_contracts", MappingContract),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_json(cls, value: str) -> "OntologySchema":
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("Ontology schema JSON root must be an object")
        return cls.from_dict(raw)
