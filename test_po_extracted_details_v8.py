from __future__ import annotations

import unittest
from pathlib import Path

from po_content_validation import (
    build_extracted_po_document_detail_rows,
)


PROJECT_DIR = Path(__file__).resolve().parent


def content_result(
    *,
    party_names: list[str],
    lines: list[dict[str, list[str]]],
) -> dict:
    def array_field(values: list[str]) -> dict:
        return {
            "type": "array",
            "valueArray": [
                {"type": "string", "valueString": value}
                for value in values
            ],
        }

    order_lines = []
    for index, line in enumerate(lines, start=1):
        order_lines.append(
            {
                "type": "object",
                "valueObject": {
                    "SerialNumber": {
                        "type": "integer",
                        "valueInteger": index,
                    },
                    "EstimateNumber": array_field(
                        line.get("estimate_number", [])
                    ),
                    "Count": array_field(line.get("count", [])),
                    "RequiredQuantity": array_field(
                        line.get("required_quantity", [])
                    ),
                    "ReferenceNumber": array_field(
                        line.get("reference_number", [])
                    ),
                    "ConfirmRate": array_field(
                        line.get("confirm_rate", [])
                    ),
                    "CertificateRateIncludedHeatSettings": array_field(
                        line.get("certification", [])
                    ),
                },
            }
        )

    return {
        "contents": [
            {
                "fields": {
                    "OrderLines": {
                        "type": "array",
                        "valueArray": order_lines,
                    },
                    "PartyName": array_field(party_names),
                }
            }
        ]
    }


class ExtractedPoDetailsV8Tests(unittest.TestCase):
    def test_detail_values_come_from_content_understanding(self) -> None:
        result = content_result(
            party_names=["PDF PARTY NAME"],
            lines=[
                {
                    "count": ["34S"],
                    "required_quantity": ["170.000"],
                    "reference_number": ["OPTION-112786B"],
                    "confirm_rate": ["650.00"],
                    "certification": ["BCT"],
                }
            ],
        )
        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175164,
                    "regular_order_number": "LM111360",
                    "reference_order_numbers": ["112786B"],
                }
            ],
        )
        self.assertEqual(
            rows,
            [
                {
                    "estimation_number": 175164,
                    "regular_order_number": "LM111360",
                    "party_name": "PDF PARTY NAME",
                    "reference_number": "OPTION-112786B",
                    "required_quantity": "170.000",
                    "count_name": "34S",
                    "certification": "BCT",
                    "net_rate": "650.00",
                }
            ],
        )

    def test_multiple_party_names_are_preserved(self) -> None:
        result = content_result(
            party_names=["PARTY ONE", "PARTY TWO"],
            lines=[
                {
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["REF1"],
                    "confirm_rate": ["500"],
                }
            ],
        )
        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 175001,
                    "regular_order_number": "LM111001",
                    "reference_order_numbers": ["REF1"],
                }
            ],
        )
        self.assertEqual(rows[0]["party_name"], "PARTY ONE, PARTY TWO")

    def test_multiple_estimations_use_extracted_estimate_number(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "estimate_number": ["174704"],
                    "count": ["44S"],
                    "required_quantity": ["700"],
                    "reference_number": ["UNRELATED-B"],
                    "confirm_rate": ["557.14"],
                },
                {
                    "estimate_number": ["Est. No. 174702"],
                    "count": ["30S"],
                    "required_quantity": ["126"],
                    "reference_number": ["UNRELATED-A"],
                    "confirm_rate": ["619.05"],
                },
            ],
        )
        rows = build_extracted_po_document_detail_rows(
            result,
            [
                {
                    "estimation_no": 174702,
                    "regular_order_number": "LM111177",
                    "reference_order_numbers": ["REF-A"],
                },
                {
                    "estimation_no": 174704,
                    "regular_order_number": "LM111178",
                    "reference_order_numbers": ["REF-B"],
                },
            ],
        )
        self.assertEqual(
            [row["estimation_number"] for row in rows],
            [174704, 174702],
        )

    def test_ambiguous_multi_estimation_document_is_blocked(self) -> None:
        result = content_result(
            party_names=["PDF PARTY"],
            lines=[
                {
                    "count": ["30S"],
                    "required_quantity": ["100"],
                    "reference_number": ["UNKNOWN"],
                    "confirm_rate": ["500"],
                }
            ],
        )
        with self.assertRaisesRegex(
            ValueError,
            "using its extracted EstimateNumber",
        ):
            build_extracted_po_document_detail_rows(
                result,
                [
                    {
                        "estimation_no": 1,
                        "regular_order_number": "LM1",
                        "reference_order_numbers": ["REF1"],
                    },
                    {
                        "estimation_no": 2,
                        "regular_order_number": "LM2",
                        "reference_order_numbers": ["REF2"],
                    },
                ],
            )

    def test_production_insert_omits_removed_columns_and_comparison(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        detail_insert = source.split(
            "INSERT INTO REGULARORDER_PODOCUMENTDETAILS (", 1
        )[1].split(") VALUES (", 1)[0]
        self.assertNotIn("FILENAME", detail_insert)
        self.assertNotIn("DOCUMENTTYPE", detail_insert)
        self.assertNotIn("validate_content_result", source)
        self.assertIn("build_extracted_po_document_detail_rows", source)
        self.assertIn('"oracle_value_comparison": "SKIPPED"', source)

    def test_v8_migration_drops_only_obsolete_columns(self) -> None:
        source = (PROJECT_DIR / "migrate_po_details_v8.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('REMOVED_COLUMNS = ("FILENAME", "DOCUMENTTYPE")', source)
        self.assertIn("DROP COLUMN", source)
        self.assertNotIn("DROP TABLE", source)


if __name__ == "__main__":
    unittest.main()
