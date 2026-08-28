from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from workflow_audit import WorkflowAuditStore


BASE_DIR = Path(__file__).resolve().parent
APP_PATH = BASE_DIR / "app.py"
TEMPLATE_PATH = BASE_DIR / "templates" / "estimation.html"


class WorkflowAuditStoreV15Tests(unittest.TestCase):
    def make_store(self, directory: Path, **options) -> WorkflowAuditStore:
        return WorkflowAuditStore(
            directory / "workflow.jsonl",
            live_log_path=directory / "workflow_live.log",
            **options,
        )

    def test_each_event_is_saved_to_history_and_live_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store = self.make_store(directory)
            store.start("workflow-1", "purchase-order.pdf")
            store.record(
                "workflow-1",
                filename="purchase-order.pdf",
                stage="po_detail_insert",
                label="PO detail insert",
                status="completed",
                message="Inserted PO detail row 1 of 1",
                details={"detail_id": 7},
            )

            history = [
                json.loads(line)
                for line in store.log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            live_text = store.live_log_path.read_text(encoding="utf-8")

            self.assertEqual([event["sequence"] for event in history], [1, 2])
            self.assertEqual(history[-1]["stage"], "po_detail_insert")
            self.assertIn("stage=request", live_text)
            self.assertIn("stage=po_detail_insert", live_text)

    def test_document_values_and_credentials_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store = self.make_store(directory)
            store.start("workflow-2", "purchase-order.pdf")
            store.record(
                "workflow-2",
                filename="purchase-order.pdf",
                stage="content_understanding",
                label="Content Understanding",
                status="completed",
                message="Extraction completed",
                details={
                    "party_name": "PRIVATE PARTY",
                    "reference_number": "PRIVATE REFERENCE",
                    "token": "PRIVATE TOKEN",
                    "detail_rows": [{"net_rate": 25}],
                    "order_line_count": 1,
                },
            )

            payload = store.log_path.read_text(encoding="utf-8")
            event = json.loads(payload.splitlines()[-1])

            self.assertNotIn("PRIVATE PARTY", payload)
            self.assertNotIn("PRIVATE REFERENCE", payload)
            self.assertNotIn("PRIVATE TOKEN", payload)
            self.assertTrue(event["details"]["party_name"]["redacted"])
            self.assertEqual(event["details"]["order_line_count"], 1)

    def test_log_files_have_restricted_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            store.start("workflow-3", "purchase-order.pdf")

            history_mode = stat.S_IMODE(store.log_path.stat().st_mode)
            live_mode = stat.S_IMODE(store.live_log_path.stat().st_mode)
            self.assertEqual(history_mode, 0o640)
            self.assertEqual(live_mode, 0o640)

    def test_rotated_history_remains_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(
                Path(temporary_directory),
                maximum_log_bytes=1,
                maximum_log_backups=3,
            )
            store.start("workflow-4", "purchase-order.pdf")
            store.record(
                "workflow-4",
                filename="purchase-order.pdf",
                stage="ocr_page",
                label="OCR page",
                status="completed",
                message="OCR page completed",
            )

            reloaded_store = self.make_store(
                Path(temporary_directory),
                maximum_log_bytes=1,
                maximum_log_backups=3,
            )
            snapshot = reloaded_store.get("workflow-4")
            self.assertIsNotNone(snapshot)
            self.assertEqual(len(snapshot["events"]), 2)


class WorkflowAuditIntegrationV15Tests(unittest.TestCase):
    def test_upload_and_database_workflows_are_instrumented(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        required_stages = {
            "temporary_file",
            "pdf_render",
            "ocr_page",
            "document_classification",
            "stamp_ocr_page",
            "content_understanding",
            "oracle_connection",
            "po_parent_upsert",
            "po_parent_commit",
            "po_detail_insert",
            "po_detail_commit",
            "po_detail_rollback",
            "temporary_file_cleanup",
            "workflow_complete",
            "workflow_closed",
        }
        for stage in required_stages:
            self.assertIn(f'"{stage}"', source)

    def test_audit_is_backend_only(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("upload_workflow.jsonl", template)
        self.assertNotIn("upload_workflow_live.log", template)
        self.assertNotIn("workflow/live", template)


if __name__ == "__main__":
    unittest.main()
