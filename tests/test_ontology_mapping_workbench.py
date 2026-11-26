import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ontology_domain.mapping_workbench import (
    build_property_contract,
    execute_and_persist,
    legacy_ontology_to_schema,
    load_latest_schema,
    preview_mapping,
    next_editable_version,
)
from ontology_domain.sqlite_repository import SQLiteOntologyRepository


class MappingWorkbenchServiceTests(unittest.TestCase):
    def setUp(self):
        legacy = SimpleNamespace(
            object_types={"employees": {"primary_key": "employee_id", "properties": ["employee_id", "name"]}},
            link_types={},
            data_mapping={},
        )
        self.schema = legacy_ontology_to_schema(legacy)
        self.object_id = next(iter(self.schema.object_types))
        self.properties = {item.name: item.id for item in self.schema.properties.values()}
        self.rows = {"employees": [
            {"employee_id": "E001", "name": "Alice"},
            {"employee_id": "E002", "name": "Bob"},
        ]}

    def add_contracts(self):
        for name in ("employee_id", "name"):
            contract = build_property_contract(
                self.schema,
                object_type_id=self.object_id,
                property_id=self.properties[name],
                source_table="employees",
                source_field=name,
                record_key_fields=["employee_id"],
            )
            contract.review_status = "approved"
            contract.status = "active"
            self.schema.mapping_contracts[contract.id] = contract

    def test_preview_uses_temporary_approval_without_mutating_drafts(self):
        for name in ("employee_id", "name"):
            contract = build_property_contract(
                self.schema,
                object_type_id=self.object_id,
                property_id=self.properties[name],
                source_table="employees",
                source_field=name,
                record_key_fields=["employee_id"],
            )
            self.schema.mapping_contracts[contract.id] = contract
        result = preview_mapping(self.schema, self.rows)
        self.assertTrue(result["success"])
        self.assertEqual(2, result["instance_count"])
        self.assertTrue(all(c.review_status == "pending" for c in self.schema.mapping_contracts.values()))

    def test_new_draft_version_is_created_after_mapping_run(self):
        self.add_contracts()
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOntologyRepository(Path(directory) / "v5.db")
            execute_and_persist(self.schema, self.rows, repository)
            draft = next_editable_version(self.schema, repository)
            self.assertEqual(self.schema.version + 1, draft.version)
            self.assertEqual(self.schema.version, draft.metadata["parent_version"])

    def test_preview_does_not_override_rejected_contracts(self):
        contract = build_property_contract(
            self.schema,
            object_type_id=self.object_id,
            property_id=self.properties["employee_id"],
            source_table="employees",
            source_field="employee_id",
            record_key_fields=["employee_id"],
        )
        contract.review_status = "rejected"
        self.schema.mapping_contracts[contract.id] = contract
        result = preview_mapping(self.schema, self.rows)
        self.assertFalse(result["success"])
        self.assertTrue(result["blocked_contracts"])
        self.assertEqual("rejected", self.schema.mapping_contracts[contract.id].review_status)

    def test_latest_persisted_schema_is_restored(self):
        legacy = SimpleNamespace(
            object_types={"employees": {"primary_key": "employee_id", "properties": ["employee_id", "name"]}},
            link_types={}, data_mapping={},
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOntologyRepository(Path(directory) / "v5.db")
            saved = legacy_ontology_to_schema(legacy)
            saved.metadata["mapping_note"] = "persisted"
            repository.save_schema(saved)
            restored = load_latest_schema(legacy, repository)
            self.assertEqual("persisted", restored.metadata["mapping_note"])
            self.assertEqual(saved.ontology_id, restored.ontology_id)

    def test_execute_persists_instances_and_run(self):
        self.add_contracts()
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOntologyRepository(Path(directory) / "v5.db")
            outcome = execute_and_persist(self.schema, self.rows, repository)
            self.assertTrue(outcome["result"]["success"])
            self.assertEqual(2, outcome["persisted"]["instance_count"])
            second = execute_and_persist(self.schema, self.rows, repository)
            self.assertNotEqual(outcome["result"]["run_id"], second["result"]["run_id"])
            history = repository.list_mapping_runs(self.schema.ontology_id, self.schema.version)
            self.assertEqual(2, len(history))
            self.assertEqual(second["result"]["run_id"], history[0]["run_id"])
            instances = repository.list_instances(self.schema.ontology_id, self.schema.version)
            self.assertEqual(2, len(instances))


if __name__ == "__main__":
    unittest.main()
