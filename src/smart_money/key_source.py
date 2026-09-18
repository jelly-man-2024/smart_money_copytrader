"""Explicitly gated private-key database source for offline or approved live signing."""
from __future__ import annotations

import os

from eth_account import Account
import pymysql

from .models import address
from .registry import CHAIN_ID, CHAINS
from .execution_controls import (
    OFFLINE_TEST_MODE, require_mainnet_signing_enabled,
    require_offline_signing_enabled,
)


OFFLINE_SIGNING_MODE = OFFLINE_TEST_MODE


def _key_connection(live_identity: tuple[str, str, str] | None = None):
    if live_identity is None:
        require_offline_signing_enabled()
    else:
        require_mainnet_signing_enabled(*live_identity)
    host = os.environ.get("SMART_MONEY_KEY_MYSQL_HOST", "127.0.0.1")
    ssl_ca = os.environ.get("SMART_MONEY_KEY_MYSQL_SSL_CA")
    if host not in {"127.0.0.1", "localhost"} and not ssl_ca:
        raise ValueError("remote key MySQL requires SMART_MONEY_KEY_MYSQL_SSL_CA")
    if host not in {"127.0.0.1", "localhost"}:
        required = (
            "SMART_MONEY_KEY_MYSQL_PORT", "SMART_MONEY_KEY_MYSQL_USER",
            "SMART_MONEY_KEY_MYSQL_PASSWORD", "SMART_MONEY_KEY_MYSQL_DATABASE",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(
                "remote key MySQL requires explicit connection settings: "
                + ",".join(missing))
    ssl = ({"ca": ssl_ca, "check_hostname": True} if ssl_ca
           else {"check_hostname": False})
    try:
        return pymysql.connect(
            host=host,
            port=int(os.environ.get("SMART_MONEY_KEY_MYSQL_PORT", "3309")),
            user=os.environ.get("SMART_MONEY_KEY_MYSQL_USER", "smart_money_key_runtime"),
            password=os.environ.get(
                "SMART_MONEY_KEY_MYSQL_PASSWORD", "local-key-runtime-only"),
            database=os.environ.get("SMART_MONEY_KEY_MYSQL_DATABASE", "smart_money_keys"),
            charset="utf8mb4", autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5, read_timeout=10, write_timeout=10, ssl=ssl,
        )
    except (pymysql.MySQLError, RuntimeError, ValueError) as exc:
        raise ValueError(f"key database connection failed: {type(exc).__name__}") from None


def key_record_status(wallet_address: str) -> dict:
    """Return public key-record metadata without selecting the private-key column."""
    wallet = address(wallet_address)
    connection = _key_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wallet_address,enabled FROM wallet_keys WHERE wallet_address=%s",
                (wallet,),
            )
            rows = cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise ValueError(f"key metadata lookup failed: {type(exc).__name__}") from None
    finally:
        connection.close()
    if len(rows) > 1:
        raise ValueError("key metadata lookup was not unique")
    if not rows:
        return {
            "wallet_address": wallet, "found": False, "enabled": False,
            "private_key_read": False, "read_only": True,
        }
    returned = address(rows[0].get("wallet_address"))
    enabled = rows[0].get("enabled")
    if returned != wallet or enabled not in {0, 1, False, True}:
        raise ValueError("invalid key metadata record")
    return {
        "wallet_address": wallet, "found": True, "enabled": bool(enabled),
        "private_key_read": False, "read_only": True,
    }


def live_key_record_status(wallet_address: str, relationship_id: str,
                           config_snapshot_hash: str) -> dict:
    """Check live key metadata under the exact relationship without reading it."""
    wallet = address(wallet_address)
    connection = _key_connection((wallet, relationship_id, config_snapshot_hash))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wallet_address,enabled FROM wallet_keys WHERE wallet_address=%s",
                (wallet,),
            )
            rows = cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise ValueError(f"key metadata lookup failed: {type(exc).__name__}") from None
    finally:
        connection.close()
    if len(rows) != 1:
        return {
            "wallet_address": wallet, "found": False, "enabled": False,
            "private_key_read": False, "read_only": True,
        }
    returned = address(rows[0].get("wallet_address"))
    enabled = rows[0].get("enabled")
    if returned != wallet or enabled not in {0, 1, False, True}:
        raise ValueError("invalid key metadata record")
    return {
        "wallet_address": wallet, "found": True, "enabled": bool(enabled),
        "private_key_read": False, "read_only": True,
    }


class OfflineDatabaseSigner:
    """Loads exactly one enabled key, validates ownership, and only signs offline."""

    def __init__(self, wallet_address: str):
        self.wallet_address = address(wallet_address)

    def __repr__(self):
        return f"OfflineDatabaseSigner(wallet_address={self.wallet_address!r})"

    def _load_account(self):
        connection = _key_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT private_key_hex FROM wallet_keys "
                    "WHERE wallet_address=%s AND enabled=TRUE",
                    (self.wallet_address,),
                )
                rows = cursor.fetchall()
        except pymysql.MySQLError as exc:
            raise ValueError(f"key lookup failed: {type(exc).__name__}") from None
        finally:
            connection.close()
        if len(rows) != 1:
            raise ValueError("expected exactly one enabled signing key")
        raw = rows[0].get("private_key_hex")
        if (not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x")):
            raise ValueError("invalid signing key record")
        try:
            account = Account.from_key(raw)
        except Exception:
            raise ValueError("invalid signing key record") from None
        if account.address.lower() != self.wallet_address:
            raise ValueError("signing key does not match expected wallet")
        return account

    def sign_transaction(self, transaction: dict) -> bytes:
        require_offline_signing_enabled()
        _validate_transaction(transaction)
        return bytes(self._load_account().sign_transaction(transaction).raw_transaction)


def _validate_transaction(transaction: dict) -> None:
    """Validate the only transaction shape accepted by either signer."""
    allowed = {"chainId", "nonce", "to", "value", "data", "gas",
               "maxFeePerGas", "maxPriorityFeePerGas", "type"}
    if not isinstance(transaction, dict) or set(transaction) - allowed:
        raise ValueError("invalid signing transaction fields")
    # A signer must refuse a chain this build does not know, but pinning it to
    # one chain meant a correctly configured second chain could not be signed at
    # all. The process controls above, not this check, are what authorize signing.
    if transaction.get("chainId") not in CHAINS:
        raise ValueError("signing transaction chain mismatch")
    for name in ("nonce", "value", "gas", "maxFeePerGas", "maxPriorityFeePerGas"):
        if not isinstance(transaction.get(name), int) or transaction[name] < 0:
            raise ValueError(f"invalid signing transaction {name}")
    if transaction.get("type") != 2:
        raise ValueError("only type-2 transactions are supported")
    address(transaction.get("to"))
    data = transaction.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("invalid signing transaction data")
    try:
        bytes.fromhex(data[2:])
    except ValueError:
        raise ValueError("invalid signing transaction data") from None


class LiveDatabaseSigner(OfflineDatabaseSigner):
    """Loads one key only after controls match one relationship and snapshot."""

    def __init__(self, wallet_address: str, relationship_id: str,
                 config_snapshot_hash: str):
        super().__init__(wallet_address)
        self.relationship_id = relationship_id
        self.config_snapshot_hash = config_snapshot_hash

    def __repr__(self):
        return (f"LiveDatabaseSigner(wallet_address={self.wallet_address!r}, "
                f"relationship_id={self.relationship_id!r}, "
                f"config_snapshot_hash={self.config_snapshot_hash!r})")

    def _live_identity(self) -> tuple[str, str, str]:
        return self.wallet_address, self.relationship_id, self.config_snapshot_hash

    def _load_account(self):
        connection = _key_connection(self._live_identity())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT private_key_hex FROM wallet_keys "
                    "WHERE wallet_address=%s AND enabled=TRUE",
                    (self.wallet_address,),
                )
                rows = cursor.fetchall()
        except pymysql.MySQLError as exc:
            raise ValueError(f"key lookup failed: {type(exc).__name__}") from None
        finally:
            connection.close()
        if len(rows) != 1:
            raise ValueError("expected exactly one enabled signing key")
        raw = rows[0].get("private_key_hex")
        if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
            raise ValueError("invalid signing key record")
        try:
            account = Account.from_key(raw)
        except Exception:
            raise ValueError("invalid signing key record") from None
        if account.address.lower() != self.wallet_address:
            raise ValueError("signing key does not match expected wallet")
        return account

    def sign_transaction(self, transaction: dict) -> bytes:
        require_mainnet_signing_enabled(*self._live_identity())
        _validate_transaction(transaction)
        return bytes(self._load_account().sign_transaction(transaction).raw_transaction)
