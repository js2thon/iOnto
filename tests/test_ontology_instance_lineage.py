import unittest

from ontology_domain import (
    Identity, ObjectType, OntologySchema, Property, Relationship,
    FieldLineage, OntologyInstance, SourceLineage, SourceRecordRef,
)


class OntologyInstanceLineageTests(unittest.TestCase):
    def make_schema(self):
        schema = OntologySchema()
        employee = ObjectType(id="ot:employee", name="Employee", label="员工")
        department = ObjectType(id="ot:department", name="Department", label="部门")
        employee_id = Property(
            id="p:employee-id", name="employee_id", label="员工编号",
            domain_object_type_id=employee.id, required=True, nullable=False,
        )
        employee_name = Property(
            id="p:name", name="name", label="姓名",
            domain_object_type_id=employee.id,
        )
        dept_id = Property(
            id="p:dept-id", name="department_id", label="部门编号",
            domain_object_type_id=department.id,
        )
        identity = Identity(
            id="i:employee", object_type_id=employee.id, name="employee key",
            property_refs=[employee_id.id], identity_type="business",
        )
        relation = Relationship(
            id="r:employee-dept", name="belongs_to", label="所属部门",
            source_object_type_id=employee.id, target_object_type_id=department.id,
        )
        employee.property_ids.extend([employee_id.id, employee_name.id])
        employee.identity_ids.append(identity.id)
        employee.relationship_ids.append(relation.id)
        department.property_ids.append(dept_id.id)
        schema.object_types = {employee.id: employee, department.id: department}
        schema.properties = {p.id: p for p in (employee_id, employee_name, dept_id)}
        schema.identities = {identity.id: identity}
        schema.relationships = {relation.id: relation}
        return schema

    def test_instance_with_record_and_field_lineage(self):
        schema = self.make_schema()
        source = SourceRecordRef(
            source_id="src:hr", dataset="hr", table="employee",
            record_key={"employee_no": "E001"}, content_hash="sha256:abc",
        )
        lineage = SourceLineage(
            sources=[source],
            fields=[FieldLineage(
                target_property_id="p:name", source=source,
                source_field="employee_name", source_value_hash="sha256:def",
                confidence=0.99,
            )],
            ingestion_run_id="ingest:001", mapping_run_id="maprun:001",
        )
        instance = OntologyInstance(
            id="employee:E001", object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            properties={"p:name": "Alice"},
            relationship_refs={"r:employee-dept": ["department:D01"]},
            ontology_id=schema.ontology_id, ontology_version=1,
            lineage=lineage,
        )
        instance.validate(schema)

    def test_rejects_unknown_property(self):
        instance = OntologyInstance(
            id="employee:E001", object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            properties={"p:missing": "x"},
        )
        with self.assertRaisesRegex(ValueError, "does not belong"):
            instance.validate(self.make_schema())

    def test_rejects_invalid_source_locator(self):
        source = SourceRecordRef(source_id="src:hr")
        with self.assertRaisesRegex(ValueError, "requires a record"):
            source.validate()

    def test_rejects_invalid_temporal_interval(self):
        instance = OntologyInstance(
            id="employee:E001", object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            valid_from="2026-12-01", valid_to="2026-01-01",
        )
        with self.assertRaisesRegex(ValueError, "cannot be after"):
            instance.validate()

    def test_rejects_unknown_relationship(self):
        instance = OntologyInstance(
            id="employee:E001", object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            relationship_refs={"r:unknown": ["department:D01"]},
        )
        with self.assertRaisesRegex(ValueError, "not outgoing"):
            instance.validate(self.make_schema())

    def test_rejects_lineage_property_not_in_object(self):
        source = SourceRecordRef(source_id="src:hr", record_key={"id": "E001"})
        instance = OntologyInstance(
            id="employee:E001", object_type_id="ot:employee",
            identity_values={"p:employee-id": "E001"},
            lineage=SourceLineage(fields=[
                FieldLineage(target_property_id="p:dept-id", source=source)
            ]),
        )
        with self.assertRaisesRegex(ValueError, "unknown target Property"):
            instance.validate(self.make_schema())


if __name__ == "__main__":
    unittest.main()
