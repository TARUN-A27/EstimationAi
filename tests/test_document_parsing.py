import pytest

from po_content_validation import normalize_content_understanding_result


def field(value):
    return {"type": "string", "valueString": str(value)}


def analysis(document_type, estimation_numbers):
    return {
        "contents": [
            {
                "kind": "document",
                "fields": {
                    "DocumentType": field(document_type),
                    "EstimationNumbers": {
                        "type": "array",
                        "valueArray": [
                            field(value) for value in estimation_numbers
                        ],
                    },
                },
            }
        ]
    }


@pytest.mark.parametrize("est_no", [165815, 165870, 165929])
def test_mix_sheet_estimation_number(est_no):
    result = normalize_content_understanding_result(
        analysis("Mixing Sheet", [est_no])
    )
    assert result["document_type"] == "mix"
    assert result["estimation_numbers"] == [est_no]


def test_estimation_sheet_uses_only_semantic_est_fields():
    result = normalize_content_understanding_result(
        analysis("Estimation Sheet", [165772, 165773, 165774, 165775])
    )
    assert result["estimation_numbers"] == [165772, 165773, 165774, 165775]
    assert 107032 not in result["estimation_numbers"]


def test_purchase_order_is_not_an_enquiry_document():
    result = normalize_content_understanding_result(
        analysis("Purchase Order", [165929])
    )
    assert result["document_type"] == "po"
