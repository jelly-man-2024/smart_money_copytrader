"""MySQL runtime ledger backend compatible with the existing Store contract."""
from __future__ import annotations

from datetime import date, datetime
import re

import pymysql

from .mysql_config import mysql_connection
from .store import Store


def _value(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


class _Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @staticmethod
    def _row(row):
        return tuple(_value(value) for value in row) if row is not None else None

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class MySqlConnectionCompat:
    """Translate the bounded SQLite SQL used by Store to MySQL DB-API calls."""

    _upsert = re.compile(
        r"\s+ON\s+CONFLICT\s*\([^)]+\)\s+DO\s+UPDATE\s+SET\s+(.+)$",
        re.IGNORECASE | re.DOTALL,
    )

    # pymysql codes for a connection that is gone: 0 (socket already closed),
    # 2003 (cannot connect), 2006 (server has gone away), 2013 (lost connection).
    _RECOVERABLE_CODES = frozenset({0, 2003, 2006, 2013})

    def __init__(self, connection, connection_factory=None):
        self._connection = connection
        self._connection_factory = connection_factory
        self._in_transaction = False
        self.reconnections = 0

    @classmethod
    def _recoverable(cls, exc: BaseException) -> bool:
        if not isinstance(exc, (pymysql.err.InterfaceError, pymysql.err.OperationalError)):
            return False
        code = exc.args[0] if exc.args else 0
        return not isinstance(code, int) or code in cls._RECOVERABLE_CODES

    def _reconnect(self) -> None:
        """Replace a dead connection; only legal outside a ledger transaction."""
        try:
            self._connection.close()
        except Exception:
            pass
        if self._connection_factory is not None:
            self._connection = self._connection_factory(
                write=False, dict_rows=False, autocommit=True)
        else:
            self._connection.ping(reconnect=True)
        self._in_transaction = False
        self.reconnections += 1

    @classmethod
    def _sql(cls, sql: str, lock: bool = False) -> str:
        statement = sql.strip()
        statement = re.sub(
            r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT IGNORE INTO", statement,
            flags=re.IGNORECASE)
        match = cls._upsert.search(statement)
        if match:
            assignments = re.sub(
                r"excluded\.([a-zA-Z_][a-zA-Z0-9_]*)",
                r"VALUES(\1)", match.group(1), flags=re.IGNORECASE)
            statement = statement[:match.start()] + " ON DUPLICATE KEY UPDATE " + assignments
        statement = statement.replace(
            "json_extract(p.attribution_payload,'$.source_event_id')",
            "CONVERT(JSON_UNQUOTE(JSON_EXTRACT("
            "p.attribution_payload,'$.source_event_id')) USING ascii) COLLATE ascii_bin")
        statement = statement.replace("?", "%s")
        if lock and statement.lstrip().upper().startswith("SELECT"):
            statement += " FOR UPDATE"
        return statement

    def execute(self, sql: str, params=()):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            if self._in_transaction:
                raise RuntimeError("nested MySQL ledger transaction")
            try:
                self._connection.begin()
            except (pymysql.err.InterfaceError, pymysql.err.OperationalError) as exc:
                if not self._recoverable(exc):
                    raise
                self._reconnect()
                self._connection.begin()
            self._in_transaction = True
            return _Cursor(self._connection.cursor())
        statement = self._sql(sql, lock=self._in_transaction)
        try:
            cursor = self._connection.cursor()
            cursor.execute(statement, params)
        except (pymysql.err.InterfaceError, pymysql.err.OperationalError) as exc:
            if not self._recoverable(exc):
                raise
            if self._in_transaction:
                # The server already discarded this transaction with the socket;
                # the caller's rollback is a no-op and the operation fails closed.
                self._in_transaction = False
                raise
            self._reconnect()
            cursor = self._connection.cursor()
            cursor.execute(statement, params)
        return _Cursor(cursor)

    def commit(self):
        try:
            self._connection.commit()
        finally:
            self._in_transaction = False

    def rollback(self):
        try:
            self._connection.rollback()
        except (pymysql.err.InterfaceError, pymysql.err.OperationalError) as exc:
            if not self._recoverable(exc):
                raise
            # Nothing to roll back on a dead connection; the next statement reconnects.
        finally:
            self._in_transaction = False

    def close(self):
        self._connection.close()


class MySqlStore(Store):
    """Run the complete operational and attribution ledger in business MySQL."""

    def __init__(self, connection_factory=mysql_connection):
        connection = connection_factory(
            write=False, dict_rows=False, autocommit=True)
        self.connection = MySqlConnectionCompat(connection, connection_factory)
        self.integrity_error = pymysql.IntegrityError
