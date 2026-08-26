# PO Validation V5

This patch corrects the validation failure observed for
LM111360_PO.pdf.

## Corrections

1. A decimal quantity such as 170.000 is no longer accepted as stamped
   estimation 170000 unless an LM/PO-like value exists on the same stamp
   row.
2. Printed reference labels such as OPTION-112786B are normalized to
   112786B before comparison.
3. A party name whose leading edge is hidden by a folded scan can use a
   controlled partial match only when every order-line field already matches
   Oracle.
4. Non-match statuses are shown explicitly in validation error messages.

## Safety

- Unrelated party names remain blocked.
- Any unresolved mismatch blocks the entire PO transaction.
- Parent and detail inserts still commit or roll back together.
- The patch does not change Estimation or Mixing insertion.

## Regression test

Run:

    PYTHONPATH=. server_venv/bin/python -m unittest -v \
        test_po_validation_v5.py

Expected result: Ran 7 tests followed by OK.
