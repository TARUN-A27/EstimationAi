from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SENSITIVE_DETAIL_KEYS = {
    "blob_data",
    "image",
    "pdf_text",
    "text",
    "password",
    "secret",
    "token",
    "key",
    "detail_rows",
    "po_document_details",
    "party_name",
    "reference_number",
    "mapping_reference_number",
    "required_quantity",
    "count_name",
    "certification",
    "net_rate",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_audit_value(value: Any, key: str = "") -> Any:
    """Return JSON-safe workflow metadata without document contents."""

    normalized_key = key.strip().lower()
    if normalized_key in SENSITIVE_DETAIL_KEYS:
        if isinstance(value, (list, tuple, set, dict)):
            return {"redacted": True, "item_count": len(value)}
        return {"redacted": True}

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        return {
            str(child_key): safe_audit_value(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [safe_audit_value(child) for child in list(value)[:100]]
    return str(value)[:500]


class WorkflowAuditStore:
    """Thread-safe live workflow registry with an append-only JSONL audit."""

    def __init__(
        self,
        log_path: Path,
        *,
        live_log_path: Path | None = None,
        maximum_live_workflows: int = 500,
        maximum_log_bytes: int = 50 * 1024 * 1024,
        maximum_log_backups: int = 10,
    ) -> None:
        self.log_path = Path(log_path)
        self.live_log_path = (
            Path(live_log_path)
            if live_log_path is not None
            else self.log_path.with_name("upload_workflow_live.log")
        )
        self.maximum_live_workflows = maximum_live_workflows
        self.maximum_log_bytes = maximum_log_bytes
        self.maximum_log_backups = max(1, maximum_log_backups)
        self._lock = threading.RLock()
        self._live: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, workflow_id: str, filename: str) -> dict[str, Any]:
        state = {
            "workflow_id": workflow_id,
            "filename": filename,
            "started_at": utc_timestamp(),
            "updated_at": None,
            "stages": {},
            "events": [],
        }
        with self._lock:
            self._live[workflow_id] = state
            self._live.move_to_end(workflow_id)
            self._trim_live_state()
        return self.record(
            workflow_id,
            filename=filename,
            stage="request",
            label="Upload request",
            status="processing",
            message="Upload workflow started",
        )

    def record(
        self,
        workflow_id: str,
        *,
        filename: str,
        stage: str,
        label: str,
        status: str,
        message: str,
        details: Any = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._live.setdefault(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "filename": filename,
                    "started_at": utc_timestamp(),
                    "updated_at": None,
                    "stages": {},
                    "events": [],
                },
            )
            sequence = len(state["events"]) + 1
            event = {
                "timestamp": utc_timestamp(),
                "sequence": sequence,
                "workflow_id": workflow_id,
                "filename": filename,
                "stage": stage,
                "label": label,
                "status": status,
                "message": str(message)[:1000],
            }
            if details is not None:
                event["details"] = safe_audit_value(details)

            state["filename"] = filename
            state["updated_at"] = event["timestamp"]
            state["stages"][stage] = {
                "label": label,
                "status": status,
                "message": event["message"],
                "updated_at": event["timestamp"],
            }
            state["events"].append(event)
            self._live.move_to_end(workflow_id)
            self._trim_live_state()
            self._append_event(event)
            self._append_live_event(event)
            return dict(event)

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._live.get(workflow_id)
            if state is not None:
                return json.loads(json.dumps(state))

        events = self._read_events(workflow_id)
        if not events:
            return None
        stages = {}
        for event in events:
            stages[event["stage"]] = {
                "label": event["label"],
                "status": event["status"],
                "message": event["message"],
                "updated_at": event["timestamp"],
            }
        return {
            "workflow_id": workflow_id,
            "filename": events[-1].get("filename", ""),
            "started_at": events[0]["timestamp"],
            "updated_at": events[-1]["timestamp"],
            "stages": stages,
            "events": events,
        }

    def _trim_live_state(self) -> None:
        while len(self._live) > self.maximum_live_workflows:
            self._live.popitem(last=False)

    def _append_event(self, event: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            payload = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
            self._restrict_permissions(self.log_path)
        except OSError:
            # Auditing must never prevent the document transaction.
            return

    def _rotate_if_needed(self) -> None:
        try:
            if self.log_path.stat().st_size < self.maximum_log_bytes:
                return
        except FileNotFoundError:
            return
        self._rotate_path(self.log_path)

    def _append_live_event(self, event: dict[str, Any]) -> None:
        try:
            self.live_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_path_if_needed(self.live_log_path)
            line = (
                f"{event['timestamp']} "
                f"workflow={event['workflow_id']} "
                f"file={event['filename']} "
                f"stage={event['stage']} "
                f"status={event['status']} "
                f"message={event['message']}\n"
            )
            with self.live_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            self._restrict_permissions(self.live_log_path)
        except OSError:
            return

    def _rotate_path_if_needed(self, path: Path) -> None:
        try:
            if path.stat().st_size < self.maximum_log_bytes:
                return
        except FileNotFoundError:
            return
        self._rotate_path(path)

    def _rotate_path(self, path: Path) -> None:
        try:
            oldest = path.with_suffix(
                path.suffix + f".{self.maximum_log_backups}"
            )
            oldest.unlink(missing_ok=True)
            for index in range(self.maximum_log_backups - 1, 0, -1):
                source = path.with_suffix(path.suffix + f".{index}")
                destination = path.with_suffix(
                    path.suffix + f".{index + 1}"
                )
                if source.exists():
                    os.replace(source, destination)
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            return

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o640)
        except OSError:
            return

    def _read_events(self, workflow_id: str) -> list[dict[str, Any]]:
        events = []
        paths = [
            self.log_path.with_suffix(self.log_path.suffix + f".{index}")
            for index in range(self.maximum_log_backups, 0, -1)
        ] + [self.log_path]
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("workflow_id") == workflow_id:
                            events.append(event)
            except FileNotFoundError:
                continue
        events.sort(key=lambda event: int(event.get("sequence", 0)))
        return events
