import unittest
from types import SimpleNamespace

import networkx as nx

from ontology_query_pipeline import (
    QueryPlanError,
    _validate_plan,
    build_provenance,
    execute_query_plan,
    validate_result,
)


def sample_ontology():
    ontology = SimpleNamespace(
        object_types={
            "Employee": {"primary_key": "employee_id", "properties": ["employee_id", "name", "department_id"]},
            "Department": {"primary_key": "department_id", "properties": ["department_id", "name"]},
        },
        objects={
            "Employee": {
                "E1": {"employee_id": "E1", "name": "Alice", "department_id": "D1"},
                "E2": {"employee_id": "E2", "name": "Bob", "department_id": "D1"},
            },
            "Department": {"D1": {"department_id": "D1", "name": "Data"}},
        },
        link_types={
            "employee_to_department": {
                "source": "Employee", "target": "Department",
                "source_property": "department_id", "target_property": "department_id",
            }
        },
        data_mapping={},
        links=nx.MultiDiGraph(),
    )
    ontology.links.add_node("Employee:E1", type="Employee", pk="E1")
    ontology.links.add_node("Employee:E2", type="Employee", pk="E2")
    ontology.links.add_node("Department:D1", type="Department", pk="D1")
    ontology.links.add_edge("Employee:E1", "Department:D1", relation_type="employee_to_department")
    ontology.links.add_edge("Employee:E2", "Department:D1", relation_type="employee_to_department")
    return ontology


class OntologyQueryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.ontology = sample_ontology()

    def test_lookup_is_deterministic(self):
        plan = _validate_plan({
            "intent": "lookup", "target_type": "Employee", "primary_key": "E1"
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        self.assertEqual(result["records"][0]["data"]["name"], "Alice")
        self.assertTrue(validate_result(self.ontology, plan, result)["valid"])

    def test_search_without_keyword_lists_target_type(self):
        plan = _validate_plan({
            "intent": "search", "target_type": "Employee",
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["data"]["name"] for item in result["records"]],
            ["Alice", "Bob"],
        )
        self.assertTrue(validate_result(self.ontology, plan, result)["valid"])

    def test_count_with_filter(self):
        plan = _validate_plan({
            "intent": "count", "target_type": "Employee",
            "filters": [{"property": "department_id", "operator": "eq", "value": "D1"}],
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        self.assertEqual(result["count"], 2)
        self.assertTrue(validate_result(self.ontology, plan, result)["valid"])

    def test_neighbors_uses_declared_relation(self):
        plan = _validate_plan({
            "intent": "neighbors", "source_type": "Employee", "source_pk": "E1",
            "relation_type": "employee_to_department", "direction": "out",
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        self.assertEqual(result["records"][0]["node_id"], "Department:D1")
        self.assertEqual(len(result["relations"]), 1)

    def test_path_returns_relation_evidence(self):
        plan = _validate_plan({
            "intent": "path", "source_type": "Employee", "source_pk": "E1",
            "target_type_to": "Department", "target_pk": "D1",
            "relation_type": "employee_to_department",
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        self.assertTrue(result["found"])
        self.assertEqual(result["length"], 1)

    def test_unknown_property_is_rejected(self):
        with self.assertRaises(QueryPlanError):
            _validate_plan({
                "intent": "count", "target_type": "Employee",
                "filters": [{"property": "salary_secret", "operator": "eq", "value": 1}],
            }, self.ontology)

    def test_count_is_recomputed_independently(self):
        plan = _validate_plan({
            "intent": "count", "target_type": "Employee",
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        result["count"] = 99
        validation = validate_result(self.ontology, plan, result)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("独立复核" in error for error in validation["errors"]))

    def test_tampered_result_fails_validation(self):
        plan = _validate_plan({
            "intent": "lookup", "target_type": "Employee", "primary_key": "E1"
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        result["records"][0]["data"]["name"] = "Mallory"
        validation = validate_result(self.ontology, plan, result)
        self.assertFalse(validation["valid"])

    def test_provenance_contains_plan_and_evidence(self):
        plan = _validate_plan({
            "intent": "lookup", "target_type": "Employee", "primary_key": "E1"
        }, self.ontology)
        result = execute_query_plan(self.ontology, plan)
        validation = validate_result(self.ontology, plan, result)
        provenance = build_provenance(self.ontology, plan, result, validation, "v1")
        self.assertEqual(provenance["version_id"], "v1")
        self.assertEqual(provenance["evidence_count"], 1)
        self.assertEqual(provenance["evidence"][0]["object_id"], "Employee:E1")


if __name__ == "__main__":
    unittest.main()
