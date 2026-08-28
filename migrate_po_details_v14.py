from __future__ import annotations

import argparse
import os
from typing import Any

from dotenv import load_dotenv


TABLE_NAME = "REGULARORDER_PODOCUMENTDETAILS"
UNIQUE_CONSTRAINT = "UQ_RO_PO_DOCDET_EST"
EXPECTED_UNIQUE_COLUMNS = ("PODOCUMENTDOCID", "ESTIMATIONNO")
NULLABLE_BUSINESS_COLUMNS = (
    "PARTYNAME",
    "REFERENCENO",
    "REQUIREDQUANTITY",
    "COUNTNAME",
    "NETRATE",
)
MANDATORY_LINK_COLUMNS = (
    "ID",
    "PODOCUMENTDOCID",
    "ESTIMATIONNO",
    "LMNO",
    "ENTRYDATETIME",
)


def insertion_enabled() -> bool:
    return os.getenv(
        "BATCH_DATABASE_INSERT_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}


def column_nullability(cursor: Any) -> dict[str, str]:
    cursor.execute(
        """
        SELECT COLUMN_NAME, NULLABLE
        FROM USER_TAB_COLUMNS
        WHERE TABLE_NAME = :table_name
        """,
        table_name=TABLE_NAME,
    )
    return {str(name): str(nullable) for name, nullable in cursor.fetchall()}


def constraint_columns(cursor: Any) -> tuple[str, ...] | None:
    cursor.execute(
        """
        SELECT cc.COLUMN_NAME
        FROM USER_CONSTRAINTS c
        JOIN USER_CONS_COLUMNS cc
          ON cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
         AND cc.TABLE_NAME = c.TABLE_NAME
        WHERE c.TABLE_NAME = :table_name
          AND c.CONSTRAINT_NAME = :constraint_name
          AND c.CONSTRAINT_TYPE = 'U'
          AND c.STATUS = 'ENABLED'
        ORDER BY cc.POSITION
        """,
        table_name=TABLE_NAME,
        constraint_name=UNIQUE_CONSTRAINT,
    )
    rows = tuple(str(row[0]) for row in cursor.fetchall())
    return rows or None


def validate_schema(cursor: Any) -> tuple[dict[str, str], tuple[str, ...] | None]:
    columns = column_nullability(cursor)
    required = set(NULLABLE_BUSINESS_COLUMNS + MANDATORY_LINK_COLUMNS)
    missing = sorted(required.difference(columns))
    if missing:
        raise RuntimeError(
            "Required PO-detail column(s) missing: " + ", ".join(missing)
        )

    constraint = constraint_columns(cursor)
    if constraint is not None and constraint != EXPECTED_UNIQUE_COLUMNS:
        raise RuntimeError(
            f"{UNIQUE_CONSTRAINT} has unexpected columns: "
            + ", ".join(constraint)
        )
    return columns, constraint


def print_status(
    columns: dict[str, str],
    constraint: tuple[str, ...] | None,
) -> None:
    print(
        f"{UNIQUE_CONSTRAINT}: "
        + ("ENABLED (" + ", ".join(constraint) + ")" if constraint else "REMOVED")
    )
    for name in NULLABLE_BUSINESS_COLUMNS:
        print(f"{name} nullable: {'YES' if columns[name] == 'Y' else 'NO'}")
    for name in MANDATORY_LINK_COLUMNS:
        print(f"{name} mandatory: {'YES' if columns[name] == 'N' else 'NO'}")


def run_check(cursor: Any) -> None:
    columns, constraint = validate_schema(cursor)
    print("Migration mode: CHECK (SELECT only)")
    print_status(columns, constraint)
    ready = constraint is None and all(
        columns[name] == "Y" for name in NULLABLE_BUSINESS_COLUMNS
    )
    print("V14 schema ready:", "YES" if ready else "NO")


def run_apply(cursor: Any) -> None:
    if insertion_enabled():
        raise RuntimeError(
            "Set BATCH_DATABASE_INSERT_ENABLED=false and restart the "
            "service before applying the V14 migration"
        )

    columns, constraint = validate_schema(cursor)
    print("Before migration:")
    print_status(columns, constraint)

    if constraint is not None:
        cursor.execute(
            f"ALTER TABLE {TABLE_NAME} DROP CONSTRAINT {UNIQUE_CONSTRAINT}"
        )
        print(f"Dropped {UNIQUE_CONSTRAINT}")
    else:
        print(f"Kept {UNIQUE_CONSTRAINT} removed")

    for name in NULLABLE_BUSINESS_COLUMNS:
        if columns[name] == "Y":
            print(f"Kept {name} nullable")
            continue
        cursor.execute(
            f"ALTER TABLE {TABLE_NAME} MODIFY ({name} NULL)"
        )
        print(f"Made {name} nullable")

    final_columns, final_constraint = validate_schema(cursor)
    if final_constraint is not None:
        raise RuntimeError(f"{UNIQUE_CONSTRAINT} was not removed")
    not_nullable = [
        name
        for name in NULLABLE_BUSINESS_COLUMNS
        if final_columns[name] != "Y"
    ]
    if not_nullable:
        raise RuntimeError(
            "Business columns remain mandatory: " + ", ".join(not_nullable)
        )
    unexpectedly_nullable = [
        name
        for name in MANDATORY_LINK_COLUMNS
        if final_columns[name] != "N"
    ]
    if unexpectedly_nullable:
        raise RuntimeError(
            "Link columns unexpectedly nullable: "
            + ", ".join(unexpectedly_nullable)
        )

    print("After migration:")
    print_status(final_columns, final_constraint)
    print("PASS: V14 PO-detail schema migration completed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Allow multiple extracted PO lines per EST and preserve missing "
            "business fields as NULL"
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
                run_apply(cursor)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
