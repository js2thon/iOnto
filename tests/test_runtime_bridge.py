"""Tests for V5-as-truth runtime bridge."""

from __future__ import annotations

import unittest

import networkx as nx

from ontology_domain.canvas_edit import CanvasEditService
from ontology_domain.runtime_bridge import (
    ensure_v5_schema,
    project_structure_to_legacy,
    structure_diff,
)


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


class TestRuntimeBridge(unittest.TestCase):
    def test_ensure_and_project_aligns(self) -> None:
        onto = _Onto()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("Person", primary_key="id", properties=["id", "name"])
        schema = ensure_v5_schema(onto, svc.get_v5_schema())
        project_structure_to_legacy(schema, onto)
        diff = structure_diff(schema, onto)
        self.assertTrue(diff["aligned"], diff)

    def test_project_removes_stale_legacy_type(self) -> None:
        onto = _Onto()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("A", primary_key="id", properties=["id"])
        svc.add_object_type("B", primary_key="id", properties=["id"])
        schema = svc.get_v5_schema()
        # manually add stale legacy type
        onto.object_types["Ghost"] = {"primary_key": "id", "properties": ["id"], "description": ""}
        diff_before = structure_diff(schema, onto)
        self.assertFalse(diff_before["aligned"])
        project_structure_to_legacy(schema, onto)
        self.assertNotIn("Ghost", onto.object_types)
        self.assertTrue(structure_diff(schema, onto)["aligned"])


if __name__ == "__main__":
    unittest.main()
