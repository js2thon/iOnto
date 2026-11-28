"""Tests for owl_reasoner facade (structural layer always available)."""

from __future__ import annotations

import unittest

import networkx as nx

from ontology_domain.canvas_edit import CanvasEditService
from ontology_domain.owl_reasoner import reason_over_ontology


class _MiniOntology:
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


class TestOwlReasoner(unittest.TestCase):
    def test_structural_reasoner_runs_without_owlrl(self) -> None:
        onto = _MiniOntology()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("Person", primary_key="id", properties=["id", "name"])
        report = reason_over_ontology(onto, schema=svc.schema, try_owlrl=False)
        self.assertTrue(report.consistent)
        # asserted_triple_count may be 0 if rdflib is not installed
        self.assertIsInstance(report.asserted_triple_count, int)

    def test_with_relationship(self) -> None:
        onto = _MiniOntology()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("A", primary_key="id", properties=["id"])
        svc.add_object_type("B", primary_key="id", properties=["id"])
        svc.add_relationship("rel", "A", "B")
        report = reason_over_ontology(onto, schema=svc.schema, try_owlrl=False)
        self.assertTrue(report.consistent)
        self.assertIn(report.engine, {"structural", "owlrl", "none", "owlrl-error"})


if __name__ == "__main__":
    unittest.main()
