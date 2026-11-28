"""Tests for ontology_domain.rdf_export (Ontosphere integration step 1)."""

from __future__ import annotations

import unittest

from rdflib import Dataset, OWL, RDF, RDFS

# Minimal stand-in that mimics ontology_studio_v3.Ontology surface
class _MiniOntology:
    def __init__(self) -> None:
        self.object_types = {
            "Person": {
                "primary_key": "id",
                "properties": ["id", "name", "age"],
                "description": "A person",
            },
            "Department": {
                "primary_key": "code",
                "properties": ["code", "title"],
                "description": "",
            },
        }
        self.objects = {
            "Person": {
                "1": {"id": "1", "name": "Alice", "age": 30},
                "2": {"id": "2", "name": "Bob", "age": 28},
            },
            "Department": {
                "ENG": {"code": "ENG", "title": "Engineering"},
            },
        }
        self.link_types = {
            "worksIn": {
                "source": "Person",
                "target": "Department",
                "description": "employment",
                "source_property": "dept",
                "target_property": "code",
            }
        }
        import networkx as nx

        self.links = nx.MultiDiGraph()
        self.links.add_node("Person:1", type="Person", pk="1")
        self.links.add_node("Person:2", type="Person", pk="2")
        self.links.add_node("Department:ENG", type="Department", pk="ENG")
        self.links.add_edge(
            "Person:1",
            "Department:ENG",
            key="worksIn",
            relation_type="worksIn",
            label="worksIn",
        )
        self.data_mapping = {
            "employees": {"table": "employees", "pk": "id"},
        }


class TestRdfExport(unittest.TestCase):
    def setUp(self) -> None:
        from ontology_domain.rdf_export import (
            export_ontology_to_dataset,
            serialize_trig,
            serialize_turtle_flat,
            serialize_owl_xml,
            export_for_ontosphere,
        )

        self.export_ontology_to_dataset = export_ontology_to_dataset
        self.serialize_trig = serialize_trig
        self.serialize_turtle_flat = serialize_turtle_flat
        self.serialize_owl_xml = serialize_owl_xml
        self.export_for_ontosphere = export_for_ontosphere
        self.onto = _MiniOntology()

    def test_dataset_has_named_graphs(self) -> None:
        ds = self.export_ontology_to_dataset(self.onto)
        self.assertIsInstance(ds, Dataset)
        contexts = {str(c.identifier) for c in ds.contexts()}
        self.assertIn("urn:ionto:schema:", contexts)
        self.assertIn("urn:ionto:data:", contexts)
        self.assertIn("urn:ionto:mapping:", contexts)

    def test_classes_and_properties(self) -> None:
        ds = self.export_ontology_to_dataset(self.onto, include_instances=False)
        # Flatten for simple checks
        triples = list(ds.quads((None, None, None, None)))
        types = [o for s, p, o, _ in triples if p == RDF.type]
        self.assertTrue(any(t == OWL.Class for t in types))
        self.assertTrue(any(t == OWL.DatatypeProperty for t in types))
        self.assertTrue(any(t == OWL.ObjectProperty for t in types))

    def test_serialize_formats(self) -> None:
        ds = self.export_ontology_to_dataset(self.onto)
        trig = self.serialize_trig(ds)
        self.assertIn("urn:ionto:schema:", trig)
        self.assertIn("owl:Class", trig) or self.assertIn("Class", trig)

        ttl = self.serialize_turtle_flat(ds)
        self.assertTrue(len(ttl) > 100)

        xml = self.serialize_owl_xml(ds)
        self.assertIn("rdf:RDF", xml) or self.assertIn("RDF", xml)

    def test_export_for_ontosphere(self) -> None:
        content, filename = self.export_for_ontosphere(self.onto)
        self.assertTrue(filename.endswith(".trig"))
        self.assertIn("ionto", content.lower())
        self.assertGreater(len(content), 200)


if __name__ == "__main__":
    unittest.main()
