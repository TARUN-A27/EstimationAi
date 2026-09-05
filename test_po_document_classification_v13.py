from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
APP_PATH = PROJECT_DIR / "app.py"


def load_classification_functions() -> dict:
    required_names = {
        "unique",
        "filename_document_type",
        "classify_document",
    }
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in required_names
    ]
    namespace = {"Path": Path, "re": re}
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]),
            str(APP_PATH),
            "exec",
        ),
        namespace,
    )
    return namespace


class PoDocumentClassificationV13Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        functions = load_classification_functions()
        cls.classify = staticmethod(functions["classify_document"])
        cls.classify_filename = staticmethod(
            functions["filename_document_type"]
        )

    def test_po_no_content_is_po(self) -> None:
        self.assertEqual(self.classify("P.O. No: PO12345"), "po")

    def test_po_number_content_is_po(self) -> None:
        self.assertEqual(self.classify("PO NUMBER 123456"), "po")

    def test_lm_no_content_is_po(self) -> None:
        self.assertEqual(self.classify("LM NO: LM111473"), "po")

    def test_punctuated_lm_no_content_is_po(self) -> None:
        self.assertEqual(self.classify("L.M. No. LM111473"), "po")

    def test_lmo_number_content_is_po(self) -> None:
        self.assertEqual(self.classify("LMO NUMBER LMO09162"), "po")

    def test_bare_lm_identifier_content_is_po(self) -> None:
        self.assertEqual(self.classify("Order reference LM111462"), "po")

    def test_mixing_content_keeps_precedence_over_lm(self) -> None:
        self.assertEqual(
            self.classify("MIXING DETAILS FOR EST NO 175000 LM111462"),
            "mix",
        )

    def test_estimation_content_keeps_precedence_over_lm(self) -> None:
        self.assertEqual(
            self.classify("CUSTOMER NAME ABC TEXTILES LM111462"),
            "estimation",
        )

    def test_lm_filename_is_po_fallback(self) -> None:
        self.assertEqual(
            self.classify_filename("28.08.26_LM111473_0001.pdf"),
            "po",
        )

    def test_lmo_filename_is_po_fallback(self) -> None:
        self.assertEqual(
            self.classify_filename("28.08.26_LMO09162_0001.pdf"),
            "po",
        )

    def test_multi_lm_filename_is_po_fallback(self) -> None:
        self.assertEqual(
            self.classify_filename(
                "28.08.26_LM111462LM111463464465466_0001.pdf"
            ),
            "po",
        )

    def test_generic_unknown_document_remains_unknown(self) -> None:
        self.assertEqual(
            self.classify("GENERIC DOCUMENT WITHOUT ORDER LABEL"),
            "unknown",
        )

    def test_upload_route_uses_filename_only_after_unknown_ai_result(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'document_type = analysis_result["document_type"]',
            source,
        )
        self.assertIn(
            'if document_type == "unknown":\n'
            '                    document_type = '
            'filename_document_type(filename)',
            source,
        )


if __name__ == "__main__":
    unittest.main()
