"""Tests for canvas structure edit → V5 write-back."""

from __future__ import annotations

import unittest

import networkx as nx

from ontology_domain.canvas_edit import CanvasEditService


class _MiniOntology:
    def __init__(self) -> None:
        self.object_types = {}
        self.objects = {}
        self.link_types = {}
        self.links = nx.MultiDiGraph()
        self.data_mapping = {}
        self.actions_log = []

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


class TestCanvasEdit(unittest.TestCase):
    def setUp(self) -> None:
        self.onto = _MiniOntology()
        self.svc = CanvasEditService(self.onto, auto_v5=True)

    def test_add_object_type_writes_legacy_and_v5(self) -> None:
        r = self.svc.add_object_type("Person", primary_key="id", properties=["id", "name"])
        self.assertTrue(r.ok)
        self.assertIn("Person", self.onto.object_types)
        self.assertIsNotNone(self.svc.schema)
        self.assertEqual(len(self.svc.schema.object_types), 1)
        self.assertGreaterEqual(len(self.svc.schema.properties), 2)

    def test_add_relationship(self) -> None:
        self.svc.add_object_type("Person", primary_key="id", properties=["id"])
        self.svc.add_object_type("Dept", primary_key="code", properties=["code"])
        r = self.svc.add_relationship("worksIn", "Person", "Dept")
        self.assertTrue(r.ok)
        self.assertIn("worksIn", self.onto.link_types)
        self.assertEqual(len(self.svc.schema.relationships), 1)

    def test_delete_object_type_blocked_by_relationship(self) -> None:
        self.svc.add_object_type("Person", primary_key="id", properties=["id"])
        self.svc.add_object_type("Dept", primary_key="code", properties=["code"])
        self.svc.add_relationship("worksIn", "Person", "Dept")
        r = self.svc.delete_object_type("Person", force=False)
        self.assertFalse(r.ok)
        r2 = self.svc.delete_object_type("Person", force=True)
        self.assertTrue(r2.ok)
        self.assertNotIn("Person", self.onto.object_types)

    def test_add_and_delete_property(self) -> None:
        self.svc.add_object_type("Person", primary_key="id", properties=["id"])
        r = self.svc.add_property("Person", "email", data_type="string")
        self.assertTrue(r.ok)
        self.assertIn("email", self.onto.object_types["Person"]["properties"])
        r2 = self.svc.delete_property("Person", "email")
        self.assertTrue(r2.ok)
        self.assertNotIn("email", self.onto.object_types["Person"]["properties"])

    def test_edit_log_recorded(self) -> None:
        self.svc.add_object_type("X", primary_key="id", properties=["id"])
        log = self.svc.get_edit_log()
        self.assertTrue(any(e["operation"] == "add_object_type" for e in log))


if __name__ == "__main__":
    unittest.main()
