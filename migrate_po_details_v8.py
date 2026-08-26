from __future__ import annotations

import argparse
import os
from typing import Any

from dotenv import load_dotenv


TABLE_NAME = "REGULARORDER_PODOCUMENTDETAILS"
REMOVED_COLUMNS = ("FILENAME", "DOCUMENTTYPE")
REQUIRED_COLUMNS = (
    "ID",
    "PODOCUMENTDOCID",
    "ESTIMATIONNO",
    "PARTYNAME",
    "LMNO",
    "REFERENCENO",
    "REQUIREDQUANTITY",
    "COUNTNAME",
    "CERTIFICATION",
    "NETRATE",
    "ENTRYDATETIME",
)


def table_columns(cursor: Any) -> dict[str, dict[str, Any]]:
    cursor.execute(
        """
        SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, NULLABLE
        FROM USER_TAB_COLUMNS
        WHERE TABLE_NAME = :table_name
        ORDER BY COLUMN_ID
        """,
        table_name=TABLE_NAME,
    )
    return {
        str(row[1]): {
            "column_id": int(row[0]),
            "data_type": str(row[2]),
            "nullable": str(row[3]),
        }
        for row in cursor.fetchall()
    }


def verify_required_columns(columns: dict[str, dict[str, Any]]) -> None:
    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError(
            "Required PO-detail column(s) missing: " + ", ".join(missing)
        )


def print_status(columns: dict[str, dict[str, Any]]) -> None:
    for name in REMOVED_COLUMNS:
        print(f"{name}: {'PRESENT' if name in columns else 'REMOVED'}")
    print("Remaining columns:", ", ".join(columns))


def run_check(cursor: Any) -> None:
    columns = table_columns(cursor)
    verify_required_columns(columns)
    print("Migration mode: CHECK (read only)")
    print_status(columns)
    cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
    print("Existing detail rows:", int(cursor.fetchone()[0]))
    print("Database operation: SELECT only")


def run_apply(connection: Any, cursor: Any) -> None:
    if os.getenv("BATCH_DATABASE_INSERT_ENABLED", "false").lower() == "true":
        raise RuntimeError(
            "Set BATCH_DATABASE_INSERT_ENABLED=false and restart the service "
            "before applying this migration"
        )

    columns = table_columns(cursor)
    verify_required_columns(columns)
    print("Before migration:")
    print_status(columns)

    for name in REMOVED_COLUMNS:
        if name not in columns:
            print(f"Kept {name} removed")
            continue
        cursor.execute(
            f"ALTER TABLE {TABLE_NAME} DROP COLUMN {name}"
        )
        print(f"Dropped {name}")

    connection.commit()
    final_columns = table_columns(cursor)
    verify_required_columns(final_columns)
    remaining_removed = [
        name for name in REMOVED_COLUMNS if name in final_columns
    ]
    if remaining_removed:
        raise RuntimeError(
            "Final schema verification failed; column(s) still present: "
            + ", ".join(remaining_removed)
        )

    print("After migration:")
    print_status(final_columns)
    cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
    print("Preserved detail rows:", int(cursor.fetchone()[0]))
    print("PASS: V8 PO-detail schema migration completed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove obsolete filename and document-type columns from "
            "REGULARORDER_PODOCUMENTDETAILS"
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    from app import get_db_connection

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            if args.check:
                run_check(cursor)
            else:
                run_apply(connection, cursor)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
