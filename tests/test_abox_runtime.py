"""Tests for V5 ABox store and query view."""

from __future__ import annotations

import unittest

import networkx as nx

from ontology_domain.abox_runtime import ABoxStore
from ontology_domain.canvas_edit import CanvasEditService
from ontology_query_pipeline import execute_query_plan


class _Onto:
    def __init__(self) -> None:
        self.object_types = {}
        self.objects = {}
        self.link_types = {}
        self.links = nx.MultiDiGraph()
        self.data_mapping = {}

    def add_object_type(self, name, primary_key, properties, description=""):
        self.object_types[name] = {
            "primary_key": primary_key,
            "properties": list(properties),
            "description": description,
        }
        self.objects.setdefault(name, {})

    def add_link_type(
        self, name, source_type, target_type, description="", source_property="", target_property=""
    ):
        self.link_types[name] = {
            "source": source_type,
            "target": target_type,
            "description": description,
            "source_property": source_property,
            "target_property": target_property,
        }


class TestABoxRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.onto = _Onto()
        svc = CanvasEditService(self.onto, auto_v5=True)
        svc.add_object_type("Person", primary_key="id", properties=["id", "name"])
        svc.add_object_type("Dept", primary_key="code", properties=["code", "title"])
        svc.add_relationship("worksIn", "Person", "Dept")
        self.schema = svc.get_v5_schema()
        # instances
        self.onto.objects["Person"] = {
            "1": {"id": "1", "name": "Alice"},
            "2": {"id": "2", "name": "Bob"},
        }
        self.onto.objects["Dept"] = {"D1": {"code": "D1", "title": "Eng"}}
        self.onto.links.add_node("Person:1", type="Person", pk="1")
        self.onto.links.add_node("Person:2", type="Person", pk="2")
        self.onto.links.add_node("Dept:D1", type="Dept", pk="D1")
        self.onto.links.add_edge("Person:1", "Dept:D1", relation_type="worksIn")

    def test_from_legacy_and_summary(self) -> None:
        store = ABoxStore.from_legacy(self.onto, self.schema)
        summary = store.summary()
        self.assertEqual(summary["instance_count"], 3)
        self.assertEqual(summary["by_type"]["Person"], 2)

    def test_query_view_lookup(self) -> None:
        store = ABoxStore.from_legacy(self.onto, self.schema)
        view = store.to_query_view()
        plan = {
            "intent": "lookup",
            "target_type": "Person",
            "primary_key": "1",
            "filters": [],
            "limit": 10,
        }
        result = execute_query_plan(view, plan)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["data"].get("name"), "Alice")

    def test_query_view_neighbors(self) -> None:
        store = ABoxStore.from_legacy(self.onto, self.schema)
        view = store.to_query_view()
        plan = {
            "intent": "neighbors",
            "source_type": "Person",
            "source_pk": "1",
            "direction": "out",
            "relation_type": "worksIn",
            "filters": [],
            "limit": 10,
        }
        result = execute_query_plan(view, plan)
        self.assertGreaterEqual(len(result.get("records") or []), 1)


if __name__ == "__main__":
    unittest.main()
