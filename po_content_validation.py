"""Content Understanding helpers for PO uploads.

The production upload flow stores fields extracted from the PO document. The
legacy comparison helpers remain available for diagnostics and regression
tests, but they are not required by the production insertion path.
"""

from __future__ import annotations

import mimetypes
import os
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Any


MONEY_QUANTUM = Decimal("0.01")
RATE_TOLERANCE = Decimal("0.01")

ORACLE_EXPECTED_VALUES_QUERY = """
    SELECT
        CostEstimation.EstNo AS ESTIMATION_NUMBER,
        CostEstimation.RefrenceNo AS REFERENCE_NUMBER,
        CostEstimation.Req_Qty AS REQUIRED_QUANTITY,
        YarnCount.CountName AS COUNT_NAME,
        PartyMaster.PartyName AS PARTY_NAME,
        CostEstimation.BookingRate AS BOOKING_RATE,
        GET_EST_CERT_TYPE(CostEstimation.EstNo) AS CERTIFICATION
    FROM CostEstimation
    INNER JOIN YarnCount
        ON YarnCount.CountCode = CostEstimation.CountCode
    INNER JOIN PartyMaster
        ON PartyMaster.PartyCode = CostEstimation.PartyCode
    WHERE CostEstimation.EstNo = :est_no
"""

ORACLE_EXPECTED_PARTY_QUERY = """
    SELECT DISTINCT TRIM(PartyMaster.PartyName) AS PARTY_NAME
    FROM CostEstimation
    INNER JOIN PartyMaster
        ON PartyMaster.PartyCode = CostEstimation.PartyCode
    WHERE CostEstimation.EstNo = :est_no
"""

CERTIFICATE_MASTER_QUERY = """
    SELECT TRIM(NAME)
    FROM PO_CERTTYPEMASTER
    WHERE NAME IS NOT NULL
    ORDER BY NAME
"""


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(child) for child in value]
    if hasattr(value, "items"):
        return {str(key): to_plain(child) for key, child in value.items()}
    return str(value)


def analyze_po_document(document_path: Path) -> dict[str, Any]:
    """Run the configured custom analyzer and return plain JSON data."""
    try:
        from azure.ai.contentunderstanding import ContentUnderstandingClient
        from azure.core.credentials import AzureKeyCredential
        from azure.core.exceptions import HttpResponseError
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Azure Content Understanding dependency is missing. Install "
            "azure-ai-contentunderstanding in the application environment"
        ) from exc

    endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT", "").strip()
    key = os.getenv("CONTENTUNDERSTANDING_KEY", "").strip()
    analyzer_id = os.getenv("CONTENTUNDERSTANDING_ANALYZER_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("CONTENTUNDERSTANDING_ENDPOINT", endpoint),
            ("CONTENTUNDERSTANDING_KEY", key),
            ("CONTENTUNDERSTANDING_ANALYZER_ID", analyzer_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing Content Understanding configuration: "
            + ", ".join(missing)
        )

    document_path = Path(document_path)
    mime_type = (
        mimetypes.guess_type(document_path.name)[0]
        or "application/octet-stream"
    )
    client = ContentUnderstandingClient(
        endpoint=endpoint.rstrip("/"),
        credential=AzureKeyCredential(key),
        api_version="2025-11-01",
    )
    try:
        poller = client.begin_analyze_binary(
            analyzer_id=analyzer_id,
            binary_input=document_path.read_bytes(),
            content_type=mime_type,
        )
        return to_plain(poller.result())
    except HttpResponseError as exc:
        status_code = getattr(exc, "status_code", "unknown")
        error = getattr(exc, "error", None)
        error_code = getattr(error, "code", None) or "unknown"
        message = getattr(exc, "message", None) or str(exc)
        raise RuntimeError(
            "Content Understanding request failed "
            f"(HTTP {status_code}, code {error_code}): {message}"
        ) from exc
    finally:
        client.close()


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def field_values(field: Any) -> list[str]:
    if not isinstance(field, Mapping):
        return []
    if isinstance(field.get("valueArray"), list):
        values: list[str] = []
        for child in field["valueArray"]:
            values.extend(field_values(child))
        return values
    for key in (
        "valueString",
        "valueInteger",
        "valueNumber",
        "valueDate",
        "valueTime",
        "valueBoolean",
    ):
        value = field.get(key)
        if value is not None:
            return [str(value).strip()]
    return []


def first_field_value(field: Any) -> str | None:
    values = field_values(field)
    return values[0] if values else None


def content_fields(result_data: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for content in result_data.get("contents") or []:
        if not isinstance(content, Mapping):
            continue
        fields = content.get("fields") or {}
        if isinstance(fields, Mapping):
            yield fields


def parse_order_lines(result_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    order_lines: list[dict[str, Any]] = []
    for fields in content_fields(result_data):
        order_lines_field = fields.get("OrderLines") or {}
        if not isinstance(order_lines_field, Mapping):
            continue
        for item in order_lines_field.get("valueArray") or []:
            if not isinstance(item, Mapping):
                continue
            value_object = item.get("valueObject") or {}
            if not isinstance(value_object, Mapping):
                continue
            order_lines.append(
                {
                    "serial_number": first_field_value(
                        value_object.get("SerialNumber")
                    ),
                    "count": field_values(value_object.get("Count")),
                    "required_quantity": field_values(
                        value_object.get("RequiredQuantity")
                    ),
                    "reference_number": field_values(
                        value_object.get("ReferenceNumber")
                    ),
                    "confirm_rate": field_values(
                        value_object.get("ConfirmRate")
                    ),
                    "certification": field_values(
                        value_object.get(
                            "CertificateRateIncludedHeatSettings"
                        )
                    ),
                }
            )
    return order_lines


def decimal_values(value: Any) -> list[Decimal]:
    if value is None:
        return []
    text = str(value).strip().replace(",", "")
    values: list[Decimal] = []
    for match in re.findall(r"[-+]?\d+(?:\.\d+)?", text):
        try:
            values.append(Decimal(match))
        except InvalidOperation:
            continue
    return values


def first_decimal(value: Any) -> Decimal | None:
    values = decimal_values(value)
    return values[0] if values else None


def parse_tax_details(result_data: Mapping[str, Any]) -> list[dict[str, str]]:
    pairs: set[tuple[Decimal, Decimal]] = set()
    for fields in content_fields(result_data):
        tax_field = fields.get("TaxDetails") or {}
        if not isinstance(tax_field, Mapping):
            continue
        for item in tax_field.get("valueArray") or []:
            if not isinstance(item, Mapping):
                continue
            value_object = item.get("valueObject") or {}
            if not isinstance(value_object, Mapping):
                continue
            sgst = first_decimal(
                first_field_value(value_object.get("SGSTPercent"))
            )
            cgst = first_decimal(
                first_field_value(value_object.get("CGSTPercent"))
            )
            if sgst is None or cgst is None or sgst < 0 or cgst < 0:
                continue
            pairs.add((sgst, cgst))
    return [
        {
            "sgst_percent": format(sgst.normalize(), "f"),
            "cgst_percent": format(cgst.normalize(), "f"),
        }
        for sgst, cgst in sorted(pairs)
    ]


def parse_party_names(result_data: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for fields in content_fields(result_data):
        names.extend(field_values(fields.get("PartyName")))
    return ordered_unique(name for name in names if name)


def identifier_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def sanitize_order_lines(
    order_lines: list[dict[str, Any]],
    estimation_numbers: Iterable[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    estimation_keys = {identifier_key(value) for value in estimation_numbers}
    removals: list[dict[str, Any]] = []
    cleaned_lines: list[dict[str, Any]] = []
    for line_number, original in enumerate(order_lines, start=1):
        cleaned = {
            key: list(value) if isinstance(value, list) else value
            for key, value in original.items()
        }
        for field_name in (
            "required_quantity",
            "confirm_rate",
            "reference_number",
        ):
            kept: list[str] = []
            for value in cleaned.get(field_name) or []:
                if identifier_key(value) in estimation_keys:
                    removals.append(
                        {
                            "line": line_number,
                            "field": field_name,
                            "value": value,
                            "reason": "matches OCR estimation number",
                        }
                    )
                else:
                    kept.append(value)
            cleaned[field_name] = kept
        cleaned_lines.append(cleaned)
    return cleaned_lines, removals


def oracle_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def fetch_oracle_expected_rows(
    cursor: Any,
    estimation_numbers: Iterable[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for estimation_number in estimation_numbers:
        cursor.execute(
            ORACLE_EXPECTED_VALUES_QUERY,
            est_no=int(estimation_number),
        )
        fetched = cursor.fetchall()
        if not fetched:
            missing.append(str(estimation_number))
            continue
        columns = [str(item[0]).lower() for item in cursor.description]
        for row in fetched:
            rows.append(
                {
                    key: oracle_scalar(value)
                    for key, value in zip(columns, row, strict=True)
                }
            )
    return rows, missing


def fetch_expected_party_names(
    cursor: Any,
    estimation_numbers: Iterable[int],
) -> dict[str, str]:
    """Return one canonical Oracle party name for each stamped EST."""
    result: dict[str, str] = {}
    for estimation_number in estimation_numbers:
        key = str(int(estimation_number))
        cursor.execute(
            ORACLE_EXPECTED_PARTY_QUERY,
            est_no=int(estimation_number),
        )
        names = ordered_unique(
            str(row[0]).strip()
            for row in cursor.fetchall()
            if row[0] is not None and str(row[0]).strip()
        )
        if len(names) != 1:
            raise ValueError(
                f"EST {key} requires exactly one Oracle party name; "
                f"found {len(names)}"
            )
        result[key] = names[0]
    return result


def fetch_certificate_master_names(cursor: Any) -> list[str]:
    """Load canonical certificate names from PO_CERTTYPEMASTER.NAME."""
    cursor.execute(CERTIFICATE_MASTER_QUERY)
    names = ordered_unique(
        str(row[0]).strip()
        for row in cursor.fetchall()
        if row[0] is not None and str(row[0]).strip()
    )
    if not names:
        raise ValueError("PO_CERTTYPEMASTER contains no certificate names")
    return names


def normalize_identifier(value: Any) -> str:
    return identifier_key(value)


def normalize_reference_number(value: Any) -> str:
    """Normalize a reference value while removing printed field labels."""
    normalized = identifier_key(value)
    for prefix in (
        "REFERENCENUMBER",
        "REFERENCENO",
        "REFERENCE",
        "OPTIONNUMBER",
        "OPTIONNO",
        "OPTION",
        "REFNO",
    ):
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix):]
            if len(remainder) >= 4:
                return remainder
    return normalized


def normalize_party_name(value: Any) -> str:
    tokens = re.findall(r"[A-Z0-9]+", str(value).upper())
    aliases = {
        "PVT": "PRIVATE",
        "LTD": "LIMITED",
        "CO": "COMPANY",
    }
    return "".join(aliases.get(token, token) for token in tokens)


def normalize_party_core(value: Any) -> str:
    """Remove generic legal suffixes before partial OCR comparison."""
    aliases = {
        "PVT": "PRIVATE",
        "LTD": "LIMITED",
        "CO": "COMPANY",
        "CORP": "CORPORATION",
    }
    generic_tokens = {
        "PRIVATE",
        "LIMITED",
        "COMPANY",
        "CORPORATION",
        "INC",
        "INCORPORATED",
        "LLC",
        "LLP",
    }
    tokens = [
        aliases.get(token, token)
        for token in re.findall(r"[A-Z0-9]+", str(value).upper())
    ]
    return "".join(
        token for token in tokens if token not in generic_tokens
    )


def compare_party_name(
    expected: Any,
    extracted_values: Iterable[Any],
) -> dict[str, Any]:
    extracted = [
        str(value).strip()
        for value in extracted_values
        if str(value).strip()
    ]
    expected_text = "" if expected is None else str(expected).strip()
    expected_key = normalize_party_name(expected_text)
    expected_core = normalize_party_core(expected_text)
    candidates: list[dict[str, Any]] = []
    for value in extracted:
        candidate_key = normalize_party_name(value)
        candidate_core = normalize_party_core(value)
        if not candidate_key:
            continue
        similarity = SequenceMatcher(
            None,
            expected_key,
            candidate_key,
        ).ratio()
        shorter_core_length = min(
            len(expected_core),
            len(candidate_core),
        )
        longer_core_length = max(
            len(expected_core),
            len(candidate_core),
            1,
        )
        core_coverage = shorter_core_length / longer_core_length
        prefix_truncation_match = bool(
            shorter_core_length >= 7
            and core_coverage >= 0.55
            and (
                expected_core.endswith(candidate_core)
                or candidate_core.endswith(expected_core)
            )
        )
        candidates.append(
            {
                "value": value,
                "similarity": round(similarity, 4),
                "core_coverage": round(core_coverage, 4),
                "prefix_truncation_match": (
                    prefix_truncation_match
                ),
            }
        )
    exact_matches = [
        item
        for item in candidates
        if item["similarity"] >= 0.94
    ]
    partial_matches = [
        item
        for item in candidates
        if item["prefix_truncation_match"]
    ]
    result: dict[str, Any] = {
        "expected": expected_text or None,
        "extracted": extracted,
        "match": bool(expected_key and exact_matches),
        "comparable": bool(expected_key and candidates),
        "candidates": candidates,
        "partial_match_available": bool(
            expected_key and partial_matches
        ),
    }
    if exact_matches:
        result["matched_value"] = max(
            exact_matches,
            key=lambda item: item["similarity"],
        )["value"]
        result["comparison_method"] = "FULL_PARTY_NAME"
    elif partial_matches:
        result["partial_matched_value"] = max(
            partial_matches,
            key=lambda item: (
                item["core_coverage"],
                item["similarity"],
            ),
        )["value"]
        result["reason"] = (
            "A leading-edge OCR truncation match is available, but it "
            "requires every order-line field to match"
        )
    elif not extracted:
        result["reason"] = "No PartyName value was extracted from the PDF"
    else:
        result["reason"] = "No extracted party name matched the Oracle party"
    return result


def match_certificate_master_names(
    extracted_values: Iterable[Any],
    master_names: Iterable[Any],
) -> dict[str, Any]:
    """Match extracted certificate text to canonical master-table names.

    Exact spelling is preferred so intentionally distinct master values such
    as B.C.T/BCT and REGEN AGRI/REGENAGRI remain distinguishable. Longer
    contained names win over shorter names such as OCS.
    """
    extracted = ordered_unique(
        str(value).strip()
        for value in extracted_values
        if str(value).strip()
    )
    masters = ordered_unique(
        str(value).strip()
        for value in master_names
        if str(value).strip()
    )
    matches: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    for value in extracted:
        raw_key = " ".join(value.upper().split())
        exact = [
            name
            for name in masters
            if " ".join(name.upper().split()) == raw_key
        ]
        if len(exact) == 1:
            matches.append(exact[0])
            continue

        literal = [
            name
            for name in masters
            if name.upper() in value.upper()
        ]
        if literal:
            longest = max(len(identifier_key(name)) for name in literal)
            winners = [
                name
                for name in literal
                if len(identifier_key(name)) == longest
            ]
            if len(winners) == 1:
                matches.append(winners[0])
                continue

        extracted_key = identifier_key(value)
        normalized = [
            name
            for name in masters
            if identifier_key(name)
            and identifier_key(name) in extracted_key
        ]
        if normalized:
            longest = max(
                len(identifier_key(name)) for name in normalized
            )
            winners = [
                name
                for name in normalized
                if len(identifier_key(name)) == longest
            ]
            normalized_keys = {
                identifier_key(name) for name in winners
            }
            if len(winners) == 1 and len(normalized_keys) == 1:
                matches.append(winners[0])
                continue
            ambiguous.append(
                {
                    "extracted": value,
                    "candidates": winners,
                }
            )

    canonical_matches = ordered_unique(matches)
    return {
        "extracted": extracted,
        "matched_master_names": canonical_matches,
        "ambiguous": ambiguous,
        "stored_value": (
            ", ".join(canonical_matches)
            if canonical_matches
            else None
        ),
    }


def normalize_numeric(value: Any) -> str:
    parsed = first_decimal(value)
    if parsed is None:
        return ""
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def compare_expected_value(
    expected: Any,
    extracted_values: Iterable[Any],
    normalizer: Any,
    *,
    null_matches_empty: bool = False,
) -> dict[str, Any]:
    extracted = [
        str(value).strip()
        for value in extracted_values
        if str(value).strip()
    ]
    normalized_extracted = ordered_unique(
        normalizer(value) for value in extracted if normalizer(value)
    )
    if expected is None or not str(expected).strip():
        return {
            "expected": None,
            "extracted": extracted,
            "match": null_matches_empty and not normalized_extracted,
            "comparable": null_matches_empty,
        }
    normalized_expected = normalizer(expected)
    return {
        "expected": str(expected).strip(),
        "extracted": extracted,
        "match": normalized_expected in normalized_extracted,
        "comparable": bool(normalized_expected),
    }


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def rates_equal(left: Decimal, right: Decimal) -> bool:
    return abs(money(left) - money(right)) <= RATE_TOLERANCE


def compare_confirm_rate(
    expected: Any,
    extracted_values: Iterable[Any],
    tax_details: list[dict[str, str]],
) -> dict[str, Any]:
    extracted = [
        str(value).strip()
        for value in extracted_values
        if str(value).strip()
    ]
    expected_rate = first_decimal(expected)
    parsed_rates: list[Decimal] = []
    for value in extracted:
        parsed_rates.extend(decimal_values(value))
    result: dict[str, Any] = {
        "expected": None if expected is None else str(expected).strip(),
        "extracted": extracted,
        "match": False,
        "comparable": expected_rate is not None and bool(parsed_rates),
        "direct_match": False,
        "comparison_method": None,
        "tax_applied": False,
    }
    if expected_rate is None or not parsed_rates:
        result["reason"] = "Oracle rate or PDF ConfirmRate is missing"
        return result
    for pdf_rate in parsed_rates:
        if rates_equal(pdf_rate, expected_rate):
            result.update(
                match=True,
                direct_match=True,
                comparison_method="DIRECT_RATE",
                matched_pdf_rate=format(money(pdf_rate), "f"),
            )
            return result
    lower_rates = [
        rate for rate in parsed_rates if money(rate) < money(expected_rate)
    ]
    if not lower_rates:
        result.update(
            comparison_method="DIRECT_RATE",
            reason="PDF rate is not lower than Oracle rate; GST was not added",
        )
        return result
    if len(tax_details) != 1:
        result.update(
            comparison_method="GST_NOT_COMPARABLE",
            reason=(
                "Exactly one distinct SGST/CGST pair is required; "
                f"found {len(tax_details)}"
            ),
        )
        return result
    sgst = Decimal(tax_details[0]["sgst_percent"])
    cgst = Decimal(tax_details[0]["cgst_percent"])
    multiplier = Decimal("1") + ((sgst + cgst) / Decimal("100"))
    calculations: list[dict[str, str]] = []
    for pdf_rate in lower_rates:
        adjusted = money(pdf_rate * multiplier)
        calculations.append(
            {
                "net_rate": format(money(pdf_rate), "f"),
                "sgst_percent": format(sgst.normalize(), "f"),
                "cgst_percent": format(cgst.normalize(), "f"),
                "rate_after_gst": format(adjusted, "f"),
            }
        )
        if rates_equal(adjusted, expected_rate):
            result.update(
                match=True,
                comparison_method="NET_PLUS_SGST_CGST",
                tax_applied=True,
                matched_pdf_rate=format(money(pdf_rate), "f"),
                sgst_percent=format(sgst.normalize(), "f"),
                cgst_percent=format(cgst.normalize(), "f"),
                calculated_rate_after_gst=format(adjusted, "f"),
                calculations=calculations,
            )
            return result
    result.update(
        comparison_method="NET_PLUS_SGST_CGST",
        tax_applied=True,
        calculations=calculations,
    )
    return result


def compare_oracle_row_to_line(
    oracle_row: Mapping[str, Any],
    order_line: Mapping[str, Any],
    tax_details: list[dict[str, str]],
) -> dict[str, Any]:
    fields = {
        "reference_number": compare_expected_value(
            oracle_row.get("reference_number"),
            order_line.get("reference_number") or [],
            normalize_reference_number,
        ),
        "required_quantity": compare_expected_value(
            oracle_row.get("required_quantity"),
            order_line.get("required_quantity") or [],
            normalize_numeric,
        ),
        "count": compare_expected_value(
            oracle_row.get("count_name"),
            order_line.get("count") or [],
            normalize_identifier,
        ),
        "confirm_rate": compare_confirm_rate(
            oracle_row.get("booking_rate"),
            order_line.get("confirm_rate") or [],
            tax_details,
        ),
        "certification": compare_expected_value(
            oracle_row.get("certification"),
            order_line.get("certification") or [],
            normalize_identifier,
            null_matches_empty=True,
        ),
    }
    weights = {
        "reference_number": 4,
        "required_quantity": 3,
        "count": 2,
        "confirm_rate": 2,
        "certification": 1,
    }
    score = sum(
        weights[name] for name, result in fields.items() if result["match"]
    )
    anchor_matches = sum(
        1
        for name in ("reference_number", "required_quantity", "count")
        if fields[name]["match"]
    )
    mismatched_fields = [
        name
        for name, result in fields.items()
        if result["comparable"] and not result["match"]
    ]
    return {
        "score": score,
        "anchor_matches": anchor_matches,
        "fields": fields,
        "mismatched_fields": mismatched_fields,
    }


def match_oracle_rows_to_order_lines(
    oracle_rows: list[dict[str, Any]],
    order_lines: list[dict[str, Any]],
    tax_details: list[dict[str, str]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    used_line_indexes: set[int] = set()
    for row_number, oracle_row in enumerate(oracle_rows, start=1):
        candidates: list[tuple[int, dict[str, Any]]] = []
        for line_index, order_line in enumerate(order_lines):
            if line_index in used_line_indexes:
                continue
            result = compare_oracle_row_to_line(
                oracle_row,
                order_line,
                tax_details,
            )
            if result["anchor_matches"]:
                candidates.append((line_index, result))
        base = {
            "oracle_row_number": row_number,
            "estimation_number": oracle_row.get("estimation_number"),
            "oracle_expected": oracle_row,
            "party_name_comparison": "NOT_INCLUDED",
        }
        if not candidates:
            comparisons.append(
                {
                    **base,
                    "status": "NOT_FOUND_IN_PDF",
                    "matched_order_line_number": None,
                    "pdf_order_line": None,
                    "mismatched_fields": [],
                    "field_results": {},
                }
            )
            continue
        candidates.sort(
            key=lambda item: (
                item[1]["score"],
                item[1]["anchor_matches"],
            ),
            reverse=True,
        )
        best_score = candidates[0][1]["score"]
        best_anchor_count = candidates[0][1]["anchor_matches"]
        equally_best = [
            item
            for item in candidates
            if item[1]["score"] == best_score
            and item[1]["anchor_matches"] == best_anchor_count
        ]
        if len(equally_best) > 1:
            comparisons.append(
                {
                    **base,
                    "status": "AMBIGUOUS",
                    "candidate_order_line_numbers": [
                        line_index + 1 for line_index, _ in equally_best
                    ],
                    "matched_order_line_number": None,
                    "pdf_order_line": None,
                    "mismatched_fields": [],
                    "field_results": {},
                }
            )
            continue
        line_index, best_result = equally_best[0]
        used_line_indexes.add(line_index)
        comparisons.append(
            {
                **base,
                "status": (
                    "MATCH"
                    if not best_result["mismatched_fields"]
                    else "MISMATCH"
                ),
                "matched_order_line_number": line_index + 1,
                "pdf_order_line": order_lines[line_index],
                "mismatched_fields": best_result["mismatched_fields"],
                "field_results": best_result["fields"],
            }
        )
    return comparisons


def validate_content_result(
    result_data: Mapping[str, Any],
    cursor: Any,
    estimation_numbers: Iterable[int],
) -> dict[str, Any]:
    estimation_numbers = list(dict.fromkeys(int(v) for v in estimation_numbers))
    raw_lines = parse_order_lines(result_data)
    order_lines, filtered_values = sanitize_order_lines(
        raw_lines,
        estimation_numbers,
    )
    tax_details = parse_tax_details(result_data)
    party_names = parse_party_names(result_data)
    oracle_rows, missing_estimations = fetch_oracle_expected_rows(
        cursor,
        estimation_numbers,
    )
    comparisons = match_oracle_rows_to_order_lines(
        oracle_rows,
        order_lines,
        tax_details,
    )
    for comparison in comparisons:
        line_status_before_party = comparison.get("status")
        party_result = compare_party_name(
            (comparison.get("oracle_expected") or {}).get("party_name"),
            party_names,
        )
        if (
            not party_result["match"]
            and line_status_before_party == "MATCH"
            and party_result.get("partial_match_available")
        ):
            party_result.update(
                match=True,
                matched_value=party_result.get(
                    "partial_matched_value"
                ),
                comparison_method=(
                    "PARTIAL_OCR_WITH_FULL_ORDER_LINE_MATCH"
                ),
            )
            party_result.pop("reason", None)
        comparison["party_name_comparison"] = (
            "MATCH" if party_result["match"] else "MISMATCH"
        )
        comparison.setdefault("field_results", {})[
            "party_name"
        ] = party_result
        if not party_result["match"]:
            mismatched_fields = comparison.setdefault(
                "mismatched_fields",
                [],
            )
            if "party_name" not in mismatched_fields:
                mismatched_fields.append("party_name")
            if comparison.get("status") == "MATCH":
                comparison["status"] = "MISMATCH"
    final_status = (
        "PASS"
        if (
            oracle_rows
            and not missing_estimations
            and comparisons
            and all(item.get("status") == "MATCH" for item in comparisons)
        )
        else "REVIEW_REQUIRED"
    )
    return {
        "analyzer_id": os.getenv("CONTENTUNDERSTANDING_ANALYZER_ID", ""),
        "estimation_numbers": [str(value) for value in estimation_numbers],
        "party_names": party_names,
        "tax_details": tax_details,
        "order_lines": order_lines,
        "filtered_values": filtered_values,
        "oracle_expected_rows": oracle_rows,
        "oracle_missing_estimations": missing_estimations,
        "comparisons": comparisons,
        "final_status": final_status,
        "database_operation": "SELECT_ONLY",
    }


def validation_failure_message(validation: Mapping[str, Any]) -> str:
    reasons: list[str] = []
    missing = validation.get("oracle_missing_estimations") or []
    if missing:
        reasons.append("no Oracle row for EST " + ", ".join(map(str, missing)))
    if not validation.get("order_lines"):
        reasons.append("no OrderLines extracted")
    for comparison in validation.get("comparisons") or []:
        status = comparison.get("status")
        if status == "MATCH":
            continue
        estimation = comparison.get("estimation_number") or "unknown"
        mismatches = comparison.get("mismatched_fields") or []
        if status not in {"MATCH", "MISMATCH"}:
            detail = str(status)
            if mismatches:
                detail += " (" + ", ".join(mismatches) + ")"
        else:
            detail = ", ".join(mismatches) if mismatches else str(status)
        reasons.append(f"EST {estimation}: {detail}")
    return "; ".join(reasons) or "Content Understanding validation did not pass"


def build_po_document_detail_rows(
    validation: Mapping[str, Any],
    estimation_order_mappings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build canonical detail values after every comparison has passed."""
    order_by_estimation = {
        str(mapping.get("estimation_no")): str(
            mapping.get("regular_order_number") or ""
        ).strip()
        for mapping in estimation_order_mappings
    }
    rows: list[dict[str, Any]] = []
    for comparison in validation.get("comparisons") or []:
        if comparison.get("status") != "MATCH":
            raise ValueError(
                "PO detail rows can be built only from MATCH comparisons"
            )
        estimation_number = str(
            comparison.get("estimation_number") or ""
        ).strip()
        regular_order_number = order_by_estimation.get(
            estimation_number,
            "",
        )
        if not estimation_number or not regular_order_number:
            raise ValueError(
                f"Missing EST-to-order mapping for {estimation_number or 'unknown'}"
            )
        expected = comparison.get("oracle_expected") or {}
        field_results = comparison.get("field_results") or {}
        party_result = field_results.get("party_name") or {}
        rate_result = field_results.get("confirm_rate") or {}
        party_name = str(
            expected.get("party_name")
            or party_result.get("matched_value")
            or ""
        ).strip()
        reference_number = str(
            expected.get("reference_number") or ""
        ).strip()
        required_quantity = str(
            expected.get("required_quantity") or ""
        ).strip()
        count_name = str(expected.get("count_name") or "").strip()
        certification_value = expected.get("certification")
        certification = (
            str(certification_value).strip()
            if certification_value is not None
            and str(certification_value).strip()
            else None
        )
        net_rate = str(rate_result.get("matched_pdf_rate") or "").strip()
        if (
            not party_name
            or not reference_number
            or not required_quantity
            or not count_name
            or not net_rate
        ):
            raise ValueError(
                f"Required PO detail value is missing for EST {estimation_number}"
            )
        rows.append(
            {
                "estimation_number": int(estimation_number),
                "regular_order_number": regular_order_number,
                "party_name": party_name,
                "reference_number": reference_number,
                "required_quantity": required_quantity,
                "count_name": count_name,
                "certification": certification,
                "net_rate": net_rate,
            }
        )
    return rows


def build_extracted_po_document_detail_rows(
    result_data: Mapping[str, Any],
    estimation_order_mappings: Iterable[Mapping[str, Any]],
    expected_party_by_estimation: Mapping[str, Any] | None = None,
    certificate_master_names: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build PO detail rows directly from Content Understanding output.

    Oracle is used only to resolve the stamped estimation number to its
    regular-order parent. No CostEstimation, YarnCount, PartyMaster, booking
    rate, quantity, count, certification, reference, or party value is used.
    """

    mappings: list[dict[str, Any]] = []
    for raw_mapping in estimation_order_mappings:
        estimation_number = str(
            raw_mapping.get("estimation_no") or ""
        ).strip()
        regular_order_number = str(
            raw_mapping.get("regular_order_number") or ""
        ).strip()
        reference_values = raw_mapping.get(
            "reference_order_numbers"
        ) or []
        if isinstance(reference_values, str):
            reference_values = [reference_values]
        reference_keys = {
            normalize_reference_number(value)
            for value in reference_values
            if normalize_reference_number(value)
        }
        reference_keys.add(
            normalize_reference_number(regular_order_number)
        )
        if not estimation_number or not regular_order_number:
            raise ValueError("Incomplete EST-to-order mapping")
        mappings.append(
            {
                "estimation_number": estimation_number,
                "regular_order_number": regular_order_number,
                "reference_keys": reference_keys,
            }
        )

    raw_lines = parse_order_lines(result_data)
    order_lines, filtered_values = sanitize_order_lines(
        raw_lines,
        [int(mapping["estimation_number"]) for mapping in mappings],
    )
    if not order_lines:
        raise ValueError("Content Understanding returned no OrderLines")
    if not mappings:
        raise ValueError("No stamped EST-to-order mappings were resolved")

    party_names = parse_party_names(result_data)
    if not party_names:
        raise ValueError("Content Understanding returned no PartyName")

    def joined_values(
        values: Iterable[Any],
        *,
        field_name: str,
        maximum_length: int,
        required: bool = True,
    ) -> str | None:
        cleaned = ordered_unique(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
        if not cleaned:
            if required:
                raise ValueError(
                    f"Content Understanding returned no {field_name}"
                )
            return None
        result = ", ".join(cleaned)
        if len(result) > maximum_length:
            raise ValueError(
                f"Extracted {field_name} exceeds the "
                f"{maximum_length}-character database limit"
            )
        return result

    def one_numeric_value(values: Iterable[Any], field_name: str) -> str:
        parsed: list[Decimal] = []
        for value in values:
            parsed.extend(decimal_values(value))
        distinct = list(dict.fromkeys(parsed))
        if len(distinct) != 1:
            raise ValueError(
                f"Each extracted order line requires exactly one "
                f"{field_name}; found {len(distinct)} distinct value(s)"
            )
        return format(distinct[0], "f")

    expected_parties = {
        str(key): str(value).strip()
        for key, value in (expected_party_by_estimation or {}).items()
        if str(value).strip()
    }
    certificate_masters = list(certificate_master_names or [])

    assigned_estimations: set[str] = set()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(order_lines, start=1):
        line_reference_values = line.get("reference_number") or []
        line_reference_keys = {
            normalize_reference_number(value)
            for value in line_reference_values
            if normalize_reference_number(value)
        }

        if len(mappings) == 1:
            mapping = mappings[0]
        else:
            candidates = [
                item
                for item in mappings
                if line_reference_keys & item["reference_keys"]
            ]
            if len(candidates) != 1:
                raise ValueError(
                    "Cannot uniquely associate extracted order line "
                    f"{line_number} with a stamped estimation number "
                    "using its extracted reference number"
                )
            mapping = candidates[0]

        estimation_number = mapping["estimation_number"]
        if estimation_number in assigned_estimations:
            raise ValueError(
                "Multiple extracted order lines resolved to EST "
                f"{estimation_number}; the detail table permits one row "
                "per estimation and parent document"
            )
        assigned_estimations.add(estimation_number)

        expected_party = expected_parties.get(estimation_number)
        if expected_parties and not expected_party:
            raise ValueError(
                f"No expected Oracle party was loaded for EST "
                f"{estimation_number}"
            )
        if expected_party:
            party_match = compare_party_name(
                expected_party,
                party_names,
            )
            if (
                not party_match["match"]
                and party_match.get("partial_match_available")
            ):
                party_match.update(
                    match=True,
                    matched_value=party_match.get(
                        "partial_matched_value"
                    ),
                    comparison_method="PARTIAL_OCR_PARTY_NAME",
                )
                party_match.pop("reason", None)
            if not party_match["match"]:
                raise ValueError(
                    "Extracted PartyName does not match the Oracle party "
                    f"for EST {estimation_number}"
                )
            party_name = str(
                party_match.get("matched_value") or ""
            ).strip()
        else:
            party_name = joined_values(
                party_names,
                field_name="PartyName",
                maximum_length=300,
            )
            party_match = {
                "match": None,
                "comparison_method": "NOT_REQUESTED",
                "matched_value": party_name,
            }

        if len(party_name) > 300:
            raise ValueError(
                "Extracted PartyName exceeds the 300-character "
                "database limit"
            )

        reference_number = joined_values(
            line_reference_values,
            field_name=f"ReferenceNumber on line {line_number}",
            maximum_length=100,
        )
        count_name = joined_values(
            line.get("count") or [],
            field_name=f"Count on line {line_number}",
            maximum_length=100,
        )
        raw_certification = line.get("certification") or []
        if certificate_masters:
            certificate_match = match_certificate_master_names(
                raw_certification,
                certificate_masters,
            )
            certification = certificate_match["stored_value"]
        else:
            certification = joined_values(
                raw_certification,
                field_name=f"Certification on line {line_number}",
                maximum_length=300,
                required=False,
            )
            certificate_match = {
                "extracted": list(raw_certification),
                "matched_master_names": (
                    [certification] if certification else []
                ),
                "ambiguous": [],
                "stored_value": certification,
                "comparison_method": "NOT_REQUESTED",
            }

        detail_row = {
            "estimation_number": int(estimation_number),
            "regular_order_number": mapping[
                "regular_order_number"
            ],
            "party_name": party_name,
            "reference_number": reference_number,
            "required_quantity": one_numeric_value(
                line.get("required_quantity") or [],
                f"RequiredQuantity on line {line_number}",
            ),
            "count_name": count_name,
            "certification": certification,
            "net_rate": one_numeric_value(
                line.get("confirm_rate") or [],
                f"ConfirmRate on line {line_number}",
            ),
        }
        if expected_parties:
            detail_row["party_match"] = party_match
        if certificate_masters:
            detail_row["certificate_match"] = certificate_match
        rows.append(detail_row)

    missing_estimations = [
        mapping["estimation_number"]
        for mapping in mappings
        if mapping["estimation_number"] not in assigned_estimations
    ]
    if missing_estimations:
        raise ValueError(
            "No extracted order line was associated with stamped EST "
            + ", ".join(missing_estimations)
        )

    return rows
