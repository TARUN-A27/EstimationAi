"""Extraction fixes from a week of failed uploads.

* A page carrying the EST NO / LM NO stamp is a PO, whatever the AI called it.
* A stamp row with an EST but no order is set aside only when Oracle says that
  EST has no order; if it has one, nothing is dropped silently.
* One PDF holding an estimation report plus the mixing details of an EST can be
  attached as that EST's mixing sheet.
* A failed EST lookup names the EST that was read.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import app as production_app
import po_content_validation as P


def field(value):
    return {"type": "string", "valueString": str(value)}


def answer(document_type, estimations=(), markdown="", mappings=()):
    fields = {"DocumentType": field(document_type)}
    if estimations:
        fields["EstimationNumbers"] = {
            "type": "array",
            "valueArray": [field(value) for value in estimations],
        }
    if mappings:
        items = []
        for estimate, reference in mappings:
            value_object = {"EstimateNumber": field(estimate)}
            if reference:
                value_object["ReferenceNumber"] = field(reference)
            items.append({"type": "object", "valueObject": value_object})
        fields["EstimationMappings"] = {"type": "array", "valueArray": items}
    return {"contents": [{"kind": "document", "fields": fields, "markdown": markdown}]}


def stamp(*rows):
    return (
        "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
        + "".join(f"<tr><td>{est}</td><td>{lm}</td></tr>" for est, lm in rows)
        + "</table>"
    )


def normalize(raw, **kwargs):
    return P.normalize_content_understanding_result(raw, **kwargs)


class StampedPageIsAPoV37Tests(unittest.TestCase):
    def test_a_clean_printed_stamp_makes_it_a_po_whatever_the_ai_says(self):
        for label in ("Mixing Sheet", "Estimation Sheet"):
            with self.subTest(ai=label):
                result = normalize(
                    answer(label, [180101], stamp(("180101", "EM900101")))
                )
                self.assertEqual(result["document_type"], "po")
                self.assertEqual(result["mapping_estimation_numbers"], [180101])

    def test_every_row_of_a_clean_stamp_counts(self):
        result = normalize(
            answer("Mixing Sheet", markdown=stamp(("180101", "LM900101"), ("180102", "LM900102")))
        )
        self.assertEqual(result["document_type"], "po")
        self.assertEqual(result["mapping_estimation_numbers"], [180101, 180102])

    def test_the_ai_type_stands_without_a_clean_stamp(self):
        report = (
            "<table><tr><td>Est No</td><td>Order No</td><td>Party</td></tr>"
            "<tr><td>180101</td><td>EM900101</td><td>ACME SPINNING MILLS</td></tr></table>"
        )
        wide = report.replace("Order No", "LM No")
        smudged = (
            "<table><tr><td>EST : )</td><td>N</td></tr>"
            "<tr><td>180101</td><td>EM900101</td></tr></table>"
        )
        cases = {
            "no table at all": "",
            "an estimation-report table that only looks like a mapping": report,
            "a wider table that has the two headers": wide,
            "a smudged header": smudged,
            "a reference that is not an order number": stamp(("180101", "LM-A")),
            "a stamp with no order in any row": stamp(("180101", ""), ("180102", "")),
            "a five digit EST": stamp(("80101", "EM900101")),
            "a row that is neither blank nor an order": stamp(("180101", "EM900101"), ("180102", "see PO")),
        }
        for name, markdown in cases.items():
            for label, expected in (("Mixing Sheet", "mix"), ("Estimation Sheet", "estimation")):
                with self.subTest(case=name, ai=label):
                    result = normalize(answer(label, [180101], markdown))
                    self.assertEqual(result["document_type"], expected)

    def test_a_stray_row_without_an_order_still_marks_a_po_but_blocks_its_mapping(self):
        # A PO whose stamp lists a second EST with no LM, labelled "estimation":
        # it is not a duplicate estimation sheet, and it must not be guessed at.
        result = normalize(
            answer("Estimation Sheet", [180101, 180102], stamp(("180102", ""), ("180101", "LM900101")))
        )
        self.assertEqual(result["document_type"], "po")
        self.assertEqual(result["estimation_mappings"], [])
        self.assertGreater(result["mapping_conflict_count"], 0)
        self.assertEqual(result["unassigned_estimation_numbers"], [180102])

    def test_the_po_path_is_untouched_for_a_po_the_ai_got_right(self):
        result = normalize(answer("Purchase Order", markdown=stamp(("180101", "LM-A"))))
        self.assertEqual(result["document_type"], "po")
        self.assertEqual(result["mapping_estimation_numbers"], [180101])


class StampRowWithoutAnOrderV37Tests(unittest.TestCase):
    TABLE = stamp(("180102", ""), ("180101", "LM900101"))

    def raw(self):
        return answer("Purchase Order", [180101, 180102], self.TABLE)

    def test_such_a_row_is_reported_and_still_blocks_by_default(self):
        result = normalize(self.raw())
        self.assertEqual(result["unassigned_estimation_numbers"], [180102])
        self.assertGreater(result["mapping_conflict_count"], 0)
        self.assertEqual(result["estimation_mappings"], [])

    def test_leaving_that_est_out_clears_the_conflict(self):
        result = normalize(self.raw(), ignored_estimation_numbers=[180102])
        self.assertEqual(result["mapping_conflict_count"], 0)
        self.assertEqual(
            [(m["estimate_number"], m["reference_number"]) for m in result["estimation_mappings"]],
            [(180101, "LM900101")],
        )
        self.assertEqual(result["estimation_numbers"], [180101])

    def test_the_ais_own_estimation_list_is_treated_the_same_way(self):
        raw = answer(
            "Purchase Order",
            [180101, 180102],
            mappings=[(180102, ""), (180101, "LM900101")],
        )
        strict = normalize(raw)
        self.assertEqual(strict["unassigned_estimation_numbers"], [180102])
        self.assertGreater(strict["mapping_conflict_count"], 0)
        relaxed = normalize(raw, ignored_estimation_numbers=[180102])
        self.assertEqual(relaxed["mapping_conflict_count"], 0)
        self.assertEqual(relaxed["mapping_estimation_numbers"], [180101])

    def analyse(self, lookup):
        with patch.object(
            P, "analyzer_configuration", return_value=("a", "CONTENTUNDERSTANDING_ANALYZER_ID")
        ), patch.object(P, "analyze_po_document", return_value=self.raw()):
            return P.analyze_uploaded_document(Path("po.pdf"), estimates_without_orders=lookup)

    def test_oracle_saying_the_est_has_no_order_lets_the_po_through(self):
        result = self.analyse(lambda estimates: set(estimates))
        self.assertEqual(result["mapping_conflict_count"], 0)
        self.assertEqual(result["ignored_unassigned_estimation_numbers"], [180102])

    def test_an_est_that_has_an_order_is_never_set_aside(self):
        result = self.analyse(lambda estimates: set())
        self.assertGreater(result["mapping_conflict_count"], 0)
        self.assertNotIn("ignored_unassigned_estimation_numbers", result)

    def test_a_failed_lookup_never_loosens_anything(self):
        def broken(_estimates):
            raise RuntimeError("database unreachable")

        self.assertGreater(self.analyse(broken)["mapping_conflict_count"], 0)

    def test_a_stamp_with_only_order_less_rows_still_blocks(self):
        only = answer("Purchase Order", [180102], stamp(("180102", "")))
        with patch.object(
            P, "analyzer_configuration", return_value=("a", "CONTENTUNDERSTANDING_ANALYZER_ID")
        ), patch.object(P, "analyze_po_document", return_value=only):
            result = P.analyze_uploaded_document(
                Path("po.pdf"), estimates_without_orders=lambda estimates: set(estimates)
            )
        self.assertEqual(result["estimation_mappings"], [])
        self.assertEqual(result["estimation_numbers"], [])


class CombinedEstimationAndMixingV37Tests(unittest.TestCase):
    MIXING_PAGE = "3). Mixing Details For Est. No- '180102', Ref. Order- 'R1'\nYarn Type : NONE"

    def analysis(self):
        return normalize(answer("Estimation Sheet", [180101, 180102], self.MIXING_PAGE))

    def test_the_ests_with_mixing_details_are_listed(self):
        self.assertEqual(self.analysis()["mixing_detail_estimation_numbers"], [180102])

    def test_a_selected_mix_is_accepted_when_its_mixing_details_are_in_the_pdf(self):
        accepted = P.accept_selected_document_type(self.analysis(), "mix", 180102)
        self.assertEqual(accepted["document_type"], "mix")
        self.assertEqual(accepted["estimation_numbers"], [180102])
        self.assertTrue(accepted["combined_document"])

    def test_every_other_selection_is_left_alone(self):
        analysis = self.analysis()
        for expected, est in (("mix", 180101), ("estimation", 180102), (None, None), ("po", 180102)):
            with self.subTest(expected=expected, est=est):
                self.assertIs(P.accept_selected_document_type(analysis, expected, est), analysis)

    def test_a_pdf_without_mixing_details_is_still_refused(self):
        plain = normalize(answer("Estimation Sheet", [180101]))
        self.assertIs(P.accept_selected_document_type(plain, "mix", 180101), plain)


class ClearerMessagesV37Tests(unittest.TestCase):
    def test_the_est_that_was_read_is_named_when_it_maps_to_no_order(self):
        class NoOrders:
            def execute(self, *_args, **_binds):
                pass

            def fetchall(self):
                return []

        with self.assertRaisesRegex(
            production_app.DocumentValidationError,
            r"\(EST 1801813 has no order in REGULARORDER\)\. Nothing was inserted",
        ):
            production_app.resolve_po_estimation_candidates(NoOrders(), [1801813], [])

    def test_which_ests_have_no_order_is_asked_of_oracle_read_only(self):
        class Cursor:
            statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, sql, binds):
                self.statements.append(sql)

            def fetchall(self):
                return [(180101,)]

        class Connection:
            closed = False

            def cursor(self):
                return Cursor()

            def close(self):
                self.closed = True

        connection = Connection()
        with patch.object(production_app, "get_db_connection", return_value=connection):
            missing = production_app.estimates_without_regular_orders([180101, 180102])
        self.assertEqual(missing, {180102})
        self.assertTrue(connection.closed)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in Cursor.statements))


if __name__ == "__main__":
    unittest.main()
