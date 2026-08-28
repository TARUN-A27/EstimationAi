from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
APP_PATH = PROJECT_DIR / "app.py"


def app_source() -> str:
    return APP_PATH.read_text(encoding="utf-8")


def function_source(function_name: str) -> str:
    source = app_source()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == function_name
    )
    return ast.get_source_segment(source, function) or ""


def load_parent_planner():
    source = app_source()
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "plan_existing_po_parent"
    ]
    namespace = {"DocumentValidationError": ValueError}
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]),
            str(APP_PATH),
            "exec",
        ),
        namespace,
    )
    return namespace["plan_existing_po_parent"]


class PoParentIndependentV10Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan_parent = staticmethod(load_parent_planner())
        cls.insert_source = function_source(
            "insert_regular_order_document"
        )

    def test_parent_planner_needs_only_parent_row(self) -> None:
        result = self.plan_parent(
            "LM111360",
            [(11, 23227, "old.pdf")],
        )
        self.assertEqual(
            result,
            {
                "id": 11,
                "docid": 23227,
                "filename": "old.pdf",
            },
        )

    def test_parent_fileformat_is_dot_pdf(self) -> None:
        self.assertIn("FILEFORMAT = '.pdf'", self.insert_source)
        self.assertIn("                        '.pdf',", self.insert_source)
        self.assertNotIn("FILEFORMAT = 'PDF'", self.insert_source)
        self.assertNotIn("                        'PDF',", self.insert_source)

    def test_parent_commits_before_detail_replacement(self) -> None:
        parent_commit = self.insert_source.index(
            "# Commit the established parent-table workflow"
        )
        detail_transaction = self.insert_source.index(
            "# Detail replacement is a separate atomic transaction"
        )
        detail_delete = self.insert_source.index(
            "DELETE FROM REGULARORDER_PODOCUMENTDETAILS"
        )
        self.assertLess(parent_commit, detail_transaction)
        self.assertLess(parent_commit, detail_delete)

    def test_detail_preparation_cannot_block_parent(self) -> None:
        self.assertIn(
            "build_extracted_po_document_detail_result",
            self.insert_source,
        )
        self.assertIn(
            "must not prevent parent",
            self.insert_source,
        )
        self.assertIn(
            'po_detail_error = str(exc)',
            self.insert_source,
        )

    def test_detail_insert_and_replacement_logic_remains_present(self) -> None:
        self.assertIn(
            "plan_existing_po_replacement",
            self.insert_source,
        )
        self.assertIn(
            "DELETE FROM REGULARORDER_PODOCUMENTDETAILS",
            self.insert_source,
        )
        self.assertIn(
            "INSERT INTO REGULARORDER_PODOCUMENTDETAILS",
            self.insert_source,
        )


if __name__ == "__main__":
    unittest.main()
