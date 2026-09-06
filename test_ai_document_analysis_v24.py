from __future__ import annotations

import unittest

from po_content_validation import normalize_content_understanding_result


def scalar(value):
    return {"type": "string", "valueString": str(value)}


def v24_response(
    document_type,
    *,
    primary=None,
    legacy=(),
    mappings=(),
    lines=(),
):
    fields = {
        "DocumentType": scalar(document_type),
        "EstimationNumbers": {
            "type": "array",
            "valueArray": [scalar(value) for value in legacy],
        },
        "OrderLines": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        "EstimateNumber": scalar(line["estimate_number"])
                    },
                }
                for line in lines
                if line.get("estimate_number") is not None
            ],
        },
    }
    if primary is not None:
        fields["PrimaryEstimationNumber"] = scalar(primary)
    if mappings:
        fields["EstimationMappings"] = {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        "EstimateNumber": scalar(estimation),
                        "ReferenceNumber": scalar(reference),
                    },
                }
                for estimation, reference in mappings
            ],
        }
    return {"contents": [{"fields": fields}]}


def estimation_report_response(values):
    return {
        "contents": [
            {
                "fields": {
                    "DocumentType": scalar("estimation"),
                    "EstimationReportNumbers": {
                        "type": "array",
                        "valueArray": [scalar(value) for value in values],
                    },
                }
            }
        ]
    }


class EstimationReportNormalizationV24Tests(unittest.TestCase):
    def test_dedicated_estimation_report_field_is_recognized(self):
        result = normalize_content_understanding_result(
            estimation_report_response(["174604", "174651"])
        )

        self.assertEqual(result["estimation_numbers"], [174604, 174651])
        self.assertEqual(result["document_estimation_numbers"], [174604, 174651])

    def test_estimation_report_uses_every_document_table_est(self):
        result = normalize_content_understanding_result(
            v24_response(
                "estimation",
                legacy=["174604", "174651", "174604"],
            )
        )

        self.assertEqual(result["estimation_numbers"], [174604, 174651])
        self.assertEqual(
            result["document_estimation_numbers"],
            [174604, 174651],
        )

    def test_estimation_report_combines_document_and_row_est_values(self):
        result = normalize_content_understanding_result(
            v24_response(
                "estimation",
                legacy=["174618", "174693"],
                lines=[
                    {"estimate_number": "174620"},
                    {"estimate_number": "174693"},
                ],
            )
        )

        self.assertEqual(
            result["estimation_numbers"],
            [174618, 174693, 174620],
        )

    def test_primary_est_does_not_hide_other_estimation_report_rows(self):
        result = normalize_content_understanding_result(
            v24_response(
                "estimation",
                primary="174651",
                legacy=["174604", "174651"],
            )
        )

        self.assertEqual(result["estimation_numbers"], [174651, 174604])

    def test_mixing_still_uses_only_one_primary_est(self):
        result = normalize_content_understanding_result(
            v24_response(
                "mix",
                primary="174651",
                legacy=["174604", "174651"],
            )
        )

        self.assertEqual(result["estimation_numbers"], [174651])

    def test_po_candidate_behavior_is_unchanged(self):
        result = normalize_content_understanding_result(
            v24_response(
                "po",
                legacy=["174600"],
                mappings=[("174651", "LM111168")],
                lines=[{"estimate_number": "174693"}],
            )
        )

        self.assertEqual(
            result["estimation_numbers"],
            [174600, 174651, 174693],
        )


if __name__ == "__main__":
    unittest.main()
