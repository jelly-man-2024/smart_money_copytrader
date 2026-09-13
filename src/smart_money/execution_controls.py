"""Fail-closed process controls for offline tests and explicitly approved live runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .models import address
from .registry import CHAIN_ID


OFFLINE_TEST_MODE = "offline_test"
MAINNET_LIVE_MODE = "mainnet_live"
MAINNET_ACKNOWLEDGEMENT = "I_ACCEPT_MAINNET_FINANCIAL_RISK"


def _stop_controls() -> None:
    stop_file = Path(os.environ.get(
        "SMART_MONEY_EMERGENCY_STOP_FILE", "var/EXECUTION_STOP"))
    if stop_file.exists():
        raise PermissionError("execution emergency stop file is active")
    if os.environ.get("SMART_MONEY_EMERGENCY_STOP", "1") != "0":
        raise PermissionError("execution emergency stop is active")


def require_offline_signing_enabled() -> None:
    """Require three independent, explicit process controls for offline signing."""
    _stop_controls()
    if os.environ.get("SMART_MONEY_EXECUTION_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("execution mode is not offline_test")
    if os.environ.get("SMART_MONEY_SIGNING_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("signing mode is not offline_test")


def _risk_acceptance(follower_wallet: str | None, relationship_id: str | None,
                     config_snapshot_hash: str | None) -> dict:
    if not follower_wallet or not relationship_id or not config_snapshot_hash:
        raise PermissionError("mainnet risk identity is incomplete")
    follower = address(follower_wallet)
    if (not isinstance(relationship_id, str) or not relationship_id
            or len(relationship_id) > 255):
        raise PermissionError("mainnet relationship identity is invalid")
    if (not isinstance(config_snapshot_hash, str)
            or len(config_snapshot_hash) != 64):
        raise PermissionError("mainnet config snapshot is invalid")
    try:
        bytes.fromhex(config_snapshot_hash)
    except ValueError:
        raise PermissionError("mainnet config snapshot is invalid") from None
    raw_path = os.environ.get("SMART_MONEY_MAINNET_RISK_ACK_FILE")
    if not raw_path:
        raise PermissionError("mainnet risk acceptance file is not configured")
    path = Path(raw_path)
    try:
        metadata = path.stat()
    except OSError:
        raise PermissionError("mainnet risk acceptance file is unavailable") from None
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise PermissionError("mainnet risk acceptance file permissions are unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PermissionError("mainnet risk acceptance file is invalid") from None
    required = {
        "version", "chain_id", "follower_wallet", "relationship_id",
        "config_snapshot_hash", "acknowledgement",
    }
    if not isinstance(document, dict) or not required <= set(document):
        raise PermissionError("mainnet risk acceptance fields are incomplete")
    try:
        matches = (
            document["version"] == 1
            and int(document["chain_id"]) == CHAIN_ID
            and address(document["follower_wallet"]) == follower
            and str(document["relationship_id"]) == relationship_id
            and document["config_snapshot_hash"] == config_snapshot_hash
            and document["acknowledgement"] == MAINNET_ACKNOWLEDGEMENT
        )
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise PermissionError("mainnet risk acceptance does not match execution")
    return {
        "version": 1, "chain_id": CHAIN_ID, "follower_wallet": follower,
        "relationship_id": relationship_id,
        "config_snapshot_hash": config_snapshot_hash,
        "acceptance_file": str(path),
    }


def require_mainnet_signing_enabled(
        follower_wallet: str | None = None, relationship_id: str | None = None,
        config_snapshot_hash: str | None = None) -> dict:
    """Authorize one exact live relationship before any private-key SELECT."""
    _stop_controls()
    if os.environ.get("SMART_MONEY_EXECUTION_MODE") != MAINNET_LIVE_MODE:
        raise PermissionError("execution mode is not mainnet_live")
    if os.environ.get("SMART_MONEY_SIGNING_MODE") != MAINNET_LIVE_MODE:
        raise PermissionError("signing mode is not mainnet_live")
    if os.environ.get("SMART_MONEY_MAINNET_CHAIN_ID") != str(CHAIN_ID):
        raise PermissionError("explicit mainnet chain id acknowledgement is missing")
    return _risk_acceptance(follower_wallet, relationship_id, config_snapshot_hash)


def require_mainnet_broadcast_enabled(
        follower_wallet: str | None = None, relationship_id: str | None = None,
        config_snapshot_hash: str | None = None) -> dict:
    """Authorize one exact transaction relationship immediately before broadcast."""
    result = require_mainnet_signing_enabled(
        follower_wallet, relationship_id, config_snapshot_hash)
    if os.environ.get("SMART_MONEY_BROADCAST_MODE") != MAINNET_LIVE_MODE:
        raise PermissionError("broadcast mode is not mainnet_live")
    return result
