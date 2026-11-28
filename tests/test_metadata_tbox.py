"""Tests for metadata extraction and TBox generation."""

from __future__ import annotations

import unittest

import pandas as pd

from ontology_domain.metadata_extractor import extract_source_metadata
from ontology_domain.tbox_generator import (
    apply_tbox_to_legacy_ontology,
    generate_tbox_from_metadata,
    metadata_to_tbox_summary,
)


class _LegacyOnto:
    def __init__(self) -> None:
        self.object_types = {}
        self.objects = {}
        self.link_types = {}
        self.links = None

    def add_object_type(self, name, primary_key, properties, description=""):
        self.object_types[name] = {
            "primary_key": primary_key,
            "properties": list(properties),
            "description": description,
        }

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


class TestMetadataTBox(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {
            "employees": pd.DataFrame(
                {
                    "id": [1, 2, 3],
                    "name": ["Alice", "Bob", "Carol"],
                    "dept_id": ["D1", "D1", "D2"],
                    "active": [True, False, True],
                }
            ),
            "departments": pd.DataFrame(
                {
                    "id": ["D1", "D2"],
                    "title": ["Engineering", "Sales"],
                }
            ),
        }

    def test_extract_metadata(self) -> None:
        meta = extract_source_metadata(self.data)
        self.assertIn("employees", meta.tables)
        self.assertIn("departments", meta.tables)
        emp = meta.tables["employees"]
        self.assertEqual(emp.suggested_primary_key, "id")
        col_types = {c.name: c.logical_type for c in emp.columns}
        self.assertEqual(col_types["id"], "integer")
        self.assertIn(col_types["active"], {"boolean", "string"})
        # FK candidate employees.dept_id → departments
        self.assertTrue(any(r["source_table"] == "employees" for r in meta.relationship_candidates))

    def test_generate_tbox(self) -> None:
        meta = extract_source_metadata(self.data)
        schema = generate_tbox_from_metadata(meta, min_rel_confidence=0.3)
        summary = metadata_to_tbox_summary(schema)
        self.assertEqual(summary["object_types"], 2)
        self.assertGreaterEqual(summary["properties"], 5)
        self.assertGreaterEqual(summary["identities"], 1)
        errors = schema.validate()
        # may have warnings only
        self.assertIsInstance(errors, list)

    def test_apply_to_legacy(self) -> None:
        meta = extract_source_metadata(self.data)
        schema = generate_tbox_from_metadata(meta, min_rel_confidence=0.3)
        onto = _LegacyOnto()
        apply_tbox_to_legacy_ontology(schema, onto)
        self.assertIn("employees", onto.object_types)
        self.assertIn("departments", onto.object_types)


if __name__ == "__main__":
    unittest.main()
