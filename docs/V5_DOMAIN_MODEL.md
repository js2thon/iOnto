# iOnto V5.0 Domain Model Foundation

## Scope

V5.0 makes business objects the semantic center of ontology modeling. Source tables,
files and columns remain source metadata; they are not the identity of ontology concepts.

This first increment adds a typed domain package and a V3/V4 compatibility adapter.
It does not replace the Streamlit runtime model or query engine.

## Six core models

| Model | Responsibility | Stable references |
|---|---|---|
| ObjectType | Business object concept and lifecycle | Property, Identity, Relationship IDs |
| Property | Typed attribute belonging to an ObjectType | Domain ObjectType ID |
| Relationship | Directed semantic association between two ObjectTypes | Source/target ObjectType IDs |
| Identity | Technical, natural, business or composite identity rule | Property IDs |
| Constraint | Machine-checkable rule with severity and repair policy | Target reference |
| MappingContract | Declarative source-to-ontology projection contract | Source and target references |

All model IDs are stable identifiers and must not be derived from display labels.
Renaming a label therefore does not change object identity or dependent references.

## Cardinality semantics

For a directed relationship A -> B:
- `source_min_cardinality/source_max_cardinality` describe how many outgoing
  relationship instances one A instance may have.
- `target_min_cardinality/target_max_cardinality` describe how many incoming
  relationship instances one B instance may have.

An omitted maximum means unbounded. This is intentionally explicit rather than
relying on ambiguous labels such as 1:N.

## Mapping safety

MappingContract transformations are declarative specifications. Arbitrary Python,
scripts, eval expressions or executable code are rejected. The execution layer must
implement an allow-listed transform vocabulary.

The legacy adapter preserves old `data_mapping` metadata but does not automatically
claim that similarly named source columns are mapped to ontology properties. A
field-level MappingContract requires an explicit mapping decision and evidence.

## Migration behavior

- Legacy ObjectTypes and Properties receive deterministic UUID5-based IDs.
- Legacy primary keys become technical Identity definitions.
- Legacy link types become candidate Relationships.
- Legacy source/table/column metadata is preserved under
  `OntologySchema.metadata.legacy_data_mapping`.
- Existing instance records are not rewritten in this increment.
- The old runtime `Ontology` class and deterministic query pipeline remain intact.

## Validation

`OntologySchema.validate()` returns all detected structural/reference errors.
Each individual model also exposes `validate()` for local validation.
The package uses only the Python standard library.

Run tests:

```bash
python -m unittest tests.test_ontology_domain_models -v
```

## Next increments

1. Add instance envelope and source-record lineage models.
2. Add schema-aware mapping contracts and a safe transform registry.
3. Add data profiling, identity resolution and relationship evidence models.
4. Add SHACL export/validation as an optional quality engine.
5. Introduce dual-read validation before switching the workbench to V5 models.


## Instance and source-lineage model

Runtime instances are represented separately from the schema by `OntologyInstance`.
Each instance carries:
- stable instance ID and ObjectType ID;
- identity values keyed by ontology Property IDs;
- property values keyed by ontology Property IDs;
- outgoing relationship references keyed by Relationship IDs;
- ontology schema version and mapping contract versions;
- valid-time interval and observed timestamp;
- record-level and field-level source lineage.

`SourceRecordRef` locates a source record using a source identifier plus a table
primary/business key, file URI, sheet/row, page, or document locator. A locator
must contain at least one concrete record/document position.

`FieldLineage` connects a target ontology Property to a source record and optional
source field, value hash, MappingContract ID/version, transformation reference,
confidence and evidence. This supports field-level provenance without storing raw
sensitive source values in lineage metadata.

The model validates that instance properties and outgoing relationships belong to
the declared ObjectType, and that lineage targets are valid properties in the
selected ontology schema. It does not itself execute mappings or resolve target
instance IDs; those belong to the mapping/materialization service.

Tests:

```bash
python -m unittest discover -s tests -p "test_ontology_instance_lineage.py" -v
```


## Deterministic MappingContract execution

`ontology_domain.mapping_engine.execute_mapping(schema, source_data, run_id=None)`
executes approved/active contracts and returns a run result containing materialized
`OntologyInstance` objects, contract versions, blocked contracts and row-level errors.

### Property mappings

A property contract uses:
- `source_ref.table` (or `dataset`) and `source_ref.field` (or `column`);
- `target_ref.object_type_id` and `target_ref.property_id`;
- `mapping_kind=direct` or `transform`;
- optional `transform.operations`, an ordered allow-list of deterministic operations.

Supported operations: trim, lower, upper, to_string, to_integer, to_decimal,
to_boolean, replace, map_values, date_iso, datetime_iso and regex_replace.
No Python, eval, script, SQL or arbitrary expression execution is permitted.

A row is materialized only when an ObjectType identity can be resolved from its
mapped properties. IDs are deterministic hashes of the ObjectType ID and identity
values. Duplicate identity rows with conflicting properties are reported rather
than overwriting an earlier instance. Required properties and declared basic data
types are checked before materialization.

### Relationship mappings

A relationship contract uses `mapping_kind=join`, a target Relationship ID,
`target_ref.target_table`, and explicit `join_keys` entries containing
`source_field` and `target_field`. Both endpoint datasets must have approved
property mappings and materialized instances. The engine then creates outgoing
relationship references using the deterministic instance IDs.

### Review and publication gate

Only contracts with `review_status=approved` and `status=active` or
`released` are executed. Pending/draft contracts are reported as blocked.
Any blocked contract or row-level error makes the run's `success` false. The
caller must not publish a run unless success is true and downstream quality checks
also pass.

### Lineage

Each mapped property receives a `FieldLineage` entry containing source table,
row locator, source field, source-value hash, mapping contract ID/version and
available evidence. Each materialized relationship edge receives a
`RelationshipLineage` entry containing both endpoint source locators, the join
contract/version and a hash of the join-key values. Raw source values are not
copied into lineage metadata.

### Scope boundary

This increment provides a deterministic in-memory execution service for data
already loaded by the current connectors. It does not yet persist instances to a
database, stream very large datasets, schedule incremental CDC, or replace the
legacy `Ontology.add_objects` runtime. Those are integration/persistence work
items for the next increment.
