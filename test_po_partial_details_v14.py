from __future__ import annotations

import unittest
from pathlib import Path

from po_content_validation import (
    build_extracted_po_document_detail_result,
)
from test_po_extracted_details_v8 import content_result


PROJECT_DIR = Path(__file__).resolve().parent


class PoPartialDetailsV14Tests(unittest.TestCase):
    def test_missing_business_fields_are_preserved_as_null(self) -> None:
        result = content_result(
            party_names=[],
            lines=[
                {
                    "estimate_number": ["175478"],
                    "count": [],
                    "required_quantity": [],
                    "reference_number": [],
                    "confirm_rate": [],
                    "certification": [],
                }
            ],
        )

        detail_result = build_extracted_po_document_detail_result(
            result,
            [
                {
                    "estimation_no": 175478,
                    "regular_order_number": "LM111478",
                }
            ],
        )

        self.assertEqual(detail_result["rejected_lines"], [])
        self.assertEqual(
            detail_result["rows"],
            [
                {
                    "estimation_number": 175478,
                    "regular_order_number": "LM111478",
                    "party_name": None,
                    "reference_number": None,
                    "required_quantity": None,
                    "count_name": None,
                    "certification": None,
                    "net_rate": None,
                }
            ],
        )

    def test_unassociated_line_does_not_discard_associated_line(self) -> None:
        result = content_result(
            party_names=["EXTRACTED PARTY"],
            lines=[
                {
                    "estimate_number": ["175501"],
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["REF-A"],
                    "confirm_rate": ["500"],
                },
                {
                    "estimate_number": [],
                    "count": ["34S"],
                    "required_quantity": ["200"],
                    "reference_number": ["REF-B"],
                    "confirm_rate": ["600"],
                },
            ],
        )

        detail_result = build_extracted_po_document_detail_result(
            result,
            [
                {
                    "estimation_no": 175501,
                    "regular_order_number": "LM111501",
                },
                {
                    "estimation_no": 175502,
                    "regular_order_number": "LM111502",
                },
            ],
        )

        self.assertEqual(len(detail_result["rows"]), 1)
        self.assertEqual(
            detail_result["rows"][0]["estimation_number"],
            175501,
        )
        self.assertEqual(len(detail_result["rejected_lines"]), 1)
        self.assertEqual(
            detail_result["rejected_lines"][0]["source_line_number"],
            2,
        )

    def test_ambiguous_numeric_line_does_not_discard_other_line(self) -> None:
        result = content_result(
            party_names=["EXTRACTED PARTY"],
            lines=[
                {
                    "count": ["30S"],
                    "required_quantity": ["100", "200"],
                    "reference_number": ["REF-A"],
                    "confirm_rate": ["500"],
                },
                {
                    "count": ["34S"],
                    "required_quantity": ["300"],
                    "reference_number": ["REF-B"],
                    "confirm_rate": ["600"],
                },
            ],
        )

        detail_result = build_extracted_po_document_detail_result(
            result,
            [
                {
                    "estimation_no": 175501,
                    "regular_order_number": "LM111501",
                }
            ],
        )

        self.assertEqual(len(detail_result["rows"]), 1)
        self.assertEqual(
            detail_result["rows"][0]["reference_number"],
            "REF-B",
        )
        self.assertEqual(len(detail_result["rejected_lines"]), 1)

    def test_schema_migration_matches_approved_changes(self) -> None:
        source = (PROJECT_DIR / "migrate_po_details_v14.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('UNIQUE_CONSTRAINT = "UQ_RO_PO_DOCDET_EST"', source)
        self.assertIn(
            'EXPECTED_UNIQUE_COLUMNS = ("PODOCUMENTDOCID", "ESTIMATIONNO")',
            source,
        )
        for column in (
            "PARTYNAME",
            "REFERENCENO",
            "REQUIREDQUANTITY",
            "COUNTNAME",
            "NETRATE",
        ):
            self.assertIn(f'"{column}"', source)
        self.assertIn("DROP CONSTRAINT", source)
        self.assertIn("MODIFY ({name} NULL)", source)
        self.assertIn("BATCH_DATABASE_INSERT_ENABLED", source)

    def test_backend_and_ui_report_partial_details(self) -> None:
        app_source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        template_source = (
            PROJECT_DIR / "templates" / "estimation.html"
        ).read_text(encoding="utf-8")

        self.assertIn('"po_detail_rejected_lines"', app_source)
        self.assertIn('else "PARTIAL"', app_source)
        self.assertIn(
            "inserted.po_detail_status === 'PARTIAL'",
            template_source,
        )
        self.assertIn("Partially Inserted", template_source)
        self.assertIn(
            "inserted.po_detail_status === 'REVIEW_REQUIRED'",
            template_source,
        )

    def test_ambiguous_filename_result_is_rejected(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            'if document_type in {"unknown", "ambiguous"}:',
            source,
        )


if __name__ == "__main__":
    unittest.main()
