from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as production_app
from app import content_sha256, resolve_po_estimation_candidates
from po_content_validation import (
    CONTENT_UNDERSTANDING_SOURCE,
    analyze_uploaded_document,
    build_extracted_po_document_detail_result,
    normalize_content_understanding_result,
)


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = PROJECT_DIR / "tests" / "fixtures" / "v33"
FIXTURES = {
    "lm111579": ("v27_lm111579_redacted.json", 2, 2),
    "lm111598": ("v27_lm111598_redacted.json", 2, 2),
    "lm111600": ("v27_lm111600_redacted.json", 1, 1),
    "lm111601": ("v27_lm111601_redacted.json", 1, 1),
    "lm111602": ("v27_lm111602_redacted.json", 2, 2),
    "lmo09182": ("v27_lmo09182_redacted.json", 1, 1),
}


def scalar(value=None, *, source=None):
    field = {"type": "string"}
    if value is not None:
        field["valueString"] = str(value)
    if source is not None:
        field["source"] = source
    return field


def raw_response(
    *,
    document_type="po",
    estimations=(),
    mappings=(),
    lines=(),
    markdown="",
):
    fields = {
        "DocumentType": scalar(document_type),
        "EstimationNumbers": {
            "type": "array",
            "valueArray": [scalar(value) for value in estimations],
        },
        "EstimationMappings": {
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
        },
        "OrderLines": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        {
                            "estimate_number": "EstimateNumber",
                            "mapping_reference_number": "MappingReferenceNumber",
                            "reference_number": "ReferenceNumber",
                            "count": "Count",
                            "required_quantity": "RequiredQuantity",
                            "confirm_rate": "ConfirmRate",
                            "certification": "Certification",
                        }[key]: scalar(value)
                        for key, value in line.items()
                    },
                }
                for line in lines
            ],
        },
    }
    return {
        "contents": [
            {
                "kind": "document",
                "startPageNumber": 1,
                "fields": fields,
                "markdown": markdown,
            }
        ]
    }


def resolved(*pairs):
    return [
        {
            "estimation_no": estimation,
            "regular_order_number": order,
        }
        for estimation, order in pairs
    ]


class MappingCursor:
    def __init__(self, rows_by_estimation):
        self.rows_by_estimation = rows_by_estimation
        self.estimation_no = None

    def execute(self, _query, **binds):
        self.estimation_no = binds["estimation_no"]

    def fetchall(self):
        return self.rows_by_estimation.get(self.estimation_no, [])


class RealFixtureV33Tests(unittest.TestCase):
    def test_all_six_redacted_v27_fixtures_parse_without_invented_fields(self):
        for name, (filename, mapping_count, line_count) in FIXTURES.items():
            with self.subTest(name=name):
                envelope = json.loads((FIXTURE_DIR / filename).read_text())
                self.assertEqual(envelope["status"], "Succeeded")
                self.assertEqual(
                    envelope["result"]["analyzerId"],
                    "EstimationAi_V27_Unified",
                )
                normalized = normalize_content_understanding_result(
                    envelope["result"]
                )
                self.assertEqual(normalized["document_type"], "po")
                self.assertEqual(
                    normalized["semantic_mapping_count"], mapping_count
                )
                self.assertEqual(
                    len(normalized["estimation_mappings"]), mapping_count
                )
                self.assertEqual(len(normalized["order_lines"]), line_count)
                self.assertEqual(normalized["fallback_mapping_count"], 0)

    def test_complete_semantic_mappings_bypass_conflicting_markdown_fallback(self):
        raw = raw_response(
            estimations=[180701],
            mappings=[(180701, "LM900701")],
            markdown=(
                "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
                "<tr><td>180799</td><td>LM900799</td></tr></table>"
            ),
        )
        result = normalize_content_understanding_result(raw)
        self.assertEqual(result["mapping_estimation_numbers"], [180701])
        self.assertEqual(result["fallback_mapping_count"], 0)
        self.assertEqual(result["mapping_fallback_used_count"], 0)

    def test_real_unresolved_mapping_reference_rejects_only_affected_lines(self):
        envelope = json.loads(
            (FIXTURE_DIR / "v27_lm111579_redacted.json").read_text()
        )
        normalized = normalize_content_understanding_result(envelope["result"])
        details = build_extracted_po_document_detail_result(
            normalized,
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(details["rows"], [])
        self.assertEqual(len(details["rejected_lines"]), 2)

    def test_supporting_email_fixture_contributes_no_identity_or_line(self):
        envelope = json.loads(
            (FIXTURE_DIR / "v27_lm111601_redacted.json").read_text()
        )
        normalized = normalize_content_understanding_result(envelope["result"])
        self.assertEqual(normalized["supporting_page_ignored_count"], 1)
        self.assertEqual(normalized["document_estimation_numbers"], [180401])
        self.assertEqual(len(normalized["order_lines"]), 1)


class AssociationV33Tests(unittest.TestCase):
    def build(self, raw, retained, original=None, **kwargs):
        normalized = normalize_content_understanding_result(raw)
        return build_extracted_po_document_detail_result(
            normalized,
            retained,
            original_estimation_order_mappings=original,
            **kwargs,
        )

    def test_multi_map_filtered_to_one_parent_never_uses_single_fallback(self):
        raw = raw_response(
            estimations=[180101, 180102],
            mappings=[(180101, "LM-A"), (180102, "LM-B")],
            lines=[{}, {}],
        )
        result = self.build(
            raw,
            resolved((180101, "ORDER-A")),
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(result["association_counts"]["single_parent_fallback_count"], 0)
        self.assertEqual([row["estimation_number"] for row in result["rows"]], [180101])
        self.assertEqual(len(result["rejected_lines"]), 1)

    def test_two_direct_lines_remain_independently_associated(self):
        result = self.build(
            raw_response(
                estimations=[180101, 180102],
                mappings=[(180101, "LM-A"), (180102, "LM-B")],
                lines=[
                    {"estimate_number": 180101},
                    {"estimate_number": 180102},
                ],
            ),
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(
            [row["estimation_number"] for row in result["rows"]],
            [180101, 180102],
        )

    def test_unresolved_populated_mapping_reference_rejects_affected_line(self):
        result = self.build(
            raw_response(
                estimations=[180101],
                mappings=[(180101, "LM-A")],
                lines=[{"mapping_reference_number": "UNKNOWN"}, {}],
            ),
            resolved((180101, "ORDER-A")),
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(len(result["rejected_lines"]), 1)

    def test_conflicting_direct_estimate_rejects_affected_line(self):
        result = self.build(
            raw_response(
                estimations=[180101],
                mappings=[(180101, "LM-A")],
                lines=[{"estimate_number": 180999}, {}],
            ),
            resolved((180101, "ORDER-A")),
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(len(result["rejected_lines"]), 1)

    def test_complete_ordered_fallback_preserves_row_order(self):
        result = self.build(
            raw_response(
                estimations=[180101, 180102],
                mappings=[(180101, "LM-A"), (180102, "LM-B")],
                lines=[{"count": "FIRST"}, {"count": "SECOND"}],
            ),
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(
            [row["estimation_number"] for row in result["rows"]],
            [180101, 180102],
        )
        self.assertEqual(result["association_counts"]["ordered_mapping_fallback_count"], 2)

    def test_mapping_line_count_mismatch_disables_ordered_fallback(self):
        result = self.build(
            raw_response(
                estimations=[180101, 180102],
                mappings=[(180101, "LM-A"), (180102, "LM-B")],
                lines=[{}, {}, {}],
            ),
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["rejected_lines"]), 3)

    def test_genuine_single_mapping_still_supports_single_fallback(self):
        result = self.build(
            raw_response(
                estimations=[180101],
                mappings=[(180101, "LM-A")],
                lines=[{"count": "REDACTED"}],
            ),
            resolved((180101, "ORDER-A")),
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["association_counts"]["single_parent_fallback_count"], 1)

    def test_structurally_rejected_line_does_not_affect_valid_line(self):
        result = self.build(
            raw_response(
                estimations=[180101, 180102],
                mappings=[(180101, "LM-A"), (180102, "LM-B")],
                lines=[
                    {"estimate_number": 180101},
                    {"estimate_number": "INVALID"},
                ],
            ),
            resolved((180101, "ORDER-A"), (180102, "ORDER-B")),
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(len(result["rejected_lines"]), 1)


class MarkdownFallbackV33Tests(unittest.TestCase):
    def normalize_table(self, rows, *, document_type="po", semantic=()):
        markdown = "<table><tr><td>EST NO</td><td>LM NO</td></tr>" + "".join(
            f"<tr><td>{est}</td><td>{reference}</td></tr>"
            for est, reference in rows
        ) + "</table>"
        return normalize_content_understanding_result(
            raw_response(
                document_type=document_type,
                mappings=semantic,
                markdown=markdown,
            )
        )

    def test_fallback_requires_po_and_anchored_table(self):
        result = self.normalize_table([("180101", "LM-A")])
        self.assertEqual(result["mapping_fallback_used_count"], 1)
        self.assertEqual(result["mapping_estimation_numbers"], [180101])
        non_po = self.normalize_table(
            [("180101", "LM-A")], document_type="mix"
        )
        self.assertEqual(non_po["mapping_fallback_used_count"], 0)

    def test_unrelated_six_digit_values_outside_table_are_ignored(self):
        raw = raw_response(
            markdown=(
                "Unrelated 999999\n"
                "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
                "<tr><td>180101</td><td>LM-A</td></tr></table>"
            )
        )
        result = normalize_content_understanding_result(raw)
        self.assertEqual(result["mapping_estimation_numbers"], [180101])

    def test_five_and_seven_digit_fallback_estimates_are_rejected(self):
        for estimate in ("80101", "1801019"):
            with self.subTest(estimate=estimate):
                result = self.normalize_table([(estimate, "LM-A")])
                self.assertEqual(result["estimation_mappings"], [])
                self.assertGreater(result["mapping_conflict_count"], 0)

    def test_incomplete_mapping_row_is_rejected(self):
        result = self.normalize_table([("180101", "")])
        self.assertEqual(result["estimation_mappings"], [])
        self.assertGreater(result["mapping_conflict_count"], 0)

    def test_conflicting_rows_require_review(self):
        result = self.normalize_table(
            [("180101", "LM-A"), ("180101", "LM-B")]
        )
        self.assertEqual(result["estimation_mappings"], [])
        self.assertGreater(result["mapping_conflict_count"], 0)

    def test_identical_mapping_pairs_are_deduplicated(self):
        result = self.normalize_table(
            [("180101", "LM-A"), ("180101", "LM-A")]
        )
        self.assertEqual(len(result["estimation_mappings"]), 1)

    def test_mapping_rows_never_fabricate_order_lines(self):
        result = self.normalize_table([("180101", "LM-A")])
        self.assertEqual(result["order_lines"], [])

    def test_unanchored_text_never_activates_fallback(self):
        result = normalize_content_understanding_result(
            raw_response(markdown="180101 LM-A 999999")
        )
        self.assertEqual(result["estimation_mappings"], [])
        self.assertEqual(result["mapping_table_detected_count"], 0)

    def test_split_headers_and_cells_remain_a_bounded_mapping_table(self):
        raw = raw_response(
            markdown=(
                "<table><tr><td>INFO</td><td>EST</td><td>NO</td>"
                "<td></td><td>LMNO</td></tr>"
                "<tr><td>REDACTED</td><td>180101</td><td></td>"
                "<td></td><td>LMI90101</td></tr>"
                "<tr><td></td><td>180102</td><td></td>"
                "<td>LMI</td><td>90102</td></tr>"
                "<tr><td></td><td></td><td></td><td></td><td></td></tr>"
                "<tr><td>TOTAL</td><td></td><td></td><td>999999</td>"
                "<td>KG</td></tr></table>"
            )
        )
        result = normalize_content_understanding_result(raw)
        self.assertEqual(result["mapping_estimation_numbers"], [180101, 180102])
        self.assertEqual(result["mapping_conflict_count"], 0)


class SemanticMappingCompletenessV33Tests(unittest.TestCase):
    @staticmethod
    def mapping_table(*pairs):
        return (
            "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
            + "".join(
                f"<tr><td>{estimation}</td><td>{reference}</td></tr>"
                for estimation, reference in pairs
            )
            + "</table>"
        )

    def incomplete_semantic_result(self, *, markdown=""):
        return normalize_content_understanding_result(
            raw_response(
                estimations=[180101, 180102],
                mappings=[(180101, "LM-A"), (180102, "")],
                markdown=markdown,
            )
        )

    def test_valid_plus_incomplete_semantic_mapping_without_fallback_blocks(self):
        result = self.incomplete_semantic_result()
        self.assertEqual(result["estimation_mappings"], [])
        self.assertGreater(result["mapping_conflict_count"], 0)
        self.assertEqual(
            result["document_estimation_numbers"],
            [180101, 180102],
        )

    def test_complete_fallback_replaces_entire_incomplete_semantic_set(self):
        result = self.incomplete_semantic_result(
            markdown=self.mapping_table(
                (180101, "LM-A"),
                (180102, "LM-B"),
            )
        )
        self.assertEqual(result["mapping_conflict_count"], 0)
        self.assertEqual(result["mapping_fallback_used_count"], 1)
        self.assertEqual(result["fallback_mapping_count"], 2)
        self.assertEqual(
            result["mapping_estimation_numbers"],
            [180101, 180102],
        )

    def test_fallback_missing_one_document_estimation_blocks(self):
        result = self.incomplete_semantic_result(
            markdown=self.mapping_table((180101, "LM-A"))
        )
        self.assertEqual(result["estimation_mappings"], [])
        self.assertEqual(result["mapping_fallback_used_count"], 0)
        self.assertGreater(result["mapping_conflict_count"], 0)
        self.assertEqual(
            result["document_estimation_numbers"],
            [180101, 180102],
        )

    def test_positive_mapping_conflict_performs_no_database_write_or_commit(self):
        result = self.incomplete_semantic_result()

        class RecordingConnection:
            def __init__(self):
                self.executed = []
                self.commit_count = 0

            def cursor(self):
                raise AssertionError("mapping conflict must precede a cursor")

            def commit(self):
                self.commit_count += 1

        connection = RecordingConnection()
        workflow = {}
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "po.pdf"
            document.write_bytes(b"%PDF-redacted")
            with patch.object(
                production_app,
                "required_env",
                return_value="1",
            ), patch.object(
                production_app,
                "get_db_connection",
                return_value=connection,
            ) as get_connection:
                with self.assertRaisesRegex(
                    production_app.DocumentValidationError,
                    "requires review",
                ):
                    production_app.insert_regular_order_document(
                        filename="po.pdf",
                        file_path=document,
                        text="",
                        remarks="test",
                        system_name="test",
                        workflow=workflow,
                        analysis_result=result,
                    )

        get_connection.assert_not_called()
        self.assertEqual(connection.executed, [])
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(
            workflow["validation"]["status"],
            "review_required",
        )


class SafetyAndValidationV33Tests(unittest.TestCase):
    def test_semantic_wrong_order_conflict_requires_review(self):
        cursor = MappingCursor({180101: [("ORDER-A", "REF-A")]})
        with self.assertRaisesRegex(
            production_app.DocumentValidationError,
            "does not resolve",
        ):
            resolve_po_estimation_candidates(
                cursor,
                [180101],
                [],
                expected_est_no=180101,
                expected_order_no="ORDER-A",
                estimation_mappings=[
                    {
                        "estimate_number": 180101,
                        "reference_numbers": ["CONFLICT"],
                    }
                ],
            )

    def test_semantic_reference_order_is_accepted(self):
        cursor = MappingCursor({180101: [("ORDER-A", "REF-A")]})
        result = resolve_po_estimation_candidates(
            cursor,
            [180101],
            [],
            expected_est_no=180101,
            expected_order_no="ORDER-A",
            estimation_mappings=[
                {
                    "estimate_number": 180101,
                    "reference_numbers": ["REF.A"],
                }
            ],
        )
        self.assertEqual(len(result["mappings"]), 1)

    def test_identical_content_is_detected_without_filename_state(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pdf"
            second = Path(directory) / "second.pdf"
            first.write_bytes(b"same-redacted-content")
            second.write_bytes(b"same-redacted-content")
            self.assertEqual(content_sha256(first), content_sha256(second))

        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        upload_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upload_files"
        )
        upload_source = ast.get_source_segment(source, upload_function)
        self.assertIn("successful_content_digests = set()", upload_source)
        self.assertIn("duplicate_content_detected_count", upload_source)
        module_assignments = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        self.assertNotIn("successful_content_digests", module_assignments)

    def test_supporting_page_sourced_line_is_ignored(self):
        raw = raw_response(
            estimations=[180101],
            mappings=[(180101, "LM-A")],
            markdown=(
                "PURCHASE ORDER\n<!-- PageBreak -->\n"
                "To: redacted@example.invalid\n[Quoted text hidden]"
            ),
        )
        raw["contents"][0]["OrderLines"] = raw["contents"][0]["fields"][
            "OrderLines"
        ]
        raw["contents"][0]["fields"]["OrderLines"]["valueArray"] = [
            {
                "type": "object",
                "valueObject": {
                    "ReferenceNumber": scalar(
                        "REDACTED", source="D(2,1,1,2,1,2,2,1,2)"
                    )
                },
            }
        ]
        result = normalize_content_understanding_result(raw)
        self.assertEqual(result["order_lines"], [])
        self.assertEqual(result["supporting_page_ignored_count"], 1)

    def test_v32_optional_match_and_failures_remain_per_field(self):
        normalized = normalize_content_understanding_result(
            raw_response(
                estimations=[180101],
                mappings=[(180101, "LM-A")],
                lines=[
                    {
                        "estimate_number": 180101,
                        "reference_number": "RIGHT",
                        "count": "WRONG",
                    }
                ],
            )
        )
        result = build_extracted_po_document_detail_result(
            normalized,
            resolved((180101, "ORDER-A")),
            certificate_master_entries=[],
            oracle_expected_rows=[
                {
                    "estimation_number": 180101,
                    "reference_number": "RIGHT",
                    "count_name": "EXPECTED",
                }
            ],
        )
        row = result["rows"][0]
        self.assertEqual(row["reference_number"], "RIGHT")
        self.assertIsNone(row["count_name"])
        self.assertIsNone(row["party_name"])
        self.assertIsNone(row["required_quantity"])
        self.assertIsNone(row["certification"])
        self.assertIsNone(row["net_rate"])

    def test_every_structurally_rejected_replacement_preserves_details(self):
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        function_source = ast.get_source_segment(source, function)
        self.assertLess(
            function_source.index("if po_detail_rows:"),
            function_source.index("DELETE FROM REGULARORDER_PODOCUMENTDETAILS"),
        )

    def test_audit_metadata_contains_only_value_free_mapping_counters(self):
        raw = raw_response(
            estimations=[180101],
            mappings=[(180101, "PRIVATE-REFERENCE")],
            lines=[{"estimate_number": 180101}],
            markdown=(
                "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
                "<tr><td>180101</td><td>PRIVATE-REFERENCE</td></tr></table>"
            ),
        )
        events = []
        with patch(
            "po_content_validation.analyzer_configuration",
            return_value=("private-analyzer", "CONTENTUNDERSTANDING_ANALYZER_ID"),
        ), patch("po_content_validation.analyze_po_document", return_value=raw):
            analyze_uploaded_document(
                Path("unused.pdf"),
                audit=lambda stage, status, message, details: events.append(
                    (stage, details)
                ),
            )
        details = next(
            detail for stage, detail in events if stage == "ai_extraction_counts"
        )
        for key in (
            "semantic_mapping_count",
            "fallback_mapping_count",
            "mapping_table_detected_count",
            "mapping_fallback_used_count",
            "mapping_conflict_count",
            "supporting_page_ignored_count",
            "original_document_mapping_count",
        ):
            self.assertIsInstance(details[key], int)
        self.assertNotIn("PRIVATE-REFERENCE", repr(events))
        self.assertNotIn("180101", repr(events))


if __name__ == "__main__":
    unittest.main()
