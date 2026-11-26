import unittest

from ontology_domain import (
    Identity, MappingContract, ObjectType, OntologySchema, Property, Relationship,
)
from ontology_domain.mapping_engine import apply_transform, execute_mapping


class MappingContractEngineTests(unittest.TestCase):
    def make_schema(self):
        schema = OntologySchema(ontology_id="ontology:test", version=3)
        employee = ObjectType(id="ot:employee", name="Employee", label="员工")
        department = ObjectType(id="ot:department", name="Department", label="部门")
        props = [
            Property(id="p:employee-id", name="employee_id", label="员工编号",
                     domain_object_type_id=employee.id, required=True, nullable=False),
            Property(id="p:name", name="name", label="姓名",
                     domain_object_type_id=employee.id, required=True, nullable=False),
            Property(id="p:dept-code", name="dept_code", label="部门编码",
                     domain_object_type_id=employee.id),
            Property(id="p:department-id", name="department_id", label="部门编号",
                     domain_object_type_id=department.id, required=True, nullable=False),
            Property(id="p:department-code", name="code", label="部门编码",
                     domain_object_type_id=department.id),
        ]
        identity_employee = Identity(
            id="i:employee", object_type_id=employee.id, name="员工业务身份",
            property_refs=["p:employee-id"], identity_type="business",
        )
        identity_department = Identity(
            id="i:department", object_type_id=department.id, name="部门业务身份",
            property_refs=["p:department-id"], identity_type="business",
        )
        relation = Relationship(
            id="r:employee-dept", name="belongs_to", label="所属部门",
            source_object_type_id=employee.id, target_object_type_id=department.id,
            # Default multiplicity allows many employees to belong to one department.
        )
        employee.property_ids = ["p:employee-id", "p:name", "p:dept-code"]
        employee.identity_ids = [identity_employee.id]
        employee.relationship_ids = [relation.id]
        department.property_ids = ["p:department-id", "p:department-code"]
        department.identity_ids = [identity_department.id]
        department.relationship_ids = [relation.id]
        schema.object_types = {employee.id: employee, department.id: department}
        schema.properties = {p.id: p for p in props}
        schema.identities = {identity_employee.id: identity_employee,
                             identity_department.id: identity_department}
        schema.relationships = {relation.id: relation}
        return schema

    def add_property_contract(self, schema, cid, table, field, object_type, prop, transform=None):
        schema.mapping_contracts[cid] = MappingContract(
            id=cid, name=cid,
            source_ref={"source_id": "src:hr", "table": table, "field": field},
            target_ref={"object_type_id": object_type, "property_id": prop},
            mapping_kind="transform" if transform else "direct",
            transform=transform, review_status="approved", status="active",
            version=2, confidence=0.98,
        )

    def test_direct_and_transform_mapping_with_field_lineage(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        self.add_property_contract(schema, "m:name", "employee", "employee_name", "ot:employee", "p:name",
                                   {"operations": [{"op": "trim"}, {"op": "upper"}]})
        result = execute_mapping(schema, {"employee": [
            {"emp_no": "E001", "employee_name": " Alice "}
        ]})
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(1, result["instance_count"])
        instance = next(iter(result["instances"].values()))
        self.assertEqual("ALICE", instance.properties["p:name"])
        self.assertEqual(2, len(instance.lineage.fields))
        self.assertEqual("m:name", instance.lineage.fields[1].mapping_contract_id)
        self.assertEqual(3, instance.ontology_version)

    def test_unapproved_contract_is_blocked(self):
        schema = self.make_schema()
        contract = MappingContract(
            id="m:pending", name="pending",
            source_ref={"table": "employee", "field": "emp_no"},
            target_ref={"object_type_id": "ot:employee", "property_id": "p:employee-id"},
        )
        schema.mapping_contracts[contract.id] = contract
        result = execute_mapping(schema, {"employee": [{"emp_no": "E1"}]})
        self.assertFalse(result["success"])
        self.assertEqual(1, len(result["blocked_contracts"]))
        self.assertEqual(0, result["instance_count"])

    def test_missing_identity_is_not_silently_materialized(self):
        schema = self.make_schema()
        schema.properties["p:employee-id"].required = False
        schema.properties["p:employee-id"].nullable = True
        self.add_property_contract(schema, "m:name", "employee", "employee_name", "ot:employee", "p:name")
        result = execute_mapping(schema, {"employee": [{"employee_name": "Alice"}]})
        self.assertFalse(result["success"])
        self.assertTrue(any("业务身份" in item["reason"] for item in result["errors"]))
        self.assertEqual(0, result["instance_count"])

    def test_relationship_join_links_materialized_instances(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        self.add_property_contract(schema, "m:name", "employee", "name", "ot:employee", "p:name")
        self.add_property_contract(schema, "m:did", "department", "dept_no", "ot:department", "p:department-id")
        relation_contract = MappingContract(
            id="m:join", name="employee department join",
            source_ref={"source_id": "src:hr", "table": "employee"},
            target_ref={"relationship_id": "r:employee-dept", "target_table": "department"},
            mapping_kind="join", join_keys=[
                {"source_field": "dept_code", "target_field": "code"}
            ], review_status="approved", status="active",
        )
        schema.mapping_contracts[relation_contract.id] = relation_contract
        result = execute_mapping(schema, {
            "employee": [{"emp_no": "E1", "name": "Alice", "dept_code": "D1"}],
            "department": [{"dept_no": "D1", "code": "D1"}],
        })
        self.assertTrue(result["success"], result["errors"])
        employee = next(v for v in result["instances"].values() if v.object_type_id == "ot:employee")
        department = next(v for v in result["instances"].values() if v.object_type_id == "ot:department")
        self.assertEqual([department.id], employee.relationship_refs["r:employee-dept"])
        self.assertEqual(1, employee.mapping_versions["m:join"])
        self.assertEqual(1, len(employee.lineage.relationships))
        self.assertEqual("m:join", employee.lineage.relationships[0].mapping_contract_id)
        self.assertEqual("employee", employee.lineage.relationships[0].source.table)
        self.assertEqual("department", employee.lineage.relationships[0].target.table)

    def test_duplicate_identity_conflict_is_reported_not_overwritten(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        self.add_property_contract(schema, "m:name", "employee", "name", "ot:employee", "p:name")
        result = execute_mapping(schema, {"employee": [
            {"emp_no": "E1", "name": "Alice"},
            {"emp_no": "E1", "name": "Bob"},
        ]})
        self.assertFalse(result["success"])
        self.assertEqual(1, result["instance_count"])
        self.assertTrue(any("拒绝覆盖" in item["reason"] for item in result["errors"]))

    def test_property_type_mismatch_is_reported(self):
        schema = self.make_schema()
        schema.properties["p:employee-id"].data_type = "integer"
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        self.add_property_contract(schema, "m:name", "employee", "name", "ot:employee", "p:name")
        result = execute_mapping(schema, {"employee": [{"emp_no": "E1", "name": "Alice"}]})
        self.assertFalse(result["success"])
        self.assertTrue(any("类型 integer" in item["reason"] for item in result["errors"]))

    def test_required_property_is_enforced(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        result = execute_mapping(schema, {"employee": [{"emp_no": "E1"}]})
        self.assertFalse(result["success"])
        self.assertTrue(any("缺少必填属性" in item["reason"] for item in result["errors"]))

    def test_many_source_instances_can_share_target_instance(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        self.add_property_contract(schema, "m:name", "employee", "name", "ot:employee", "p:name")
        self.add_property_contract(schema, "m:did", "department", "dept_no", "ot:department", "p:department-id")
        schema.mapping_contracts["m:join"] = MappingContract(
            id="m:join", name="join",
            source_ref={"table": "employee"},
            target_ref={"relationship_id": "r:employee-dept", "target_table": "department"},
            mapping_kind="join", join_keys=[{"source_field": "dept_code", "target_field": "code"}],
            review_status="approved", status="active",
        )
        result = execute_mapping(schema, {
            "employee": [
                {"emp_no": "E1", "name": "Alice", "dept_code": "D1"},
                {"emp_no": "E2", "name": "Bob", "dept_code": "D1"},
            ],
            "department": [{"dept_no": "D1", "code": "D1"}],
        })
        self.assertTrue(result["success"], result["errors"])
        employees = [v for v in result["instances"].values() if v.object_type_id == "ot:employee"]
        department = next(v for v in result["instances"].values() if v.object_type_id == "ot:department")
        self.assertEqual(2, len(employees))
        self.assertTrue(all(v.relationship_refs["r:employee-dept"] == [department.id] for v in employees))

    def test_transform_allow_list_rejects_code(self):
        with self.assertRaises(ValueError):
            apply_transform("x", {"operations": [{"op": "eval", "value": "__import__('os')"}]})

    def test_missing_source_field_is_reported(self):
        schema = self.make_schema()
        self.add_property_contract(schema, "m:eid", "employee", "emp_no", "ot:employee", "p:employee-id")
        result = execute_mapping(schema, {"employee": [{"other": "E1"}]})
        self.assertFalse(result["success"])
        self.assertTrue(any("源字段不存在" in item["reason"] for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
