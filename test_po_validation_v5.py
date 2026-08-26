from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from po_content_validation import (
    build_po_document_detail_rows,
    compare_oracle_row_to_line,
    compare_party_name,
    normalize_reference_number,
    validate_content_result,
)


PROJECT_DIR = Path(__file__).resolve().parent


def load_stamp_functions() -> dict:
    """Load the production stamp functions without importing Flask/Azure."""
    required_names = {
        "unique",
        "compact_ocr_text",
        "is_est_stamp_header",
        "is_po_stamp_header",
        "is_decimal_quantity_candidate",
        "has_same_row_stamp_order_value",
        "extract_po_stamp_numbers_from_lines",
    }
    tree = ast.parse(
        (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
    )
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in required_names
    ]
    namespace = {
        "re": re,
        "DocumentValidationError": ValueError,
    }
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]),
            str(PROJECT_DIR / "app.py"),
            "exec",
        ),
        namespace,
    )
    return namespace


def line(
    text: str,
    center_x: float,
    center_y: float,
    *,
    width: float = 180,
    height: float = 40,
) -> dict:
    return {
        "page": 1,
        "text": text,
        "left": center_x - width / 2,
        "right": center_x + width / 2,
        "top": center_y - height / 2,
        "bottom": center_y + height / 2,
        "center_x": center_x,
        "center_y": center_y,
        "page_width": 3000,
        "page_height": 4000,
    }


class StampExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.functions = load_stamp_functions()

    def test_total_quantity_is_not_an_estimation(self) -> None:
        lines = [
            line("EST NO", 1100, 2500),
            line("LM NO", 1750, 2500),
            line("175164", 1100, 2660),
            line("LM111360", 1750, 2660),
            # This printed total lies below the stamp and inside the
            # historical vertical search range.
            line("170.000", 1100, 3180),
        ]
        extracted = self.functions[
            "extract_po_stamp_numbers_from_lines"
        ](lines)
        self.assertEqual(extracted, [175164])

    def test_punctuated_est_is_allowed_when_lm_is_on_same_row(self) -> None:
        lines = [
            line("EST NO", 1100, 2500),
            line("LM NO", 1750, 2500),
            line("175.164", 1100, 2660),
            line("LM111360", 1750, 2660),
        ]
        extracted = self.functions[
            "extract_po_stamp_numbers_from_lines"
        ](lines)
        self.assertEqual(extracted, [175164])


class ContentValidationTests(unittest.TestCase):
    class FakeCursor:
        description = [
            ("ESTIMATION_NUMBER",),
            ("REFERENCE_NUMBER",),
            ("REQUIRED_QUANTITY",),
            ("COUNT_NAME",),
            ("PARTY_NAME",),
            ("BOOKING_RATE",),
            ("CERTIFICATION",),
        ]

        def execute(self, _query: str, **_binds) -> None:
            return None

        def fetchall(self) -> list[tuple]:
            return [
                (
                    175164,
                    "112786B",
                    170.0,
                    "34S",
                    "ASM KNITWEARS PRIVATE LTD",
                    650.0,
                    None,
                )
            ]

    def test_option_label_is_removed_from_reference(self) -> None:
        self.assertEqual(
            normalize_reference_number("OPTION-112786B"),
            "112786B",
        )

    def test_incomplete_party_is_only_a_conditional_partial_match(self) -> None:
        result = compare_party_name(
            "ASM KNITWEARS PRIVATE LTD",
            ["ITWEARS PRIVATE LIMITED"],
        )
        self.assertFalse(result["match"])
        self.assertTrue(result["partial_match_available"])

    def test_unrelated_party_is_not_a_partial_match(self) -> None:
        result = compare_party_name(
            "ASM KNITWEARS PRIVATE LTD",
            ["SRG APPARELS LIMITED"],
        )
        self.assertFalse(result["match"])
        self.assertFalse(result["partial_match_available"])

    def test_lm111360_line_values_match_oracle(self) -> None:
        result = compare_oracle_row_to_line(
            {
                "reference_number": "112786B",
                "required_quantity": "170.0",
                "count_name": "34S",
                "booking_rate": "650.0",
                "certification": None,
            },
            {
                "reference_number": ["OPTION-112786B"],
                "required_quantity": ["170.000"],
                "count": ["34S"],
                "confirm_rate": ["650.00"],
                "certification": [],
            },
            [],
        )
        self.assertEqual(result["mismatched_fields"], [])

    def test_lm111360_complete_validation_passes(self) -> None:
        content_result = {
            "contents": [
                {
                    "fields": {
                        "OrderLines": {
                            "valueArray": [
                                {
                                    "valueObject": {
                                        "SerialNumber": {
                                            "valueInteger": 1
                                        },
                                        "Count": {
                                            "valueArray": [
                                                {"valueString": "34S"}
                                            ]
                                        },
                                        "RequiredQuantity": {
                                            "valueArray": [
                                                {
                                                    "valueString": (
                                                        "170.000"
                                                    )
                                                }
                                            ]
                                        },
                                        "ReferenceNumber": {
                                            "valueArray": [
                                                {
                                                    "valueString": (
                                                        "OPTION-112786B"
                                                    )
                                                }
                                            ]
                                        },
                                        "ConfirmRate": {
                                            "valueArray": [
                                                {
                                                    "valueString": (
                                                        "650.00"
                                                    )
                                                }
                                            ]
                                        },
                                        (
                                            "CertificateRateIncluded"
                                            "HeatSettings"
                                        ): {
                                            "valueArray": []
                                        },
                                    }
                                }
                            ]
                        },
                        "PartyName": {
                            "valueArray": [
                                {
                                    "valueString": (
                                        "ITWEARS PRIVATE LIMITED"
                                    )
                                }
                            ]
                        },
                        "TaxDetails": {"valueArray": []},
                    }
                }
            ]
        }
        validation = validate_content_result(
            content_result,
            self.FakeCursor(),
            [175164],
        )
        self.assertEqual(validation["final_status"], "PASS")
        comparison = validation["comparisons"][0]
        self.assertEqual(comparison["status"], "MATCH")
        self.assertEqual(
            comparison["field_results"]["party_name"][
                "comparison_method"
            ],
            "PARTIAL_OCR_WITH_FULL_ORDER_LINE_MATCH",
        )
        detail_rows = build_po_document_detail_rows(
            validation,
            [
                {
                    "estimation_no": 175164,
                    "regular_order_number": "LM111360",
                }
            ],
        )
        self.assertEqual(
            detail_rows[0]["party_name"],
            "ASM KNITWEARS PRIVATE LTD",
        )


if __name__ == "__main__":
    unittest.main()
