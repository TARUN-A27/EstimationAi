from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from po_content_validation import (
    build_extracted_po_document_detail_result,
    normalize_content_understanding_result,
)
from test_ai_document_analysis_v16 import scalar
from test_ai_document_analysis_v17 import resolved_mappings, v17_response
from workflow_audit import WorkflowAuditStore


PROJECT_DIR = Path(__file__).resolve().parent


def array_field(values):
    return {
        "type": "array",
        "valueArray": [scalar(value) for value in values],
    }


def v18_response(*, mappings=(), lines=()):
    raw = v17_response("po", mappings=mappings, lines=lines)
    raw_lines = raw["contents"][0]["fields"]["OrderLines"]["valueArray"]
    for source, target in zip(lines, raw_lines, strict=True):
        if "mapping_reference_number" not in source:
            continue
        alias = source.get("mapping_alias", "MappingReferenceNumber")
        value = source["mapping_reference_number"]
        target["valueObject"][alias] = (
            array_field(value) if isinstance(value, list) else scalar(value)
        )
    return raw


class OrderLineAssociationV18Tests(unittest.TestCase):
    def build(self, raw, mappings):
        normalized = normalize_content_understanding_result(raw)
        return normalized, build_extracted_po_document_detail_result(
            normalized,
            mappings,
        )

    def test_six_messy_lines_resolve_through_all_mapping_aliases(self):
        aliases = [
            "MappingReferenceNumber",
            "MappingReferenceNo",
            "LineMappingReference",
            "LineLMNumber",
            "LMNumber",
            "MappingReferenceNumber",
        ]
        semantic_references = [
            "LM-001/A",
            "LM 002 B",
            "lm.003-c",
            "LM(004)D",
            "LM_005_E",
            "LM-006-F",
        ]
        line_references = [
            " lm 001.a ",
            "lm-002/b",
            " LM 003 C ",
            "lm/004-d",
            "lm.005 e",
            " lm_006/f ",
        ]
        estimations = [174651, 174652, 174653, 174654, 174655, 174656]
        lines = [
            {
                "mapping_reference_number": mapping_reference,
                "mapping_alias": alias,
                "reference_number": f"PRINTED / {index:02d}",
                "count": "30S",
            }
            for index, (alias, mapping_reference) in enumerate(
                zip(aliases, line_references, strict=True),
                start=1,
            )
        ]
        normalized, details = self.build(
            v18_response(
                mappings=list(zip(estimations, semantic_references, strict=True)),
                lines=lines,
            ),
            resolved_mappings(
                *((estimation, f"ORDER-{estimation}") for estimation in estimations)
            ),
        )

        self.assertEqual(
            [line["mapping_reference_number"] for line in normalized["order_lines"]],
            [[value.strip()] for value in line_references],
        )
        self.assertEqual(
            [row["estimation_number"] for row in details["rows"]],
            estimations,
        )
        self.assertEqual(
            details["association_counts"]["mapping_reference_mapping_count"],
            6,
        )
        self.assertEqual(len(details["rejected_lines"]), 0)

    def test_stored_reference_remains_original_printed_business_value(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "LM-ROW-1")],
                lines=[
                    {
                        "mapping_reference_number": "LM ROW 1",
                        "reference_number": "Printed Ref / A-19",
                    }
                ],
            ),
            resolved_mappings((174651, "ORDER-A")),
        )
        self.assertEqual(
            details["rows"][0]["reference_number"],
            "Printed Ref / A-19",
        )

    def test_mapping_reference_is_never_stored_as_business_reference(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "PRIVATE-LM-19")],
                lines=[{"mapping_reference_number": "PRIVATE-LM-19"}],
            ),
            resolved_mappings((174651, "ORDER-A")),
        )
        self.assertIsNone(details["rows"][0]["reference_number"])
        self.assertNotIn(
            "mapping_reference_number",
            details["rows"][0],
        )

    def test_several_lines_may_share_one_mapping_reference(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "LM-SHARED")],
                lines=[
                    {
                        "mapping_reference_number": "LM shared",
                        "reference_number": "PRINT-1",
                    },
                    {
                        "mapping_reference_number": "lm.shared",
                        "reference_number": "PRINT-2",
                    },
                    {
                        "mapping_reference_number": "LM/SHARED",
                        "reference_number": "PRINT-3",
                    },
                ],
            ),
            resolved_mappings((174651, "ORDER-A")),
        )
        self.assertEqual(len(details["rows"]), 3)
        self.assertTrue(
            all(row["estimation_number"] == 174651 for row in details["rows"])
        )
        self.assertEqual(
            [row["reference_number"] for row in details["rows"]],
            ["PRINT-1", "PRINT-2", "PRINT-3"],
        )

    def test_mapping_reference_has_priority_over_legacy_reference(self):
        _, details = self.build(
            v18_response(
                mappings=[
                    (174651, "MAP-REF-A"),
                    (174693, "PRINTED-B"),
                ],
                lines=[
                    {
                        "mapping_reference_number": "map ref a",
                        "reference_number": "PRINTED-B",
                    }
                ],
            ),
            resolved_mappings((174651, "ORDER-A"), (174693, "ORDER-B")),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(details["rows"][0]["reference_number"], "PRINTED-B")
        self.assertEqual(
            details["association_counts"]["mapping_reference_mapping_count"],
            1,
        )
        self.assertEqual(
            details["association_counts"]["legacy_reference_mapping_count"],
            0,
        )

    def test_direct_line_est_has_priority_over_mapping_reference(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "MAP-A"), (174693, "MAP-B")],
                lines=[
                    {
                        "estimate_number": "174651",
                        "mapping_reference_number": "MAP-B",
                        "reference_number": "PRINTED-REF",
                    }
                ],
            ),
            resolved_mappings((174651, "ORDER-A"), (174693, "ORDER-B")),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(
            details["association_counts"]["direct_line_mapping_count"],
            1,
        )
        self.assertEqual(
            details["association_counts"]["mapping_reference_mapping_count"],
            0,
        )

    def test_legacy_reference_fallback_remains_supported(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "LEGACY-REF")],
                lines=[{"reference_number": "legacy ref"}],
            ),
            resolved_mappings((174651, "ORDER-A")),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(
            details["association_counts"]["legacy_reference_mapping_count"],
            1,
        )

    def test_ambiguous_mapping_rejects_only_affected_line(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "MAP-A"), (174693, "MAP-B")],
                lines=[
                    {
                        "mapping_reference_number": "MAP-A",
                        "reference_number": "PRINT-GOOD",
                    },
                    {
                        "mapping_reference_number": ["MAP-A", "MAP-B"],
                        "reference_number": "PRINT-AMBIGUOUS",
                    },
                ],
            ),
            resolved_mappings((174651, "ORDER-A"), (174693, "ORDER-B")),
        )
        self.assertEqual(len(details["rows"]), 1)
        self.assertEqual(details["rows"][0]["reference_number"], "PRINT-GOOD")
        self.assertEqual(len(details["rejected_lines"]), 1)
        self.assertEqual(details["rejected_lines"][0]["source_line_number"], 2)

    def test_valid_lines_remain_insertable_when_another_has_no_safe_mapping(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "MAP-A"), (174693, "MAP-B")],
                lines=[
                    {"mapping_reference_number": "MAP-A"},
                    {
                        "mapping_reference_number": "UNKNOWN-MAP",
                        "reference_number": "MAP-B",
                    },
                    {"mapping_reference_number": "MAP-B"},
                ],
            ),
            resolved_mappings((174651, "ORDER-A"), (174693, "ORDER-B")),
        )
        self.assertEqual(
            [row["estimation_number"] for row in details["rows"]],
            [174651, 174693],
        )
        self.assertEqual(len(details["rejected_lines"]), 1)

    def test_single_parent_fallback_remains_supported(self):
        _, details = self.build(
            v18_response(
                mappings=[(174651, "MAP-A")],
                lines=[{"reference_number": "UNRELATED-PRINTED-REF"}],
            ),
            resolved_mappings((174651, "ORDER-A")),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(
            details["association_counts"]["single_parent_fallback_count"],
            1,
        )

    def test_mapping_reference_values_are_redacted_from_audit_events(self):
        secret_mapping_reference = "PRIVATE-MAPPING-LM-991"
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            audit_store = WorkflowAuditStore(audit_path)
            audit_store.record(
                "workflow-v18",
                filename="redacted.pdf",
                stage="po_detail_preparation",
                label="PO detail preparation",
                status="completed",
                message="Prepared independently insertable PO detail lines",
                details={
                    "mapping_reference_number": secret_mapping_reference,
                    "direct_line_mapping_count": 0,
                    "mapping_reference_mapping_count": 1,
                    "legacy_reference_mapping_count": 0,
                    "single_parent_fallback_count": 0,
                    "prepared_row_count": 1,
                    "rejected_line_count": 0,
                },
            )
            event = json.loads(audit_path.read_text(encoding="utf-8"))

        self.assertNotIn(secret_mapping_reference, json.dumps(event))
        self.assertEqual(
            event["details"]["mapping_reference_number"],
            {"redacted": True},
        )
        self.assertEqual(
            event["details"]["mapping_reference_mapping_count"],
            1,
        )

    def test_parent_transaction_independence_remains_unchanged(self):
        source_text = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        source = ast.get_source_segment(source_text, function)
        self.assertLess(
            source.index('"po_parent_commit"'),
            source.index('"po_detail_transaction"'),
        )
        self.assertIn('"po_detail_rollback"', source)


if __name__ == "__main__":
    unittest.main()
