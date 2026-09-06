from __future__ import annotations

from pathlib import Path as EnvPath
from dotenv import load_dotenv

load_dotenv(EnvPath(__file__).resolve().with_name(".env"))

import os
import re
import time
import uuid
from io import BytesIO
from PIL import Image
from pathlib import Path

import oracledb
import pdfplumber
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from azure.cognitiveservices.vision.computervision.models import OperationStatusCodes
from flask import Flask, jsonify, redirect, render_template, request, url_for
from msrest.authentication import CognitiveServicesCredentials
from pdf2image import convert_from_path
from pending_reports import (
    EST_SHEET_PENDING_QUERY,
    MIXING_SHEET_PENDING_QUERY,
    PO_DOCUMENT_PENDING_QUERY,
    REGULAR_ORDER_ESTIMATION_DETAILS_QUERY,
)
from po_content_validation import (
    analyze_uploaded_document,
    build_extracted_po_document_detail_result,
)
from workflow_audit import WorkflowAuditStore
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf"}
MAX_FILES_PER_REQUEST = 50
MAX_PDF_PAGES = 50

LOG_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(
    UPLOAD_FOLDER=str(UPLOAD_DIR),
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_BYTES", 100 * 1024 * 1024)),
)

WORKFLOW_AUDIT = WorkflowAuditStore(
    LOG_DIR / "upload_workflow.jsonl",
    live_log_path=LOG_DIR / "upload_workflow_live.log",
)


class ConfigurationError(RuntimeError):
    pass


class DocumentValidationError(ValueError):
    pass


WORKFLOW_STAGE_LABELS = {
    "uploaded": "Uploaded",
    "ocr": "AI document analysis",
    "content_understanding": "AI document analysis",
    "validation": "PO detail preparation",
    "database_insert": "Database insertion",
}


class UploadWorkflow(dict):
    def __init__(self, workflow_id: str, filename: str) -> None:
        super().__init__({
            name: {
                "label": label,
                "status": "pending",
                "message": "Waiting",
            }
            for name, label in WORKFLOW_STAGE_LABELS.items()
        })
        self.workflow_id = workflow_id
        self.filename = filename
        self.started_monotonic = time.monotonic()


def new_upload_workflow(
    filename: str,
    workflow_id: str | None = None,
) -> UploadWorkflow:
    workflow = UploadWorkflow(
        workflow_id or str(uuid.uuid4()),
        filename,
    )
    event = WORKFLOW_AUDIT.start(workflow.workflow_id, filename)
    app.logger.info(
        "WORKFLOW id=%s file=%s stage=%s status=%s message=%s",
        workflow.workflow_id,
        filename,
        event["stage"],
        event["status"],
        event["message"],
    )
    return workflow


def record_workflow_event(
    workflow: dict | None,
    stage: str,
    status: str,
    message: str,
    details=None,
) -> None:
    if not isinstance(workflow, UploadWorkflow):
        return
    event = WORKFLOW_AUDIT.record(
        workflow.workflow_id,
        filename=workflow.filename,
        stage=stage,
        label=WORKFLOW_STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
        status=status,
        message=message,
        details=details,
    )
    app.logger.info(
        "WORKFLOW id=%s file=%s stage=%s status=%s message=%s",
        workflow.workflow_id,
        workflow.filename,
        event["stage"],
        event["status"],
        event["message"],
    )


def public_workflow_id(workflow: dict | None) -> str | None:
    if isinstance(workflow, UploadWorkflow):
        return workflow.workflow_id
    return None


def workflow_duration_ms(workflow: dict | None) -> int | None:
    if isinstance(workflow, UploadWorkflow):
        return round(
            (time.monotonic() - workflow.started_monotonic) * 1000
        )
    return None


def set_workflow_stage(
    workflow: dict,
    stage: str,
    status: str,
    message: str,
    details=None,
) -> None:
    item = workflow.setdefault(
        stage,
        {"label": WORKFLOW_STAGE_LABELS.get(stage, stage)},
    )
    item.update({"status": status, "message": message})
    if details is not None:
        item["details"] = details
    record_workflow_event(
        workflow,
        stage,
        status,
        message,
        details,
    )


def fail_active_workflow_stage(workflow: dict, message: str) -> None:
    for stage in WORKFLOW_STAGE_LABELS:
        item = workflow.get(stage) or {}
        if item.get("status") == "processing":
            set_workflow_stage(
                workflow,
                stage,
                "failed",
                message,
            )
            return


def block_pending_workflow_stages(workflow: dict, message: str) -> None:
    for stage in WORKFLOW_STAGE_LABELS:
        item = workflow.get(stage) or {}
        if item.get("status") == "pending":
            set_workflow_stage(
                workflow,
                stage,
                "blocked",
                message,
            )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def batch_database_insert_enabled() -> bool:
    return os.getenv(
        "BATCH_DATABASE_INSERT_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}


def configure_oracle_client() -> None:
    lib_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "").strip()
    try:
        if lib_dir:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        else:
            oracledb.init_oracle_client()
    except oracledb.ProgrammingError as exc:
        # A process may import this module after another module initialized the client.
        if "already been initialized" not in str(exc).lower():
            raise


configure_oracle_client()


def get_db_connection():
    try:
        port = int(required_env("SCM_DB_PORT"))
    except ValueError as exc:
        raise ConfigurationError("SCM_DB_PORT must be numeric") from exc
    dsn = oracledb.makedsn(
        required_env("SCM_DB_HOST"),
        port,
        service_name=required_env("SCM_DB_SERVICE"),
    )
    return oracledb.connect(
        user=required_env("SCM_DB_USER"),
        password=required_env("SCM_DB_PASSWORD"),
        dsn=dsn,
    )


def get_azure_client() -> ComputerVisionClient:
    return ComputerVisionClient(
        required_env("AZURE_OCR_ENDPOINT"),
        CognitiveServicesCredentials(required_env("AZURE_OCR_KEY")),
    )


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def unique(values):
    seen = set()
    return [value for value in values if not (value in seen or seen.add(value))]


def plan_existing_po_replacement(
    order_no: str,
    parent_rows: list[tuple],
    detail_rows: list[tuple],
) -> dict | None:
    """Validate existing PO rows and return a safe replacement plan."""
    order_key = order_no.strip().upper()

    if len(parent_rows) > 1:
        docids = sorted({
            str(row[1])
            for row in parent_rows
            if row[1] is not None
        })
        raise DocumentValidationError(
            "Existing PO data has multiple parent rows for regular "
            f"order {order_no}: DOCIDs {', '.join(docids) or 'NULL'}. "
            "Nothing was changed"
        )

    if not parent_rows:
        if detail_rows:
            raise DocumentValidationError(
                "Existing PO detail data has no matching parent row "
                f"for regular order {order_no}. Nothing was changed"
            )
        return None

    parent_id, parent_docid, parent_filename = parent_rows[0]
    if parent_id is None or parent_docid is None:
        raise DocumentValidationError(
            f"Existing parent key is incomplete for regular order {order_no}. "
            "Nothing was changed"
        )

    inconsistent_details = []
    detail_ids = []
    for detail_id, detail_docid, detail_lmno, estimation_no in detail_rows:
        detail_key = str(detail_lmno or "").strip().upper()
        detail_docid_value = (
            int(detail_docid)
            if detail_docid is not None
            else None
        )
        if (
            detail_docid_value != int(parent_docid)
            or detail_key != order_key
        ):
            inconsistent_details.append(
                f"ID {detail_id}/DOCID {detail_docid}/LMNO "
                f"{detail_lmno or 'NULL'}"
            )
        detail_ids.append(int(detail_id))

    if inconsistent_details:
        raise DocumentValidationError(
            "Existing PO parent/detail linkage is inconsistent for "
            f"regular order {order_no}: "
            + "; ".join(inconsistent_details)
            + ". Nothing was changed"
        )

    return {
        "id": int(parent_id),
        "docid": int(parent_docid),
        "filename": str(parent_filename or "").strip(),
        "detail_ids": detail_ids,
    }


def plan_existing_po_parent(
    order_no: str,
    parent_rows: list[tuple],
) -> dict | None:
    """Resolve one reusable PO parent without inspecting detail data."""
    if len(parent_rows) > 1:
        docids = sorted({
            str(row[1])
            for row in parent_rows
            if row[1] is not None
        })
        raise DocumentValidationError(
            "Existing PO data has multiple parent rows for regular "
            f"order {order_no}: DOCIDs {', '.join(docids) or 'NULL'}. "
            "Nothing was changed"
        )

    if not parent_rows:
        return None

    parent_id, parent_docid, parent_filename = parent_rows[0]
    if parent_id is None or parent_docid is None:
        raise DocumentValidationError(
            f"Existing parent key is incomplete for regular order {order_no}. "
            "Nothing was changed"
        )

    return {
        "id": int(parent_id),
        "docid": int(parent_docid),
        "filename": str(parent_filename or "").strip(),
    }


def rows_as_dicts(cursor):
    columns = [item[0].lower() for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def regular_order_estimation_details(rows):
    """Collapse every REGULARORDER row into one display row per EST number."""
    details_by_estimation = {}

    for row in rows:
        raw_estimation_no = row.get("est_no")
        if raw_estimation_no is None:
            continue

        estimation_key = str(raw_estimation_no).strip()
        detail = details_by_estimation.setdefault(
            estimation_key,
            {
                "est_no": raw_estimation_no,
                "est_date": row.get("est_date"),
                "regular_order_values": [],
                "mixing_sheet_values": [],
                "est_sheet_values": [],
                "po_file_values": [],
            },
        )

        def add_unique(target, value):
            if value is None:
                return
            cleaned = str(value).strip()
            if cleaned and cleaned not in target:
                target.append(cleaned)

        add_unique(
            detail["regular_order_values"],
            row.get("regular_order_no"),
        )

        document_type_code = row.get("document_type_code")
        if document_type_code is not None:
            try:
                document_type_code = int(document_type_code)
            except (TypeError, ValueError):
                document_type_code = None

        if document_type_code == 1:
            add_unique(
                detail["est_sheet_values"],
                row.get("enquiry_filename"),
            )
        elif document_type_code == 2:
            add_unique(
                detail["mixing_sheet_values"],
                row.get("enquiry_filename"),
            )

        add_unique(detail["po_file_values"], row.get("po_filename"))

    details = []

    for detail in details_by_estimation.values():
        details.append(
            {
                "est_no": detail["est_no"],
                "est_date": detail["est_date"],
                "regular_orders": (
                    ", ".join(detail["regular_order_values"])
                    if detail["regular_order_values"]
                    else "N/A"
                ),
                "mixingsheet": (
                    ", ".join(detail["mixing_sheet_values"])
                    if detail["mixing_sheet_values"]
                    else "N/A"
                ),
                "estsheet": (
                    ", ".join(detail["est_sheet_values"])
                    if detail["est_sheet_values"]
                    else "N/A"
                ),
                "po_matching_file": (
                    ", ".join(detail["po_file_values"])
                    if detail["po_file_values"]
                    else "N/A"
                ),
            }
        )

    return details


def azure_read_lines_from_image(image) -> list[dict]:
    """Run Azure OCR and preserve each line's page coordinates."""
    client = get_azure_client()
    retry_delay = 5

    image = image.convert("RGB")

    for attempt in range(5):
        try:
            stream = BytesIO()
            image.save(stream, format="JPEG", quality=95)
            stream.seek(0)

            response = client.read_in_stream(
                stream,
                language="en",
                raw=True,
            )
            operation_id = response.headers[
                "Operation-Location"
            ].split("/")[-1]
            break
        except Exception as exc:
            if "Too Many Requests" in str(exc) and attempt < 4:
                time.sleep(retry_delay)
                continue
            raise RuntimeError(
                f"Azure OCR submission failed: {exc}"
            ) from exc

    while True:
        result = client.get_read_result(operation_id)
        if result.status not in ("notStarted", "running"):
            break
        time.sleep(1)

    if result.status != OperationStatusCodes.succeeded:
        raise RuntimeError(
            f"Azure OCR failed with status {result.status}"
        )

    detected_lines = []

    for page_number, page in enumerate(
        result.analyze_result.read_results,
        start=1,
    ):
        page_width = float(
            getattr(page, "width", None) or image.width
        )
        page_height = float(
            getattr(page, "height", None) or image.height
        )

        for line in page.lines:
            text = line.text.strip()
            box = list(
                getattr(line, "bounding_box", None) or []
            )

            if not text or len(box) < 8:
                continue

            x_values = [float(value) for value in box[0::2]]
            y_values = [float(value) for value in box[1::2]]

            detected_lines.append(
                {
                    "page": page_number,
                    "text": text,
                    "box": box,
                    "left": min(x_values),
                    "right": max(x_values),
                    "top": min(y_values),
                    "bottom": max(y_values),
                    "center_x": (
                        min(x_values) + max(x_values)
                    ) / 2,
                    "center_y": (
                        min(y_values) + max(y_values)
                    ) / 2,
                    "page_width": page_width,
                    "page_height": page_height,
                }
            )

    return detected_lines


def azure_ocr_from_image(image) -> str:
    return "\n".join(
        line["text"]
        for line in azure_read_lines_from_image(image)
    ).strip()


def compact_ocr_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def is_est_stamp_header(value: str) -> bool:
    return compact_ocr_text(value) in {
        "ESTNO",
        "ESTN0",
        "ESTNUMBER",
    }


def is_po_stamp_header(value: str) -> bool:
    """Recognize the right-hand LM NO/PO NO stamp header."""
    return compact_ocr_text(value) in {
        "LMNO",
        "LMN0",
        "LNO",
        "LN0",
        "PONO",
        "PON0",
        "ONO",
        "ON0",
    }


def is_decimal_quantity_candidate(value: str) -> bool:
    """Identify six-digit-looking quantities such as 170.000."""
    return bool(
        re.fullmatch(
            r"\s*\d{1,3}[.,]\d{3}\s*",
            value,
        )
    )


def has_same_row_stamp_order_value(
    lines: list[dict],
    candidate_line: dict,
    po_header: dict,
    header_height: float,
    page_height: float,
    page_width: float,
) -> bool:
    """Return true when an LM/PO-like value accompanies an EST row."""
    order_value_pattern = re.compile(
        r"(?:LM|LMO|DM|DMI|PO)[A-Z0-9/-]*\d{5,9}",
        flags=re.IGNORECASE,
    )

    # Azure can merge both stamp cells into one OCR line.
    if order_value_pattern.search(
        compact_ocr_text(candidate_line["text"])
    ):
        return True

    candidate_height = max(
        candidate_line["bottom"] - candidate_line["top"],
        1.0,
    )
    row_tolerance = max(
        header_height * 2.5,
        candidate_height * 2.5,
        page_height * 0.012,
    )
    right_cell_start = po_header["left"] - page_width * 0.02

    return any(
        line["page"] == candidate_line["page"]
        and line["center_x"] >= right_cell_start
        and abs(
            line["center_y"] - candidate_line["center_y"]
        ) <= row_tolerance
        and order_value_pattern.search(
            compact_ocr_text(line["text"])
        )
        for line in lines
    )


def extract_po_stamp_numbers_from_lines(
    lines: list[dict],
) -> list[int]:
    """
    Extract six-digit values below the EST NO header and inside
    the EST column. Numbers elsewhere on the document are ignored.
    """
    headers = [
        line for line in lines
        if is_est_stamp_header(line["text"])
    ]

    if not headers:
        return []

    selected = []

    for header in headers:
        page = header["page"]
        page_width = header["page_width"]
        page_height = header["page_height"]

        header_width = max(
            header["right"] - header["left"],
            1.0,
        )
        header_height = max(
            header["bottom"] - header["top"],
            1.0,
        )

        same_row_tolerance = max(
            header_height * 2.5,
            page_height * 0.01,
        )

        po_headers = [
            line
            for line in lines
            if (
                line["page"] == page
                and is_po_stamp_header(line["text"])
                and line["center_x"] > header["center_x"]
                and abs(
                    line["center_y"] - header["center_y"]
                ) <= same_row_tolerance
            )
        ]

        left_boundary = max(
            0.0,
            header["left"] - max(
                header_width * 1.25,
                page_width * 0.015,
            ),
        )

        if not po_headers:
            raise DocumentValidationError(
                "The EST NO stamp header was found, but its matching "
                "LM NO/PO NO header was not detected. The upload was "
                "blocked to prevent LM digits being treated as an EST "
                "number"
            )

        nearest_po_header = min(
            po_headers,
            key=lambda line: line["left"],
        )
        right_boundary = (
            nearest_po_header["left"]
            - page_width * 0.002
        )

        bottom_boundary = min(
            page_height,
            header["bottom"] + max(
                header_height * 15,
                page_height * 0.18,
            ),
        )

        for line in lines:
            if line["page"] != page:
                continue

            if not (
                header["bottom"] < line["center_y"]
                <= bottom_boundary
            ):
                continue

            if not (
                left_boundary <= line["center_x"]
                < right_boundary
            ):
                continue

            # Defense in depth: if Azure merges both stamp
            # cells into one OCR line, remove the LM/PO token before
            # looking for an estimation number.
            candidate_text = re.sub(
                r"\b(?:L\s*M|P\s*O)\s*[-:/]?\s*\d{5,9}\b",
                "",
                line["text"],
                flags=re.IGNORECASE,
            )

            # A printed table total can sit below the physical stamp and
            # inside the EST column. For example, quantity 170.000 has six
            # digits after punctuation is removed. Treat a decimal-form
            # value as an EST only when an LM/PO-like value is present on
            # the same stamp row.
            if (
                is_decimal_quantity_candidate(candidate_text)
                and not has_same_row_stamp_order_value(
                    lines,
                    line,
                    nearest_po_header,
                    header_height,
                    page_height,
                    page_width,
                )
            ):
                continue

            # Letter-aware boundaries prevent the six digits inside
            # values such as LM111319 from matching as EST 111319.
            matches = re.findall(
                r"(?<![A-Z0-9])"
                r"(?:\d[\s./-]*){6}"
                r"(?![A-Z0-9])",
                candidate_text,
                flags=re.IGNORECASE,
            )

            for match in matches:
                digits = re.sub(r"\D", "", match)

                if len(digits) == 6:
                    selected.append(
                        (
                            page,
                            line["center_y"],
                            int(digits),
                        )
                    )

    selected.sort(key=lambda item: (item[0], item[1]))

    return unique(
        estimation_no
        for _, _, estimation_no in selected
    )


def prepare_stamp_image(image):
    """Improve smaller scans without removing handwritten strokes."""
    image = image.convert("RGB")

    minimum_width = 3000

    if image.width < minimum_width:
        scale = minimum_width / image.width
        image = image.resize(
            (
                round(image.width * scale),
                round(image.height * scale),
            ),
            Image.Resampling.LANCZOS,
        )

    return image


def extract_po_stamp_estimation_numbers(
    file_path: Path,
    workflow: dict | None = None,
) -> list[int]:
    """
    Extract every six-digit value beneath the stamped EST NO header.

    Supports PDF, PNG, JPG, JPEG, TIFF, BMP and WEBP files.
    The returned list is ordered according to the stamp rows.
    """
    file_path = Path(file_path)
    started = time.monotonic()
    record_workflow_event(
        workflow,
        "stamp_render",
        "processing",
        "Rendering document for stamped EST extraction",
    )

    if not file_path.is_file():
        raise DocumentValidationError(
            f"Document not found: {file_path}"
        )

    suffix = file_path.suffix.lower()
    poppler_path = (
        os.getenv("POPPLER_PATH", "").strip() or None
    )

    if suffix == ".pdf":
        images = convert_from_path(
            str(file_path),
            dpi=300,
            poppler_path=poppler_path,
            first_page=1,
            last_page=MAX_PDF_PAGES,
        )
    elif suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    }:
        with Image.open(file_path) as opened_image:
            images = [opened_image.copy()]
    else:
        raise DocumentValidationError(
            f"Unsupported stamp document type: {suffix}"
        )

    record_workflow_event(
        workflow,
        "stamp_render",
        "completed",
        f"Prepared {len(images)} image page(s) for stamp extraction",
        {
            "page_count": len(images),
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )

    estimation_numbers = []
    stamp_detected = False

    for page_number, image in enumerate(images, start=1):
        page_started = time.monotonic()
        record_workflow_event(
            workflow,
            "stamp_ocr_page",
            "processing",
            f"Reading stamped EST area on page {page_number}",
            {"page_number": page_number, "page_count": len(images)},
        )
        prepared_image = prepare_stamp_image(image)
        lines = azure_read_lines_from_image(prepared_image)

        if any(
            is_est_stamp_header(line["text"])
            for line in lines
        ):
            stamp_detected = True
            estimation_numbers.extend(
                extract_po_stamp_numbers_from_lines(lines)
            )
        record_workflow_event(
            workflow,
            "stamp_ocr_page",
            "completed",
            f"Finished stamped EST scan on page {page_number}",
            {
                "page_number": page_number,
                "page_count": len(images),
                "stamp_header_found": any(
                    is_est_stamp_header(line["text"])
                    for line in lines
                ),
                "duration_ms": round(
                    (time.monotonic() - page_started) * 1000
                ),
            },
        )

    estimation_numbers = unique(estimation_numbers)

    if not stamp_detected:
        record_workflow_event(
            workflow,
            "stamp_extraction",
            "failed",
            "Stamped EST header was not detected",
        )
        raise DocumentValidationError(
            f"Azure OCR could not locate the stamped EST NO "
            f"header in {file_path.name}"
        )

    if not estimation_numbers:
        record_workflow_event(
            workflow,
            "stamp_extraction",
            "failed",
            "Stamped EST header was found but no EST value was extracted",
        )
        raise DocumentValidationError(
            f"The EST NO stamp was found in {file_path.name}, "
            f"but no clear six-digit estimation number was detected"
        )

    record_workflow_event(
        workflow,
        "stamp_extraction",
        "completed",
        f"Extracted {len(estimation_numbers)} stamped EST value(s)",
        {"estimation_count": len(estimation_numbers)},
    )
    return estimation_numbers


def extract_pdf_text(
    pdf_path: Path,
    workflow: dict | None = None,
) -> str:
    """Render and OCR every physical PDF page."""
    poppler_path = os.getenv("POPPLER_PATH", "").strip() or None
    render_started = time.monotonic()
    record_workflow_event(
        workflow,
        "pdf_render",
        "processing",
        "Rendering PDF pages for OCR",
    )

    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=300,
            poppler_path=poppler_path,
        )
    except Exception as error:
        record_workflow_event(
            workflow,
            "pdf_render",
            "failed",
            f"PDF rendering failed: {error}",
        )
        raise DocumentValidationError(
            f"Could not render PDF: {error}"
        ) from error

    page_count = len(images)
    if page_count == 0:
        record_workflow_event(
            workflow,
            "pdf_render",
            "failed",
            "PDF contains no renderable pages",
        )
        raise DocumentValidationError(
            "PDF contains no renderable pages"
        )

    if page_count > MAX_PDF_PAGES:
        record_workflow_event(
            workflow,
            "pdf_render",
            "failed",
            f"PDF page count {page_count} exceeds the limit",
            {"page_count": page_count, "maximum_pages": MAX_PDF_PAGES},
        )
        raise DocumentValidationError(
            f"PDF has {page_count} pages; maximum is {MAX_PDF_PAGES}"
        )

    record_workflow_event(
        workflow,
        "pdf_render",
        "completed",
        f"Rendered {page_count} PDF page(s)",
        {
            "page_count": page_count,
            "duration_ms": round(
                (time.monotonic() - render_started) * 1000
            ),
        },
    )

    extracted_pages = []

    for page_number, image in enumerate(images, start=1):
        page_started = time.monotonic()
        record_workflow_event(
            workflow,
            "ocr_page",
            "processing",
            f"OCR processing page {page_number} of {page_count}",
            {"page_number": page_number, "page_count": page_count},
        )
        try:
            page_text = (
                azure_ocr_from_image(image) or ""
            ).strip()
        except Exception as error:
            record_workflow_event(
                workflow,
                "ocr_page",
                "failed",
                f"OCR failed on page {page_number}: {error}",
                {"page_number": page_number, "page_count": page_count},
            )
            raise DocumentValidationError(
                f"Azure OCR failed on PDF page {page_number}: {error}"
            ) from error

        # Do not silently insert a document if any page was missed.
        if not page_text:
            record_workflow_event(
                workflow,
                "ocr_page",
                "failed",
                f"OCR returned no text for page {page_number}",
                {"page_number": page_number, "page_count": page_count},
            )
            raise DocumentValidationError(
                f"No text could be extracted from PDF page {page_number}"
            )

        print(
            f"OCR page {page_number}/{page_count}: "
            f"{len(page_text)} characters"
        )
        record_workflow_event(
            workflow,
            "ocr_page",
            "completed",
            f"OCR completed page {page_number} of {page_count}",
            {
                "page_number": page_number,
                "page_count": page_count,
                "character_count": len(page_text),
                "duration_ms": round(
                    (time.monotonic() - page_started) * 1000
                ),
            },
        )

        extracted_pages.append(page_text)

    return "\n\n".join(extracted_pages).strip()






def filename_document_type(filename: str) -> str:
    """Classify explicit document markers in an upload filename."""
    name = re.sub(
        r"[^A-Z0-9]+",
        "_",
        Path(filename).stem.upper(),
    ).strip("_")

    found = []

    if (
        "PODOCUMENT" in name
        or "FILE_PO" in name
        or re.search(r"(?:^|_)PO(?:_|$)", name)
        or re.search(r"(?:^|_)(?:LMO?|PO)\d{4,}", name)
    ):
        found.append("po")

    if (
        "MIXSHEET" in name
        or "FILE_MIX" in name
        or re.search(r"(?:^|_)MIXING_?SHEET(?:_|$)", name)
        or re.search(r"(?:^|_)MIX(?:_|$)", name)
    ):
        found.append("mix")

    if (
        "ESTSHEET" in name
        or "FILE_EST" in name
        or re.search(r"(?:^|_)ESTIMATION_?SHEET(?:_|$)", name)
        or re.search(r"(?:^|_)EST(?:_|$)", name)
    ):
        found.append("estimation")

    found = unique(found)

    if len(found) == 1:
        return found[0]

    return "ambiguous" if found else "unknown"


def expected_document_type_from_request() -> str | None:
    raw = request.form.get(
        "expected_document_type",
        "",
    ).strip().lower()

    aliases = {
        "est": "estimation",
        "estimation": "estimation",
        "mix": "mix",
        "mixing": "mix",
        "po": "po",
    }

    if not raw:
        return None

    value = aliases.get(raw)

    if value is None:
        raise DocumentValidationError(
            f"Invalid selected document type: {raw!r}"
        )

    return value


def expected_est_no_from_request() -> int | None:
    raw = request.form.get(
        "expected_est_no",
        "",
    ).strip()

    if raw in {"", "-", "N/A", "None"}:
        return None

    if not re.fullmatch(r"\d{5,7}", raw):
        raise DocumentValidationError(
            f"Invalid selected EST number: {raw!r}"
        )

    return int(raw)


def extract_expected_po_estimation_number(
    file_path: Path,
    expected_est_no: int,
    workflow: dict | None = None,
) -> int:
    """
    If Azure misses the EST NO header, accept only the exact EST
    number from the selected pending row. Oracle validates its
    RORDERNO before insertion.
    """
    expected_est_no = int(expected_est_no)
    expected_text = str(expected_est_no)

    if len(expected_text) != 6:
        raise DocumentValidationError(
            f"PO EST number must contain six digits: {expected_text}"
        )

    digit_map = str.maketrans({
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "S": "5",
        "Z": "2",
        "B": "8",
        "G": "6",
    })

    pattern = re.compile(
        r"(?<![A-Z0-9])"
        r"(?:[0-9OQDILSZBG][\s./-]*){6}"
        r"(?![A-Z0-9])",
        re.IGNORECASE,
    )

    poppler_path = (
        os.getenv("POPPLER_PATH", "").strip() or None
    )

    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)

    if page_count > MAX_PDF_PAGES:
        raise DocumentValidationError(
            f"PDF has {page_count} pages; "
            f"maximum is {MAX_PDF_PAGES}"
        )

    detected = set()

    # One page at a time avoids keeping the entire PDF in memory.
    for page_number in range(1, page_count + 1):
        page_started = time.monotonic()
        record_workflow_event(
            workflow,
            "targeted_est_ocr_page",
            "processing",
            f"Checking selected EST on page {page_number} of {page_count}",
            {"page_number": page_number, "page_count": page_count},
        )
        images = convert_from_path(
            str(file_path),
            dpi=300,
            poppler_path=poppler_path,
            first_page=page_number,
            last_page=page_number,
        )

        for image in images:
            try:
                lines = azure_read_lines_from_image(
                    prepare_stamp_image(image)
                )
            finally:
                try:
                    image.close()
                except Exception:
                    pass

            for line in lines:
                for match in pattern.findall(line["text"]):
                    digits = re.sub(
                        r"\D",
                        "",
                        match.upper().translate(digit_map),
                    )

                    if len(digits) == 6:
                        detected.add(digits)
        record_workflow_event(
            workflow,
            "targeted_est_ocr_page",
            "completed",
            f"Checked selected EST on page {page_number} of {page_count}",
            {
                "page_number": page_number,
                "page_count": page_count,
                "duration_ms": round(
                    (time.monotonic() - page_started) * 1000
                ),
            },
        )

    if expected_text not in detected:
        shown = ", ".join(sorted(detected)) or "none"

        record_workflow_event(
            workflow,
            "targeted_est_verification",
            "failed",
            "Selected EST was not confirmed by targeted OCR",
            {"detected_candidate_count": len(detected)},
        )
        raise DocumentValidationError(
            f"Selected EST No. {expected_text} was not found "
            f"by Azure OCR in {file_path.name}. "
            f"OCR six-digit values: {shown}. "
            "Nothing was inserted"
        )

    record_workflow_event(
        workflow,
        "targeted_est_verification",
        "completed",
        "Selected EST was confirmed by targeted OCR",
    )
    return expected_est_no


def classify_document(text: str) -> str:
    """Classify a PDF only from its extracted content."""
    normalized_text = re.sub(
        r"[^A-Z0-9]+",
        " ",
        (text or "").upper(),
    )
    normalized_text = " ".join(normalized_text.split())

    # Mixing may also contain CUSTOMER NAME.
    if re.search(r"\bMIXING\b", normalized_text):
        return "mix"

    if re.search(
        r"\bCUSTOMER\s+NAME\b",
        normalized_text,
    ):
        return "estimation"

    # PO sheets may label the business order as PO NO, LM NO, or LMO NO.
    # Punctuation has already been normalized to spaces, so these patterns
    # also cover P.O. No, L.M. No, and similar printed/OCR variants.
    if re.search(
        r"\b(?:P\s*O|L\s*M(?:\s*O)?)\s+(?:NO|NUMBER)\b",
        normalized_text,
    ):
        return "po"

    # Some scans preserve only the identifier and lose the NO/NUMBER label.
    if re.search(
        r"\b(?:P\s*O|L\s*M(?:\s*O)?)\s*\d{4,}",
        normalized_text,
    ):
        return "po"

    return "unknown"

def extract_estimation_numbers(
    text: str,
    document_type: str,
) -> list[int]:
    """Extract every possible EST number from the document."""

    if document_type == "mix":
        patterns = (
            r"MIXING\s*(?:DETAILS\s*FOR\s*)?"
            r"EST\.?\s*NO\.?\s*[-:#]?\s*['\"]?(\d{5,7})",
            r"EST\.?\s*NO\.?\s*[-:#]?\s*['\"]?(\d{5,7})",
        )

        matches = []

        for pattern in patterns:
            matches.extend(
                re.findall(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                )
            )

        return [
            int(value)
            for value in unique(matches)
        ]

    if document_type == "estimation":
        # Do not discard a valid EST merely because its prefix is
        # different from the other EST numbers in the same PDF.
        matches = re.findall(
            r"(?<![A-Za-z0-9])\d{6}(?![A-Za-z0-9])",
            text,
        )

        return [
            int(value)
            for value in unique(matches)
        ]

    return []




def existing_estimation_numbers(cursor, candidates: list[int]) -> list[int]:
    candidates = unique(candidates)
    if not candidates:
        return []

    binds = {f"est_{index}": value for index, value in enumerate(candidates)}
    placeholders = ", ".join(f":{name}" for name in binds)
    cursor.execute(
        f"SELECT ESTNO FROM COSTESTIMATION WHERE ESTNO IN ({placeholders})",
        binds,
    )
    existing = {int(row[0]) for row in cursor.fetchall()}
    return [value for value in candidates if value in existing]


def resolve_po_estimation_candidates(
    cursor,
    document_estimation_numbers: list[int],
    line_estimation_numbers: list[int],
    *,
    expected_est_no: int | None = None,
    expected_order_no: str | None = None,
) -> dict:
    """Resolve each AI EST independently without deriving new identities."""
    document_numbers = unique(
        int(value) for value in document_estimation_numbers
    )
    line_numbers = unique(int(value) for value in line_estimation_numbers)
    candidates = unique([*document_numbers, *line_numbers])

    if expected_est_no is not None and int(expected_est_no) not in document_numbers:
        raise DocumentValidationError(
            f"Selected EST No. {expected_est_no} was not returned in the "
            "AI document-level EST fields. Nothing was inserted"
        )

    mapped = []
    rejected_estimations = []
    for estimation_no in candidates:
        cursor.execute(
            """
            SELECT DISTINCT
                TRIM(RORDERNO),
                TRIM(REFORDERNO)
            FROM REGULARORDER
            WHERE ESTIMATIONNO = :estimation_no
            """,
            estimation_no=estimation_no,
        )
        rows = []
        for row in cursor.fetchall():
            regular_order_no = str(row[0] or "").strip()
            reference_order_no = str(row[1] or "").strip()
            if regular_order_no:
                rows.append((regular_order_no, reference_order_no))

        order_numbers = sorted({row[0] for row in rows})
        if len(order_numbers) != 1:
            rejected_estimations.append(
                {
                    "estimation_no": estimation_no,
                    "source": (
                        "document_and_line"
                        if estimation_no in document_numbers
                        and estimation_no in line_numbers
                        else "document"
                        if estimation_no in document_numbers
                        else "line"
                    ),
                    "reason": (
                        "NO_REGULARORDER_MAPPING"
                        if not order_numbers
                        else "AMBIGUOUS_REGULARORDER_MAPPING"
                    ),
                }
            )
            continue

        order_no = order_numbers[0]
        mapped.append(
            {
                "estimation_no": estimation_no,
                "regular_order_number": order_no,
                "reference_order_numbers": sorted(
                    {
                        reference
                        for regular, reference in rows
                        if regular == order_no and reference
                    }
                ),
            }
        )

    excluded_mappings = []
    if expected_order_no:
        expected_key = str(expected_order_no).strip().upper()

        def is_selected(mapping):
            return expected_key in {
                mapping["regular_order_number"].upper(),
                *(
                    value.upper()
                    for value in mapping["reference_order_numbers"]
                ),
            }

        selected_est_mapping = next(
            (
                mapping
                for mapping in mapped
                if mapping["estimation_no"] == int(expected_est_no)
                and is_selected(mapping)
            ),
            None,
        )
        if selected_est_mapping is None:
            raise DocumentValidationError(
                "The AI-returned selected EST does not resolve to the "
                f"selected order/reference {expected_order_no}. "
                "Nothing was inserted"
            )
        retained = [mapping for mapping in mapped if is_selected(mapping)]
        excluded_mappings = [
            mapping for mapping in mapped if not is_selected(mapping)
        ]
    else:
        retained = mapped

    if not retained:
        raise DocumentValidationError(
            "No AI-extracted EST value maps uniquely to REGULARORDER. "
            "Nothing was inserted"
        )

    return {
        "mappings": retained,
        "order_numbers": unique(
            mapping["regular_order_number"] for mapping in retained
        ),
        "rejected_estimations": rejected_estimations,
        "excluded_mappings": excluded_mappings,
        "candidate_count": len(candidates),
    }


def insert_enquiry_document(
    filename: str,
    file_path: Path,
    document_type: str,
    candidate_est_nos: list[int],
    remarks: str,
    system_name: str,
    expected_est_no: int | None = None,
    workflow: dict | None = None,
):

    record_workflow_event(
        workflow,
        "enquiry_validation",
        "processing",
        "Validating estimation or mixing document insertion inputs",
    )

    type_codes = {"estimation": 1, "mix": 2}

    if document_type not in type_codes:
        raise DocumentValidationError(
            "Only estimation and mixing sheets can be uploaded"
        )

    candidate_est_nos = unique(
        int(value) for value in candidate_est_nos
    )

    if expected_est_no is not None:
        expected_est_no = int(expected_est_no)

        if expected_est_no not in candidate_est_nos:
            raise DocumentValidationError(
                f"Selected EST No. {expected_est_no} was not returned "
                "by AI document analysis. Nothing was inserted"
            )


    if not candidate_est_nos:
        raise DocumentValidationError(
            f"No estimation number found in {filename}"
        )

    if not file_path.is_file():
        raise DocumentValidationError(
            f"Uploaded file is missing: {filename}"
        )

    document_type_code = type_codes[document_type]
    blob_data = file_path.read_bytes()
    file_format = Path(filename).suffix.lower()

    if file_format != ".pdf":
        raise DocumentValidationError("Only PDF files are allowed")

    if not blob_data:
        raise DocumentValidationError("Uploaded PDF is empty")

    record_workflow_event(
        workflow,
        "enquiry_validation",
        "completed",
        "Insertion inputs validated",
        {"candidate_estimation_count": len(candidate_est_nos)},
    )
    record_workflow_event(
        workflow,
        "oracle_connection",
        "processing",
        "Opening Oracle connection",
    )
    try:
        connection = get_db_connection()
    except Exception as exc:
        record_workflow_event(
            workflow,
            "oracle_connection",
            "failed",
            f"Oracle connection failed: {exc}",
        )
        raise
    record_workflow_event(
        workflow,
        "oracle_connection",
        "completed",
        "Oracle connection opened",
    )

    try:
        with connection.cursor() as cursor:
            record_workflow_event(
                workflow,
                "estimation_lookup",
                "processing",
                "Checking extracted EST values in COSTESTIMATION",
            )
            est_nos = existing_estimation_numbers(
                cursor,
                candidate_est_nos,
            )

            if not est_nos:
                raise DocumentValidationError(
                    f"No extracted estimation number from {filename} "
                    "exists in COSTESTIMATION"
                )
            record_workflow_event(
                workflow,
                "estimation_lookup",
                "completed",
                f"Resolved {len(est_nos)} valid EST value(s)",
                {
                    "candidate_count": len(candidate_est_nos),
                    "valid_count": len(est_nos),
                },
            )

            # Dynamic duplicate check using every valid EST number
            # extracted from the uploaded PDF.
            record_workflow_event(
                workflow,
                "duplicate_check",
                "processing",
                "Checking whether extracted EST values already exist",
            )
            valid_est_set = set(est_nos)
            invalid_est_nos = [
                value
                for value in candidate_est_nos
                if value not in valid_est_set
            ]

            est_binds = {
                f"detail_est_{index}": est_no
                for index, est_no in enumerate(est_nos)
            }
            est_placeholders = ", ".join(
                f":detail_est_{index}"
                for index in range(len(est_nos))
            )

            cursor.execute(
                f"""
                SELECT ESTNO, DOCID
                FROM ENQUIRYDOCUMENTDETAILS
                WHERE ESTNO IN ({est_placeholders})
                  AND ENQUIRYDOCUMENTTYPECODE =
                      :detail_document_type_code
                ORDER BY ESTNO, DOCID
                """,
                {
                    **est_binds,
                    "detail_document_type_code":
                        document_type_code,
                },
            )

            existing_docids_by_est = {}

            for existing_est_no, existing_docid in cursor.fetchall():
                existing_docids_by_est.setdefault(
                    int(existing_est_no),
                    [],
                ).append(int(existing_docid))

            already_inserted_est_nos = [
                est_no
                for est_no in est_nos
                if est_no in existing_docids_by_est
            ]

            missing_est_nos = [
                est_no
                for est_no in est_nos
                if est_no not in existing_docids_by_est
            ]

            if not missing_est_nos:
                raise DocumentValidationError(
                    "Every valid EST number in "
                    f"{filename} is already inserted for "
                    f"document type {document_type_code}. "
                    "Nothing was inserted"
                )

            record_workflow_event(
                workflow,
                "duplicate_check",
                "completed",
                "Completed existing-document duplicate check",
                {
                    "new_estimation_count": len(missing_est_nos),
                    "existing_estimation_count": len(
                        already_inserted_est_nos
                    ),
                },
            )

            # Store all missing EST numbers from this uploaded PDF as one
            # new document group. Oracle owns DOCID allocation so concurrent
            # Estimation/Mixing uploads cannot receive the same value.
            cursor.execute(
                """
                SELECT EnquiryDocument_DocId_Seq.NEXTVAL
                FROM DUAL
                """
            )
            document_docid = int(cursor.fetchone()[0])

            record_workflow_event(
                workflow,
                "enquiry_parent_insert",
                "processing",
                "Inserting document parent row",
            )

            cursor.execute(
                """
                INSERT INTO ENQUIRYDOCUMENT (
                    ID,
                    DOCID,
                    FILENAME,
                    BLOBIMAGE,
                    FILEFORMAT,
                    REMARKS,
                    ENTRYDATETIME,
                    SYSTEMNAME,
                    ENQUIRYDOCUMENTTYPECODE
                ) VALUES (
                    ENQUIRYDOCUMENT_SEQ.NEXTVAL,
                    :document_docid,
                    :filename,
                    :blob_data,
                    :file_format,
                    NVL(:remarks, 'Pending interface upload'),
                    TO_CHAR(SYSTIMESTAMP, 'DD.MM.YYYY HH24:MI:SS'),
                    :system_name,
                    :document_type_code
                )
                """,
                document_docid=document_docid,
                filename=filename,
                blob_data=blob_data,
                file_format=file_format,
                remarks=remarks[:200],
                system_name=system_name[:50],
                document_type_code=document_type_code,
            )
            record_workflow_event(
                workflow,
                "enquiry_parent_insert",
                "completed",
                "Document parent row inserted",
                {"docid": document_docid},
            )

            # Insert one detail row for every verified EST number.
            # All rows use the same DOCID generated for this PDF.
            record_workflow_event(
                workflow,
                "enquiry_detail_insert",
                "processing",
                f"Inserting {len(missing_est_nos)} document detail row(s)",
            )
            cursor.executemany(
                """
                INSERT INTO ENQUIRYDOCUMENTDETAILS (
                    DOCID,
                    ESTNO,
                    ENQUIRYDOCUMENTTYPECODE
                ) VALUES (
                    :document_docid,
                    :est_no,
                    :document_type_code
                )
                """,
                [
                    {
                        "document_docid": document_docid,
                        "est_no": est_no,
                        "document_type_code": document_type_code,
                    }
                    for est_no in missing_est_nos
                ],
            )
            record_workflow_event(
                workflow,
                "enquiry_detail_insert",
                "completed",
                f"Inserted {len(missing_est_nos)} document detail row(s)",
                {"inserted_row_count": len(missing_est_nos)},
            )

        record_workflow_event(
            workflow,
            "oracle_commit",
            "processing",
            "Committing estimation or mixing document transaction",
        )
        connection.commit()
        record_workflow_event(
            workflow,
            "oracle_commit",
            "completed",
            "Estimation or mixing document transaction committed",
        )

        return {
            "docid": document_docid,
            "pdf_reused": False,
            "estimation_numbers": missing_est_nos,
            "inserted_estimation_numbers":
                missing_est_nos,
            "skipped_existing_estimation_numbers":
                already_inserted_est_nos,
            "invalid_estimation_numbers":
                invalid_est_nos,
        }

    except Exception as exc:
        record_workflow_event(
            workflow,
            "oracle_rollback",
            "processing",
            f"Rolling back document transaction: {exc}",
        )
        connection.rollback()
        record_workflow_event(
            workflow,
            "oracle_rollback",
            "completed",
            "Document transaction rolled back",
        )
        raise

    finally:
        connection.close()
        record_workflow_event(
            workflow,
            "oracle_connection",
            "closed",
            "Oracle connection closed",
        )


def upload_entry_context() -> tuple[str, str]:
    remarks = request.form.get("remarks", "").strip() or required_env("SCM_ENTRY_REMARKS")
    system_name = (
        os.getenv("SCM_SYSTEM_NAME", "").strip()
        or request.remote_addr
        or "ESTIMATION_APP"
    )
    return remarks, system_name


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/enquiry", methods=["GET"])
def enquiry():
    return render_template(
        "enquiry.html",
        details=[],
        est_pending=[],
        mix_pending=[],
        loaded=False,
    )


@app.route("/fetch_details", methods=["GET"])
def fetch_details():
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            cursor.execute(REGULAR_ORDER_ESTIMATION_DETAILS_QUERY)
            details = regular_order_estimation_details(
                rows_as_dicts(cursor)
            )

            cursor.execute(EST_SHEET_PENDING_QUERY)
            est_pending = rows_as_dicts(cursor)

            cursor.execute(MIXING_SHEET_PENDING_QUERY)
            mix_pending = rows_as_dicts(cursor)

        return render_template(
            "enquiry.html",
            details=details,
            est_pending=est_pending,
            mix_pending=mix_pending,
            loaded=True,
        )
    except Exception as exc:
        app.logger.exception("Failed to fetch pending enquiry attachments")
        return (
            render_template(
                "enquiry.html",
                details=[],
                est_pending=[],
                mix_pending=[],
                loaded=True,
                error=str(exc),
            ),
            500,
        )
    finally:
        if connection is not None:
            connection.close()


def insert_regular_order_document(
    filename: str,
    file_path: Path,
    text: str,
    remarks: str,
    system_name: str,
    expected_est_no: int | None = None,
    expected_order_no: str | None = None,
    workflow: dict | None = None,
    validation_only: bool = False,
    analysis_result: dict | None = None,
):
    # Keep the text argument for response/caller compatibility. Production
    # identity and detail values come only from the shared AI analysis.
    _ = text

    record_workflow_event(
        workflow,
        "po_input_validation",
        "processing",
        "Validating PO document insertion inputs",
    )

    if not file_path.is_file():
        raise DocumentValidationError(
            f"Uploaded file is missing: {filename}"
        )

    if len(filename) > 100:
        raise DocumentValidationError(
            "PO filename exceeds the 100-character database limit"
        )

    blob_data = file_path.read_bytes()
    if not blob_data:
        raise DocumentValidationError("Uploaded PDF is empty")

    try:
        entry_user_code = int(required_env("SCM_ENTRY_USER_CODE"))
    except ValueError as exc:
        raise ConfigurationError(
            "SCM_ENTRY_USER_CODE must be numeric"
        ) from exc

    record_workflow_event(
        workflow,
        "po_input_validation",
        "completed",
        "PO document insertion inputs validated",
        {"file_size_bytes": len(blob_data)},
    )

    if not isinstance(analysis_result, dict):
        raise DocumentValidationError(
            "AI document analysis result is unavailable for this PO"
        )
    document_estimation_numbers = unique(
        int(value)
        for value in analysis_result.get(
            "document_estimation_numbers",
            analysis_result.get("estimation_numbers") or [],
        )
    )
    line_estimation_numbers = unique(
        int(value)
        for value in analysis_result.get("line_estimation_numbers") or []
    )
    extracted_estimation_numbers = unique(
        [*document_estimation_numbers, *line_estimation_numbers]
    )

    if not extracted_estimation_numbers:
        raise DocumentValidationError(
            f"AI document analysis found no Estimation No. values in {filename}"
        )

    if workflow is not None:
        set_workflow_stage(
            workflow,
            "ocr",
            "completed",
            "AI document analysis completed",
            {
                "document_estimation_count": len(
                    document_estimation_numbers
                ),
                "line_estimation_count": len(line_estimation_numbers),
            },
        )

    if workflow is not None:
        set_workflow_stage(
            workflow,
            "content_understanding",
            "processing",
            "Using extracted PO fields from the shared AI result",
        )

    content_result = analysis_result
    po_detail_error = None
    po_detail_rejections = []
    extracted_order_lines = len(content_result.get("order_lines") or [])
    if workflow is not None:
        set_workflow_stage(
            workflow,
            "content_understanding",
            "completed",
            f"Reused AI result containing {extracted_order_lines} order line(s)",
            {
                "order_line_count": extracted_order_lines,
                "primary_estimation_present": (
                    content_result.get("primary_estimation_present", False)
                ),
                "estimation_mapping_count": len(
                    content_result.get("estimation_mappings") or []
                ),
            },
        )

    record_workflow_event(
        workflow,
        "oracle_connection",
        "processing",
        "Opening Oracle connection for PO insertion",
    )
    try:
        connection = get_db_connection()
    except Exception as exc:
        record_workflow_event(
            workflow,
            "oracle_connection",
            "failed",
            f"Oracle connection failed: {exc}",
        )
        raise
    record_workflow_event(
        workflow,
        "oracle_connection",
        "completed",
        "Oracle connection opened for PO insertion",
    )

    try:
        with connection.cursor() as cursor:
            if workflow is not None:
                set_workflow_stage(
                    workflow,
                    "validation",
                    "processing",
                    "Preparing extracted PO details",
                )

            record_workflow_event(
                workflow,
                "oracle_est_order_mapping",
                "processing",
                "Resolving AI-extracted EST candidates independently",
                {"candidate_count": len(extracted_estimation_numbers)},
            )
            resolution = resolve_po_estimation_candidates(
                cursor,
                document_estimation_numbers,
                line_estimation_numbers,
                expected_est_no=expected_est_no,
                expected_order_no=expected_order_no,
            )
            estimation_order_mappings = resolution["mappings"]
            order_numbers = resolution["order_numbers"]
            rejected_estimations = resolution["rejected_estimations"]
            excluded_mappings = resolution["excluded_mappings"]
            mapping_review_required = bool(
                rejected_estimations or excluded_mappings
            )
            estimation_numbers = [
                mapping["estimation_no"]
                for mapping in estimation_order_mappings
            ]

            record_workflow_event(
                workflow,
                "oracle_est_order_mapping",
                "completed",
                "Resolved AI-extracted EST values to regular-order parents",
                {
                    "estimation_count": len(estimation_order_mappings),
                    "regular_order_count": len(order_numbers),
                    "unmapped_estimation_count": len(
                        rejected_estimations
                    ),
                    "excluded_parent_count": len(excluded_mappings),
                },
            )
            app.logger.info(
                "PO MAPPING filename=%s candidates=%s mapped=%s "
                "unmapped=%s excluded_parents=%s",
                filename,
                resolution["candidate_count"],
                len(estimation_order_mappings),
                len(rejected_estimations),
                len(excluded_mappings),
            )

            po_detail_rows = []
            association_counts = {
                "direct_line_mapping_count": 0,
                "mapping_reference_mapping_count": 0,
                "legacy_reference_mapping_count": 0,
                "single_parent_fallback_count": 0,
                "ordered_mapping_fallback_count": 0,
            }
            if content_result is not None:
                try:
                    record_workflow_event(
                        workflow,
                        "po_detail_preparation",
                        "processing",
                        "Preparing extracted PO lines for storage",
                    )
                    detail_result = build_extracted_po_document_detail_result(
                        content_result,
                        estimation_order_mappings,
                    )
                    po_detail_rows = detail_result["rows"]
                    po_detail_rejections = detail_result[
                        "rejected_lines"
                    ]
                    association_counts.update(
                        detail_result.get("association_counts") or {}
                    )
                    if po_detail_rejections:
                        app.logger.warning(
                            "PO DETAIL PARTIAL filename=%s "
                            "prepared_rows=%s rejected_lines=%s",
                            filename,
                            len(po_detail_rows),
                            po_detail_rejections,
                        )
                    record_workflow_event(
                        workflow,
                        "po_detail_preparation",
                        (
                            "partial"
                            if po_detail_rejections
                            else "completed"
                        ),
                        "Prepared independently insertable PO detail lines",
                        {
                            **association_counts,
                            "prepared_row_count": len(po_detail_rows),
                            "rejected_line_count": len(
                                po_detail_rejections
                            ),
                        },
                    )
                except Exception as exc:
                    # Analyzer/result-level failures must not prevent parent
                    # persistence. Line-level failures are returned separately
                    # and do not discard independently insertable rows.
                    po_detail_error = str(exc)
                    po_detail_rows = []
                    app.logger.warning(
                        "PO DETAIL REVIEW filename=%s error=%s",
                        filename,
                        po_detail_error,
                    )
                    record_workflow_event(
                        workflow,
                        "po_detail_preparation",
                        "failed",
                        f"PO detail preparation failed: {exc}",
                    )
            else:
                record_workflow_event(
                    workflow,
                    "po_detail_preparation",
                    "skipped",
                    "PO detail preparation skipped because extraction was unavailable",
                )

            extraction_details = {
                "analyzer_configuration": content_result.get(
                    "analyzer_configuration",
                    "CONTENTUNDERSTANDING_ANALYZER_ID",
                ),
                "estimation_numbers": estimation_numbers,
                "regular_order_numbers": order_numbers,
                "unmapped_estimation_count": len(rejected_estimations),
                "excluded_parent_count": len(excluded_mappings),
                **association_counts,
                "detail_rows": po_detail_rows,
                "detail_status": (
                    "REVIEW_REQUIRED"
                    if po_detail_error
                    else "PARTIAL"
                    if po_detail_rejections or mapping_review_required
                    else "READY"
                ),
                "detail_error": po_detail_error,
                "rejected_lines": po_detail_rejections,
                "oracle_value_comparison": "SKIPPED",
                "party_name_comparison": "SKIPPED",
                "certificate_master_match": "SKIPPED",
            }
            app.logger.info(
                "PO DETAILS PREPARED filename=%s analyzer_config=%s "
                "prepared_row_count=%s rejected_line_count=%s "
                "unmapped_estimation_count=%s excluded_parent_count=%s "
                "oracle_detail_value_comparison=SKIPPED "
                "party_comparison=SKIPPED certificate_master=SKIPPED",
                filename,
                extraction_details["analyzer_configuration"],
                len(po_detail_rows),
                len(po_detail_rejections),
                len(rejected_estimations),
                len(excluded_mappings),
            )

            audit_detail_summary = {
                "primary_estimation_present": (
                    content_result.get("primary_estimation_present", False)
                ),
                "estimation_mapping_count": len(
                    content_result.get("estimation_mappings") or []
                ),
                "mapped_estimation_count": len(estimation_order_mappings),
                "regular_order_count": len(order_numbers),
                "unmapped_estimation_count": len(rejected_estimations),
                "excluded_parent_count": len(excluded_mappings),
                "prepared_row_count": len(po_detail_rows),
                "rejected_line_count": len(po_detail_rejections),
                **association_counts,
            }

            if workflow is not None:
                if po_detail_error:
                    set_workflow_stage(
                        workflow,
                        "validation",
                        "review_required",
                        "Parent PO can be stored; extracted details "
                        f"require review: {po_detail_error}",
                        audit_detail_summary,
                    )
                elif po_detail_rejections or mapping_review_required:
                    set_workflow_stage(
                        workflow,
                        "validation",
                        "review_required",
                        f"Prepared {len(po_detail_rows)} extracted line(s); "
                        f"{len(po_detail_rejections)} line(s) and "
                        f"{len(rejected_estimations) + len(excluded_mappings)} "
                        "EST mapping(s) require review",
                        audit_detail_summary,
                    )
                else:
                    set_workflow_stage(
                        workflow,
                        "validation",
                        "completed",
                        "Extracted PO details prepared without "
                        "business-value matching",
                        audit_detail_summary,
                    )
                set_workflow_stage(
                    workflow,
                    "database_insert",
                    "processing",
                    "Inserting PO parent document",
                )

            if validation_only:
                if workflow is not None:
                    set_workflow_stage(
                        workflow,
                        "database_insert",
                        "not_applicable",
                        "Validation-only mode: no database write was performed",
                        {
                            **audit_detail_summary,
                        },
                    )
                return {
                    "validation_only": True,
                    "document_rows_inserted": 0,
                    "regular_order_numbers": order_numbers,
                    "estimation_numbers": estimation_numbers,
                    "estimation_order_mappings": (
                        estimation_order_mappings
                    ),
                    "po_extraction": extraction_details,
                    "po_document_details": po_detail_rows,
                    "po_detail_status": (
                        "REVIEW_REQUIRED"
                        if po_detail_error
                        else "PARTIAL"
                        if po_detail_rejections or mapping_review_required
                        else "READY"
                    ),
                    "po_detail_error": po_detail_error,
                    "po_detail_rejected_lines": po_detail_rejections,
                    "po_estimation_rejections": rejected_estimations,
                    "po_excluded_parent_mappings": excluded_mappings,
                    "file_size_bytes": len(blob_data),
                }

            # Lock the counter before duplicate checks and allocation.
            # This serializes concurrent uploads from LAN users.
            record_workflow_event(
                workflow,
                "po_parent_counter_lock",
                "processing",
                "Locking PO DOCID configuration row",
            )
            cursor.execute(
                """
                SELECT MAXDOCID
                FROM REGULARORDER_PODOCUMENT_CONFIG
                FOR UPDATE
                """
            )
            config_rows = cursor.fetchall()

            if len(config_rows) != 1:
                raise DocumentValidationError(
                    "REGULARORDER_PODOCUMENT_CONFIG must contain "
                    "exactly one row. Nothing was inserted"
                )

            configured_max_docid = int(config_rows[0][0])
            record_workflow_event(
                workflow,
                "po_parent_counter_lock",
                "completed",
                "PO DOCID configuration row locked",
            )

            # Parent persistence uses only the established RORDERNO identity.
            # Detail-table state must not block or roll back the parent PDF.
            existing_parent_by_order = {}

            for order_index, order_no in enumerate(order_numbers, start=1):
                record_workflow_event(
                    workflow,
                    "po_parent_lookup",
                    "processing",
                    f"Checking existing PO parent {order_index} of {len(order_numbers)}",
                    {
                        "order_index": order_index,
                        "order_count": len(order_numbers),
                    },
                )
                order_no_key = order_no.strip().upper()
                cursor.execute(
                    """
                    SELECT ID, DOCID, FILENAME
                    FROM REGULARORDER_PODOCUMENT
                    WHERE UPPER(TRIM(RORDERNO)) = :order_no_key
                    FOR UPDATE
                    """,
                    order_no_key=order_no_key,
                )
                parent_rows = cursor.fetchall()

                replacement = plan_existing_po_parent(
                    order_no,
                    parent_rows,
                )
                if replacement is not None:
                    existing_parent_by_order[order_no] = replacement
                    app.logger.warning(
                        "PO REPLACEMENT PLANNED order=%s docid=%s "
                        "old_filename=%s",
                        order_no,
                        replacement["docid"],
                        replacement["filename"],
                    )
                record_workflow_event(
                    workflow,
                    "po_parent_lookup",
                    "completed",
                    f"Checked PO parent {order_index} of {len(order_numbers)}",
                    {
                        "order_index": order_index,
                        "order_count": len(order_numbers),
                        "replacement": replacement is not None,
                    },
                )

            cursor.execute(
                """
                SELECT
                    NVL(MAX(ID), 0),
                    NVL(MAX(DOCID), 0)
                FROM REGULARORDER_PODOCUMENT
                """
            )
            maximum_id, maximum_docid = map(
                int,
                cursor.fetchone(),
            )

            if configured_max_docid != maximum_docid:
                raise DocumentValidationError(
                    "PO DOCID counter mismatch: configuration "
                    f"MAXDOCID is {configured_max_docid}, but the "
                    f"table maximum is {maximum_docid}. "
                    "Nothing was inserted"
                )

            processed_rows = []
            deleted_detail_row_count = 0

            for order_index, order_no in enumerate(order_numbers, start=1):
                existing_parent = existing_parent_by_order.get(order_no)
                if existing_parent is not None:
                    record_workflow_event(
                        workflow,
                        "po_parent_upsert",
                        "processing",
                        f"Replacing PO parent {order_index} of {len(order_numbers)}",
                    )
                    cursor.execute(
                        """
                        UPDATE REGULARORDER_PODOCUMENT
                        SET FILENAME = :filename,
                            BLOBIMAGE = :blob_data,
                            FILEFORMAT = '.pdf',
                            REMARKS = :remarks,
                            ENTRYDATETIME = SYSDATE,
                            SYSTEMNAME = :system_name,
                            ENTRYUSERCODE = :entry_user_code
                        WHERE ID = :document_id
                          AND DOCID = :document_docid
                          AND UPPER(TRIM(RORDERNO)) = :order_no_key
                        """,
                        filename=filename,
                        blob_data=blob_data,
                        remarks=remarks[:200],
                        system_name=system_name[:50],
                        entry_user_code=entry_user_code,
                        document_id=existing_parent["id"],
                        document_docid=existing_parent["docid"],
                        order_no_key=order_no.strip().upper(),
                    )
                    if cursor.rowcount != 1:
                        raise DocumentValidationError(
                            "Failed to update the existing PO parent for "
                            f"regular order {order_no}. Nothing was changed"
                        )

                    processed_rows.append(
                        {
                            "id": existing_parent["id"],
                            "docid": existing_parent["docid"],
                            "regular_order_number": order_no,
                            "action": "replaced",
                            "old_filename": existing_parent["filename"],
                            "deleted_detail_rows": 0,
                        }
                    )
                    record_workflow_event(
                        workflow,
                        "po_parent_upsert",
                        "completed",
                        f"Replaced PO parent {order_index} of {len(order_numbers)}",
                        {
                            "action": "replaced",
                            "docid": existing_parent["docid"],
                        },
                    )
                    continue

                record_workflow_event(
                    workflow,
                    "po_parent_sequence",
                    "processing",
                    f"Allocating parent ID for order {order_index} of {len(order_numbers)}",
                )
                cursor.execute(
                    """
                    SELECT REGULARORDER_PODOCUMENT_SEQ.NEXTVAL
                    FROM DUAL
                    """
                )
                document_id = int(cursor.fetchone()[0])
                document_docid = maximum_docid + 1

                if document_id <= maximum_id:
                    raise DocumentValidationError(
                        f"Unsafe ID sequence value {document_id}; "
                        f"current table maximum is {maximum_id}"
                    )

                record_workflow_event(
                    workflow,
                    "po_parent_upsert",
                    "processing",
                    f"Inserting PO parent {order_index} of {len(order_numbers)}",
                )
                cursor.execute(
                    """
                    INSERT INTO REGULARORDER_PODOCUMENT (
                        ID,
                        DOCID,
                        RORDERNO,
                        FILENAME,
                        BLOBIMAGE,
                        FILEFORMAT,
                        REMARKS,
                        ENTRYDATETIME,
                        SYSTEMNAME,
                        ENTRYUSERCODE
                    ) VALUES (
                        :document_id,
                        :document_docid,
                        :order_no,
                        :filename,
                        :blob_data,
                        '.pdf',
                        :remarks,
                        SYSDATE,
                        :system_name,
                        :entry_user_code
                    )
                    """,
                    document_id=document_id,
                    document_docid=document_docid,
                    order_no=order_no,
                    filename=filename,
                    blob_data=blob_data,
                    remarks=remarks[:200],
                    system_name=system_name[:50],
                    entry_user_code=entry_user_code,
                )

                processed_rows.append(
                    {
                        "id": document_id,
                        "docid": document_docid,
                        "regular_order_number": order_no,
                        "action": "inserted",
                        "deleted_detail_rows": 0,
                    }
                )
                record_workflow_event(
                    workflow,
                    "po_parent_upsert",
                    "completed",
                    f"Inserted PO parent {order_index} of {len(order_numbers)}",
                    {"action": "inserted", "docid": document_docid},
                )

                maximum_id = document_id
                maximum_docid = document_docid

            docid_by_order = {
                row["regular_order_number"]: row["docid"]
                for row in processed_rows
            }

            record_workflow_event(
                workflow,
                "po_parent_counter_update",
                "processing",
                "Updating PO DOCID configuration",
            )
            cursor.execute(
                """
                UPDATE REGULARORDER_PODOCUMENT_CONFIG
                SET MAXDOCID = :new_max_docid
                WHERE MAXDOCID = :old_max_docid
                """,
                new_max_docid=maximum_docid,
                old_max_docid=configured_max_docid,
            )

            if cursor.rowcount != 1:
                raise DocumentValidationError(
                    "Failed to update the PO DOCID counter. "
                    "Nothing was inserted"
                )
            record_workflow_event(
                workflow,
                "po_parent_counter_update",
                "completed",
                "PO DOCID configuration updated",
            )

        # Commit the established parent-table workflow independently. Party,
        # certificate and extracted line checks cannot undo this transaction.
        record_workflow_event(
            workflow,
            "po_parent_commit",
            "processing",
            "Committing PO parent transaction",
        )
        connection.commit()
        record_workflow_event(
            workflow,
            "po_parent_commit",
            "completed",
            f"Committed {len(processed_rows)} PO parent row(s)",
            {"parent_row_count": len(processed_rows)},
        )

        inserted_detail_rows = []

        # Detail replacement is a separate atomic transaction. Existing
        # details are retained when preparation or insertion requires review.
        if po_detail_rows:
            try:
                record_workflow_event(
                    workflow,
                    "po_detail_transaction",
                    "processing",
                    f"Starting transaction for {len(po_detail_rows)} PO detail row(s)",
                    {"detail_row_count": len(po_detail_rows)},
                )
                with connection.cursor() as cursor:
                    detail_rows_by_order = {}
                    for detail in po_detail_rows:
                        detail_rows_by_order.setdefault(
                            detail["regular_order_number"],
                            [],
                        ).append(detail)

                    for order_index, order_no in enumerate(
                        detail_rows_by_order,
                        start=1,
                    ):
                        record_workflow_event(
                            workflow,
                            "po_detail_lock",
                            "processing",
                            f"Locking detail set {order_index} of {len(detail_rows_by_order)}",
                        )
                        order_no_key = order_no.strip().upper()
                        parent_docid = docid_by_order.get(order_no)
                        if parent_docid is None:
                            raise DocumentValidationError(
                                "No parent DOCID was found for regular "
                                f"order {order_no}"
                            )

                        cursor.execute(
                            """
                            SELECT ID, DOCID, FILENAME
                            FROM REGULARORDER_PODOCUMENT
                            WHERE ID = :document_id
                              AND DOCID = :document_docid
                              AND UPPER(TRIM(RORDERNO)) = :order_no_key
                            FOR UPDATE
                            """,
                            document_id=next(
                                row["id"]
                                for row in processed_rows
                                if row["regular_order_number"] == order_no
                            ),
                            document_docid=parent_docid,
                            order_no_key=order_no_key,
                        )
                        parent_rows = cursor.fetchall()

                        cursor.execute(
                            """
                            SELECT
                                ID,
                                PODOCUMENTDOCID,
                                LMNO,
                                ESTIMATIONNO
                            FROM REGULARORDER_PODOCUMENTDETAILS
                            WHERE PODOCUMENTDOCID = :document_docid
                               OR UPPER(TRIM(LMNO)) = :order_no_key
                            FOR UPDATE
                            """,
                            document_docid=parent_docid,
                            order_no_key=order_no_key,
                        )
                        existing_detail_rows = cursor.fetchall()
                        replacement = plan_existing_po_replacement(
                            order_no,
                            parent_rows,
                            existing_detail_rows,
                        )
                        expected_deleted = len(
                            replacement["detail_ids"]
                            if replacement
                            else []
                        )
                        record_workflow_event(
                            workflow,
                            "po_detail_lock",
                            "completed",
                            f"Locked detail set {order_index} of {len(detail_rows_by_order)}",
                            {"existing_detail_count": expected_deleted},
                        )

                        record_workflow_event(
                            workflow,
                            "po_detail_delete",
                            "processing",
                            f"Replacing existing detail set {order_index} of {len(detail_rows_by_order)}",
                        )
                        cursor.execute(
                            """
                            DELETE FROM REGULARORDER_PODOCUMENTDETAILS
                            WHERE PODOCUMENTDOCID = :document_docid
                              AND UPPER(TRIM(LMNO)) = :order_no_key
                            """,
                            document_docid=parent_docid,
                            order_no_key=order_no_key,
                        )
                        if cursor.rowcount != expected_deleted:
                            raise DocumentValidationError(
                                "PO detail replacement row-count changed "
                                f"for regular order {order_no}: expected "
                                f"{expected_deleted}, deleted "
                                f"{cursor.rowcount}"
                            )
                        deleted_detail_row_count += cursor.rowcount
                        record_workflow_event(
                            workflow,
                            "po_detail_delete",
                            "completed",
                            f"Removed {cursor.rowcount} previous detail row(s)",
                            {"deleted_row_count": cursor.rowcount},
                        )

                    for detail_index, detail in enumerate(
                        po_detail_rows,
                        start=1,
                    ):
                        parent_docid = docid_by_order.get(
                            detail["regular_order_number"]
                        )
                        if parent_docid is None:
                            raise DocumentValidationError(
                                "No parent DOCID was found for "
                                f"EST No. {detail['estimation_number']}"
                            )

                        record_workflow_event(
                            workflow,
                            "po_detail_sequence",
                            "processing",
                            f"Allocating detail ID {detail_index} of {len(po_detail_rows)}",
                        )
                        cursor.execute(
                            """
                            SELECT REGORDER_PODOCDETAILS_SEQ.NEXTVAL
                            FROM DUAL
                            """
                        )
                        detail_id = int(cursor.fetchone()[0])

                        record_workflow_event(
                            workflow,
                            "po_detail_insert",
                            "processing",
                            f"Inserting PO detail row {detail_index} of {len(po_detail_rows)}",
                        )
                        cursor.execute(
                            """
                            INSERT INTO REGULARORDER_PODOCUMENTDETAILS (
                                ID,
                                PODOCUMENTDOCID,
                                ESTIMATIONNO,
                                PARTYNAME,
                                LMNO,
                                REFERENCENO,
                                REQUIREDQUANTITY,
                                COUNTNAME,
                                CERTIFICATION,
                                NETRATE,
                                ENTRYDATETIME
                            ) VALUES (
                                :detail_id,
                                :parent_docid,
                                :estimation_number,
                                :party_name,
                                :regular_order_number,
                                :reference_number,
                                :required_quantity,
                                :count_name,
                                :certification,
                                :net_rate,
                                SYSDATE
                            )
                            """,
                            detail_id=detail_id,
                            parent_docid=parent_docid,
                            estimation_number=detail["estimation_number"],
                            party_name=detail["party_name"],
                            regular_order_number=detail[
                                "regular_order_number"
                            ],
                            reference_number=detail["reference_number"],
                            required_quantity=detail["required_quantity"],
                            count_name=detail["count_name"],
                            certification=detail["certification"],
                            net_rate=detail["net_rate"],
                        )

                        inserted_detail_rows.append(
                            {
                                "id": detail_id,
                                "podocument_docid": parent_docid,
                                **detail,
                            }
                        )
                        record_workflow_event(
                            workflow,
                            "po_detail_insert",
                            "completed",
                            f"Inserted PO detail row {detail_index} of {len(po_detail_rows)}",
                            {
                                "detail_index": detail_index,
                                "detail_row_count": len(po_detail_rows),
                                "detail_id": detail_id,
                                "parent_docid": parent_docid,
                            },
                        )

                record_workflow_event(
                    workflow,
                    "po_detail_commit",
                    "processing",
                    "Committing PO detail transaction",
                )
                connection.commit()
                record_workflow_event(
                    workflow,
                    "po_detail_commit",
                    "completed",
                    f"Committed {len(inserted_detail_rows)} PO detail row(s)",
                    {"inserted_row_count": len(inserted_detail_rows)},
                )
            except Exception as exc:
                record_workflow_event(
                    workflow,
                    "po_detail_rollback",
                    "processing",
                    f"Rolling back PO detail transaction: {exc}",
                )
                connection.rollback()
                record_workflow_event(
                    workflow,
                    "po_detail_rollback",
                    "completed",
                    "PO detail transaction rolled back",
                )
                inserted_detail_rows = []
                deleted_detail_row_count = 0
                po_detail_error = str(exc)
                extraction_details["detail_status"] = "REVIEW_REQUIRED"
                extraction_details["detail_error"] = po_detail_error
                if workflow is not None:
                    set_workflow_stage(
                        workflow,
                        "validation",
                        "review_required",
                        "PO parent was stored; extracted details require "
                        f"review: {po_detail_error}",
                        audit_detail_summary,
                    )
                app.logger.exception(
                    "PO parent stored but detail transaction requires "
                    "review for %s",
                    filename,
                )
        else:
            record_workflow_event(
                workflow,
                "po_detail_transaction",
                "skipped",
                "No independently insertable PO detail rows were available",
            )

        po_detail_status = (
            "REVIEW_REQUIRED"
            if po_detail_error
            else "PARTIAL"
            if (po_detail_rejections or mapping_review_required)
            and inserted_detail_rows
            else "REVIEW_REQUIRED"
            if po_detail_rejections or mapping_review_required
            else "INSERTED"
            if inserted_detail_rows
            else "READY"
        )
        extraction_details["detail_status"] = po_detail_status
        extraction_details["detail_error"] = po_detail_error

        inserted_parent_count = sum(
            row["action"] == "inserted"
            for row in processed_rows
        )
        replaced_parent_count = sum(
            row["action"] == "replaced"
            for row in processed_rows
        )
        replaced_order_numbers = [
            row["regular_order_number"]
            for row in processed_rows
            if row["action"] == "replaced"
        ]

        if workflow is not None:
            set_workflow_stage(
                workflow,
                "database_insert",
                "completed",
                "Stored PO parent rows: "
                f"{inserted_parent_count} inserted, "
                f"{replaced_parent_count} replaced"
                + (
                    "; extracted details require review"
                    if po_detail_error
                    else "; some extracted lines or EST mappings require review"
                    if po_detail_rejections or mapping_review_required
                    else f"; detail rows inserted: "
                    f"{len(inserted_detail_rows)}"
                ),
                {
                    "parent_row_count": len(processed_rows),
                    "inserted_parent_count": inserted_parent_count,
                    "replaced_parent_count": replaced_parent_count,
                    "old_detail_rows_deleted": deleted_detail_row_count,
                    "po_detail_rows_inserted": len(
                        inserted_detail_rows
                    ),
                    "po_detail_lines_rejected": len(
                        po_detail_rejections
                    ),
                    "unmapped_estimation_count": len(
                        rejected_estimations
                    ),
                    "excluded_parent_count": len(excluded_mappings),
                },
            )

        return {
            # Preserve the original single-row response fields.
            "docid": processed_rows[0]["docid"],
            "regular_order_number": (
                processed_rows[0]["regular_order_number"]
            ),
            # New complete multi-order response fields.
            "document_rows_processed": len(processed_rows),
            "document_rows_inserted": inserted_parent_count,
            "document_rows_replaced": replaced_parent_count,
            "replaced_order_numbers": replaced_order_numbers,
            "old_detail_rows_deleted": deleted_detail_row_count,
            "ids": [row["id"] for row in processed_rows],
            "docids": [row["docid"] for row in processed_rows],
            "regular_order_numbers": [
                row["regular_order_number"]
                for row in processed_rows
            ],
            "estimation_numbers": estimation_numbers,
            "estimation_order_mappings": (
                estimation_order_mappings
            ),
            "po_extraction": extraction_details,
            "po_document_details": inserted_detail_rows,
            "po_detail_rows_inserted": len(inserted_detail_rows),
            "po_detail_status": po_detail_status,
            "po_detail_error": po_detail_error,
            "po_detail_rejected_lines": po_detail_rejections,
            "po_estimation_rejections": rejected_estimations,
            "po_excluded_parent_mappings": excluded_mappings,
            "message": (
                "PO parent stored; extracted details require review"
                if po_detail_error
                else "PO parent and available extracted details stored; "
                "some lines or EST mappings require review"
                if po_detail_rejections or mapping_review_required
                else "PO parent and available extracted details stored"
            ),
            "file_size_bytes": len(blob_data),
        }

    except Exception as exc:
        # Roll back only work that has not already been committed. Parent
        # persistence is committed before the separately guarded detail
        # transaction.
        record_workflow_event(
            workflow,
            "po_uncommitted_rollback",
            "processing",
            f"Rolling back uncommitted PO work: {exc}",
        )
        connection.rollback()
        record_workflow_event(
            workflow,
            "po_uncommitted_rollback",
            "completed",
            "Uncommitted PO work rolled back",
        )
        if workflow is not None:
            database_stage = workflow.get("database_insert") or {}
            if database_stage.get("status") == "processing":
                set_workflow_stage(
                    workflow,
                    "database_insert",
                    "failed",
                    str(exc),
                )
            fail_active_workflow_stage(workflow, str(exc))
        raise
    finally:
        connection.close()
        record_workflow_event(
            workflow,
            "oracle_connection",
            "closed",
            "Oracle connection closed after PO processing",
        )


@app.route("/estimation")
def estimation():
    return render_template(
        "estimation.html",
        batch_insert_enabled=batch_database_insert_enabled(),
    )


@app.route("/process_enquiry", methods=["POST"])
def process_enquiry():
    return upload_files()


@app.route("/upload", methods=["GET", "POST"])
def upload_files():
    if request.method == "GET":
        return render_template(
            "estimation.html",
            batch_insert_enabled=batch_database_insert_enabled(),
        )

    uploaded_files = request.files.getlist("files[]")
    if not uploaded_files or all(not item.filename for item in uploaded_files):
        return jsonify({"error": "No PDF files uploaded"}), 400
    if len(uploaded_files) > MAX_FILES_PER_REQUEST:
        return jsonify({"error": f"Maximum {MAX_FILES_PER_REQUEST} files per request"}), 400

    try:
        expected_document_type = (
            expected_document_type_from_request()
        )
        expected_est_no = expected_est_no_from_request()
        expected_order_no = (
            request.form.get(
                "expected_order_no",
                "",
            ).strip()
            or None
        )

        if expected_document_type is not None:
            selected_files = [
                item
                for item in uploaded_files
                if item.filename
            ]

            if len(selected_files) != 1:
                raise DocumentValidationError(
                    "A pending-row upload accepts "
                    "exactly one PDF"
                )

            if expected_est_no is None:
                raise DocumentValidationError(
                    "The selected pending row has "
                    "no EST number"
                )

            if (
                expected_document_type == "po"
                and not expected_order_no
            ):
                raise DocumentValidationError(
                    "The selected PO row has no order number"
                )

        remarks, system_name = upload_entry_context()
        requested_validation_only = request.form.get(
            "validation_only",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        validation_only = (
            requested_validation_only
            or not batch_database_insert_enabled()
        )

    except DocumentValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    except ConfigurationError as exc:
        return jsonify({"error": str(exc)}), 500

    temporary_paths = []
    results = []
    errors = []
    skipped = []

    try:
        for uploaded_file in uploaded_files:
            filename = secure_filename(
                uploaded_file.filename or ""
            )
            workflow = new_upload_workflow(
                filename or "(empty filename)"
            )
            workflow_id = public_workflow_id(workflow)
            record_workflow_event(
                workflow,
                "file_validation",
                "processing",
                "Validating uploaded filename and file type",
            )

            if not filename or not allowed_file(filename):
                record_workflow_event(
                    workflow,
                    "file_validation",
                    "failed",
                    "Uploaded file is not an allowed PDF",
                )
                set_workflow_stage(
                    workflow,
                    "uploaded",
                    "failed",
                    "Only PDF files are allowed",
                )
                block_pending_workflow_stages(
                    workflow,
                    "Blocked because the upload was rejected",
                )
                errors.append({
                    "filename": filename or "(empty)",
                    "error": "Only PDF files are allowed",
                    "workflow": workflow,
                    "workflow_id": workflow_id,
                })
                record_workflow_event(
                    workflow,
                    "workflow_complete",
                    "failed",
                    "Upload workflow ended during file validation",
                )
                continue

            record_workflow_event(
                workflow,
                "file_validation",
                "completed",
                "Uploaded filename and file type validated",
            )

            temporary_path = (
                UPLOAD_DIR
                / f"{uuid.uuid4().hex}_{filename}"
            )
            temporary_paths.append((temporary_path, workflow))
            record_workflow_event(
                workflow,
                "temporary_file",
                "processing",
                "Saving temporary upload file",
            )
            try:
                uploaded_file.save(temporary_path)
            except Exception as exc:
                record_workflow_event(
                    workflow,
                    "temporary_file",
                    "failed",
                    f"Temporary upload save failed: {exc}",
                )
                set_workflow_stage(
                    workflow,
                    "uploaded",
                    "failed",
                    "The uploaded PDF could not be saved temporarily",
                )
                block_pending_workflow_stages(
                    workflow,
                    "Blocked because temporary upload save failed",
                )
                errors.append({
                    "filename": filename,
                    "error": "The uploaded PDF could not be saved temporarily",
                    "workflow": workflow,
                    "workflow_id": workflow_id,
                })
                record_workflow_event(
                    workflow,
                    "workflow_complete",
                    "failed",
                    "Upload workflow ended because temporary save failed",
                    {"duration_ms": workflow_duration_ms(workflow)},
                )
                continue
            record_workflow_event(
                workflow,
                "temporary_file",
                "completed",
                "Temporary upload file saved",
                {"file_size_bytes": temporary_path.stat().st_size},
            )
            set_workflow_stage(
                workflow,
                "uploaded",
                "completed",
                "PDF received by the server",
                {"file_size_bytes": temporary_path.stat().st_size},
            )

            try:
                set_workflow_stage(
                    workflow,
                    "ocr",
                    "processing",
                    "Analyzing PDF and identifying the document",
                )
                analysis_result = analyze_uploaded_document(
                    temporary_path,
                    expected_document_type=expected_document_type,
                    audit=lambda stage, status, message, details=None: (
                        record_workflow_event(
                            workflow,
                            stage,
                            status,
                            message,
                            details,
                        )
                    ),
                )
                document_type = analysis_result["document_type"]
                classification_source = analysis_result["analysis_source"]
                if document_type == "unknown":
                    document_type = filename_document_type(filename)
                    classification_source = "filename"

                record_workflow_event(
                    workflow,
                    "document_classification",
                    (
                        "failed"
                        if document_type in {"unknown", "ambiguous"}
                        else "completed"
                    ),
                    f"Document classified as {document_type}",
                    {
                        "document_type": document_type,
                        "classification_source": classification_source,
                    },
                )

                set_workflow_stage(
                    workflow,
                    "ocr",
                    "completed",
                    "AI document analysis completed",
                    {
                        "document_type": document_type,
                        "estimation_count": len(
                            analysis_result["estimation_numbers"]
                        ),
                        "order_line_count": len(
                            analysis_result["order_lines"]
                        ),
                    },
                )

                if document_type in {"unknown", "ambiguous"}:
                    raise DocumentValidationError(
                        "AI document analysis could not identify this PDF "
                        "reliably. The document requires review"
                    )

                if (
                    expected_document_type is not None
                    and document_type != expected_document_type
                ):
                    raise DocumentValidationError(
                        f"Wrong document type: selected "
                        f"{expected_document_type}, but PDF content "
                        f"identifies {document_type}"
                    )

                if document_type == "po":
                    inserted = insert_regular_order_document(
                        filename=filename,
                        file_path=temporary_path,
                        text="",
                        remarks=remarks,
                        system_name=system_name,
                        expected_est_no=expected_est_no,
                        expected_order_no=expected_order_no,
                        workflow=workflow,
                        validation_only=validation_only,
                        analysis_result=analysis_result,
                    )
                else:
                    set_workflow_stage(
                        workflow,
                        "content_understanding",
                        "completed",
                        "AI semantic fields are ready for this document",
                    )
                    set_workflow_stage(
                        workflow,
                        "validation",
                        "not_applicable",
                        "PO detail preparation is not required",
                    )
                    candidates = analysis_result["estimation_numbers"]

                    app.logger.warning(
                        "UPLOAD DEBUG filename=%s document_type=%s candidates=%s expected_est_no=%s",
                        filename,
                        document_type,
                        candidates,
                        expected_est_no,
                    )

                    if validation_only:
                        set_workflow_stage(
                            workflow,
                            "database_insert",
                            "not_applicable",
                            "Read-only mode: no database write was performed",
                        )
                        inserted = {
                            "validation_only": True,
                            "document_rows_inserted": 0,
                            "detected_estimation_numbers": candidates,
                            "message": (
                                "AI document analysis completed in read-only mode; "
                                "company staff must enable insertion"
                            ),
                        }
                    else:
                        set_workflow_stage(
                            workflow,
                            "database_insert",
                            "processing",
                            "Inserting validated document",
                        )
                        inserted = insert_enquiry_document(
                            filename=filename,
                            file_path=temporary_path,
                            document_type=document_type,
                            candidate_est_nos=candidates,
                            remarks=remarks,
                            system_name=system_name,
                            expected_est_no=expected_est_no,
                            workflow=workflow,
                        )
                        set_workflow_stage(
                            workflow,
                            "database_insert",
                            "completed",
                            "Document inserted successfully",
                            {
                                "docid": inserted.get("docid"),
                                "estimation_numbers": inserted.get(
                                    "inserted_estimation_numbers",
                                    [],
                                ),
                            },
                        )

                app.logger.info(
                    "UPLOAD RESULT filename=%s document_type=%s "
                    "parent_rows=%s detail_rows=%s detail_status=%s",
                    filename,
                    document_type,
                    inserted.get("document_rows_processed", 1),
                    inserted.get("po_detail_rows_inserted", 0),
                    inserted.get("po_detail_status", "NOT_APPLICABLE"),
                )

                results.append({
                    "filename": filename,
                    "document_type": document_type,
                    "workflow": workflow,
                    "workflow_id": workflow_id,
                    **inserted,
                })
                record_workflow_event(
                    workflow,
                    "workflow_complete",
                    "completed",
                    "Upload workflow completed",
                    {
                        "document_type": document_type,
                        "validation_only": bool(
                            inserted.get("validation_only")
                        ),
                        "parent_row_count": inserted.get(
                            "document_rows_processed",
                            1,
                        ),
                        "detail_row_count": inserted.get(
                            "po_detail_rows_inserted",
                            0,
                        ),
                        "detail_status": inserted.get(
                            "po_detail_status",
                            "NOT_APPLICABLE",
                        ),
                        "duration_ms": workflow_duration_ms(workflow),
                    },
                )

            except Exception as exc:
                message = str(exc)

                is_duplicate = (
                    isinstance(exc, DocumentValidationError)
                    and re.search(
                        r"\balready (?:inserted|uploaded)\b",
                        message,
                        re.IGNORECASE,
                    )
                )

                if is_duplicate:
                    set_workflow_stage(
                        workflow,
                        "database_insert",
                        "skipped",
                        message,
                    )
                    fail_active_workflow_stage(workflow, message)
                    block_pending_workflow_stages(
                        workflow,
                        "Not required after duplicate detection",
                    )
                    skipped.append({
                        "filename": filename,
                        "error": message,
                        "workflow": workflow,
                        "workflow_id": workflow_id,
                    })
                    record_workflow_event(
                        workflow,
                        "workflow_complete",
                        "skipped",
                        f"Upload workflow skipped: {message}",
                        {"duration_ms": workflow_duration_ms(workflow)},
                    )
                    app.logger.info(
                        "Skipped existing document %s: %s",
                        filename,
                        message,
                    )
                else:
                    fail_active_workflow_stage(workflow, message)
                    block_pending_workflow_stages(
                        workflow,
                        "Blocked because an earlier stage did not complete",
                    )
                    app.logger.exception(
                        "Failed to process %s",
                        filename,
                    )
                    errors.append({
                        "filename": filename,
                        "error": message,
                        "workflow": workflow,
                        "workflow_id": workflow_id,
                    })
                    record_workflow_event(
                        workflow,
                        "workflow_complete",
                        "failed",
                        f"Upload workflow failed: {message}",
                        {"duration_ms": workflow_duration_ms(workflow)},
                    )
    finally:
        for temporary_path, workflow in temporary_paths:
            try:
                record_workflow_event(
                    workflow,
                    "temporary_file_cleanup",
                    "processing",
                    "Removing temporary upload file",
                )
                temporary_path.unlink(missing_ok=True)
                record_workflow_event(
                    workflow,
                    "temporary_file_cleanup",
                    "completed",
                    "Temporary upload file removed",
                )
                record_workflow_event(
                    workflow,
                    "workflow_closed",
                    "completed",
                    "Upload workflow and temporary-file cleanup finished",
                    {"duration_ms": workflow_duration_ms(workflow)},
                )
            except OSError:
                app.logger.warning("Could not remove temporary upload %s", temporary_path)
                record_workflow_event(
                    workflow,
                    "temporary_file_cleanup",
                    "failed",
                    "Temporary upload file could not be removed",
                )
                record_workflow_event(
                    workflow,
                    "workflow_closed",
                    "review_required",
                    "Upload workflow finished but temporary-file cleanup failed",
                    {"duration_ms": workflow_duration_ms(workflow)},
                )

    inserted_est_links = sum(
        len(
            document.get(
                "inserted_estimation_numbers",
                [],
            )
        )
        for document in results
    )

    skipped_est_links = sum(
        len(
            document.get(
                "skipped_existing_estimation_numbers",
                [],
            )
        )
        for document in results
    )

    inserted_document_count = sum(
        1
        for document in results
        if not document.get("validation_only")
    )
    validated_only_count = sum(
        1
        for document in results
        if document.get("validation_only")
    )

    if results:
        status = "partial" if errors else "success"
        status_code = 200
    elif skipped:
        status = "partial" if errors else "skipped"
        status_code = 200
    else:
        status = "failed"
        status_code = 422

    return (
        jsonify(
            {
                "status": status,
                "inserted": inserted_document_count,
                "validated_only_count": validated_only_count,
                "skipped_count": len(skipped),
                "failed_count": len(errors),
                "inserted_estimation_links":
                    inserted_est_links,
                "skipped_existing_estimation_links":
                    skipped_est_links,
                "matches": len(results),
                "documents": results,
                "skipped": skipped,
                "errors": errors,
                "redirect_url": url_for(
                    "fetch_details",
                    status=status,
                    inserted=inserted_document_count,
                ),
            }
        ),
        status_code,
    )



PENDING_DOCUMENT_PAGE_REPORTS = {
    "est": {
        "label": "Estimation Sheet",
        "query": EST_SHEET_PENDING_QUERY,
        "search_columns": (
            "ESTNO",
            "PARTYNAME",
            "COUNTNAME",
            "REFRENCENO",
            "REPNAME",
        ),
    },
    "mix": {
        "label": "Mixing Sheet",
        "query": MIXING_SHEET_PENDING_QUERY,
        "search_columns": (
            "ESTNO",
            "PARTYNAME",
            "COUNTNAME",
            "REFRENCENO",
            "REPNAME",
        ),
    },
    "po": {
        "label": "PO Document",
        "query": PO_DOCUMENT_PENDING_QUERY,
        "search_columns": (
            "RORDERNO",
            "ESTIMATIONNO",
            "PARTYNAME",
            "PARTYORDERNO",
            "COUNTNAME",
            "REFORDERNO",
            "REPNAME",
        ),
    },
}


def _positive_int_argument(name, default):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _pending_page_query(
    report,
    filter_text,
    page,
    page_size,
):
    base_query = report["query"].strip().rstrip(";")
    binds = {}
    filter_clause = ""

    if filter_text:
        predicates = [
            (
                "LOWER(NVL(TO_CHAR(pending_source."
                + column
                + "), '')) LIKE :pending_filter"
            )
            for column in report["search_columns"]
        ]
        filter_clause = " WHERE (" + " OR ".join(predicates) + ")"
        binds["pending_filter"] = f"%{filter_text.lower()}%"

    start_row = (page - 1) * page_size
    end_row = page * page_size
    binds["pending_start_row"] = start_row
    binds["pending_end_row"] = end_row

    # PENDING_TOTAL_ROWS is calculated before ROWNUM limits the current page.
    query = f"""
        SELECT *
        FROM (
            SELECT counted_rows.*, ROWNUM AS PENDING_ROW_NUMBER
            FROM (
                SELECT
                    pending_source.*,
                    COUNT(*) OVER() AS PENDING_TOTAL_ROWS
                FROM ({base_query}) pending_source
                {filter_clause}
            ) counted_rows
            WHERE ROWNUM <= :pending_end_row
        )
        WHERE PENDING_ROW_NUMBER > :pending_start_row
    """
    return query, binds




@app.route("/regularorder", methods=["GET"])
def regularorder():
    # Optimized single-report pending-document page.
    document_type = request.args.get("type", "est").strip().lower()
    report = PENDING_DOCUMENT_PAGE_REPORTS.get(document_type)

    if report is None:
        return redirect(url_for("regularorder", type="est"))

    page_size = _positive_int_argument("rows", 20)
    if page_size not in {10, 20, 50, 100}:
        page_size = 20

    page = _positive_int_argument("page", 1)
    filter_text = request.args.get("filter", "").strip()[:200]
    connection = None

    try:
        query, binds = _pending_page_query(
            report,
            filter_text,
            page,
            page_size,
        )
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.arraysize = page_size
            cursor.prefetchrows = page_size
            cursor.execute(query, binds)
            rows = rows_as_dicts(cursor)

        total_rows = (
            int(rows[0].get("pending_total_rows") or 0)
            if rows
            else 0
        )

        for row in rows:
            row.pop("pending_row_number", None)
            row.pop("pending_total_rows", None)

        if page > 1 and not rows:
            return redirect(
                url_for(
                    "regularorder",
                    type=document_type,
                    rows=page_size,
                    filter=filter_text or None,
                    page=1,
                )
            )

        has_previous = page > 1
        has_next = page * page_size < total_rows

        return render_template(
            "regularorder.html",
            document_type=document_type,
            document_label=report["label"],
            rows=rows,
            total_rows=total_rows,
            page=page,
            page_size=page_size,
            filter_text=filter_text,
            has_previous=has_previous,
            has_next=has_next,
        )
    except Exception as exc:
        app.logger.exception(
            "Failed to fetch %s pending document attachments",
            document_type,
        )
        return (
            render_template(
                "regularorder.html",
                document_type=document_type,
                document_label=report["label"],
                rows=[],
                total_rows=0,
                page=page,
                page_size=page_size,
                filter_text=filter_text,
                has_previous=False,
                has_next=False,
                error=str(exc),
            ),
            500,
        )
    finally:
        if connection is not None:
            connection.close()




@app.route("/process_regularorder", methods=["POST"])
def process_regularorder():
    return upload_files()


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "Upload exceeds the configured size limit"}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
