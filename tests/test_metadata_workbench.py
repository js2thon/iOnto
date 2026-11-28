"""Tests for editable multi-source metadata workbench."""

from __future__ import annotations

import unittest

import pandas as pd

from ontology_domain.metadata_workbench import MetadataWorkbench
from ontology_domain.tbox_generator import generate_tbox_from_workbench


class TestMetadataWorkbench(unittest.TestCase):
    def test_multi_bundle_merge(self) -> None:
        wb = MetadataWorkbench()
        wb.add_from_dataframes(
            {"employees": pd.DataFrame({"id": [1, 2], "dept_id": ["D1", "D2"], "name": ["a", "b"]})},
            name="emp",
            source_kind="file",
        )
        wb.add_from_dataframes(
            {"departments": pd.DataFrame({"id": ["D1", "D2"], "title": ["E", "S"]})},
            name="dept",
            source_kind="file",
        )
        meta = wb.get_working_copy()
        self.assertIn("employees", meta.tables)
        self.assertIn("departments", meta.tables)
        self.assertEqual(len(wb.list_bundles()), 2)

    def test_disable_bundle(self) -> None:
        wb = MetadataWorkbench()
        b1 = wb.add_from_dataframes(
            {"a": pd.DataFrame({"id": [1]})}, name="a", source_kind="file"
        )
        wb.add_from_dataframes(
            {"b": pd.DataFrame({"id": [2]})}, name="b", source_kind="file"
        )
        wb.set_enabled(b1.id, False)
        meta = wb.get_working_copy()
        self.assertNotIn("a", meta.tables)
        self.assertIn("b", meta.tables)

    def test_edit_column_and_table(self) -> None:
        wb = MetadataWorkbench()
        wb.add_from_dataframes(
            {"t": pd.DataFrame({"id": [1, 2], "x": ["a", "b"]})},
            name="t",
            source_kind="file",
        )
        wb.update_column("t", "x", logical_type="string", nullable=False)
        wb.update_table("t", suggested_primary_key="id", description="demo")
        meta = wb.get_working_copy()
        col = next(c for c in meta.tables["t"].columns if c.name == "x")
        self.assertFalse(col.nullable)
        self.assertEqual(meta.tables["t"].suggested_primary_key, "id")
        self.assertEqual(meta.tables["t"].description, "demo")

    def test_manual_relationship_and_tbox(self) -> None:
        wb = MetadataWorkbench()
        wb.add_from_dataframes(
            {
                "employees": pd.DataFrame({"id": [1], "dept_id": ["D1"]}),
                "departments": pd.DataFrame({"id": ["D1"], "title": ["E"]}),
            },
            name="both",
            source_kind="file",
        )
        wb.add_relationship_candidate(
            "employees", "departments", "dept_id", "id", confidence=0.9
        )
        schema = generate_tbox_from_workbench(wb, min_rel_confidence=0.5)
        self.assertEqual(len(schema.object_types), 2)
        self.assertGreaterEqual(len(schema.relationships), 1)


if __name__ == "__main__":
    unittest.main()
