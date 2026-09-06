from __future__ import annotations

import unittest

from po_content_validation import (
    build_extracted_po_document_detail_result,
    CONTENT_UNDERSTANDING_SOURCE,
)


class OrderedMappingFallbackV30Tests(unittest.TestCase):
    def build(self, *, semantic_mappings, lines, resolved):
        normalized_lines = []
        for line in lines:
            normalized_line = {}
            for key, value in line.items():
                normalized_line[key] = (
                    value if isinstance(value, list) else [value]
                )
            normalized_lines.append(normalized_line)
        normalized = {
            "analysis_source": CONTENT_UNDERSTANDING_SOURCE,
            "order_lines": normalized_lines,
            "party_names": [],
            "estimation_mappings": [
                {
                    "estimate_number": estimation,
                    "reference_numbers": [reference],
                }
                for estimation, reference in semantic_mappings
            ],
        }
        return build_extracted_po_document_detail_result(
            normalized,
            [
                {
                    "estimation_no": estimation,
                    "regular_order_number": order,
                }
                for estimation, order in resolved
            ],
        )

    def test_complete_ordered_mapping_associates_each_line_by_position(self):
        details = self.build(
            semantic_mappings=[
                (174702, "LMILI177"),
                (174704, "LM111178"),
            ],
            lines=[
                {"count": "30S", "reference_number": "114745A"},
                {"count": "44S", "reference_number": "114745A"},
            ],
            resolved=[
                (174702, "ORDER-177"),
                (174704, "ORDER-178"),
            ],
        )

        self.assertEqual(
            [row["estimation_number"] for row in details["rows"]],
            [174702, 174704],
        )
        self.assertEqual(
            [row["reference_number"] for row in details["rows"]],
            ["114745A", "114745A"],
        )
        self.assertEqual(details["rejected_lines"], [])
        self.assertEqual(
            details["association_counts"][
                "ordered_mapping_fallback_count"
            ],
            2,
        )

    def test_line_and_mapping_count_mismatch_disables_fallback(self):
        details = self.build(
            semantic_mappings=[
                (174702, "LM111177"),
                (174704, "LM111178"),
            ],
            lines=[{}, {}, {}],
            resolved=[
                (174702, "ORDER-177"),
                (174704, "ORDER-178"),
            ],
        )

        self.assertEqual(details["rows"], [])
        self.assertEqual(len(details["rejected_lines"]), 3)
        self.assertEqual(
            details["association_counts"][
                "ordered_mapping_fallback_count"
            ],
            0,
        )

    def test_duplicate_mapping_est_disables_fallback(self):
        details = self.build(
            semantic_mappings=[
                (174702, "LM111177"),
                (174702, "LM111178"),
            ],
            lines=[{}, {}],
            resolved=[
                (174702, "ORDER-177"),
                (174704, "ORDER-178"),
            ],
        )

        self.assertEqual(details["rows"], [])
        self.assertEqual(len(details["rejected_lines"]), 2)

    def test_explicit_unresolved_mapping_reference_blocks_fallback(self):
        details = self.build(
            semantic_mappings=[
                (174702, "LM111177"),
                (174704, "LM111178"),
            ],
            lines=[
                {"mapping_reference_number": "UNKNOWN"},
                {},
            ],
            resolved=[
                (174702, "ORDER-177"),
                (174704, "ORDER-178"),
            ],
        )

        self.assertEqual(details["rows"], [])
        self.assertEqual(len(details["rejected_lines"]), 2)
        self.assertEqual(
            details["association_counts"][
                "ordered_mapping_fallback_count"
            ],
            0,
        )

    def test_direct_estimation_still_has_priority(self):
        details = self.build(
            semantic_mappings=[
                (174702, "LM111177"),
                (174704, "LM111178"),
            ],
            lines=[
                {"estimate_number": "174704"},
                {"estimate_number": "174702"},
            ],
            resolved=[
                (174702, "ORDER-177"),
                (174704, "ORDER-178"),
            ],
        )

        self.assertEqual(
            [row["estimation_number"] for row in details["rows"]],
            [174704, 174702],
        )
        self.assertEqual(
            details["association_counts"]["direct_line_mapping_count"],
            2,
        )
        self.assertEqual(
            details["association_counts"][
                "ordered_mapping_fallback_count"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
