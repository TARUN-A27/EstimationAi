"""PO DOCID: MAXDOCID + 1 from the locked configuration row, as the ERP does.

The next DOCID is the counter plus one and is never compared with the table.
Cancelling a PO document deletes its row (a trigger archives it) but the DOCID
stays issued, so REGULARORDER_PODOCUMENT_CONFIG.MAXDOCID can legitimately sit
ahead of the table. DOCID is unique in the table, so a counter that is ever
behind it fails the INSERT and is reported clearly.
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import oracledb

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
        state["statements"].append(text)
        self.rowcount = 0
        if text.startswith(("INSERT", "UPDATE", "DELETE")):
            if text.startswith("INSERT INTO REGULARORDER_PODOCUMENT") and state["insert_error"]:
                raise state["insert_error"]
            state["writes"].append((" ".join(text.split()[:3]), binds))
            self.rowcount = 1
            if text.startswith("UPDATE REGULARORDER_PODOCUMENT_CONFIG"):
                self.rowcount = int(binds["old_max_docid"] == state["counter"])
        elif "REGULARORDER_PODOCUMENT_CONFIG" in text:
            self.rows = [(state["counter"],)]
        elif "NEXTVAL" in text:
            self.rows = [(state["max_id"] + 1,)]
        elif "NVL(MAX(ID), 0)" in text:
            # Answer either shape of the query faithfully: the table's DOCID
            # tops out at 24050 (what the incident's table looked like).
            self.rows = (
                [(state["max_id"], 24050)]
                if "MAX(DOCID)" in text
                else [(state["max_id"],)]
            )
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
            "statements": [],
            "writes": [],
            "existing_parents": [],
            "order_no": "LM900101",
            "insert_error": None,
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
    def test_next_docid_is_the_counter_plus_one_and_the_counter_follows(self):
        connection = ScriptedConnection(counter=24050)
        result = upload(connection)
        self.assertEqual(result["docids"], [24051])
        config = connection.writes("CONFIG")
        self.assertEqual(
            (config[0]["old_max_docid"], config[0]["new_max_docid"]),
            (24050, 24051),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        # The counter is the only DOCID source: no sequence is involved.
        self.assertFalse(
            any("DOCID_SEQ" in s for s in connection.state["statements"])
        )

    def test_counter_ahead_of_the_table_is_normal(self):
        # The incident: cancelled POs left MAXDOCID at 24053 while the table's
        # highest DOCID fell back to 24050. 24051-24053 stay issued.
        connection = ScriptedConnection(counter=24053)
        result = upload(connection)
        self.assertEqual(result["docids"], [24054])
        self.assertEqual(connection.writes("INSERT")[0]["document_docid"], 24054)

    def test_the_table_maximum_is_never_read_or_compared(self):
        for counter in (24053, 24048):  # ahead of, and behind, a table at 24050
            with self.subTest(counter=counter):
                connection = ScriptedConnection(counter=counter)
                result = upload(connection)
                self.assertEqual(result["docids"], [counter + 1])
                self.assertFalse(
                    any("MAX(DOCID)" in s for s in connection.state["statements"])
                )

    def test_the_counter_row_is_locked_before_the_insert_like_the_erp(self):
        connection = ScriptedConnection(counter=24050)
        upload(connection)
        statements = connection.state["statements"]
        lock = next(
            i for i, s in enumerate(statements)
            if "FROM REGULARORDER_PODOCUMENT_CONFIG FOR UPDATE" in s
        )
        insert = next(
            i for i, s in enumerate(statements)
            if s.startswith("INSERT INTO REGULARORDER_PODOCUMENT")
        )
        self.assertLess(lock, insert)

    def test_replacing_a_parent_never_lowers_a_counter_that_is_ahead(self):
        connection = ScriptedConnection(
            counter=24053, existing_parents=[(24100, 24020, "old.pdf")]
        )
        result = upload(connection)
        self.assertEqual(result["document_rows_replaced"], 1)
        self.assertEqual(connection.writes("INSERT"), [])
        config = connection.writes("CONFIG")
        self.assertEqual(config[0]["new_max_docid"], 24053)

    def test_a_counter_behind_the_table_fails_on_the_unique_key_with_a_clear_message(self):
        collision = oracledb.IntegrityError(
            types.SimpleNamespace(code=1, message="ORA-00001: unique constraint violated")
        )
        connection = ScriptedConnection(counter=24048, insert_error=collision)
        with self.assertRaisesRegex(
            production_app.DocumentValidationError,
            r"DOCID 24049 \(MAXDOCID \+ 1\) already exists.*MAXDOCID is behind the table"
            r".*Nothing was inserted",
        ):
            upload(connection)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(connection.writes("CONFIG"), [])

    def test_other_integrity_errors_are_not_mistaken_for_a_docid_collision(self):
        other = oracledb.IntegrityError(
            types.SimpleNamespace(code=2291, message="ORA-02291: parent key not found")
        )
        connection = ScriptedConnection(counter=24050, insert_error=other)
        with self.assertRaises(oracledb.IntegrityError):
            upload(connection)


if __name__ == "__main__":
    unittest.main()
