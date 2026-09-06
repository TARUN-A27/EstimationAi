from __future__ import annotations

import ast
import unittest
from pathlib import Path

from po_content_validation import (
    CERTIFICATE_MASTER_QUERY,
    CONTENT_UNDERSTANDING_SOURCE,
    build_extracted_po_document_detail_result,
    fetch_certificate_master_entries,
    match_certificate_master_names,
)


PROJECT_DIR = Path(__file__).resolve().parent
MAPPINGS = [
    {
        "estimation_no": 174702,
        "regular_order_number": "ORDER-177",
    }
]


def build(lines, master):
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
            "party_names": [],
            "estimation_mappings": [],
        },
        MAPPINGS,
        certificate_master_entries=master,
    )


class MasterCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def execute(self, query):
        self.query = query

    def fetchall(self):
        return self.rows


class CertificationMasterV31Tests(unittest.TestCase):
    def test_query_loads_code_and_name_on_supplied_cursor(self):
        cursor = MasterCursor([(91, "B.C.T")])
        entries = fetch_certificate_master_entries(cursor)

        self.assertEqual(entries, [(91, "B.C.T")])
        self.assertEqual(cursor.query, CERTIFICATE_MASTER_QUERY)
        self.assertIn("SELECT CODE, NAME", CERTIFICATE_MASTER_QUERY)
        self.assertIn("FROM PO_CERTTYPEMASTER", CERTIFICATE_MASTER_QUERY)

    def test_exact_name_match_stores_canonical_name_not_code(self):
        result = build(
            [{"certification": "B.C.T"}],
            [(9173, "B.C.T")],
        )

        self.assertEqual(result["rows"][0]["certification"], "B.C.T")
        self.assertNotEqual(result["rows"][0]["certification"], 9173)
        self.assertNotIn("9173", repr(result))

    def test_case_punctuation_and_spacing_are_normalized(self):
        for extracted in ("b.c.t", "BCT", "B.C.T.", "B C T"):
            with self.subTest(extracted=extracted):
                match = match_certificate_master_names(
                    [extracted],
                    [(1, "B.C.T")],
                )
                self.assertEqual(match["status"], "matched")
                self.assertEqual(match["stored_value"], "B.C.T")

    def test_master_name_is_a_keyword_in_longer_text(self):
        result = build(
            [{"certification": "Certified under the g.r.s. standard"}],
            [(4, "GRS")],
        )

        self.assertEqual(result["rows"][0]["certification"], "GRS")

    def assert_missing_certification(self, line):
        result = build([line], [(1, "B.C.T")])

        self.assertEqual(result["rejected_lines"], [])
        self.assertIsNone(result["rows"][0]["certification"])
        self.assertEqual(
            result["certification_counts"],
            {
                "certification_present_count": 0,
                "certification_master_match_count": 0,
                "certification_missing_count": 1,
                "certification_unmatched_count": 0,
                "certification_ambiguous_count": 0,
            },
        )

    def test_absent_certification_remains_null(self):
        self.assert_missing_certification({})

    def test_none_certification_remains_null(self):
        self.assert_missing_certification({"certification": None})

    def test_empty_list_certification_remains_null(self):
        result = build([{"certification": []}], [(1, "B.C.T")])

        self.assertIsNone(result["rows"][0]["certification"])
        self.assertEqual(
            result["certification_counts"]["certification_missing_count"],
            1,
        )

    def test_empty_string_certification_remains_null(self):
        self.assert_missing_certification({"certification": ""})

    def test_whitespace_certification_remains_null(self):
        self.assert_missing_certification({"certification": "   "})

    def test_supported_placeholders_remain_null(self):
        for placeholder in (
            "N/A",
            "n/a",
            "NA",
            "na",
            "NONE",
            "None",
            "NULL",
            "null",
            "NOT APPLICABLE",
            "not applicable",
        ):
            with self.subTest(placeholder=placeholder):
                self.assert_missing_certification(
                    {"certification": placeholder}
                )

    def test_unmatched_certification_rejects_only_that_line(self):
        result = build(
            [
                {"reference_number": "BAD", "certification": "UNKNOWN"},
                {"reference_number": "GOOD", "certification": "GRS"},
            ],
            [(4, "GRS")],
        )

        self.assertEqual(len(result["rejected_lines"]), 1)
        self.assertEqual(result["rejected_lines"][0]["source_line_number"], 1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["reference_number"], "GOOD")
        self.assertEqual(
            result["certification_counts"][
                "certification_unmatched_count"
            ],
            1,
        )

    def test_different_canonical_names_with_same_keyword_are_ambiguous(self):
        result = build(
            [
                {"reference_number": "REVIEW", "certification": "B C T"},
                {"reference_number": "GOOD", "certification": "GRS"},
            ],
            [(1, "B.C.T"), (2, "BCT"), (3, "GRS")],
        )

        self.assertEqual(len(result["rejected_lines"]), 1)
        self.assertEqual(result["rows"][0]["reference_number"], "GOOD")
        self.assertEqual(
            result["certification_counts"][
                "certification_ambiguous_count"
            ],
            1,
        )

    def test_unique_longest_specific_keyword_wins(self):
        result = build(
            [{"certification": "ORGANIC OCS NPOP certified"}],
            [(1, "OCS"), (2, "ORGANIC OCS NPOP")],
        )

        self.assertEqual(
            result["rows"][0]["certification"],
            "ORGANIC OCS NPOP",
        )

    def test_multiple_equivalent_values_store_one_canonical_name(self):
        extracted = ["BCT", "B.C.T"]
        result = build(
            [{"certification": extracted}],
            [(1, "B.C.T")],
        )

        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(result["rows"][0]["certification"], "B.C.T")
        self.assertEqual(extracted, ["BCT", "B.C.T"])

    def test_matched_plus_unmatched_value_rejects_the_line(self):
        result = build(
            [{"certification": ["GRS", "UNKNOWN"]}],
            [(1, "GRS")],
        )

        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["rejected_lines"]), 1)
        self.assertIn("no master match", result["rejected_lines"][0]["reason"])
        self.assertEqual(
            result["certification_counts"][
                "certification_unmatched_count"
            ],
            1,
        )

    def test_multiple_different_valid_certifications_are_ambiguous(self):
        result = build(
            [{"certification": ["GRS", "ORGANIC OCS NPOP"]}],
            [(1, "GRS"), (2, "OCS"), (3, "ORGANIC OCS NPOP")],
        )

        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["rejected_lines"]), 1)
        self.assertIn("ambiguous", result["rejected_lines"][0]["reason"])
        self.assertEqual(
            result["certification_counts"][
                "certification_ambiguous_count"
            ],
            1,
        )

    def test_duplicate_identical_master_rows_are_not_ambiguous(self):
        result = build(
            [{"certification": "Certificate B C T"}],
            [(1, "B.C.T"), (2, "B.C.T")],
        )

        self.assertEqual(result["rejected_lines"], [])
        self.assertEqual(result["rows"][0]["certification"], "B.C.T")

    def test_equal_length_distinct_matches_are_ambiguous(self):
        match = match_certificate_master_names(
            ["ABC DEF ABC"],
            [(1, "ABC DEF"), (2, "DEF ABC")],
        )

        self.assertEqual(match["status"], "ambiguous")
        self.assertIsNone(match["stored_value"])

    def test_unmatched_and_ambiguous_lines_keep_other_valid_lines(self):
        result = build(
            [
                {"reference_number": "UNMATCHED", "certification": "OTHER"},
                {"reference_number": "VALID", "certification": "GRS"},
                {
                    "reference_number": "AMBIGUOUS",
                    "certification": ["GRS", "ORGANIC OCS NPOP"],
                },
            ],
            [(1, "GRS"), (2, "OCS"), (3, "ORGANIC OCS NPOP")],
        )

        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["reference_number"], "VALID")
        self.assertEqual(len(result["rejected_lines"]), 2)
        self.assertEqual(
            result["certification_counts"][
                "certification_unmatched_count"
            ],
            1,
        )
        self.assertEqual(
            result["certification_counts"][
                "certification_ambiguous_count"
            ],
            1,
        )

    def test_other_business_fields_are_not_canonicalized(self):
        result = build(
            [
                {
                    "party_name": "Raw Party, Ltd.",
                    "reference_number": "Ref / 19-A",
                    "required_quantity": "170.000",
                    "count": "34 s",
                    "confirm_rate": "650.00",
                    "certification": "grs",
                }
            ],
            [(4, "GRS")],
        )
        row = result["rows"][0]

        self.assertEqual(row["party_name"], "Raw Party, Ltd.")
        self.assertEqual(row["reference_number"], "Ref / 19-A")
        self.assertEqual(row["required_quantity"], "170.000")
        self.assertEqual(row["count_name"], "34 s")
        self.assertEqual(row["net_rate"], "650.00")

    def test_audit_result_contains_counts_not_certificate_values(self):
        result = build(
            [
                {"certification": []},
                {"certification": "GRS"},
                {"certification": "UNKNOWN"},
                {"certification": "BCT"},
            ],
            [(1, "GRS"), (2, "B.C.T"), (3, "BCT")],
        )

        self.assertEqual(
            result["certification_counts"],
            {
                "certification_present_count": 3,
                "certification_master_match_count": 1,
                "certification_missing_count": 1,
                "certification_unmatched_count": 1,
                "certification_ambiguous_count": 1,
            },
        )
        match = match_certificate_master_names(
            ["PRIVATE EXTRACTED VALUE"],
            [(99, "PRIVATE MASTER NAME")],
        )
        self.assertEqual(set(match), {"status", "stored_value"})

    def test_v30_ordered_mapping_and_transaction_boundaries_remain(self):
        source_text = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        source = ast.get_source_segment(source_text, function)

        self.assertIn("ordered_mapping_fallback_count", source)
        self.assertLess(
            source.index("fetch_certificate_master_entries(cursor)"),
            source.index('"po_parent_commit"'),
        )
        self.assertLess(
            source.index('"po_parent_commit"'),
            source.index('"po_detail_transaction"'),
        )
        self.assertIn('"po_detail_rollback"', source)
        self.assertIn("connection.rollback()", source)


if __name__ == "__main__":
    unittest.main()
