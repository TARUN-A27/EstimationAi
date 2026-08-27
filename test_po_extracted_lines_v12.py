from __future__ import annotations

import unittest

from po_content_validation import (
    build_extracted_po_document_detail_rows,
    parse_order_lines,
)
from test_po_extracted_details_v8 import content_result


class ExtractedPoLinesV12Tests(unittest.TestCase):
    def test_parse_order_lines_reads_estimate_number(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["Est. No. 175478"],
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["ANY REFERENCE"],
                    "confirm_rate": ["500"],
                }
            ],
        )

        lines = parse_order_lines(result)

        self.assertEqual(lines[0]["estimate_number"], ["Est. No. 175478"])

    def test_multiple_lines_for_same_est_are_all_stored(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["175478"],
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["REF-ONE"],
                    "confirm_rate": ["500"],
                },
                {
                    "estimate_number": ["175478"],
                    "count": ["34S"],
                    "required_quantity": ["200"],
                    "reference_number": ["REF-TWO"],
                    "confirm_rate": ["600"],
                },
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175478,
                    "regular_order_number": "LM111478",
                    "reference_order_numbers": ["DO-NOT-MATCH"],
                }
            ],
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["estimation_number"] for row in rows],
            [175478, 175478],
        )
        self.assertEqual(
            [row["reference_number"] for row in rows],
            ["REF-ONE", "REF-TWO"],
        )

    def test_multi_est_linkage_does_not_compare_reference_values(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["175502"],
                    "count": ["44S"],
                    "required_quantity": ["700"],
                    "reference_number": ["NOT-A-MAPPING"],
                    "confirm_rate": ["557.14"],
                },
                {
                    "estimate_number": ["175501"],
                    "count": ["30S"],
                    "required_quantity": ["126"],
                    "reference_number": ["ALSO-NOT-A-MAPPING"],
                    "confirm_rate": ["619.05"],
                },
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175501,
                    "regular_order_number": "LM111501",
                    "reference_order_numbers": ["REF-A"],
                },
                {
                    "estimation_no": 175502,
                    "regular_order_number": "LM111502",
                    "reference_order_numbers": ["REF-B"],
                },
            ],
        )

        self.assertEqual(
            [row["estimation_number"] for row in rows],
            [175502, 175501],
        )
        self.assertEqual(
            [row["regular_order_number"] for row in rows],
            ["LM111502", "LM111501"],
        )

    def test_single_est_does_not_require_extracted_estimate_number(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["ANY REFERENCE"],
                    "confirm_rate": ["500"],
                }
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175503,
                    "regular_order_number": "LM111503",
                    "reference_order_numbers": [],
                }
            ],
        )

        self.assertEqual(rows[0]["estimation_number"], 175503)
        self.assertEqual(rows[0]["reference_number"], "ANY REFERENCE")

    def test_extracted_value_equal_to_est_is_not_filtered(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["175504"],
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["175504"],
                    "confirm_rate": ["500"],
                }
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175504,
                    "regular_order_number": "LM111504",
                    "reference_order_numbers": [],
                }
            ],
        )

        self.assertEqual(rows[0]["reference_number"], "175504")

    def test_missing_line_for_another_est_does_not_block_extracted_row(
        self,
    ) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["175505"],
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["EXTRACTED-ONLY"],
                    "confirm_rate": ["500"],
                }
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175505,
                    "regular_order_number": "LM111505",
                    "reference_order_numbers": [],
                },
                {
                    "estimation_no": 175506,
                    "regular_order_number": "LM111506",
                    "reference_order_numbers": [],
                },
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["estimation_number"], 175505)


if __name__ == "__main__":
    unittest.main()
