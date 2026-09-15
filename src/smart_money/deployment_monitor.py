"""Process-local pinned race-code verification, independent of trade latency.

Only the background task performs RPC. Errors retain the last verified result;
a confirmed mismatch latches the path off until operator review/restart. This
does not certify arbitrary proxies, inner routers or historical execution state.
"""
import asyncio
from copy import deepcopy
import time

from eth_utils import keccak

from .early_intent import hash32
from .registry import CHAIN_ID
from .relay_race import RACE_ADDRESS, RACE_RULE, verify_deployment

DEPLOYMENT_CHECK_INTERVAL_SECONDS = 30.0


class DeploymentMonitor:
    def __init__(self, rpc, report, *, clock=time.time):
        self.rpc, self.report, self.clock = rpc, report, clock
        self._snapshot = None
        self._changed = False
        self._closed = False
        self._task = None
        self._lock = asyncio.Lock()
        self.last_checked_at = None
        self.last_success_at = None
        self.consecutive_errors = 0

    def require_ready(self):
        if self._closed:
            raise ValueError("deployment_monitor_closed")
        if self._changed:
            raise ValueError("deployment_code_changed")
        if self._snapshot is None:
            raise ValueError("deployment_not_yet_verified")

    def snapshot(self):
        self.require_ready()
        return deepcopy(self._snapshot)

    def status(self):
        return dict(contract=RACE_ADDRESS, rule=RACE_RULE,
                    ready=not self._closed and not self._changed and self._snapshot is not None,
                    changed=self._changed, last_checked_at=self.last_checked_at,
                    last_success_at=self.last_success_at,
                    seconds_since_success=(None if self.last_success_at is None else
                                           max(0, self.clock() - self.last_success_at)),
                    consecutive_errors=self.consecutive_errors,
                    check_interval_seconds=DEPLOYMENT_CHECK_INTERVAL_SECONDS)

    async def check_once(self):
        async with self._lock:
            started = self.clock()
            try:
                chain = await self.rpc.call("eth_chainId")
                if int(chain, 16) != CHAIN_ID:
                    raise ValueError("wrong_chain")
                block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
                hash32(block["hash"])
                code = await self.rpc.call("eth_getCode", [RACE_ADDRESS, {
                    "blockHash": block["hash"], "requireCanonical": True}])
                # Malformed responses are check failures, not evidence of change.
                if not isinstance(code, str) or not code.startswith("0x"):
                    raise ValueError("invalid_code_response")
                raw = bytes.fromhex(code[2:])
                if len(code) != 2 + 2 * len(raw):
                    raise ValueError("invalid_code_response")
                payload = dict(chain_id=CHAIN_ID, contract=RACE_ADDRESS, rule=RACE_RULE,
                               code=code, block_hash=block["hash"], block_number=block["number"])
                observed = self.clock()
                if started > observed:
                    raise ValueError("clock_moved_backwards")
            except Exception as exc:
                self.last_checked_at = self.clock()
                self.consecutive_errors += 1
                self.report("deployment_check_failed", **self.status(),
                            error_type=type(exc).__name__, live_trading=False)
                return
            self.last_checked_at = observed
            self.consecutive_errors = 0
            digest = "0x" + keccak(raw).hex()
            try:
                verify_deployment(payload)
            except ValueError:
                self._changed = True
                self.report("deployment_code_changed", **self.status(),
                            observed_code_hash=digest, block_hash=block["hash"], live_trading=False)
                return
            # A later matching observation does not erase a detected change.
            if self._changed:
                self.report("deployment_check_still_disabled", **self.status(), live_trading=False)
                return
            self.last_success_at = observed
            self._snapshot = dict(payload=payload, observed_at=observed,
                                  capture_started_at=started,
                                  provenance="monitor_periodic_pinned_deployment")
            self.report("deployment_check_passed", **self.status(),
                        runtime_code_hash=digest, block_hash=block["hash"], live_trading=False)

    async def start(self):
        if self._closed or self._task is not None:
            raise ValueError("deployment_monitor_already_started_or_closed")
        await self.check_once()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(DEPLOYMENT_CHECK_INTERVAL_SECONDS)
            await self.check_once()

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
