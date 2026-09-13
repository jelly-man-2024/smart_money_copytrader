"""Fail-closed process controls for offline tests and explicitly enabled live runs."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from .models import address
from .registry import CHAIN_ID


OFFLINE_TEST_MODE = "offline_test"
MAINNET_LIVE_MODE = "mainnet_live"


def _stop_controls() -> None:
    stop_file = Path(os.environ.get(
        "SMART_MONEY_EMERGENCY_STOP_FILE", "var/EXECUTION_STOP"))
    if stop_file.exists():
        raise PermissionError("execution emergency stop file is active")


def require_offline_signing_enabled() -> None:
    """Require three independent, explicit process controls for offline signing."""
    _stop_controls()
    if os.environ.get("SMART_MONEY_EMERGENCY_STOP", "1") != "0":
        raise PermissionError("execution emergency stop is active")
    if os.environ.get("SMART_MONEY_EXECUTION_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("execution mode is not offline_test")
    if os.environ.get("SMART_MONEY_SIGNING_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("signing mode is not offline_test")


def _risk_acceptance(follower_wallet: str | None, relationship_id: str | None,
                     config_snapshot_hash: str | None) -> dict:
    """Treat one freshly loaded enabled live MySQL row as relationship consent."""
    if not follower_wallet or not relationship_id or not config_snapshot_hash:
        raise PermissionError("mainnet risk identity is incomplete")
    follower = address(follower_wallet)
    if (not isinstance(relationship_id, str) or not relationship_id.isdecimal()
            or int(relationship_id) <= 0):
        raise PermissionError("mainnet relationship identity is invalid")
    if (not isinstance(config_snapshot_hash, str)
            or len(config_snapshot_hash) != 64):
        raise PermissionError("mainnet config snapshot is invalid")
    try:
        bytes.fromhex(config_snapshot_hash)
    except ValueError:
        raise PermissionError("mainnet config snapshot is invalid") from None

    try:
        # Keep the offline import path independent from MySQL. Live signing and
        # broadcast call this function immediately before each sensitive step.
        from .mysql_config import load_enabled_mainnet_acceptance
        acceptance = load_enabled_mainnet_acceptance(relationship_id)
        policy = acceptance["policy"]
        accepted_at = acceptance["accepted_at"]
        updated_at = acceptance["updated_at"]
        matches = (
            policy.run_mode == MAINNET_LIVE_MODE
            and address(policy.follower_wallet) == follower
            and policy.relationship_id == relationship_id
            and policy.snapshot_hash == config_snapshot_hash
            and isinstance(accepted_at, datetime)
            and isinstance(updated_at, datetime)
            and accepted_at >= updated_at
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PermissionError(
            "enabled mainnet relationship acceptance is unavailable") from None
    if not matches:
        raise PermissionError(
            "enabled mainnet relationship is stale or does not match execution")
    return {
        "version": 1, "chain_id": CHAIN_ID, "follower_wallet": follower,
        "relationship_id": relationship_id,
        "config_snapshot_hash": config_snapshot_hash,
        "accepted_at": accepted_at.isoformat(timespec="microseconds"),
        "acceptance_source": "enabled_mainnet_mysql_relationship",
    }


def require_mainnet_signing_enabled(
        follower_wallet: str | None = None, relationship_id: str | None = None,
        config_snapshot_hash: str | None = None) -> dict:
    """Authorize one exact enabled MySQL relationship before any key SELECT."""
    _stop_controls()
    return _risk_acceptance(follower_wallet, relationship_id, config_snapshot_hash)


def require_mainnet_broadcast_enabled(
        follower_wallet: str | None = None, relationship_id: str | None = None,
        config_snapshot_hash: str | None = None) -> dict:
    """Authorize one exact transaction relationship immediately before broadcast."""
    return require_mainnet_signing_enabled(
        follower_wallet, relationship_id, config_snapshot_hash)
