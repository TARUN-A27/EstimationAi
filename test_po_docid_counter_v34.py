"""PO DOCID counter: a counter ahead of the table is legitimate.

Cancelling a PO document deletes its row (a trigger archives it) but the DOCID
stays issued, so REGULARORDER_PODOCUMENT_CONFIG.MAXDOCID can sit ahead of
MAX(DOCID). The ERP then continues from counter + 1. The upload must do the
same and must never reuse an issued DOCID or lower the counter.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as production_app


class ScriptedCursor:
    """Answers only the statements the PO parent path issues; records writes."""

    def __init__(self, state):
        self.state = state
        self.rows = []
        self.rowcount = 0
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, *args, **binds):
        if args and isinstance(args[0], dict):
            binds = {**args[0], **binds}
        text = " ".join(sql.split()).upper()
        state = self.state
        self.rowcount = 0
        if text.startswith(("INSERT", "UPDATE", "DELETE")):
            state["writes"].append((" ".join(text.split()[:3]), binds))
            self.rowcount = 1
            if text.startswith("UPDATE REGULARORDER_PODOCUMENT_CONFIG"):
                self.rowcount = int(binds["old_max_docid"] == state["counter"])
        elif "REGULARORDER_PODOCUMENT_CONFIG" in text:
            self.rows = [(state["counter"],)]
        elif "NEXTVAL" in text:
            self.rows = [(state["max_id"] + 1,)]
        elif "NVL(MAX(ID), 0)" in text:
            self.rows = [(state["max_id"], state["table_max_docid"])]
        elif "FROM REGULARORDER_PODOCUMENT WHERE" in text:
            self.rows = state["existing_parents"]
        elif "FROM REGULARORDER WHERE ESTIMATIONNO" in text:
            self.rows = [(state["order_no"], "")]
        else:  # certificate master and Oracle expected rows
            self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0]


class ScriptedConnection:
    def __init__(self, **state):
        self.state = {
            "writes": [],
            "existing_parents": [],
            "order_no": "LM900101",
            "max_id": 24156,
            **state,
        }
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return ScriptedCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def writes(self, kind):
        return [binds for name, binds in self.state["writes"] if kind in name]


def upload(connection):
    analysis = {
        "document_estimation_numbers": [180101],
        "line_estimation_numbers": [],
        "estimation_numbers": [180101],
        "estimation_mappings": [],
        "order_lines": [],
    }
    with tempfile.TemporaryDirectory() as folder:
        pdf = Path(folder) / "po.pdf"
        pdf.write_bytes(b"%PDF-test")
        with patch.object(
            production_app, "get_db_connection", return_value=connection
        ), patch.dict(os.environ, {"SCM_ENTRY_USER_CODE": "1"}):
            return production_app.insert_regular_order_document(
                filename="po.pdf",
                file_path=pdf,
                text="",
                remarks="test",
                system_name="test",
                analysis_result=analysis,
            )


class PoDocidCounterV34Tests(unittest.TestCase):
    def test_counter_ahead_of_table_allocates_from_the_counter(self):
        # The incident: three cancelled documents left MAXDOCID at 24053 while
        # the table maximum fell back to 24050. 24051-24053 stay issued.
        connection = ScriptedConnection(counter=24053, table_max_docid=24050)
        result = upload(connection)
        self.assertEqual(result["docids"], [24054])
        self.assertEqual(connection.writes("INSERT")[0]["document_docid"], 24054)
        config = connection.writes("CONFIG")
        self.assertEqual(
            (config[0]["old_max_docid"], config[0]["new_max_docid"]),
            (24053, 24054),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_replacing_a_parent_never_lowers_a_counter_that_is_ahead(self):
        connection = ScriptedConnection(
            counter=24053,
            table_max_docid=24050,
            existing_parents=[(24100, 24020, "old.pdf")],
        )
        result = upload(connection)
        self.assertEqual(result["document_rows_replaced"], 1)
        self.assertEqual(connection.writes("INSERT"), [])
        config = connection.writes("CONFIG")
        self.assertEqual(config[0]["new_max_docid"], 24053)

    def test_counter_behind_the_table_is_still_refused_without_writing(self):
        connection = ScriptedConnection(counter=24048, table_max_docid=24050)
        with self.assertRaisesRegex(
            production_app.DocumentValidationError, "counter mismatch"
        ):
            upload(connection)
        self.assertEqual(connection.state["writes"], [])
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_counter_equal_to_the_table_behaves_as_before(self):
        connection = ScriptedConnection(counter=24050, table_max_docid=24050)
        result = upload(connection)
        self.assertEqual(result["docids"], [24051])
        config = connection.writes("CONFIG")
        self.assertEqual(
            (config[0]["old_max_docid"], config[0]["new_max_docid"]),
            (24050, 24051),
        )


if __name__ == "__main__":
    unittest.main()
