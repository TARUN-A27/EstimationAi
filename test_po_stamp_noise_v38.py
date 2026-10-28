"""Corrections written on a stamp, and handwritten order numbers that are misread.

Staff correct a stamp by hand: an entry is crossed out and rewritten. The scribble
is read as stray text ("OH2", "09977331") in the EST cell with nothing beside it, and
that one row used to discard every mapping in the document. Order numbers written by
hand are also misread (the letter O for 0, M for H), which refused a pending-row
upload although its EST was right.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as production_app
import po_content_validation as P


def field(value):
    return {"type": "string", "valueString": str(value)}


def answer(document_type, estimations=(), markdown=""):
    fields = {"DocumentType": field(document_type)}
    if estimations:
        fields["EstimationNumbers"] = {
            "type": "array",
            "valueArray": [field(value) for value in estimations],
        }
    return {"contents": [{"kind": "document", "fields": fields, "markdown": markdown}]}


def stamp(*rows):
    return (
        "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
        + "".join(f"<tr><td>{est}</td><td>{lm}</td></tr>" for est, lm in rows)
        + "</table>"
    )


def pairs(result):
    return [(m["estimate_number"], m["reference_number"]) for m in result["estimation_mappings"]]


class CrossedOutStampRowV38Tests(unittest.TestCase):
    def test_a_crossed_out_row_is_skipped_and_the_clean_rows_kept(self):
        for scribble in ("OH2", "09977331", "x"):
            with self.subTest(scribble=scribble):
                scan = P.parse_markdown_estimation_mappings(
                    answer("Purchase Order", markdown=stamp((scribble, ""), ("180101", "EM900101")))
                )
                self.assertEqual(
                    [(m["estimate_number"], m["reference_number"]) for m in scan["mappings"]],
                    [(180101, "EM900101")],
                )
                self.assertEqual(scan["mapping_conflict_count"], 0)
                self.assertEqual(scan["struck_rows"], 1)

    def test_one_crossed_out_row_no_longer_discards_the_other_stamp_boxes(self):
        boxes = (
            stamp(("180101", "EM900101"), ("180102", "EM900102"))
            + stamp(("09977331", ""), ("180103", "EM900103"))
            + stamp(("180104", "EM900104"))
        )
        result = P.normalize_content_understanding_result(
            answer("Purchase Order", [180101, 180102, 180103, 180104], boxes)
        )
        self.assertEqual(
            pairs(result),
            [(180101, "EM900101"), (180102, "EM900102"), (180103, "EM900103"), (180104, "EM900104")],
        )
        self.assertEqual(result["mapping_conflict_count"], 0)
        self.assertEqual(result["ignored_struck_row_count"], 1)

    def test_a_stamp_found_but_no_est_in_the_ai_fields_still_gives_the_po_its_est(self):
        result = P.normalize_content_understanding_result(
            answer("Purchase Order", markdown=stamp(("OH2", ""), ("180101", "EM900101")))
        )
        self.assertEqual(result["estimation_numbers"], [180101])

    def test_a_crossed_out_row_never_hides_an_est_the_document_lists(self):
        # The AI also lists 180109, which no stamp row covers: the mapping must stay blocked.
        result = P.normalize_content_understanding_result(
            answer("Purchase Order", [180101, 180109], stamp(("OH2", ""), ("180101", "EM900101")))
        )
        self.assertEqual(result["estimation_mappings"], [])
        self.assertGreater(result["mapping_conflict_count"], 0)

    def test_stray_text_with_an_order_beside_it_still_blocks(self):
        scan = P.parse_markdown_estimation_mappings(
            answer("Purchase Order", markdown=stamp(("OH2", "EM900101"), ("180102", "EM900102")))
        )
        self.assertEqual(scan["mappings"], [])
        self.assertGreater(scan["mapping_conflict_count"], 0)
        self.assertEqual(scan["struck_rows"], 0)

    def test_a_number_that_could_be_an_est_is_never_treated_as_crossed_out(self):
        for number in ("17010", "1801010"):  # 5 and 7 digits: a misread EST, not a scribble
            with self.subTest(number=number):
                scan = P.parse_markdown_estimation_mappings(
                    answer("Purchase Order", markdown=stamp((number, ""), ("180101", "EM900101")))
                )
                self.assertEqual(scan["mappings"], [])
                self.assertGreater(scan["mapping_conflict_count"], 0)
                self.assertEqual(scan["struck_rows"], 0)

    def test_a_real_est_without_an_order_is_still_reported_as_unassigned(self):
        scan = P.parse_markdown_estimation_mappings(
            answer("Purchase Order", markdown=stamp(("180102", ""), ("180101", "EM900101")))
        )
        self.assertEqual(scan["unassigned_estimates"], [180102])
        self.assertEqual(scan["struck_rows"], 0)

    def test_a_stamp_with_a_crossed_out_row_still_settles_a_mislabelled_document_as_a_po(self):
        result = P.normalize_content_understanding_result(
            answer("Mixing Sheet", markdown=stamp(("OH2", ""), ("180101", "EM900101")))
        )
        self.assertEqual(result["document_type"], "po")


class StampedEstimationCommaV38Tests(unittest.TestCase):
    def test_a_thousands_comma_is_read_as_the_est(self):
        scan = P.parse_markdown_estimation_mappings(
            answer("Purchase Order", markdown=stamp(("180,101", "EM900101"), ("180102", "EM900102")))
        )
        self.assertEqual(
            [(m["estimate_number"], m["reference_number"]) for m in scan["mappings"]],
            [(180101, "EM900101"), (180102, "EM900102")],
        )

    def test_any_other_stray_character_inside_the_digits_still_blocks(self):
        for cell in ("18010 1", "180.101", "1,80101", "180,1011"):
            with self.subTest(cell=cell):
                scan = P.parse_markdown_estimation_mappings(
                    answer("Purchase Order", markdown=stamp((cell, "EM900101"), ("180102", "EM900102")))
                )
                self.assertEqual(scan["mappings"], [])
                self.assertGreater(scan["mapping_conflict_count"], 0)


class StampedOrderMatchV38Tests(unittest.TestCase):
    def test_the_readings_that_handwriting_produces_are_accepted(self):
        for stamped, order in (
            ("OMO19900", "OMO19900"),
            ("OM019900", "OMO19900"),  # letter O read as the digit 0
            ("OMO19900", "OM019900"),
            ("LM009900", "LMO09900"),
            ("OH29900", "OM29900"),  # M read as H
            ("ON29900", "OM29900"),  # M read as N
            ("om 29900", "OM29900"),  # case and spacing never matter
        ):
            with self.subTest(stamped=stamped, order=order):
                self.assertTrue(P.stamped_order_matches(stamped, order))

    def test_a_different_order_is_never_accepted(self):
        for stamped, order in (
            ("OM29901", "OM29900"),  # the neighbouring order
            ("OM29910", "OM29900"),
            ("DM99001", "LM99001"),  # a different series with the same digits
            ("04999C", "04999D"),  # references that differ only by a letter suffix
            ("OH29901", "OM29900"),  # a lookalike letter AND a wrong digit
            ("OM2990", "OM29900"),  # a different length
            ("", "OM29900"),
            ("OM29900", ""),
        ):
            with self.subTest(stamped=stamped, order=order):
                self.assertFalse(P.stamped_order_matches(stamped, order))


class OrdersByEstimation:
    """A read-only cursor answering "which order does this EST belong to"."""

    def __init__(self, orders):
        self.orders = orders
        self.rows = []

    def execute(self, _sql, **binds):
        self.rows = self.orders.get(binds["estimation_no"], [])

    def fetchall(self):
        return self.rows


class PendingRowCrossCheckV38Tests(unittest.TestCase):
    def resolve(self, stamped, *, order="EM900101"):
        return production_app.resolve_po_estimation_candidates(
            OrdersByEstimation({180101: [("EM900101", "")]}),
            [180101],
            [],
            expected_est_no=180101,
            expected_order_no=order,
            estimation_mappings=[{"estimate_number": 180101, "reference_numbers": [stamped]}],
        )

    def test_a_handwriting_misread_of_the_selected_order_no_longer_refuses_the_upload(self):
        for stamped in ("EM900101", "EM9OO101", "EH900101"):
            with self.subTest(stamped=stamped):
                result = self.resolve(stamped)
                self.assertEqual(result["order_numbers"], ["EM900101"])
                self.assertEqual(result["rejected_estimations"], [])

    def test_a_stamp_naming_another_order_is_still_refused(self):
        for stamped in ("EM900102", "LM900101"):
            with self.subTest(stamped=stamped):
                with self.assertRaisesRegex(
                    production_app.DocumentValidationError, "does not resolve to the selected order"
                ):
                    self.resolve(stamped)


class MappingProblemMessageV38Tests(unittest.TestCase):
    """The refusal says which stamp rows are the problem, not only that there is one."""

    def refusal(self, **analysis):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"SCM_ENTRY_USER_CODE": "1"}
        ):
            pdf = Path(folder) / "po.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            with self.assertRaises(production_app.DocumentValidationError) as caught:
                production_app.insert_regular_order_document(
                    filename="po.pdf",
                    file_path=pdf,
                    text="",
                    remarks="r",
                    system_name="s",
                    analysis_result={
                        "estimation_numbers": [180101, 180102],
                        "mapping_conflict_count": 1,
                        **analysis,
                    },
                )
        return str(caught.exception)

    def test_the_stamp_rows_without_an_order_are_named(self):
        message = self.refusal(unassigned_estimation_numbers=[180102, 180103])
        self.assertIn("(EST 180102, 180103 listed on the stamp without an order number)", message)
        self.assertTrue(message.startswith("The document contains incomplete or conflicting"))
        self.assertTrue(message.endswith("requires review (EST 180102, 180103 listed on the stamp "
                                         "without an order number). Nothing was inserted"))

    def test_without_such_rows_the_message_is_unchanged(self):
        self.assertEqual(
            self.refusal(unassigned_estimation_numbers=[]),
            "The document contains incomplete or conflicting EST-to-order mapping "
            "evidence and requires review. Nothing was inserted",
        )


if __name__ == "__main__":
    unittest.main()
