from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


def load_replacement_planner():
    source_path = PROJECT_DIR / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "plan_existing_po_replacement"
    ]
    namespace = {"DocumentValidationError": ValueError}
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    return namespace["plan_existing_po_replacement"]


class PoReplacementPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = staticmethod(load_replacement_planner())

    def test_new_order_has_no_replacement_plan(self) -> None:
        self.assertIsNone(self.plan("LM111360", [], []))

    def test_existing_order_reuses_parent_and_replaces_details(self) -> None:
        result = self.plan(
            "lm111360",
            [(11, 23227, "old.pdf")],
            [
                (1, 23227, "LM111360", 175164),
                (2, 23227, "lm111360", 175165),
            ],
        )
        self.assertEqual(result["id"], 11)
        self.assertEqual(result["docid"], 23227)
        self.assertEqual(result["filename"], "old.pdf")
        self.assertEqual(result["detail_ids"], [1, 2])

    def test_orphan_detail_blocks_replacement(self) -> None:
        with self.assertRaisesRegex(ValueError, "no matching parent"):
            self.plan(
                "LM111360",
                [],
                [(1, 23227, "LM111360", 175164)],
            )

    def test_mismatched_detail_link_blocks_replacement(self) -> None:
        with self.assertRaisesRegex(ValueError, "linkage is inconsistent"):
            self.plan(
                "LM111360",
                [(11, 23227, "old.pdf")],
                [(1, 99999, "LM111360", 175164)],
            )

    def test_multiple_parent_rows_block_replacement(self) -> None:
        with self.assertRaisesRegex(ValueError, "multiple parent rows"):
            self.plan(
                "LM111360",
                [
                    (11, 23227, "old.pdf"),
                    (12, 23228, "older.pdf"),
                ],
                [],
            )

    def test_backend_contains_atomic_replacement_statements(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn("UPDATE REGULARORDER_PODOCUMENT", source)
        self.assertIn(
            "DELETE FROM REGULARORDER_PODOCUMENTDETAILS",
            source,
        )
        self.assertIn('"document_rows_replaced"', source)
        self.assertIn('"replaced_order_numbers"', source)


if __name__ == "__main__":
    unittest.main()
