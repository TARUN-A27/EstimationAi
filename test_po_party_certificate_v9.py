from __future__ import annotations

import unittest
from pathlib import Path

from po_content_validation import (
    build_extracted_po_document_detail_rows,
    match_certificate_master_names,
)
from test_po_extracted_details_v8 import content_result


PROJECT_DIR = Path(__file__).resolve().parent
MASTER_NAMES = [
    "B.C.T",
    "BCI",
    "BCT",
    "CMIA",
    "FAIR TRADE",
    "GOTS",
    "GRS",
    "OCS",
    "ORG.IC2",
    "ORGANIC COTTON",
    "ORGANIC NPOP",
    "ORGANIC OCS NPOP",
    "RCS",
    "RECYCLE COTTON",
    "RECYCLE POLYSTER",
    "REGEN AGRI",
    "REGENAGRI",
]


def mappings() -> list[dict]:
    return [
        {
            "estimation_no": 175164,
            "regular_order_number": "LM111360",
            "reference_order_numbers": ["112786B"],
        }
    ]


def result_with(*, party: str, certification: list[str]) -> dict:
    return content_result(
        party_names=[party],
        lines=[
            {
                "count": ["34S"],
                "required_quantity": ["170.000"],
                "reference_number": ["OPTION-112786B"],
                "confirm_rate": ["650.00"],
                "certification": certification,
            }
        ],
    )


class PartyCertificateV9Tests(unittest.TestCase):
    def test_exact_dotted_certificate_keeps_master_spelling(self) -> None:
        match = match_certificate_master_names(["B.C.T"], MASTER_NAMES)
        self.assertEqual(match["stored_value"], "B.C.T")

    def test_exact_plain_certificate_remains_distinct(self) -> None:
        match = match_certificate_master_names(["BCT"], MASTER_NAMES)
        self.assertEqual(match["stored_value"], "BCT")

    def test_longest_certificate_name_wins(self) -> None:
        match = match_certificate_master_names(
            ["Certificate: ORGANIC OCS NPOP certified"],
            MASTER_NAMES,
        )
        self.assertEqual(match["stored_value"], "ORGANIC OCS NPOP")

    def test_unknown_certificate_is_not_inserted(self) -> None:
        rows = build_extracted_po_document_detail_rows(
            result_with(
                party="ASM KNITWEARS PRIVATE LTD",
                certification=["UNKNOWN CERT"],
            ),
            mappings(),
            {"175164": "ASM KNITWEARS PRIVATE LTD"},
            MASTER_NAMES,
        )
        self.assertIsNone(rows[0]["certification"])
        self.assertEqual(
            rows[0]["certificate_match"]["matched_master_names"],
            [],
        )

    def test_party_match_is_required_but_extracted_value_is_stored(self) -> None:
        rows = build_extracted_po_document_detail_rows(
            result_with(
                party="ASM KNITWEARS PRIVATE LIMITED",
                certification=["GRS"],
            ),
            mappings(),
            {"175164": "ASM KNITWEARS PRIVATE LTD"},
            MASTER_NAMES,
        )
        self.assertEqual(
            rows[0]["party_name"],
            "ASM KNITWEARS PRIVATE LIMITED",
        )
        self.assertTrue(rows[0]["party_match"]["match"])
        self.assertEqual(rows[0]["certification"], "GRS")

    def test_existing_partial_ocr_party_rule_still_passes(self) -> None:
        rows = build_extracted_po_document_detail_rows(
            result_with(
                party="ITWEARS PRIVATE LIMITED",
                certification=[],
            ),
            mappings(),
            {"175164": "ASM KNITWEARS PRIVATE LTD"},
            MASTER_NAMES,
        )
        self.assertEqual(rows[0]["party_name"], "ITWEARS PRIVATE LIMITED")
        self.assertEqual(
            rows[0]["party_match"]["comparison_method"],
            "PARTIAL_OCR_PARTY_NAME",
        )

    def test_unrelated_party_blocks_complete_pdf(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_extracted_po_document_detail_rows(
                result_with(
                    party="UNRELATED COMPANY",
                    certification=["BCI"],
                ),
                mappings(),
                {"175164": "ASM KNITWEARS PRIVATE LTD"},
                MASTER_NAMES,
            )

    def test_production_no_longer_uses_legacy_matching_helpers(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("fetch_oracle_expected_rows", source)
        self.assertNotIn("validate_content_result", source)
        self.assertNotIn("fetch_expected_party_names", source)
        self.assertNotIn("fetch_certificate_master_names", source)


if __name__ == "__main__":
    unittest.main()
