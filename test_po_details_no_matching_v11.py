from __future__ import annotations

import ast
import unittest
from pathlib import Path

from po_content_validation import (
    build_extracted_po_document_detail_rows,
)
from test_po_extracted_details_v8 import content_result


PROJECT_DIR = Path(__file__).resolve().parent
APP_PATH = PROJECT_DIR / "app.py"


def mappings() -> list[dict]:
    return [
        {
            "estimation_no": 175164,
            "regular_order_number": "LM111360",
            "reference_order_numbers": ["112786B"],
        }
    ]


class PoDetailsNoMatchingV11Tests(unittest.TestCase):
    def test_unmatched_party_and_certificate_are_stored_as_extracted(
        self,
    ) -> None:
        result = content_result(
            party_names=["UNRELATED EXTRACTED PARTY"],
            lines=[
                {
                    "count": ["34S"],
                    "required_quantity": ["170.000"],
                    "reference_number": ["OPTION-112786B"],
                    "confirm_rate": ["650.00"],
                    "certification": ["CUSTOM CERTIFICATE TEXT"],
                }
            ],
        )

        rows = build_extracted_po_document_detail_rows(
            result,
            mappings(),
        )

        self.assertEqual(rows[0]["party_name"], "UNRELATED EXTRACTED PARTY")
        self.assertEqual(
            rows[0]["certification"],
            "CUSTOM CERTIFICATE TEXT",
        )
        self.assertNotIn("party_match", rows[0])
        self.assertNotIn("certificate_match", rows[0])

    def test_production_builder_receives_no_comparison_data(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        insert_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_regular_order_document"
        )
        calls = [
            node
            for node in ast.walk(insert_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_extracted_po_document_detail_result"
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].args), 2)
        self.assertEqual(calls[0].keywords, [])
        self.assertNotIn("fetch_expected_party_names", source)
        self.assertNotIn("fetch_certificate_master_names", source)

    def test_workflow_reports_all_value_matching_as_skipped(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('"oracle_value_comparison": "SKIPPED"', source)
        self.assertIn('"party_name_comparison": "SKIPPED"', source)
        self.assertIn('"certificate_master_match": "SKIPPED"', source)

    def test_detail_replacement_and_insert_sql_remain_unchanged(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "DELETE FROM REGULARORDER_PODOCUMENTDETAILS",
            source,
        )
        self.assertIn(
            "INSERT INTO REGULARORDER_PODOCUMENTDETAILS",
            source,
        )
        self.assertIn("plan_existing_po_replacement", source)

    def test_parent_v10_fileformat_change_remains_active(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("FILEFORMAT = '.pdf'", source)
        self.assertNotIn("FILEFORMAT = 'PDF'", source)


if __name__ == "__main__":
    unittest.main()
