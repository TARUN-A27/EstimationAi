"""PO parent DOCIDs come from EnquiryDocument_DocId_Seq; MAXDOCID is not checked.

The configuration row is still locked (it serializes concurrent uploads) and is
moved forward to the highest DOCID issued so the ERP, which allocates
MAXDOCID + 1, never collides. A sequence that is behind the table fails the
INSERT on the unique DOCID key and is reported clearly.
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
            state["writes"].append((" ".join(text.split()[:3]), text, binds))
            self.rowcount = 1
        elif "ENQUIRYDOCUMENT_DOCID_SEQ.NEXTVAL" in text:
            value = state["docid_sequence"].pop(0)
            state["docid_draws"].append(value)
            self.rows = [(value,)]
        elif "REGULARORDER_PODOCUMENT_SEQ.NEXTVAL" in text:
            state["max_id"] += 1
            self.rows = [(state["max_id"],)]
        elif "FROM REGULARORDER_PODOCUMENT_CONFIG" in text:
            self.rows = [(state["counter"],)]
        elif "NVL(MAX(ID), 0)" in text:
            self.rows = [(state["max_id"],)]
        elif "FROM REGULARORDER_PODOCUMENT WHERE" in text:
            self.rows = state["existing_parents"].get(binds["order_no_key"], [])
        elif "FROM REGULARORDER WHERE ESTIMATIONNO" in text:
            self.rows = [(state["orders"][binds["estimation_no"]], "")]
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
            "docid_draws": [],
            "docid_sequence": [25001, 25002, 25003],
            "existing_parents": {},
            "orders": {180101: "LM900101", 180102: "LM900102"},
            "insert_error": None,
            "counter": 24056,
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
        return [binds for name, _text, binds in self.state["writes"] if kind in name]

    def write_sql(self, kind):
        return [text for name, text, _binds in self.state["writes"] if kind in name]


def upload(connection, estimations=(180101,)):
    analysis = {
        "document_estimation_numbers": list(estimations),
        "line_estimation_numbers": [],
        "estimation_numbers": list(estimations),
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


class PoDocidSequenceV35Tests(unittest.TestCase):
    def test_new_parent_takes_its_docid_from_the_enquiry_sequence(self):
        connection = ScriptedConnection()
        result = upload(connection)
        self.assertEqual(result["docids"], [25001])
        self.assertEqual(connection.writes("INSERT")[0]["document_docid"], 25001)
        self.assertEqual(connection.state["docid_draws"], [25001])
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        statements = connection.state["statements"]
        self.assertTrue(
            any("ENQUIRYDOCUMENT_DOCID_SEQ.NEXTVAL FROM DUAL" in s for s in statements)
        )
        self.assertFalse(any("REGULARORDER_PODOCUMENT_DOCID" in s for s in statements))

    def test_max_docid_is_never_checked(self):
        # Ahead of, behind and absurdly far from the sequence: none of it matters.
        for counter in (24053, 24048, 99999999):
            with self.subTest(counter=counter):
                connection = ScriptedConnection(counter=counter)
                result = upload(connection)
                self.assertEqual(result["docids"], [25001])
                self.assertFalse(
                    any("MAX(DOCID)" in s for s in connection.state["statements"])
                )

    def test_parent_persistence_is_still_serialized_by_the_configuration_lock(self):
        connection = ScriptedConnection()
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

    def test_counter_follows_the_highest_issued_docid_and_only_moves_up(self):
        connection = ScriptedConnection()
        result = upload(connection, estimations=(180101, 180102))
        self.assertEqual(result["docids"], [25001, 25002])
        updates = connection.writes("CONFIG")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["highest_docid"], 25002)
        self.assertIn("GREATEST(NVL(MAXDOCID, 0)", connection.write_sql("CONFIG")[0])

    def test_replacing_a_parent_draws_no_docid_and_leaves_the_counter_alone(self):
        connection = ScriptedConnection(
            existing_parents={"LM900101": [(24100, 24020, "old.pdf")]}
        )
        result = upload(connection)
        self.assertEqual(result["document_rows_replaced"], 1)
        self.assertEqual(result["docids"], [24020])
        self.assertEqual(connection.state["docid_draws"], [])
        self.assertEqual(connection.writes("INSERT"), [])
        self.assertEqual(connection.writes("CONFIG"), [])

    def test_sequence_behind_the_table_is_reported_clearly_and_nothing_is_committed(self):
        collision = oracledb.IntegrityError(
            types.SimpleNamespace(code=1, message="ORA-00001: unique constraint violated")
        )
        connection = ScriptedConnection(docid_sequence=[20404], insert_error=collision)
        with self.assertRaisesRegex(
            production_app.DocumentValidationError,
            r"DOCID 20404 from EnquiryDocument_DocId_Seq .*behind the table.*Nothing was inserted",
        ):
            upload(connection)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(connection.writes("CONFIG"), [])

    def test_other_integrity_errors_are_not_mistaken_for_a_docid_collision(self):
        other = oracledb.IntegrityError(
            types.SimpleNamespace(code=2291, message="ORA-02291: parent key not found")
        )
        connection = ScriptedConnection(insert_error=other)
        with self.assertRaises(oracledb.IntegrityError):
            upload(connection)


if __name__ == "__main__":
    unittest.main()
