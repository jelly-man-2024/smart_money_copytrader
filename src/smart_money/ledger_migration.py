"""One-way, fail-closed migration from a stopped SQLite ledger to business MySQL."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .mysql_config import mysql_connection


LEDGER_TABLES = (
    "signals", "candidates", "chain_cursors", "canonical_blocks",
    "candidate_inclusions", "solver_order_evidence", "paper_budget_cycles",
    "paper_budgets", "paper_proposals", "paper_reservations", "paper_orders",
    "paper_fills", "paper_positions", "paper_position_reservations",
    "paper_realized_pnl", "paper_position_marks", "paper_decisions",
    "execution_nonce_reservations", "execution_plans", "execution_attempts",
    "copy_operation_claims",
    "early_trials", "early_trial_operations",
    "early_feed_jobs",
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def sqlite_sha256(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid ledger identifier")
    return f"`{value}`"


def _normalized(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if value is None:
        return None
    return str(value)


def _assert_same_row(table: str, primary_key: tuple[str, ...], source: dict,
                     target: dict) -> None:
    different = [column for column, value in source.items()
                 if _normalized(value) != _normalized(target.get(column))]
    if different:
        identity = ",".join(f"{key}={source[key]}" for key in primary_key)
        raise ValueError(
            f"target row conflicts with SQLite source: {table} {identity} "
            f"columns={','.join(different)}")


def migrate_sqlite_ledger(path: str | Path, expected_sha256: str,
                          connection_factory=mysql_connection) -> dict:
    """Copy all runtime tables atomically, refusing active WAL or conflicting rows."""
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise ValueError("SQLite ledger does not exist")
    if (not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError("expected SQLite sha256 must be 64 lowercase hex characters")
    wal = Path(str(source_path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("SQLite WAL is non-empty; stop the writer and checkpoint first")
    actual_sha256 = sqlite_sha256(source_path)
    if actual_sha256 != expected_sha256:
        raise ValueError("SQLite ledger sha256 mismatch")

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = None
    report = {}
    try:
        source.execute("BEGIN")
        target = connection_factory(write=False)
        target.begin()
        with target.cursor() as cursor:
            for table in LEDGER_TABLES:
                source_info = source.execute(
                    f"PRAGMA table_info({_identifier(table)})").fetchall()
                if not source_info:
                    if table == "early_feed_jobs":
                        continue  # Optional evidence-only table in legacy ledgers.
                    if table in {"copy_operation_claims", "early_trials", "early_trial_operations"}:
                        # Only pre-migration, unenrolled ledgers may omit it.
                        field = ("copy_operation_order_id" if table == "copy_operation_claims"
                                 else "early_trial_id")
                        enrolled = any(field in json.loads(row[0])
                                       for row in source.execute(
                                           "SELECT attribution_payload FROM paper_proposals"))
                        if table in {"early_trials", "early_trial_operations"}:
                            other = ("early_trial_operations" if table == "early_trials"
                                     else "early_trials")
                            if source.execute(f"PRAGMA table_info({_identifier(other)})").fetchall():
                                enrolled = enrolled or bool(source.execute(
                                    f"SELECT 1 FROM {_identifier(other)} LIMIT 1").fetchone())
                        if not enrolled:
                            continue
                    raise ValueError(f"SQLite ledger table missing: {table}")
                columns = tuple(row[1] for row in source_info)
                primary_key = tuple(row[1] for row in sorted(
                    (row for row in source_info if row[5]), key=lambda row: row[5]))
                if not primary_key:
                    raise ValueError(f"SQLite ledger primary key missing: {table}")
                cursor.execute(f"SHOW COLUMNS FROM {_identifier(table)}")
                target_columns = {row["Field"] for row in cursor.fetchall()}
                missing = set(columns) - target_columns
                if missing:
                    raise ValueError(
                        f"business MySQL schema missing columns for {table}: "
                        + ",".join(sorted(missing)))

                rows = [dict(row) for row in source.execute(
                    f"SELECT * FROM {_identifier(table)}")]
                inserted = existing = 0
                column_sql = ",".join(_identifier(column) for column in columns)
                placeholders = ",".join(["%s"] * len(columns))
                where = " AND ".join(
                    f"{_identifier(column)}=%s" for column in primary_key)
                for row in rows:
                    cursor.execute(
                        f"SELECT {column_sql} FROM {_identifier(table)} "
                        f"WHERE {where}", tuple(row[column] for column in primary_key))
                    found = cursor.fetchone()
                    if found is not None:
                        _assert_same_row(table, primary_key, row, found)
                        existing += 1
                        continue
                    cursor.execute(
                        f"INSERT INTO {_identifier(table)} ({column_sql}) "
                        f"VALUES ({placeholders})", tuple(row[column] for column in columns))
                    if cursor.rowcount != 1:
                        raise ValueError(f"business MySQL insert failed: {table}")
                    inserted += 1
                report[table] = {
                    "source_rows": len(rows), "inserted_rows": inserted,
                    "existing_identical_rows": existing,
                }
        target.commit()
        source.commit()
    except ValueError:
        if target is not None:
            target.rollback()
        source.rollback()
        raise
    except Exception as exc:
        if target is not None:
            target.rollback()
        source.rollback()
        raise ValueError(
            f"business MySQL ledger migration failed: {type(exc).__name__}") from None
    finally:
        source.close()
        if target is not None:
            target.close()
    return {
        "source_sha256": actual_sha256,
        "tables": report,
        "source_rows": sum(item["source_rows"] for item in report.values()),
        "inserted_rows": sum(item["inserted_rows"] for item in report.values()),
        "existing_identical_rows": sum(
            item["existing_identical_rows"] for item in report.values()),
        "migration_direction": "sqlite_to_business_mysql",
        "private_key_data_migrated": False,
        "copy_eligible": False,
    }
