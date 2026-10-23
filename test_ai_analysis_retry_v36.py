"""An unusable AI answer is analysed once more.

Azure's answers vary from run to run for the very same file: in one week the same
PO was stored twice and then failed with no EST, another failed twice and then
stored, and a third came back with no content at all, twice. One more analysis
(only when the first was quick) rescues those without paying for good files twice.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

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


EMPTY = {"contents": []}
GOOD_PO = answer("Purchase Order", [180101])
NO_EST_PO = answer("Purchase Order")
STAMP = (
    "<table><tr><td>EST NO</td><td>LM NO</td></tr>"
    "<tr><td>{est}</td><td>LM900101</td></tr></table>"
)


def analyse(answers, *, monotonic=None):
    events = []

    def audit(stage, status, _message, _details=None):
        events.append((stage, status))

    with patch.object(
        P, "analyzer_configuration", return_value=("analyzer", "CONTENTUNDERSTANDING_ANALYZER_ID")
    ), patch.object(P, "analyze_po_document", side_effect=answers) as azure:
        if monotonic is None:
            result = P.analyze_uploaded_document(Path("po.pdf"), audit=audit)
        else:
            with patch.object(P.time, "monotonic", side_effect=monotonic):
                result = P.analyze_uploaded_document(Path("po.pdf"), audit=audit)
    return result, azure.call_count, events


class AnalysisRetryV36Tests(unittest.TestCase):
    def test_a_result_with_no_estimation_number_is_analysed_once_more(self):
        result, calls, events = analyse([NO_EST_PO, GOOD_PO])
        self.assertEqual(result["estimation_numbers"], [180101])
        self.assertEqual(calls, 2)
        self.assertIn(("ai_retry", "processing"), events)
        self.assertIn(("ai_retry", "completed"), events)

    def test_a_usable_first_result_is_never_analysed_twice(self):
        result, calls, events = analyse([GOOD_PO])
        self.assertEqual(result["estimation_numbers"], [180101])
        self.assertEqual(calls, 1)
        self.assertNotIn(("ai_retry", "processing"), events)

    def test_an_empty_response_is_retried_and_a_good_second_answer_is_used(self):
        result, calls, _events = analyse([EMPTY, GOOD_PO])
        self.assertEqual(result["estimation_numbers"], [180101])
        self.assertEqual(calls, 2)

    def test_a_conflicting_po_mapping_is_analysed_again(self):
        conflicting = answer("Purchase Order", [180101], STAMP.format(est="80101"))
        clean = answer("Purchase Order", [180101], STAMP.format(est="180101"))
        result, calls, _events = analyse([conflicting, clean])
        self.assertEqual(calls, 2)
        self.assertEqual(result["mapping_conflict_count"], 0)
        self.assertEqual(result["mapping_estimation_numbers"], [180101])

    def test_two_unusable_answers_stop_after_the_second_and_keep_the_first(self):
        result, calls, _events = analyse([NO_EST_PO, answer("Mixing Sheet")])
        self.assertEqual(calls, 2)  # never a third analysis
        self.assertEqual(result["document_type"], "po")  # the first answer is kept
        self.assertEqual(result["estimation_numbers"], [])

    def test_two_empty_responses_still_raise_the_original_error(self):
        events = []
        with patch.object(
            P, "analyzer_configuration", return_value=("analyzer", "CONTENTUNDERSTANDING_ANALYZER_ID")
        ), patch.object(P, "analyze_po_document", side_effect=[EMPTY, EMPTY]) as azure:
            with self.assertRaisesRegex(P.ContentAnalysisError, "no document content"):
                P.analyze_uploaded_document(
                    Path("po.pdf"), audit=lambda s, st, m, d=None: events.append((s, st))
                )
        self.assertEqual(azure.call_count, 2)
        self.assertIn(("ai_normalization", "failed"), events)

    def test_a_retry_that_fails_leaves_the_first_answer_standing(self):
        for failure in (
            P.ContentAnalysisError("AI document analysis timed out"),
            "not a mapping",  # a malformed response
        ):
            with self.subTest(failure=str(failure)[:20]):
                result, calls, events = analyse([NO_EST_PO, failure])
                self.assertEqual(calls, 2)
                self.assertEqual(result["document_type"], "po")
                self.assertEqual(result["estimation_numbers"], [])
                self.assertNotIn(("ai_normalization", "failed"), events)

    def test_a_malformed_first_answer_is_audited_and_not_retried(self):
        events = []
        with patch.object(
            P, "analyzer_configuration", return_value=("analyzer", "CONTENTUNDERSTANDING_ANALYZER_ID")
        ), patch.object(P, "analyze_po_document", side_effect=["not a mapping", GOOD_PO]) as azure:
            with self.assertRaisesRegex(P.ContentAnalysisError, "malformed"):
                P.analyze_uploaded_document(
                    Path("po.pdf"), audit=lambda s, st, m, d=None: events.append((s, st))
                )
        self.assertEqual(azure.call_count, 1)
        self.assertIn(("ai_normalization", "failed"), events)

    def test_a_slow_first_analysis_is_not_repeated(self):
        # started at 0 s, finished at 120 s: a second pass could pass the 300 s limit.
        result, calls, events = analyse([NO_EST_PO], monotonic=[0, 120])
        self.assertEqual(calls, 1)
        self.assertEqual(result["estimation_numbers"], [])
        self.assertNotIn(("ai_retry", "processing"), events)

    def test_configuration_and_azure_failures_are_not_retried(self):
        with patch.object(
            P, "analyzer_configuration", return_value=("analyzer", "CONTENTUNDERSTANDING_ANALYZER_ID")
        ), patch.object(
            P, "analyze_po_document", side_effect=P.ContentAnalysisError("AI document analysis timed out")
        ) as azure:
            with self.assertRaisesRegex(P.ContentAnalysisError, "timed out"):
                P.analyze_uploaded_document(Path("po.pdf"))
        self.assertEqual(azure.call_count, 1)


if __name__ == "__main__":
    unittest.main()
