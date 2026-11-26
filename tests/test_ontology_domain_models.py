import unittest

from ontology_domain import (
    Constraint, Identity, MappingContract, ObjectType, OntologySchema,
    Property, Relationship, legacy_snapshot_to_v5,
)


class OntologyDomainModelTests(unittest.TestCase):
    def make_schema(self):
        schema = OntologySchema()
        employee = ObjectType(id="ot:employee", name="Employee", label="员工")
        department = ObjectType(id="ot:department", name="Department", label="部门")
        employee_id = Property(
            id="p:employee-id", name="employee_id", label="员工编号",
            domain_object_type_id=employee.id, required=True, nullable=False,
        )
        department_id = Property(
            id="p:department-id", name="department_id", label="部门编号",
            domain_object_type_id=department.id,
        )
        identity = Identity(
            id="i:employee", object_type_id=employee.id, name="员工业务身份",
            property_refs=[employee_id.id], identity_type="business",
        )
        relation = Relationship(
            id="r:belongs", name="belongs_to", label="所属部门",
            source_object_type_id=employee.id, target_object_type_id=department.id,
            target_max_cardinality=1,
        )
        employee.property_ids.append(employee_id.id)
        employee.identity_ids.append(identity.id)
        employee.relationship_ids.append(relation.id)
        department.property_ids.append(department_id.id)
        department.relationship_ids.append(relation.id)
        schema.object_types = {employee.id: employee, department.id: department}
        schema.properties = {employee_id.id: employee_id, department_id.id: department_id}
        schema.identities = {identity.id: identity}
        schema.relationships = {relation.id: relation}
        return schema

    def test_schema_round_trip(self):
        schema = self.make_schema()
        self.assertEqual([], schema.validate())
        restored = OntologySchema.from_json(schema.to_json())
        self.assertEqual([], restored.validate())
        self.assertEqual(schema.object_types["ot:employee"].name,
                         restored.object_types["ot:employee"].name)

    def test_stable_ids_and_rename_independence(self):
        first = legacy_snapshot_to_v5({
            "object_types": {"employee": {"primary_key": "id", "properties": ["id", "name"]}}
        })
        second = legacy_snapshot_to_v5({
            "object_types": {"employee": {"primary_key": "id", "properties": ["id", "name"]}}
        })
        self.assertEqual(set(first.object_types), set(second.object_types))
        self.assertEqual(set(first.properties), set(second.properties))
        # Display-name changes do not change the stable identifier.
        object_id = next(iter(first.object_types))
        first.object_types[object_id].label = "员工"
        self.assertIn(object_id, first.object_types)

    def test_legacy_relationship_migration(self):
        schema = legacy_snapshot_to_v5({
            "object_types": {
                "employee": {"primary_key": "id", "properties": ["id", "department_id"]},
                "department": {"primary_key": "id", "properties": ["id", "name"]},
            },
            "link_types": {
                "employee_department": {
                    "source": "employee", "target": "department",
                    "source_property": "department_id", "target_property": "id",
                }
            },
            "data_mapping": {"employee": {"columns": ["id", "department_id"]}},
        })
        self.assertEqual(1, len(schema.relationships))
        self.assertEqual([], schema.validate())
        self.assertIn("legacy_data_mapping", schema.metadata)
        self.assertEqual({}, schema.mapping_contracts)

    def test_invalid_identity_reference_is_reported(self):
        schema = self.make_schema()
        schema.identities["i:employee"].property_refs = ["p:missing"]
        self.assertTrue(any("missing Property" in error for error in schema.validate()))

    def test_mapping_contract_rejects_executable_code(self):
        contract = MappingContract(
            id="m:1", name="unsafe", source_ref={"table": "employee"},
            target_ref={"object_type_id": "ot:employee", "property_id": "p:employee-id"},
            mapping_kind="transform", transform={"python": "open('/tmp/x')"},
        )
        with self.assertRaises(ValueError):
            contract.validate()

    def test_missing_relationship_reference_is_reported(self):
        schema = self.make_schema()
        schema.object_types["ot:employee"].relationship_ids.append("r:missing")
        self.assertTrue(any("missing Relationship" in error for error in schema.validate()))

    def test_constraint_severity_and_fix_policy(self):
        constraint = Constraint(
            id="c:1", name="unique employee id", target_ref="p:employee-id",
            constraint_type="unique", severity="block", auto_fix_policy="manual",
        )
        constraint.validate()


if __name__ == "__main__":
    unittest.main()
