import tempfile
import unittest
from pathlib import Path

from ontology_domain import (
    Identity, ObjectType, OntologyInstance, OntologySchema, Property,
)
from ontology_domain.sqlite_repository import RepositoryError, SQLiteOntologyRepository


class SQLiteOntologyRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "ontology.db"
        self.repo = SQLiteOntologyRepository(self.db_path)
        self.schema = OntologySchema(ontology_id="ontology:test", version=1)
        obj = ObjectType(id="ot:employee", name="Employee", label="员工")
        prop = Property(
            id="p:employee-id", name="employee_id", label="员工编号",
            domain_object_type_id=obj.id, required=True, nullable=False,
        )
        identity = Identity(
            id="i:employee", object_type_id=obj.id, name="员工编号",
            property_refs=[prop.id],
        )
        obj.property_ids.append(prop.id)
        obj.identity_ids.append(identity.id)
        self.schema.object_types[obj.id] = obj
        self.schema.properties[prop.id] = prop
        self.schema.identities[identity.id] = identity

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_instance(self):
        return OntologyInstance(
            id="ot:employee:001",
            object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            properties={"p:employee-id": "E001"},
            ontology_id=self.schema.ontology_id,
            ontology_version=self.schema.version,
        )

    def test_schema_round_trip_and_checksum(self):
        saved = self.repo.save_schema(self.schema)
        loaded = self.repo.load_schema("ontology:test", 1)
        self.assertEqual([], loaded.validate())
        self.assertEqual(saved["checksum"], self.repo.list_schema_versions("ontology:test")[0]["checksum"])

    def test_mapping_run_persists_instances_and_lineage(self):
        self.repo.save_schema(self.schema)
        instance = self.make_instance()
        result = {
            "run_id": "mapping-run:one",
            "ontology_id": self.schema.ontology_id,
            "ontology_version": self.schema.version,
            "instances": {instance.id: instance},
            "instance_count": 1,
            "success": True,
            "errors": [],
            "blocked_contracts": [],
        }
        saved = self.repo.save_mapping_run(self.schema, result)
        self.assertEqual("succeeded", saved["status"])
        self.assertEqual(instance.id, self.repo.list_instances("ontology:test", 1)[0]["id"])
        self.assertEqual(instance.id, self.repo.load_mapping_run("mapping-run:one")["instances"][instance.id]["id"])

    def test_release_bundle_is_immutable_and_verifiable(self):
        self.repo.save_schema(self.schema)
        first = self.repo.publish_release(self.schema)
        second = self.repo.publish_release(self.schema)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["checksum"], second["checksum"])
        bundle = self.repo.load_release(first["release_id"])
        self.assertEqual("ontology:test", bundle["ontology_id"])
        self.schema.metadata["changed_after_release"] = True
        with self.assertRaises(RepositoryError):
            self.repo.save_schema(self.schema)

    def test_publish_rejects_unapproved_mapping_contract(self):
        from ontology_domain import MappingContract
        contract = MappingContract(
            id="map:employee", name="员工编号",
            source_ref={"table": "employees", "field": "employee_id"},
            target_ref={"object_type_id": "ot:employee", "property_id": "p:employee-id"},
        )
        self.schema.mapping_contracts[contract.id] = contract
        self.repo.save_schema(self.schema)
        with self.assertRaisesRegex(RepositoryError, "unapproved/inactive"):
            self.repo.publish_release(self.schema)

    def test_list_mapping_runs_returns_latest_first(self):
        self.repo.save_schema(self.schema)
        for run_id in ("run-old", "run-new"):
            self.repo.save_mapping_run(self.schema, {
                "run_id": run_id, "ontology_id": self.schema.ontology_id,
                "ontology_version": self.schema.version, "instances": {}, "success": True,
            })
        rows = self.repo.list_mapping_runs("ontology:test", 1, limit=1)
        self.assertEqual(1, len(rows))
        self.assertIn(rows[0]["run_id"], {"run-old", "run-new"})
        self.assertEqual("succeeded", rows[0]["status"])

    def test_schema_version_cannot_change_after_mapping_run(self):
        self.repo.save_schema(self.schema)
        self.repo.save_mapping_run(self.schema, {
            "run_id": "immutable-run", "ontology_id": self.schema.ontology_id,
            "ontology_version": self.schema.version, "instances": {}, "success": True,
        })
        self.schema.metadata["changed_after_run"] = True
        with self.assertRaisesRegex(RepositoryError, "already has mapping runs"):
            self.repo.save_schema(self.schema)

    def test_publish_requires_successful_mapping_run(self):
        from ontology_domain import MappingContract
        contract = MappingContract(
            id="map:employee", name="员工编号",
            source_ref={"table": "employees", "field": "employee_id"},
            target_ref={"object_type_id": "ot:employee", "property_id": "p:employee-id"},
            review_status="approved", status="active",
        )
        self.schema.mapping_contracts[contract.id] = contract
        self.repo.save_schema(self.schema)
        with self.assertRaisesRegex(RepositoryError, "before a mapping run succeeds"):
            self.repo.publish_release(self.schema)
        self.repo.save_mapping_run(self.schema, {
            "run_id": "failed-run", "ontology_id": self.schema.ontology_id,
            "ontology_version": self.schema.version, "instances": {}, "success": False,
        })
        with self.assertRaisesRegex(RepositoryError, "latest mapping run failed-run is failed"):
            self.repo.publish_release(self.schema)
        self.repo.save_mapping_run(self.schema, {
            "run_id": "successful-run", "ontology_id": self.schema.ontology_id,
            "ontology_version": self.schema.version, "instances": {}, "success": True,
        })
        release = self.repo.publish_release(self.schema)
        self.assertTrue(release["created"])

    def test_run_version_must_match_schema(self):
        with self.assertRaises(RepositoryError):
            self.repo.save_mapping_run(self.schema, {
                "run_id": "wrong-version", "ontology_id": "ontology:test",
                "ontology_version": 2, "instances": {},
            })


if __name__ == "__main__":
    unittest.main()
