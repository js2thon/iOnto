# iOnto V5.0 Overall Code Review

Review baseline: `main` after MappingContract execution PR #5
(`77ee282cd8cbf77e1302c51ecae0bc9a5b97e000`).

Review scope:
- `ontology_studio_v3.py`: ingestion, object/relationship runtime, persistence,
  version lifecycle, configuration and Streamlit navigation.
- `ontology_query_pipeline.py`: plan validation, deterministic execution,
  independent result validation and provenance.
- `ontology_domain/`: V5 domain schema, legacy adapter, instances, lineage and
  MappingContract executor.
- `requirements.txt`, README and GitHub Actions workflows.

This is a source-level engineering review plus CI verification. It is not a
production load test, security penetration test, or a live PostgreSQL/Streamlit
browser acceptance test.

## Findings addressed in this review branch

| Severity | Finding | Correction |
|---|---|---|
| Critical data integrity | Legacy builder could invent sequential IDs when no reliable key existed, silently skip null keys and overwrite duplicate keys | Require a valid explicit/inferred unique non-null key; reject null/duplicate keys and missing IDs |
| High query correctness | Path validator checked returned edges but did not independently verify the shortest path; an empty/tampered path could pass | Recompute shortest path from canonical graph and compare full path and edge evidence |
| High evidence completeness | Neighbor validator checked only that supplied edges existed, not that relation evidence was complete | Compare exact relation evidence against canonical graph |
| High ontology integrity | V5 identities/properties/relationships could reference objects they did not belong to; constraints and mapping targets were only partially checked | Validate ownership, endpoint and target references |
| High mapping safety | Executor could treat unsupported lookup/derived kinds like direct mapping | Explicitly support property direct/transform and relationship join; block unsupported kinds and empty mapping plans |
| High provenance | Database source rows could be identified only by query result row number, which is not stable without ORDER BY | Require configured record key fields for database sources and store them in lineage |
| High version governance | Version lifecycle could skip states; save accepted invalid statuses | Enforce draft → validated → released → deprecated transitions and valid initial status |
| High snapshot integrity | Round-trip check compared only counts/edge keys, missing data loss in properties or metadata | Compare complete normalized snapshots |
| Medium concurrency | All writes used the same `.tmp` path and could collide across sessions | Use unique same-directory temporary files, fsync and atomic replace |
| Medium path safety | Version IDs were interpolated into filesystem paths | Reject path separators and invalid version identifiers |
| Medium credential handling | LLM config was written directly to its final path | Use atomic owner-restricted temporary file replacement; config remains plaintext locally |
| Medium transform safety | Unknown transformation specification fields could be silently ignored | Reject unknown transform spec keys |

## Remaining gaps before V5 can be called end-to-end

### P1 — Product and architecture gaps

1. **Mapping workbench integration**: the V5 mapping executor is currently a Python service over in-memory row dictionaries. It is not yet connected to the Streamlit data-source screens, a visual field-mapping editor, preview/simulation, approval UI or release gate.
2. **Persistence**: V5 OntologySchema, MappingContracts, OntologyInstances and lineage are not yet stored in the workspace/version repository. Existing version snapshots still serialize the legacy `Ontology` runtime.
3. **Query migration**: online Q&A and CQ evaluation still execute against the legacy name/primary-key model. They do not yet query V5 stable ObjectType/Property/Relationship IDs or bind every plan to a V5 schema and mapping version.
4. **Business-semantic object discovery**: the legacy builder still treats table names as object types and column names as properties. It does not yet infer/merge business objects from business documents or across heterogeneous datasets.
5. **Relationship discovery**: legacy auto-detection remains largely column-name convention based. It does not yet combine value profiling, business-document evidence, semantic inference, relationship confidence and human review.
6. **Identity semantics**: V5 stores normalization, case sensitivity, unique scope and source priority, but the mapping executor does not yet apply all of these rules. Identity resolution and source conflict policy need a dedicated implementation.
7. **Quality governance**: V5 native checks exist, but SHACL/pySHACL validation, semantic constraints, impact analysis and automated safe-fix orchestration are not yet wired into one publish gate.
8. **Versioning**: V5 schema, individual contracts, instance materializations and source schema fingerprints are not yet versioned as one immutable release unit.
9. **Offline visualization**: the business-object graph HTML loads AntV X6 and Dagre from jsDelivr. A fully air-gapped deployment needs vendored local assets or a no-CDN fallback.
10. **Authentication/authorization**: the Streamlit application has no built-in user login, role-based access or object/property/action-level authorization. Keep it bound to localhost or protect it with an authenticated reverse proxy before exposing it on a network.
11. **Scale**: CSV/Excel/PostgreSQL data is loaded into pandas DataFrames and materialized in memory. Chunked ingestion, incremental refresh and large-data persistence remain future work.

### P2 — Further engineering improvements

- OWL export still uses the fixed `http://example.org/ontology#` namespace and does not export the full V5 constraints/cardinality/identity model.
- The legacy quality report is a useful first pass but does not provide complete structural, semantic, identity, mapping and application-level validation.
- LLM API keys remain plaintext in the local `llm_config.json`; the file is git-ignored and now written with restrictive temporary-file permissions on POSIX, but OS credential storage or encryption should be considered.
- Legacy relation inference and OWL export need dedicated regression tests. The main Streamlit module has import-time UI side effects, which currently makes isolated unit testing harder.
- The V5 mapping executor is in-memory and synchronous; no transaction boundary, resumable run, idempotent persistence, retry strategy or CDC checkpoint is implemented.

## Recommended next order

1. Finish this review branch and merge its data-integrity/query/version fixes.
2. Add a V5 persistence repository and immutable release bundle (schema + mapping contracts + instance run + lineage).
3. Connect the mapping executor to a Streamlit mapping workbench: field mapping, preview, simulation, approval and publish gate.
4. Add stable source-key handling to all database connectors and durable file/document locators.
5. Migrate query planning and CQ evaluation to V5 IDs and V5 version binding.
6. Improve business-object discovery and relationship inference; only then add SHACL and automated semantic repair.
7. Package visualization assets locally and add deployment authentication controls.

## Validation status

CI is the acceptance gate for this review branch. Do not merge until both the
V5 domain-model workflow and the deterministic query-pipeline workflow pass.
Live Streamlit, browser, PostgreSQL and air-gapped acceptance tests remain
manual release checks.
