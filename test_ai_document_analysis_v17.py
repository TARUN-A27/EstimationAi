from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from po_content_validation import (
    analyze_uploaded_document,
    build_extracted_po_document_detail_result,
    normalize_content_understanding_result,
)
from test_ai_document_analysis_v16 import response, scalar


PROJECT_DIR = Path(__file__).resolve().parent
REDACTED_FIXTURE = (
    PROJECT_DIR
    / "tests"
    / "fixtures"
    / "azure_content_understanding_po_redacted.json"
)


def v17_response(
    document_type,
    *,
    primary=None,
    legacy=(),
    mappings=(),
    lines=(),
):
    result = response(document_type, legacy, lines)
    fields = result["contents"][0]["fields"]
    if primary is not None:
        fields["PrimaryEstimationNumber"] = scalar(primary)
    if mappings:
        fields["EstimationMappings"] = {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        "EstimateNumber": scalar(estimation),
                        "ReferenceNumber": scalar(reference),
                    },
                }
                for estimation, reference in mappings
            ],
        }
    return result


def resolved_mappings(*pairs):
    return [
        {
            "estimation_no": estimation,
            "regular_order_number": order,
            "reference_order_numbers": [],
        }
        for estimation, order in pairs
    ]


class UnifiedAnalyzerNormalizationV17Tests(unittest.TestCase):
    def test_primary_174651_wins_over_unrelated_174604(self):
        result = normalize_content_understanding_result(
            v17_response(
                "estimation",
                primary="174651",
                legacy=["174604"],
            )
        )
        self.assertEqual(result["primary_estimation_number"], 174651)
        self.assertEqual(result["legacy_estimation_numbers"], [174604])
        self.assertEqual(result["estimation_numbers"], [174651])

    def test_primary_174693_ignores_legacy_and_table_values(self):
        result = normalize_content_understanding_result(
            v17_response(
                "estimation",
                primary="174693",
                legacy=["174618", "174619"],
                lines=[{"estimate_number": "174620"}],
            )
        )
        self.assertEqual(result["estimation_numbers"], [174693])
        self.assertEqual(result["line_estimation_numbers"], [174620])

    def test_mixing_uses_primary_estimation_number(self):
        result = normalize_content_understanding_result(
            v17_response("mix", primary="174693", legacy=["174618"])
        )
        self.assertEqual(result["estimation_numbers"], [174693])

    def test_multiple_legacy_values_without_primary_require_review(self):
        result = normalize_content_understanding_result(
            v17_response("mix", legacy=["174618", "174693"])
        )
        self.assertEqual(result["estimation_numbers"], [])
        self.assertTrue(result["warnings"])

    def test_invalid_present_primary_does_not_enable_legacy_fallback(self):
        result = normalize_content_understanding_result(
            v17_response("estimation", primary="not-an-est", legacy=["174651"])
        )
        self.assertTrue(result["primary_estimation_present"])
        self.assertIsNone(result["primary_estimation_number"])
        self.assertEqual(result["estimation_numbers"], [])

    def test_po_estimation_mappings_parse_and_join_candidates(self):
        result = normalize_content_understanding_result(
            v17_response(
                "po",
                legacy=["174600"],
                mappings=[("174651", " REF-A "), ("174693", "REF-B")],
                lines=[{"estimate_number": "174700"}],
            )
        )
        self.assertEqual(
            result["estimation_mappings"][0],
            {
                "estimate_number": 174651,
                "reference_number": "REF-A",
                "reference_numbers": ["REF-A"],
            },
        )
        self.assertEqual(
            result["estimation_numbers"],
            [174600, 174651, 174693, 174700],
        )

    def test_v17_analysis_audit_contains_counts_only(self):
        raw = v17_response(
            "po",
            primary="174651",
            mappings=[("174651", "PRIVATE-REFERENCE")],
            lines=[{"reference_number": "PRIVATE-REFERENCE"}],
        )
        events = []
        with patch(
            "po_content_validation.analyzer_configuration",
            return_value=("private-analyzer-id", "CONTENTUNDERSTANDING_ANALYZER_ID"),
        ), patch(
            "po_content_validation.analyze_po_document",
            return_value=raw,
        ):
            analyze_uploaded_document(
                Path("unused.pdf"),
                audit=lambda stage, status, message, details: events.append(
                    (stage, details)
                ),
            )
        count_details = next(
            details
            for stage, details in events
            if stage == "ai_extraction_counts"
        )
        self.assertTrue(count_details["primary_estimation_present"])
        self.assertEqual(count_details["estimation_mapping_count"], 1)
        self.assertNotIn("PRIVATE-REFERENCE", str(events))
        self.assertNotIn("private-analyzer-id", str(events))

    def test_redacted_real_v17_response_shape(self):
        result = normalize_content_understanding_result(
            json.loads(REDACTED_FIXTURE.read_text(encoding="utf-8"))
        )
        self.assertEqual(result["primary_estimation_number"], 175535)
        self.assertEqual(len(result["estimation_mappings"]), 1)
        self.assertEqual(result["estimation_numbers"], [175535])


class OrderLineAssociationV17Tests(unittest.TestCase):
    def build(self, result_data, mappings):
        normalized = normalize_content_understanding_result(result_data)
        return build_extracted_po_document_detail_result(
            normalized,
            mappings,
        )

    def test_multi_po_lines_resolve_through_reference_mappings(self):
        details = self.build(
            v17_response(
                "po",
                mappings=[("174651", "REF-A"), ("174693", "REF-B")],
                lines=[
                    {"reference_number": " ref a ", "count": "30S"},
                    {"reference_number": "REF-B", "count": "34S"},
                ],
            ),
            resolved_mappings(
                (174651, "LM-A"),
                (174693, "LM-B"),
            ),
        )
        self.assertEqual(
            [row["estimation_number"] for row in details["rows"]],
            [174651, 174693],
        )
        self.assertEqual(
            details["association_counts"]["legacy_reference_mapping_count"],
            2,
        )

    def test_multiple_lines_can_share_one_est(self):
        details = self.build(
            v17_response(
                "po",
                mappings=[("174651", "REF-A")],
                lines=[
                    {"reference_number": "REF-A", "count": "30S"},
                    {"reference_number": "ref.a", "count": "34S"},
                ],
            ),
            resolved_mappings((174651, "LM-A")),
        )
        self.assertEqual(len(details["rows"]), 2)
        self.assertTrue(
            all(row["estimation_number"] == 174651 for row in details["rows"])
        )

    def test_direct_line_est_has_priority_over_reference(self):
        details = self.build(
            v17_response(
                "po",
                mappings=[("174651", "REF-A"), ("174693", "REF-B")],
                lines=[
                    {
                        "estimate_number": "174651",
                        "reference_number": "REF-B",
                    }
                ],
            ),
            resolved_mappings(
                (174651, "LM-A"),
                (174693, "LM-B"),
            ),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(
            details["association_counts"]["direct_line_mapping_count"],
            1,
        )

    def test_single_est_fallback_remains_supported(self):
        details = self.build(
            v17_response("po", legacy=["174651"], lines=[{"count": "30S"}]),
            resolved_mappings((174651, "LM-A")),
        )
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(
            details["association_counts"]["single_parent_fallback_count"],
            1,
        )

    def test_ambiguous_reference_mapping_rejects_only_that_line(self):
        raw = v17_response(
            "po",
            mappings=[("174651", "REF-A"), ("174693", "REF-B")],
            lines=[
                {"reference_number": "REF-A", "count": "30S"},
                {"reference_number": "placeholder", "count": "34S"},
            ],
        )
        second_line = raw["contents"][0]["fields"]["OrderLines"][
            "valueArray"
        ][1]["valueObject"]
        second_line["ReferenceNumber"] = {
            "type": "array",
            "valueArray": [scalar("REF-A"), scalar("REF-B")],
        }
        details = self.build(
            raw,
            resolved_mappings(
                (174651, "LM-A"),
                (174693, "LM-B"),
            ),
        )
        self.assertEqual(len(details["rows"]), 1)
        self.assertEqual(details["rows"][0]["estimation_number"], 174651)
        self.assertEqual(len(details["rejected_lines"]), 1)
        self.assertEqual(details["rejected_lines"][0]["source_line_number"], 2)

    def test_parent_commit_remains_independent_of_detail_rejection(self):
        tree = ast.parse((PROJECT_DIR / "app.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        source = ast.get_source_segment(
            (PROJECT_DIR / "app.py").read_text(encoding="utf-8"),
            function,
        )
        self.assertLess(
            source.index('"po_parent_commit"'),
            source.index('"po_detail_transaction"'),
        )
        self.assertIn('"po_detail_rollback"', source)


if __name__ == "__main__":
    unittest.main()
