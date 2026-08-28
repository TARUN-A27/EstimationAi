from __future__ import annotations

import unittest
from pathlib import Path

from po_content_validation import build_po_document_detail_rows


PROJECT_DIR = Path(__file__).resolve().parent


class PoDetailV6Tests(unittest.TestCase):
    def test_detail_row_contains_reference_and_quantity(self) -> None:
        validation = {
            "comparisons": [
                {
                    "status": "MATCH",
                    "estimation_number": "175164",
                    "oracle_expected": {
                        "party_name": "ASM KNITWEARS PRIVATE LTD",
                        "reference_number": "112786B",
                        "required_quantity": "170.0",
                        "count_name": "34S",
                        "certification": None,
                    },
                    "field_results": {
                        "confirm_rate": {"matched_pdf_rate": "650.00"},
                        "party_name": {},
                    },
                }
            ]
        }
        rows = build_po_document_detail_rows(
            validation,
            [
                {
                    "estimation_no": 175164,
                    "regular_order_number": "LM111360",
                }
            ],
        )
        self.assertEqual(rows[0]["reference_number"], "112786B")
        self.assertEqual(rows[0]["required_quantity"], "170.0")

    def test_insert_statement_contains_new_columns_and_binds(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn("REFERENCENO", source)
        self.assertIn("REQUIREDQUANTITY", source)
        self.assertIn(":reference_number", source)
        self.assertIn(":required_quantity", source)
        self.assertIn(
            'reference_number=detail["reference_number"]',
            source,
        )
        self.assertIn('detail["required_quantity"]', source)


if __name__ == "__main__":
    unittest.main()
