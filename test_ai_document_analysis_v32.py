from __future__ import annotations

import ast
import unittest
from pathlib import Path

from po_content_validation import (
    CONTENT_UNDERSTANDING_SOURCE,
    ORACLE_EXPECTED_VALUES_QUERY,
    build_extracted_po_document_detail_result,
    fetch_oracle_expected_rows,
)


PROJECT_DIR = Path(__file__).resolve().parent
MAPPINGS = [
    {
        "estimation_no": 175164,
        "regular_order_number": "LM111360",
    }
]
MASTER = [(7, "GRS"), (8, "B.C.T"), (9, "BCT")]


def expected(**overrides):
    row = {
        "estimation_number": "175164",
        "reference_number": "RIGHT-REF",
        "required_quantity": "1875",
        "count_name": "30S",
        "party_name": "ACME PRIVATE LIMITED",
        "booking_rate": "609",
        "certification": "GRS",
    }
    row.update(overrides)
    return row


def build(lines, oracle_rows=None, *, party_names=(), master=MASTER):
    normalized_lines = []
    for line in lines:
        normalized_lines.append(
            {
                key: value if isinstance(value, list) else [value]
                for key, value in line.items()
            }
        )
    return build_extracted_po_document_detail_result(
        {
            "analysis_source": CONTENT_UNDERSTANDING_SOURCE,
            "order_lines": normalized_lines,
            "party_names": list(party_names),
            "estimation_mappings": [],
        },
        MAPPINGS,
        certificate_master_entries=master,
        oracle_expected_rows=(
            [expected()] if oracle_rows is None else oracle_rows
        ),
    )


class ExpectedRowsCursor:
    description = [
        ("ESTIMATION_NUMBER",),
        ("REFERENCE_NUMBER",),
        ("REQUIRED_QUANTITY",),
        ("COUNT_NAME",),
        ("PARTY_NAME",),
        ("BOOKING_RATE",),
        ("CERTIFICATION",),
    ]

    def __init__(self):
        self.executions = []
        self.estimation_number = None

    def execute(self, query, **binds):
        self.executions.append((query, binds))
        self.estimation_number = binds["est_no"]

    def fetchall(self):
        if self.estimation_number == 175164:
            return [
                (
                    175164,
                    "RIGHT-REF",
                    1875,
                    "30S",
                    "ACME PRIVATE LIMITED",
                    609,
                    "GRS",
                )
            ]
        return []


class PerFieldOracleValidationV32Tests(unittest.TestCase):
    def test_expected_query_runs_once_per_unique_est_on_supplied_cursor(self):
        cursor = ExpectedRowsCursor()

        rows, missing = fetch_oracle_expected_rows(
            cursor,
            [175164, 175164, 999999],
        )

        self.assertEqual(len(cursor.executions), 2)
        self.assertTrue(
            all(query == ORACLE_EXPECTED_VALUES_QUERY for query, _ in cursor.executions)
        )
        self.assertEqual(
            [binds for _, binds in cursor.executions],
            [{"est_no": 175164}, {"est_no": 999999}],
        )
        self.assertEqual(rows[0]["estimation_number"], "175164")
        self.assertEqual(missing, ["999999"])

    def test_one_mismatch_nulls_only_that_field(self):
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "party_name": "Acme Pvt. Ltd.",
                    "reference_number": "WRONG",
                    "required_quantity": "1,875.000",
                    "count": "30 S",
                    "confirm_rate": "609.00",
                    "certification": "g.r.s.",
                }
            ]
        )

        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(
            result["rows"][0],
            {
                "estimation_number": 175164,
                "regular_order_number": "LM111360",
                "party_name": "Acme Pvt. Ltd.",
                "reference_number": None,
                "required_quantity": "1875.000",
                "count_name": "30 S",
                "certification": "GRS",
                "net_rate": "609.00",
            },
        )
        self.assertEqual(
            result["field_validation_results"][0]["fields"],
            {
                "party_name": "MATCH",
                "reference_number": "MISMATCH",
                "required_quantity": "MATCH",
                "count": "MATCH",
                "confirm_rate": "MATCH",
                "certification": "MATCH",
            },
        )
        self.assertNotIn("fields", result["rows"][0])

    def test_missing_fields_are_null_and_counted_without_rejection(self):
        result = build(
            [{"estimate_number": "175164", "count": "30S"}],
        )

        row = result["rows"][0]
        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(row["count_name"], "30S")
        for field_name in (
            "party_name",
            "reference_number",
            "required_quantity",
            "certification",
            "net_rate",
        ):
            self.assertIsNone(row[field_name])
        self.assertEqual(
            result["field_validation_counts"][
                "oracle_reference_number_missing_count"
            ],
            1,
        )
        self.assertEqual(
            result["field_validation_counts"][
                "oracle_required_quantity_missing_count"
            ],
            1,
        )

    def test_invalid_and_multiple_numeric_values_null_only_their_fields(self):
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "reference_number": "RIGHT-REF",
                    "required_quantity": ["1875", "1900"],
                    "count": "30S",
                    "confirm_rate": "609 malformed",
                }
            ]
        )

        row = result["rows"][0]
        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(row["reference_number"], "RIGHT-REF")
        self.assertEqual(row["count_name"], "30S")
        self.assertIsNone(row["required_quantity"])
        self.assertIsNone(row["net_rate"])
        statuses = result["field_validation_results"][0]["fields"]
        self.assertEqual(statuses["required_quantity"], "AMBIGUOUS")
        self.assertEqual(statuses["confirm_rate"], "INVALID")

    def test_party_uses_safe_equality_not_substring_or_fuzzy_matching(self):
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "party_name": "ACME",
                    "reference_number": "RIGHT-REF",
                }
            ]
        )

        self.assertIsNone(result["rows"][0]["party_name"])
        self.assertEqual(
            result["field_validation_results"][0]["fields"]["party_name"],
            "MISMATCH",
        )

    def test_unmatched_or_ambiguous_certificate_no_longer_rejects_line(self):
        for certification in (
            "UNKNOWN",
            ["GRS", "BCT"],
        ):
            with self.subTest(certification=certification):
                result = build(
                    [
                        {
                            "estimate_number": "175164",
                            "reference_number": "RIGHT-REF",
                            "certification": certification,
                        }
                    ]
                )
                self.assertEqual(result["rejected_lines"], [])
                self.assertIsNone(result["rows"][0]["certification"])
                self.assertEqual(
                    result["rows"][0]["reference_number"],
                    "RIGHT-REF",
                )

    def test_ambiguous_oracle_row_nulls_all_present_business_fields(self):
        duplicate = expected()
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "reference_number": "RIGHT-REF",
                    "count": "30S",
                }
            ],
            [expected(), duplicate],
        )

        self.assertEqual(result["rejected_lines"], [])
        self.assertIsNone(result["rows"][0]["reference_number"])
        self.assertIsNone(result["rows"][0]["count_name"])
        self.assertEqual(
            result["field_validation_results"][0]["oracle_row_status"],
            "AMBIGUOUS",
        )

    def test_unique_best_oracle_row_preserves_only_its_matching_fields(self):
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "reference_number": "REF-B",
                    "required_quantity": "200.000",
                    "count": "WRONG",
                }
            ],
            [
                expected(
                    reference_number="REF-A",
                    required_quantity="100",
                    count_name="20S",
                ),
                expected(
                    reference_number="REF-B",
                    required_quantity="200",
                    count_name="40S",
                ),
            ],
        )

        row = result["rows"][0]
        self.assertEqual(row["reference_number"], "REF-B")
        self.assertEqual(row["required_quantity"], "200.000")
        self.assertIsNone(row["count_name"])
        statuses = result["field_validation_results"][0]["fields"]
        self.assertEqual(statuses["reference_number"], "MATCH")
        self.assertEqual(statuses["required_quantity"], "MATCH")
        self.assertEqual(statuses["count"], "MISMATCH")

    def test_one_oracle_row_is_not_reused_for_a_second_line(self):
        line = {
            "estimate_number": "175164",
            "reference_number": "RIGHT-REF",
        }
        result = build([line, line])

        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["reference_number"], "RIGHT-REF")
        self.assertIsNone(result["rows"][1]["reference_number"])

    def test_missing_oracle_row_keeps_structural_row_with_null_business_data(self):
        result = build(
            [
                {
                    "estimate_number": "175164",
                    "reference_number": "RIGHT-REF",
                    "count": "30S",
                }
            ],
            [],
        )

        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(result["rows"][0]["estimation_number"], 175164)
        self.assertEqual(
            result["rows"][0]["regular_order_number"],
            "LM111360",
        )
        for field_name in (
            "party_name",
            "reference_number",
            "required_quantity",
            "count_name",
            "certification",
            "net_rate",
        ):
            self.assertIsNone(result["rows"][0][field_name])

    def test_app_fetches_expected_values_before_parent_commit(self):
        source_text = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        source = ast.get_source_segment(source_text, function)

        self.assertIn("fetch_oracle_expected_rows", source)
        self.assertLess(
            source.index("fetch_oracle_expected_rows"),
            source.index('"po_parent_commit"'),
        )
        self.assertIn("oracle_expected_rows=oracle_expected_rows", source)


if __name__ == "__main__":
    unittest.main()
