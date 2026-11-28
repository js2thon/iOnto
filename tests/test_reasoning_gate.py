"""Tests for release reasoning gate and SQLite persistence path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ontology_domain.canvas_edit import CanvasEditService
from ontology_domain.models import OntologySchema
from ontology_domain.reasoning_gate import publish_with_gate, run_reasoning_gate
from ontology_domain.sqlite_repository import SQLiteOntologyRepository

import networkx as nx


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


class TestReasoningGate(unittest.TestCase):
    def test_gate_passes_on_valid_schema(self) -> None:
        onto = _MiniOntology()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("Person", primary_key="id", properties=["id", "name"])
        report = run_reasoning_gate(svc.schema, ontology=onto)
        self.assertTrue(report.passed, report.to_dict())

    def test_gate_blocks_empty_schema(self) -> None:
        schema = OntologySchema()
        report = run_reasoning_gate(schema)
        self.assertFalse(report.passed)
        codes = {f.code for f in report.findings}
        self.assertIn("EMPTY_SCHEMA", codes)

    def test_persist_and_publish(self) -> None:
        onto = _MiniOntology()
        svc = CanvasEditService(onto, auto_v5=True)
        svc.add_object_type("Dept", primary_key="code", properties=["code", "title"])
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            repo = SQLiteOntologyRepository(db)
            out = publish_with_gate(repo, svc.schema, ontology=onto)
            self.assertTrue(out["published"], out)
            loaded = repo.load_schema(svc.schema.ontology_id, svc.schema.version)
            self.assertEqual(len(loaded.object_types), 1)
            releases = repo.list_releases(svc.schema.ontology_id)
            self.assertTrue(len(releases) >= 1)

    def test_publish_blocked_without_force(self) -> None:
        schema = OntologySchema()
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteOntologyRepository(str(Path(tmp) / "t.db"))
            out = publish_with_gate(repo, schema, force=False)
            self.assertFalse(out["published"])


if __name__ == "__main__":
    unittest.main()
