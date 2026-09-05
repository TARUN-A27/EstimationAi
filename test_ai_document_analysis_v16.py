from __future__ import annotations

import ast
import importlib.metadata
import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as production_app
from app import resolve_po_estimation_candidates
from po_content_validation import (
    CONTENT_UNDERSTANDING_SOURCE,
    ContentAnalysisError,
    analyze_po_document,
    build_extracted_po_document_detail_result,
    normalize_content_understanding_result,
)
from workflow_audit import WorkflowAuditStore


PROJECT_DIR = Path(__file__).resolve().parent
REDACTED_FIXTURE = (
    PROJECT_DIR
    / "tests"
    / "fixtures"
    / "azure_content_understanding_po_redacted.json"
)


class MappingCursor:
    def __init__(self, rows_by_estimation):
        self.rows_by_estimation = rows_by_estimation
        self.estimation_no = None

    def execute(self, _query, **binds):
        self.estimation_no = binds["estimation_no"]

    def fetchall(self):
        return self.rows_by_estimation.get(self.estimation_no, [])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class MappingConnection:
    def __init__(self, rows_by_estimation):
        self.mapping_cursor = MappingCursor(rows_by_estimation)

    def cursor(self):
        return self.mapping_cursor

    def close(self):
        return None

    def rollback(self):
        return None


def scalar(value):
    if isinstance(value, int):
        return {"type": "integer", "valueInteger": value}
    return {"type": "string", "valueString": value}


def response(document_type=None, estimations=(), lines=(), party_name=None):
    fields = {}
    if document_type is not None:
        fields["DocumentType"] = scalar(document_type)
    if estimations:
        fields["EstimationNumbers"] = {
            "type": "array",
            "valueArray": [scalar(value) for value in estimations],
        }
    if party_name is not None:
        fields["PartyName"] = scalar(party_name)
    if lines:
        items = []
        for line in lines:
            properties = {}
            aliases = {
                "estimate_number": "EstimateNumber",
                "party_name": "PartyName",
                "reference_number": "ReferenceNumber",
                "required_quantity": "RequiredQuantity",
                "count": "CountName",
                "certification": "Certification",
                "confirm_rate": "NetRate",
            }
            for key, field_name in aliases.items():
                if key in line and line[key] is not None:
                    properties[field_name] = scalar(line[key])
            items.append({"type": "object", "valueObject": properties})
        fields["OrderLines"] = {"type": "array", "valueArray": items}
    return {"contents": [{"kind": "document", "fields": fields}], "warnings": []}


class NormalizationV16Tests(unittest.TestCase):
    def test_estimation_sheet_classified_by_ai(self):
        result = normalize_content_understanding_result(
            response("Estimation Sheet", [175535])
        )
        self.assertEqual(result["document_type"], "estimation")

    def test_mixing_sheet_classified_by_ai(self):
        result = normalize_content_understanding_result(
            response("Mixing Sheet", [175535])
        )
        self.assertEqual(result["document_type"], "mix")

    def test_po_classified_by_ai(self):
        result = normalize_content_understanding_result(
            response("Purchase Order", [175535])
        )
        self.assertEqual(result["document_type"], "po")

    def test_multiple_est_numbers_are_extracted_in_order(self):
        result = normalize_content_understanding_result(
            response("estimation", [175535, 175536])
        )
        self.assertEqual(result["estimation_numbers"], [175535, 175536])

    def test_exact_duplicate_est_numbers_are_removed(self):
        result = normalize_content_understanding_result(
            response("mix", ["175535", "EST 175535", 175536])
        )
        self.assertEqual(result["estimation_numbers"], [175535, 175536])

    def test_combined_po_keeps_document_and_line_est_numbers_separate(self):
        result = normalize_content_understanding_result(
            response(
                "po",
                [175535],
                [
                    {"estimate_number": "175535"},
                    {"estimate_number": "175536"},
                ],
            )
        )
        self.assertEqual(result["document_estimation_numbers"], [175535])
        self.assertEqual(result["line_estimation_numbers"], [175535, 175536])

    def test_multiple_order_lines_remain_separate(self):
        result = normalize_content_understanding_result(
            response(
                "po",
                [175535],
                [
                    {"estimate_number": "175535", "count": "30S"},
                    {"estimate_number": "175535", "count": "34S"},
                ],
            )
        )
        self.assertEqual(len(result["order_lines"]), 2)
        self.assertEqual(result["order_lines"][1]["count"], ["34S"])

    def test_missing_optional_detail_fields_remain_null(self):
        normalized = normalize_content_understanding_result(
            response("po", [175535], [{"estimate_number": "175535"}])
        )
        detail = build_extracted_po_document_detail_result(
            normalized,
            [{"estimation_no": 175535, "regular_order_number": "LM111535"}],
        )["rows"][0]
        for key in (
            "party_name",
            "reference_number",
            "required_quantity",
            "count_name",
            "certification",
            "net_rate",
        ):
            self.assertIsNone(detail[key])

    def test_unknown_document_type_requires_review(self):
        result = normalize_content_understanding_result(response())
        self.assertEqual(result["document_type"], "unknown")

    def test_order_lines_do_not_replace_missing_document_type(self):
        result = normalize_content_understanding_result(
            response(None, [175535], [{"estimate_number": "175535"}])
        )
        self.assertEqual(result["document_type"], "unknown")

    def test_missing_all_est_fields_stays_empty(self):
        result = normalize_content_understanding_result(response("po"))
        self.assertEqual(result["document_estimation_numbers"], [])
        self.assertEqual(result["line_estimation_numbers"], [])

    def test_redacted_real_shape_fixture_is_normalized(self):
        result = normalize_content_understanding_result(
            json.loads(REDACTED_FIXTURE.read_text(encoding="utf-8"))
        )
        self.assertEqual(result["document_type"], "po")
        self.assertEqual(result["document_estimation_numbers"], [175535])
        self.assertEqual(len(result["order_lines"]), 1)

    def test_filename_never_supplies_an_estimation_number(self):
        result = normalize_content_understanding_result(response())
        self.assertEqual(result["estimation_numbers"], [])
        self.assertEqual(
            production_app.filename_document_type("EST_999999.pdf"),
            "estimation",
        )

    def test_unassociated_multi_est_line_is_reviewed_individually(self):
        normalized = normalize_content_understanding_result(
            response(
                "po",
                [175535, 175536],
                [
                    {"estimate_number": "175535", "count": "30S"},
                    {"count": "34S"},
                ],
            )
        )
        details = build_extracted_po_document_detail_result(
            normalized,
            [
                {"estimation_no": 175535, "regular_order_number": "LM111535"},
                {"estimation_no": 175536, "regular_order_number": "LM111536"},
            ],
        )
        self.assertEqual(len(details["rows"]), 1)
        self.assertEqual(len(details["rejected_lines"]), 1)

    def test_explicit_mismatching_est_is_rejected_for_single_parent(self):
        normalized = normalize_content_understanding_result(
            response(
                "po",
                [175535],
                [{"estimate_number": "175536", "count": "30S"}],
            )
        )
        details = build_extracted_po_document_detail_result(
            normalized,
            [{"estimation_no": 175535, "regular_order_number": "LM111535"}],
        )
        self.assertEqual(details["rows"], [])
        self.assertEqual(len(details["rejected_lines"]), 1)


class EstimationResolutionV16Tests(unittest.TestCase):
    def validation_only_insert(self, normalized, rows_by_estimation, **options):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "po.pdf"
            document.write_bytes(b"%PDF-test")
            with patch.object(
                production_app,
                "get_db_connection",
                return_value=MappingConnection(rows_by_estimation),
            ), patch.dict(os.environ, {"SCM_ENTRY_USER_CODE": "1"}):
                return production_app.insert_regular_order_document(
                    filename="po.pdf",
                    file_path=document,
                    text="",
                    remarks="test",
                    system_name="test",
                    validation_only=True,
                    analysis_result=normalized,
                    **options,
                )

    def test_valid_document_est_and_invalid_line_est_resolve_independently(self):
        resolution = resolve_po_estimation_candidates(
            MappingCursor({175535: [("LM111535", "REF-535")]}),
            [175535],
            [999999],
        )
        self.assertEqual(resolution["order_numbers"], ["LM111535"])
        self.assertEqual(len(resolution["rejected_estimations"]), 1)
        self.assertEqual(
            resolution["rejected_estimations"][0]["source"],
            "line",
        )

    def test_invalid_line_does_not_block_valid_parent_or_detail(self):
        normalized = normalize_content_understanding_result(
            response(
                "po",
                [175535],
                [
                    {"estimate_number": "175535", "count": "30S"},
                    {"estimate_number": "999999", "count": "34S"},
                ],
            )
        )
        resolution = resolve_po_estimation_candidates(
            MappingCursor({175535: [("LM111535", "")]}),
            normalized["document_estimation_numbers"],
            normalized["line_estimation_numbers"],
        )
        details = build_extracted_po_document_detail_result(
            normalized,
            resolution["mappings"],
        )
        self.assertEqual(resolution["order_numbers"], ["LM111535"])
        self.assertEqual(len(details["rows"]), 1)
        self.assertEqual(len(details["rejected_lines"]), 1)

        inserted = self.validation_only_insert(
            normalized,
            {175535: [("LM111535", "")]},
        )
        self.assertEqual(inserted["regular_order_numbers"], ["LM111535"])
        self.assertEqual(len(inserted["po_document_details"]), 1)
        self.assertEqual(len(inserted["po_detail_rejected_lines"]), 1)

    def test_selected_order_excludes_unrelated_extracted_order(self):
        resolution = resolve_po_estimation_candidates(
            MappingCursor(
                {
                    175535: [("LM111535", "REF-535")],
                    175536: [("LM111536", "REF-536")],
                }
            ),
            [175535, 175536],
            [175535, 175536],
            expected_est_no=175535,
            expected_order_no="LM111535",
        )
        self.assertEqual(resolution["order_numbers"], ["LM111535"])
        self.assertEqual(len(resolution["excluded_mappings"]), 1)

    def test_unrelated_parent_never_reaches_insert_or_replace_loop(self):
        source = inspect.getsource(
            production_app.insert_regular_order_document
        )
        self.assertIn(
            'order_numbers = resolution["order_numbers"]',
            source,
        )
        resolution = resolve_po_estimation_candidates(
            MappingCursor(
                {
                    175535: [("LM111535", "")],
                    175536: [("LM111536", "")],
                }
            ),
            [175535, 175536],
            [],
            expected_est_no=175535,
            expected_order_no="LM111535",
        )
        self.assertNotIn("LM111536", resolution["order_numbers"])

        normalized = normalize_content_understanding_result(
            response(
                "po",
                [175535, 175536],
                [
                    {"estimate_number": "175535"},
                    {"estimate_number": "175536"},
                ],
            )
        )
        inserted = self.validation_only_insert(
            normalized,
            {
                175535: [("LM111535", "")],
                175536: [("LM111536", "")],
            },
            expected_est_no=175535,
            expected_order_no="LM111535",
        )
        self.assertEqual(inserted["regular_order_numbers"], ["LM111535"])
        self.assertNotIn("LM111536", inserted["regular_order_numbers"])


class ServiceBoundaryV16Tests(unittest.TestCase):
    def _analyze_with_poller(self, poller, audit=None):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "upload.pdf"
            document.write_bytes(b"%PDF-test")
            client = MagicMock()
            client.begin_analyze_binary.return_value = poller
            with patch(
                "azure.ai.contentunderstanding.ContentUnderstandingClient",
                return_value=client,
            ) as client_factory, patch.dict(
                os.environ,
                {
                    "CONTENTUNDERSTANDING_ENDPOINT": "https://example.invalid",
                    "CONTENTUNDERSTANDING_KEY": "secret",
                    "CONTENTUNDERSTANDING_ANALYZER_ID": "analyzer",
                },
            ):
                return (
                    analyze_po_document(document, audit=audit),
                    client,
                    client_factory,
                )

    def test_pdf_bytes_are_submitted_directly(self):
        poller = MagicMock()
        poller.result.return_value = response("po", [175535])
        poller.status.return_value = "Succeeded"
        _, client, client_factory = self._analyze_with_poller(poller)
        kwargs = client.begin_analyze_binary.call_args.kwargs
        self.assertEqual(kwargs["binary_input"], b"%PDF-test")
        self.assertEqual(kwargs["content_type"], "application/pdf")
        self.assertNotIn("polling_interval", kwargs)
        self.assertEqual(
            client_factory.call_args.kwargs["polling_interval"],
            2,
        )

    def test_real_sdk_version_and_method_signature(self):
        from azure.ai.contentunderstanding import ContentUnderstandingClient

        self.assertEqual(
            importlib.metadata.version("azure-ai-contentunderstanding"),
            "1.1.0",
        )
        signature = inspect.signature(
            ContentUnderstandingClient.begin_analyze_binary
        )
        self.assertIn("binary_input", signature.parameters)
        self.assertIn("content_type", signature.parameters)
        requirements = (PROJECT_DIR / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("azure-ai-contentunderstanding==1.1.0", requirements)

    def test_ai_timeout_is_safe(self):
        poller = MagicMock()
        poller.result.side_effect = TimeoutError("internal timeout")
        events = []
        with self.assertRaisesRegex(ContentAnalysisError, "timed out"):
            self._analyze_with_poller(
                poller,
                lambda stage, status, message, details: events.append(
                    (stage, status)
                ),
            )
        self.assertIn(("ai_polling", "failed"), events)
        self.assertIn(("ai_response", "failed"), events)
        self.assertIn(("ai_analysis_request", "failed"), events)

    def test_failed_or_cancelled_response_is_safe(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                poller = MagicMock()
                poller.result.return_value = response("po", [175535])
                poller.status.return_value = status
                events = []
                with self.assertRaises(ContentAnalysisError):
                    self._analyze_with_poller(
                        poller,
                        lambda stage, event_status, message, details: (
                            events.append((stage, event_status))
                        ),
                    )
                self.assertIn(("ai_polling", "failed"), events)
                self.assertIn(("ai_response", "failed"), events)
                self.assertIn(("ai_analysis_request", "failed"), events)

    def test_malformed_ai_response_is_rejected(self):
        with self.assertRaisesRegex(ContentAnalysisError, "malformed"):
            normalize_content_understanding_result({"contents": ["bad"]})

    def test_production_upload_has_no_ocr_extraction_call(self):
        tree = ast.parse((PROJECT_DIR / "app.py").read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        forbidden = {
            "extract_pdf_text",
            "extract_estimation_numbers",
            "extract_po_stamp_estimation_numbers",
            "extract_expected_po_estimation_number",
            "get_azure_client",
        }
        for function_name in ("upload_files", "insert_regular_order_document"):
            calls = {
                node.func.id
                for node in ast.walk(functions[function_name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertTrue(forbidden.isdisjoint(calls))

    def test_one_analysis_result_is_reused_for_po_workflow(self):
        normalized = {
            "document_type": "po",
            "estimation_numbers": [175535],
            "document_estimation_numbers": [175535],
            "line_estimation_numbers": [],
            "order_lines": [],
            "party_names": [],
            "analysis_source": CONTENT_UNDERSTANDING_SOURCE,
            "warnings": [],
            "analyzer_configuration": "CONTENTUNDERSTANDING_ANALYZER_ID",
        }
        with production_app.app.test_client() as client, patch.object(
            production_app, "analyze_uploaded_document", return_value=normalized
        ) as analyzer, patch.object(
            production_app,
            "insert_regular_order_document",
            return_value={
                "validation_only": True,
                "document_rows_inserted": 0,
                "estimation_numbers": [175535],
            },
        ) as insertion:
            result = client.post(
                "/upload",
                data={"files[]": (io.BytesIO(b"%PDF-test"), "po.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(result.status_code, 200)
        analyzer.assert_called_once()
        self.assertIs(insertion.call_args.kwargs["analysis_result"], normalized)

    def test_ai_audit_stages_are_emitted(self):
        poller = MagicMock()
        poller.result.return_value = response("po", [175535])
        poller.status.return_value = "Succeeded"
        events = []
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "upload.pdf"
            document.write_bytes(b"%PDF-test")
            client = MagicMock()
            client.begin_analyze_binary.return_value = poller
            with patch(
                "azure.ai.contentunderstanding.ContentUnderstandingClient",
                return_value=client,
            ), patch.dict(
                os.environ,
                {
                    "CONTENTUNDERSTANDING_ENDPOINT": "https://example.invalid",
                    "CONTENTUNDERSTANDING_KEY": "secret",
                    "CONTENTUNDERSTANDING_ANALYZER_ID": "analyzer",
                },
            ):
                analyze_po_document(
                    document,
                    audit=lambda stage, status, message, details: events.append(
                        (stage, status, details)
                    ),
                )
        stages = {event[0] for event in events}
        self.assertTrue(
            {
                "ai_analysis_request",
                "ai_pdf_submission",
                "ai_request_accepted",
                "ai_polling",
                "ai_response",
            }.issubset(stages)
        )
        request_event = next(
            event for event in events if event[0] == "ai_analysis_request"
        )
        self.assertEqual(
            request_event[2],
            {"analyzer_configuration": "CONTENTUNDERSTANDING_ANALYZER_ID"},
        )
        self.assertNotIn("secret", str(request_event[2]).lower())
        self.assertNotIn("analyzer_id", request_event[2])

    def test_v15_structured_and_live_auditing_remains_active(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowAuditStore(Path(directory) / "workflow.jsonl")
            store.start("v16", "po.pdf")
            store.record(
                "v16",
                filename="po.pdf",
                stage="ai_response",
                label="AI response",
                status="completed",
                message="AI response received",
            )
            self.assertTrue(store.log_path.exists())
            self.assertTrue(store.live_log_path.exists())

    def test_existing_parent_and_replacement_plans_remain_intact(self):
        self.assertIsNone(production_app.plan_existing_po_parent("LM1", []))
        plan = production_app.plan_existing_po_replacement(
            "LM1", [(7, 9, "old.pdf")], [(11, 9, "LM1", 175535)]
        )
        self.assertEqual(plan["detail_ids"], [11])


if __name__ == "__main__":
    unittest.main()
