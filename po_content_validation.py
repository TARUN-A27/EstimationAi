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
CONTENT_UNDERSTANDING_SOURCE = "azure_ai_content_understanding"


class ContentAnalysisError(RuntimeError):
    """A safe, user-facing Content Understanding workflow failure."""


def _audit(
    audit: Any,
    stage: str,
    status: str,
    message: str,
    details=None,
) -> None:
    if audit is None:
        return
    try:
        audit(stage, status, message, details)
    except Exception:
        # Workflow logging is deliberately best-effort.
        return


def analyzer_configuration(
    expected_document_type: str | None,
) -> tuple[str, str]:
    """Resolve an analyzer without exposing or hardcoding its identifier."""
    analyzer_names = {
        "estimation": "CONTENTUNDERSTANDING_ESTIMATION_ANALYZER_ID",
        "mix": "CONTENTUNDERSTANDING_MIXING_ANALYZER_ID",
        "po": "CONTENTUNDERSTANDING_PO_ANALYZER_ID",
    }
    candidates: list[str] = []
    if expected_document_type in analyzer_names:
        candidates.append(analyzer_names[expected_document_type])
    candidates.extend(
        [
            "CONTENTUNDERSTANDING_ROUTER_ANALYZER_ID",
            "CONTENTUNDERSTANDING_ANALYZER_ID",
        ]
    )
    for environment_name in candidates:
        analyzer_id = os.getenv(environment_name, "").strip()
        if analyzer_id:
            return analyzer_id, environment_name
    raise ContentAnalysisError(
        "AI document analysis is not configured for this document workflow"
    )

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


def analyze_po_document(
    document_path: Path,
    *,
    analyzer_id: str | None = None,
    analyzer_config_name: str = "CONTENTUNDERSTANDING_ANALYZER_ID",
    audit: Any = None,
) -> dict[str, Any]:
    """Submit the original document bytes and return the plain AI response."""
    try:
        from azure.ai.contentunderstanding import ContentUnderstandingClient
        from azure.core.credentials import AzureKeyCredential
    except ModuleNotFoundError as exc:
        raise ContentAnalysisError(
            "AI document analysis is unavailable in the application environment"
        ) from exc

    endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT", "").strip()
    key = os.getenv("CONTENTUNDERSTANDING_KEY", "").strip()
    analyzer_id = analyzer_id or os.getenv(analyzer_config_name, "").strip()
    missing = [
        name
        for name, value in (
            ("CONTENTUNDERSTANDING_ENDPOINT", endpoint),
            ("CONTENTUNDERSTANDING_KEY", key),
            (analyzer_config_name, analyzer_id),
        )
        if not value
    ]
    if missing:
        raise ContentAnalysisError(
            "AI document analysis configuration is incomplete"
        )

    document_path = Path(document_path)
    if not document_path.is_file():
        raise ContentAnalysisError(
            "The uploaded PDF is unavailable for analysis"
        )
    if document_path.suffix.lower() != ".pdf":
        raise ContentAnalysisError("AI document analysis accepts PDF uploads only")
    mime_type = (
        mimetypes.guess_type(document_path.name)[0]
        or "application/octet-stream"
    )
    try:
        request_timeout = float(
            os.getenv("CONTENTUNDERSTANDING_REQUEST_TIMEOUT_SECONDS", "30")
        )
        analysis_timeout = float(
            os.getenv("CONTENTUNDERSTANDING_ANALYSIS_TIMEOUT_SECONDS", "300")
        )
        if request_timeout <= 0 or analysis_timeout <= 0:
            raise ValueError
    except ValueError as exc:
        raise ContentAnalysisError(
            "AI document analysis timeout configuration is invalid"
        ) from exc

    client = None
    request_accepted = False
    try:
        client = ContentUnderstandingClient(
            endpoint=endpoint.rstrip("/"),
            credential=AzureKeyCredential(key),
            api_version="2025-11-01",
            polling_interval=2,
            connection_timeout=request_timeout,
            read_timeout=request_timeout,
        )
        _audit(
            audit,
            "ai_analysis_request",
            "processing",
            "AI analysis request started",
            {"analyzer_configuration": analyzer_config_name},
        )
        pdf_bytes = document_path.read_bytes()
        if not pdf_bytes:
            raise ContentAnalysisError("The uploaded PDF is empty")
        _audit(
            audit,
            "ai_pdf_submission",
            "processing",
            "Submitting PDF directly to the AI analyzer",
            {"file_size_bytes": len(pdf_bytes)},
        )
        poller = client.begin_analyze_binary(
            analyzer_id=analyzer_id,
            binary_input=pdf_bytes,
            content_type=mime_type,
        )
        request_accepted = True
        _audit(audit, "ai_pdf_submission", "completed", "PDF submitted")
        _audit(
            audit,
            "ai_request_accepted",
            "completed",
            "Analyzer request accepted",
        )
        _audit(
            audit,
            "ai_polling",
            "processing",
            "AI analysis polling started",
        )
        result_data = to_plain(poller.result(timeout=analysis_timeout))
        status_member = getattr(poller, "status", "succeeded")
        status = str(
            status_member() if callable(status_member) else status_member
        ).lower()
        if status in {"failed", "canceled", "cancelled"}:
            _audit(
                audit,
                "ai_polling",
                "failed",
                "AI analysis polling ended without success",
            )
            _audit(
                audit,
                "ai_response",
                "failed",
                "AI analyzer returned a failed or cancelled operation",
            )
            _audit(
                audit,
                "ai_analysis_request",
                "failed",
                "AI analysis request did not complete",
            )
            raise ContentAnalysisError(
                "AI document analysis did not complete successfully"
            )
        _audit(
            audit,
            "ai_polling",
            "completed",
            "AI analysis polling completed",
        )
        _audit(audit, "ai_response", "completed", "AI response received")
        _audit(
            audit,
            "ai_analysis_request",
            "completed",
            "AI analysis request completed",
        )
        return result_data
    except TimeoutError as exc:
        _audit(
            audit,
            "ai_polling",
            "failed",
            "AI document analysis timed out",
        )
        _audit(
            audit,
            "ai_response",
            "failed",
            "AI response was not received before the timeout",
        )
        _audit(
            audit,
            "ai_analysis_request",
            "failed",
            "AI analysis request timed out",
        )
        raise ContentAnalysisError("AI document analysis timed out") from exc
    except ContentAnalysisError:
        raise
    except Exception as exc:
        if not request_accepted:
            _audit(
                audit,
                "ai_pdf_submission",
                "failed",
                "PDF submission to the AI analyzer failed",
            )
            _audit(
                audit,
                "ai_request_accepted",
                "failed",
                "AI analyzer did not accept the request",
            )
        _audit(audit, "ai_response", "failed", "AI document analysis failed")
        _audit(
            audit,
            "ai_analysis_request",
            "failed",
            "AI analysis request failed",
        )
        raise ContentAnalysisError(
            "AI document analysis failed; the upload was not processed"
        ) from exc
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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


def named_field(fields: Mapping[str, Any], *names: str) -> Any:
    by_key = {identifier_key(key): value for key, value in fields.items()}
    for name in names:
        value = by_key.get(identifier_key(name))
        if value is not None:
            return value
    return None


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
        order_lines_field = (
            named_field(fields, "OrderLines", "OrderLineItems") or {}
        )
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
                    "estimate_number": field_values(
                        named_field(
                            value_object,
                            "EstimateNumber",
                            "EstimationNumber",
                            "EstimateNo",
                            "EstNo",
                        )
                    ),
                    "party_name": field_values(
                        named_field(value_object, "PartyName")
                    ),
                    "count": field_values(
                        named_field(value_object, "Count", "CountName")
                    ),
                    "required_quantity": field_values(
                        named_field(value_object, "RequiredQuantity")
                    ),
                    "reference_number": field_values(
                        named_field(value_object, "ReferenceNumber")
                    ),
                    "confirm_rate": field_values(
                        named_field(value_object, "NetRate", "ConfirmRate")
                    ),
                    "certification": field_values(
                        named_field(
                            value_object,
                            "Certification",
                            "CertificateRateIncludedHeatSettings",
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
        names.extend(field_values(named_field(fields, "PartyName")))
    return ordered_unique(name for name in names if name)


def parse_estimation_mappings(
    result_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Parse semantic EST/reference pairs without altering stored values."""
    mappings: list[dict[str, Any]] = []
    for fields in content_fields(result_data):
        mapping_field = named_field(
            fields,
            "EstimationMappings",
            "EstimationMapping",
            "EstimateMappings",
            "EstimateMapping",
            "ESTMappings",
        )
        if not isinstance(mapping_field, Mapping):
            continue
        for item in mapping_field.get("valueArray") or []:
            if not isinstance(item, Mapping):
                continue
            value_object = item.get("valueObject") or {}
            if not isinstance(value_object, Mapping):
                continue
            estimate_values = field_values(
                named_field(
                    value_object,
                    "EstimateNumber",
                    "EstimationNumber",
                    "EstimateNo",
                    "EstNo",
                )
            )
            normalized_estimates = ordered_unique(
                str(number)
                for number in (
                    normalize_estimation_number(value)
                    for value in estimate_values
                )
                if number is not None
            )
            reference_numbers = ordered_unique(
                value
                for value in field_values(
                    named_field(
                        value_object,
                        "ReferenceNumber",
                        "ReferenceNo",
                        "RefNumber",
                        "RefNo",
                    )
                )
                if value
            )
            mappings.append(
                {
                    "estimate_number": (
                        int(normalized_estimates[0])
                        if len(normalized_estimates) == 1
                        else None
                    ),
                    "reference_number": (
                        reference_numbers[0]
                        if len(reference_numbers) == 1
                        else None
                    ),
                    "reference_numbers": reference_numbers,
                }
            )
    return mappings


def normalize_document_type(value: Any) -> str:
    key = identifier_key(value)
    aliases = {
        "EST": "estimation",
        "ESTIMATION": "estimation",
        "ESTIMATIONSHEET": "estimation",
        "MIX": "mix",
        "MIXING": "mix",
        "MIXINGSHEET": "mix",
        "PO": "po",
        "PURCHASEORDER": "po",
        "PURCHASEORDERSHEET": "po",
        "FULLPURCHASEORDER": "po",
        "FULLPURCHASEORDERSHEET": "po",
        "PODOCUMENT": "po",
        "POSHEET": "po",
        "UNKNOWN": "unknown",
        "OTHER": "unknown",
    }
    return aliases.get(key, "unknown")


def normalize_estimation_number(value: Any) -> int | None:
    """Normalize one semantic EST field without mining arbitrary text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        digits = str(value)
    elif isinstance(value, float) and value.is_integer():
        digits = str(int(value))
    else:
        text = str(value).strip().replace(",", "")
        matches = re.findall(r"(?<!\d)\d{5,7}(?!\d)", text)
        if len(matches) != 1:
            return None
        digits = matches[0]
    if not re.fullmatch(r"\d{5,7}", digits):
        return None
    return int(digits)


def normalize_content_understanding_result(
    result_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert the configured analyzer response to the production contract."""
    if not isinstance(result_data, Mapping):
        raise ContentAnalysisError(
            "AI document analysis returned a malformed response"
        )
    contents = result_data.get("contents")
    if not isinstance(contents, list) or not contents:
        raise ContentAnalysisError("AI document analysis returned no document content")

    type_values: list[str] = []
    primary_estimation_values: list[Any] = []
    document_estimation_values: list[Any] = []
    warnings: list[str] = []
    raw_warnings = result_data.get("warnings") or []
    if isinstance(raw_warnings, list):
        warnings.extend("Analyzer reported a warning" for _ in raw_warnings)

    estimation_field_names = (
        "EstimationNumbers",
        "EstimateNumbers",
        "ESTNumbers",
        "ESTNumber",
        "EstimationNumber",
        "EstimateNumber",
        "EstimationNo",
        "EstimateNo",
        "EstNo",
    )
    for content in contents:
        if not isinstance(content, Mapping):
            raise ContentAnalysisError(
                "AI document analysis returned malformed content"
            )
        category = content.get("category")
        if category:
            type_values.append(str(category))
        fields = content.get("fields") or {}
        if not isinstance(fields, Mapping):
            raise ContentAnalysisError(
                "AI document analysis returned malformed fields"
            )
        type_values.extend(
            field_values(named_field(fields, "DocumentType", "DocumentCategory"))
        )
        primary_estimation_values.extend(
            field_values(
                named_field(
                    fields,
                    "PrimaryEstimationNumber",
                    "PrimaryEstimateNumber",
                    "PrimaryEstimationNo",
                    "PrimaryEstimateNo",
                    "PrimaryEstNo",
                )
            )
        )
        for field_name in estimation_field_names:
            document_estimation_values.extend(
                field_values(named_field(fields, field_name))
            )

    order_lines = parse_order_lines(result_data)
    estimation_mappings = parse_estimation_mappings(result_data)

    document_types = ordered_unique(
        normalized
        for normalized in (normalize_document_type(value) for value in type_values)
        if normalized != "unknown"
    )
    if len(document_types) > 1:
        document_type = "ambiguous"
    elif document_types:
        document_type = document_types[0]
    else:
        document_type = "unknown"

    primary_estimation_numbers = ordered_unique(
        str(number)
        for number in (
            normalize_estimation_number(value)
            for value in primary_estimation_values
        )
        if number is not None
    )
    primary_estimation_number = (
        int(primary_estimation_numbers[0])
        if len(primary_estimation_numbers) == 1
        else None
    )
    primary_estimation_present = bool(primary_estimation_values)
    legacy_estimation_numbers = ordered_unique(
        str(number)
        for number in (
            normalize_estimation_number(value)
            for value in document_estimation_values
        )
        if number is not None
    )
    line_estimation_numbers = ordered_unique(
        str(number)
        for line in order_lines
        for number in (
            normalize_estimation_number(value)
            for value in (line.get("estimate_number") or [])
        )
        if number is not None
    )
    mapping_estimation_numbers = ordered_unique(
        str(mapping["estimate_number"])
        for mapping in estimation_mappings
        if mapping.get("estimate_number") is not None
    )

    if document_type in {"estimation", "mix"}:
        if primary_estimation_number is not None:
            effective_estimation_numbers = [
                str(primary_estimation_number)
            ]
        elif (
            not primary_estimation_present
            and len(legacy_estimation_numbers) == 1
        ):
            effective_estimation_numbers = legacy_estimation_numbers
        else:
            effective_estimation_numbers = []
            if primary_estimation_present:
                warnings.append(
                    "Primary estimation value is invalid or ambiguous"
                )
            elif len(legacy_estimation_numbers) > 1:
                warnings.append(
                    "Multiple legacy estimation candidates require review"
                )
        document_estimation_numbers = effective_estimation_numbers
    elif document_type == "po":
        document_estimation_numbers = ordered_unique(
            [*legacy_estimation_numbers, *mapping_estimation_numbers]
        )
        effective_estimation_numbers = ordered_unique(
            [*document_estimation_numbers, *line_estimation_numbers]
        )
    else:
        document_estimation_numbers = ordered_unique(
            [*legacy_estimation_numbers, *mapping_estimation_numbers]
        )
        effective_estimation_numbers = []

    return {
        "document_type": document_type,
        "primary_estimation_present": primary_estimation_present,
        "primary_estimation_number": primary_estimation_number,
        "estimation_mappings": estimation_mappings,
        "legacy_estimation_numbers": [
            int(value) for value in legacy_estimation_numbers
        ],
        "mapping_estimation_numbers": [
            int(value) for value in mapping_estimation_numbers
        ],
        # Compatibility key: workflow-authoritative semantic EST values.
        "estimation_numbers": [
            int(value) for value in effective_estimation_numbers
        ],
        "document_estimation_numbers": [
            int(value) for value in document_estimation_numbers
        ],
        "line_estimation_numbers": [
            int(value) for value in line_estimation_numbers
        ],
        "order_lines": order_lines,
        "party_names": parse_party_names(result_data),
        "analysis_source": CONTENT_UNDERSTANDING_SOURCE,
        "warnings": warnings,
    }


def analyze_uploaded_document(
    document_path: Path,
    *,
    expected_document_type: str | None = None,
    audit: Any = None,
) -> dict[str, Any]:
    """Single paid-analysis boundary used by every upload workflow."""
    analyzer_id, config_name = analyzer_configuration(expected_document_type)
    raw_result = analyze_po_document(
        document_path,
        analyzer_id=analyzer_id,
        analyzer_config_name=config_name,
        audit=audit,
    )
    try:
        normalized = normalize_content_understanding_result(raw_result)
    except ContentAnalysisError:
        _audit(
            audit,
            "ai_normalization",
            "failed",
            "AI response normalization failed",
        )
        raise
    normalized["analyzer_configuration"] = config_name
    _audit(
        audit,
        "document_classification",
        (
            "completed"
            if normalized["document_type"] not in {"unknown", "ambiguous"}
            else "failed"
        ),
        f"AI classified document as {normalized['document_type']}",
        {
            "document_type": normalized["document_type"],
            "classification_source": CONTENT_UNDERSTANDING_SOURCE,
        },
    )
    _audit(
        audit,
        "ai_extraction_counts",
        "completed",
        "AI semantic fields extracted",
        {
            "primary_estimation_present": (
                normalized["primary_estimation_present"]
            ),
            "estimation_mapping_count": len(
                normalized["estimation_mappings"]
            ),
            "document_estimation_count": len(
                normalized["document_estimation_numbers"]
            ),
            "line_estimation_count": len(
                normalized["line_estimation_numbers"]
            ),
            "order_line_count": len(normalized["order_lines"]),
        },
    )
    _audit(
        audit,
        "ai_normalization",
        "completed",
        "AI extraction normalization completed",
        {
            "primary_estimation_present": (
                normalized["primary_estimation_present"]
            ),
            "estimation_mapping_count": len(
                normalized["estimation_mappings"]
            ),
            "document_estimation_count": len(
                normalized["document_estimation_numbers"]
            ),
            "line_estimation_count": len(
                normalized["line_estimation_numbers"]
            ),
            "order_line_count": len(normalized["order_lines"]),
            "warning_count": len(normalized["warnings"]),
        },
    )
    return normalized


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


def build_extracted_po_document_detail_result(
    result_data: Mapping[str, Any],
    estimation_order_mappings: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build independently insertable PO lines from analyzer output.

    Oracle is used only to resolve the stamped estimation number to its
    regular-order parent. No CostEstimation, YarnCount, PartyMaster, booking
    rate, quantity, count, certification, reference, or party value is used.

    Missing extracted business fields are represented by ``None``. A line is
    rejected only when its parent cannot be resolved in a multi-EST document,
    a numeric field contains more than one distinct value, or extracted text
    cannot fit the target column. Rejection of one line never discards another
    independently valid line.
    """

    mappings: list[dict[str, Any]] = []
    for raw_mapping in estimation_order_mappings:
        estimation_number = str(
            raw_mapping.get("estimation_no") or ""
        ).strip()
        regular_order_number = str(
            raw_mapping.get("regular_order_number") or ""
        ).strip()
        if not estimation_number or not regular_order_number:
            raise ValueError("Incomplete EST-to-order mapping")
        mappings.append(
            {
                "estimation_number": estimation_number,
                "regular_order_number": regular_order_number,
            }
        )

    # Production detail storage preserves the analyzer output. The legacy
    # sanitization/comparison path remains available for diagnostics only.
    if (
        result_data.get("analysis_source") == CONTENT_UNDERSTANDING_SOURCE
        and isinstance(result_data.get("order_lines"), list)
    ):
        order_lines = list(result_data["order_lines"])
        party_names = list(result_data.get("party_names") or [])
    else:
        order_lines = parse_order_lines(result_data)
        party_names = parse_party_names(result_data)
    if not order_lines:
        raise ValueError("Content Understanding returned no OrderLines")
    if not mappings:
        raise ValueError("No stamped EST-to-order mappings were resolved")

    def joined_values(
        values: Iterable[Any],
        *,
        field_name: str,
        maximum_length: int,
    ) -> str | None:
        cleaned = ordered_unique(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
        if not cleaned:
            return None
        result = ", ".join(cleaned)
        if len(result) > maximum_length:
            raise ValueError(
                f"Extracted {field_name} exceeds the "
                f"{maximum_length}-character database limit"
            )
        return result

    def one_numeric_value(
        values: Iterable[Any],
        field_name: str,
    ) -> str | None:
        parsed: list[Decimal] = []
        for value in values:
            parsed.extend(decimal_values(value))
        distinct = list(dict.fromkeys(parsed))
        if not distinct:
            return None
        if len(distinct) != 1:
            raise ValueError(
                f"Extracted {field_name} contains "
                f"{len(distinct)} distinct numeric values"
            )
        return format(distinct[0], "f")

    mapping_by_estimation = {
        mapping["estimation_number"]: mapping
        for mapping in mappings
    }
    reference_to_estimations: dict[str, set[str]] = {}
    for semantic_mapping in result_data.get("estimation_mappings") or []:
        estimation_number = semantic_mapping.get("estimate_number")
        if estimation_number is None:
            continue
        estimation_key = str(int(estimation_number))
        if estimation_key not in mapping_by_estimation:
            continue
        for reference in semantic_mapping.get("reference_numbers") or []:
            reference_key = identifier_key(reference)
            if reference_key:
                reference_to_estimations.setdefault(
                    reference_key,
                    set(),
                ).add(estimation_key)

    def extracted_estimation_keys(values: Iterable[Any]) -> set[str]:
        keys: set[str] = set()
        for value in values:
            number = normalize_estimation_number(value)
            if number is not None:
                keys.add(str(number))
        return keys

    document_party_name = joined_values(
        party_names,
        field_name="PartyName",
        maximum_length=300,
    )

    rows: list[dict[str, Any]] = []
    rejected_lines: list[dict[str, Any]] = []
    association_counts = {
        "direct_line_mapping_count": 0,
        "reference_mapping_count": 0,
        "single_parent_fallback_count": 0,
    }
    for line_number, line in enumerate(order_lines, start=1):
        try:
            raw_estimate_values = line.get("estimate_number") or []
            estimate_keys = extracted_estimation_keys(raw_estimate_values)
            if len(estimate_keys) > 1:
                raise ValueError(
                    "Extracted EstimateNumber is ambiguous on this line"
                )
            if len(estimate_keys) == 1:
                estimate_key = next(iter(estimate_keys))
                mapping = mapping_by_estimation.get(estimate_key)
                if mapping is None:
                    raise ValueError(
                        "Extracted EstimateNumber has no resolved PO parent"
                    )
                association_counts["direct_line_mapping_count"] += 1
            else:
                reference_estimation_keys: set[str] = set()
                for reference in line.get("reference_number") or []:
                    reference_estimation_keys.update(
                        reference_to_estimations.get(
                            identifier_key(reference),
                            set(),
                        )
                    )
                if len(reference_estimation_keys) == 1:
                    mapping = mapping_by_estimation[
                        next(iter(reference_estimation_keys))
                    ]
                    association_counts["reference_mapping_count"] += 1
                elif len(reference_estimation_keys) > 1:
                    raise ValueError(
                        "Extracted ReferenceNumber maps this line to "
                        "multiple resolved EST values"
                    )
                elif len(mappings) == 1:
                    mapping = mappings[0]
                    association_counts[
                        "single_parent_fallback_count"
                    ] += 1
                else:
                    raise ValueError(
                        "Cannot uniquely associate this line with one "
                        "resolved EST using its extracted EstimateNumber "
                        "or ReferenceNumber"
                    )

            detail_row = {
                "estimation_number": int(mapping["estimation_number"]),
                "regular_order_number": mapping[
                    "regular_order_number"
                ],
                "party_name": joined_values(
                    line.get("party_name") or [],
                    field_name=f"PartyName on line {line_number}",
                    maximum_length=300,
                ) or document_party_name,
                "reference_number": joined_values(
                    line.get("reference_number") or [],
                    field_name=f"ReferenceNumber on line {line_number}",
                    maximum_length=100,
                ),
                "required_quantity": one_numeric_value(
                    line.get("required_quantity") or [],
                    f"RequiredQuantity on line {line_number}",
                ),
                "count_name": joined_values(
                    line.get("count") or [],
                    field_name=f"Count on line {line_number}",
                    maximum_length=100,
                ),
                "certification": joined_values(
                    line.get("certification") or [],
                    field_name=f"Certification on line {line_number}",
                    maximum_length=300,
                ),
                "net_rate": one_numeric_value(
                    line.get("confirm_rate") or [],
                    f"ConfirmRate on line {line_number}",
                ),
            }
            rows.append(detail_row)
        except ValueError as exc:
            rejected_lines.append(
                {
                    "source_line_number": line_number,
                    "reason": str(exc),
                }
            )

    return {
        "rows": rows,
        "rejected_lines": rejected_lines,
        "association_counts": association_counts,
    }


def build_extracted_po_document_detail_rows(
    result_data: Mapping[str, Any],
    estimation_order_mappings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only independently valid rows."""

    return build_extracted_po_document_detail_result(
        result_data,
        estimation_order_mappings,
    )["rows"]
