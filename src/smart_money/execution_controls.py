"""Fail-closed process controls. Mainnet execution is intentionally unavailable."""
from __future__ import annotations

import os
from pathlib import Path


OFFLINE_TEST_MODE = "offline_test"


def require_offline_signing_enabled() -> None:
    """Require three independent, explicit process controls for offline signing."""
    stop_file = Path(os.environ.get(
        "SMART_MONEY_EMERGENCY_STOP_FILE", "var/EXECUTION_STOP"))
    if stop_file.exists():
        raise PermissionError("execution emergency stop file is active")
    if os.environ.get("SMART_MONEY_EMERGENCY_STOP", "1") != "0":
        raise PermissionError("execution emergency stop is active")
    if os.environ.get("SMART_MONEY_EXECUTION_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("execution mode is not offline_test")
    if os.environ.get("SMART_MONEY_SIGNING_MODE") != OFFLINE_TEST_MODE:
        raise PermissionError("signing mode is not offline_test")


def require_mainnet_broadcast_enabled() -> None:
    """No environment combination can enable mainnet broadcast in this milestone."""
    raise PermissionError("mainnet signing and broadcast are not implemented or authorized")
