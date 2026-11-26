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
