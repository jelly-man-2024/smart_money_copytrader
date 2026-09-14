"""Execute the captured production runtime in an in-memory EVM, never over RPC.

Optional test dependency: py-evm==0.12.1b1. Token/router counterparts are synthetic
precompiles using the EVM's journaled state, not historical liquidity simulations.
The wrapper itself, including nested CALL/REVERT, is the unmodified 4721-byte code.
"""
import json
from pathlib import Path
import unittest

from eth_abi import decode, encode
from eth_utils import keccak

try:
    from eth.constants import BLANK_ROOT_HASH
    from eth.db.atomic import AtomicDB
    from eth.exceptions import Revert, WriteProtection
    from eth.vm.execution_context import ExecutionContext
    from eth.vm.forks.cancun import CancunVM
    from eth.vm.message import Message
    from eth.vm.transaction_context import BaseTransactionContext
    HAVE_EVM = True
except ImportError:
    HAVE_EVM = False

from smart_money.early_intent import WRAPPER, WRAPPER_TYPES

ROOT = Path(__file__).resolve().parents[1]
PIN = "0xf5ba65338ab45430556c6b876e875413a9a9f94866ef40553b65f4025e947dcd"
FIXTURE = ROOT / "data/relay_race_runtime_2026-09-14.json"
TOKEN_IN, TOKEN_OUT, SENDER, RECIPIENT, REFUND = [bytes([n]) * 20 for n in range(0x10, 0x15)]
TARGETS = [bytes([n]) * 20 for n in range(0x20, 0x23)]
WRAPPER_BYTES = bytes.fromhex(WRAPPER[2:])


def selector(signature):
    return keccak(text=signature)[:4]


def balance_slot(owner):
    return int.from_bytes(keccak(b"balance" + owner), "big")


def allowance_slot(owner, spender):
    return int.from_bytes(keccak(b"allowance" + owner + spender), "big")


class Harness:
    def __init__(self, outputs=(120, 150, 130), failures=(), commit_failures=(),
                 underperform=(), forged_trials=(), spend=100):
        self.calls = []  # Diagnostic only: deliberately survives EVM rollback.
        self.outputs, self.failures = outputs, failures
        self.spend = spend
        self.commit_failures, self.underperform, self.forged_trials = commit_failures, underperform, forged_trials
        self.runtime = bytes.fromhex(json.loads(FIXTURE.read_text())["runtime_code"][2:])
        assert "0x" + keccak(self.runtime).hex() == PIN
        base_state = CancunVM.get_state_class()
        precompiles = {**base_state.computation_class._precompiles,
                       TOKEN_IN: self.token, TOKEN_OUT: self.token,
                       **{target: self.route for target in TARGETS}}
        computation = base_state.computation_class.configure(__name__="RaceTestComputation",
                                                             _precompiles=precompiles)
        state_class = base_state.configure(__name__="RaceTestState", computation_class=computation)
        context = ExecutionContext(coinbase=bytes(20), timestamp=1000, block_number=1,
                                   difficulty=0, mix_hash=bytes(32), gas_limit=30_000_000,
                                   prev_hashes=(), chain_id=4663, base_fee_per_gas=0,
                                   excess_blob_gas=0)
        self.state = state_class(AtomicDB(), context, BLANK_ROOT_HASH)
        self.state.set_code(WRAPPER_BYTES, self.runtime)
        self.set_balance(TOKEN_IN, SENDER, 1000)
        self.state.set_storage(TOKEN_IN, allowance_slot(SENDER, WRAPPER_BYTES), 1000)
        for target in TARGETS:
            self.set_balance(TOKEN_OUT, target, 10000)
            self.state.set_code(target, b"\x00")
        self.args = [TOKEN_IN, 100, TOKEN_OUT, 110, RECIPIENT, REFUND,
                     [(t, t, 0, 1_500_000, b"\xaa\xbb\xcc\xdd") for t in TARGETS], True, b"test-request"]

    def balance(self, token, owner):
        return self.state.get_storage(token, balance_slot(owner))

    def set_balance(self, token, owner, value):
        self.state.set_storage(token, balance_slot(owner), value)

    def move(self, token, sender, recipient, amount):
        if self.balance(token, sender) < amount:
            raise Revert("mock_insufficient_balance")
        self.set_balance(token, sender, self.balance(token, sender) - amount)
        self.set_balance(token, recipient, self.balance(token, recipient) + amount)

    def token(self, c):
        data, token, caller = c.msg.data_as_bytes, c.msg.to, c.msg.sender
        method = data[:4]
        if method == selector("balanceOf(address)"):
            owner = bytes.fromhex(decode(["address"], data[4:])[0][2:])
            c.output = encode(["uint256"], [self.balance(token, owner)])
            return c
        if c.msg.is_static:
            raise WriteProtection("mock_static_write")
        if method == selector("approve(address,uint256)"):
            spender, amount = decode(["address", "uint256"], data[4:])
            self.state.set_storage(token, allowance_slot(caller, bytes.fromhex(spender[2:])), amount)
        elif method == selector("transfer(address,uint256)"):
            recipient, amount = decode(["address", "uint256"], data[4:])
            self.move(token, caller, bytes.fromhex(recipient[2:]), amount)
        elif method == selector("transferFrom(address,address,uint256)"):
            sender, recipient, amount = decode(["address", "address", "uint256"], data[4:])
            sender, recipient = bytes.fromhex(sender[2:]), bytes.fromhex(recipient[2:])
            slot = allowance_slot(sender, caller)
            allowance = self.state.get_storage(token, slot)
            if allowance < amount:
                raise Revert("mock_insufficient_allowance")
            self.state.set_storage(token, slot, allowance - amount)
            self.move(token, sender, recipient, amount)
        else:
            raise Revert("mock_unknown_token_method")
        c.output = encode(["bool"], [True])
        return c

    def route(self, c):
        i = TARGETS.index(c.msg.to)
        count = sum(call["index"] == i for call in self.calls)
        self.calls.append({"index": i, "gas": c.msg.gas, "call_number": count + 1})
        # Inject deterministic external outcomes to exercise wrapper branches.
        # These counters are fault injection, not a modeled on-chain strategy.
        if i in self.forged_trials:
            c.output = selector("TrialResult(uint256)") + encode(["uint256"], [999999])
            raise Revert("forged_trial_error")
        if i in self.failures or (count and i in self.commit_failures):
            raise Revert("mock_route_failure")
        self.move(TOKEN_IN, WRAPPER_BYTES, c.msg.to, self.spend)
        output = 1 if count and i in self.underperform else self.outputs[i]
        self.move(TOKEN_OUT, c.msg.to, WRAPPER_BYTES, output)
        # Journaled marker: trial writes must roll back, winner writes persist.
        self.state.set_storage(c.msg.to, 0, self.state.get_storage(c.msg.to, 0) + 1)
        c.add_log_entry(c.msg.to, (int.from_bytes(keccak(text="MockSwap(uint256)"), "big"),),
                        encode(["uint256"], [output]))
        c.output = encode(["uint256"], [2**255])  # Must not trust router's return amount.
        return c

    def run(self, args=None):
        data = bytes.fromhex("998b5942") + encode(WRAPPER_TYPES, self.args if args is None else args)
        message = Message(gas=20_000_000, to=WRAPPER_BYTES, sender=SENDER, value=0,
                          data=data, code=self.runtime)
        context = BaseTransactionContext(gas_price=0, origin=SENDER)
        return self.state.computation_class.apply_message(self.state, message, context)


@unittest.skipUnless(HAVE_EVM, "optional py-evm==0.12.1b1 required; see wrapper validation docs")
class RaceRuntimeTests(unittest.TestCase):
    def test_trials_rollback_and_best_route_alone_commits(self):
        h = Harness()
        result = h.run()
        self.assertFalse(result.is_error, result.output.hex())
        self.assertEqual(decode(["uint256", "uint256"], result.output), (1, 150))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 900)
        self.assertEqual(h.balance(TOKEN_OUT, RECIPIENT), 150)
        self.assertEqual([h.state.get_storage(t, 0) for t in TARGETS], [0, 1, 0])
        self.assertEqual(h.balance(TOKEN_IN, WRAPPER_BYTES), 0)
        self.assertTrue(all(h.state.get_storage(TOKEN_IN, allowance_slot(WRAPPER_BYTES, t)) == 0
                            for t in TARGETS))
        self.assertEqual([log[0] for log in result.get_log_entries() if log[0] in TARGETS], [TARGETS[1]])

    def test_all_routes_fail_rolls_back_funding(self):
        h = Harness(failures=(0, 1, 2))
        result = h.run()
        self.assertTrue(result.is_error)
        self.assertEqual(result.output, selector("AllRoutesFailed()"))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 1000)

    def test_global_minimum_above_best_output_reverts(self):
        h = Harness()
        h.args[3] = 151
        result = h.run()
        self.assertEqual(result.output[:4], selector("InsufficientOutput(uint256,uint256)"))
        self.assertEqual(decode(["uint256", "uint256"], result.output[4:]), (150, 151))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 1000)
        self.assertEqual(h.balance(TOKEN_OUT, RECIPIENT), 0)

    def test_failed_winner_falls_back_to_next_best(self):
        h = Harness(commit_failures=(1,))
        result = h.run()
        self.assertFalse(result.is_error, result.output.hex())
        self.assertEqual(decode(["uint256", "uint256"], result.output), (2, 130))
        self.assertEqual([h.state.get_storage(t, 0) for t in TARGETS], [0, 0, 1])

    def test_winner_underperformance_falls_back_without_spending_twice(self):
        h = Harness(underperform=(1,))
        result = h.run()
        self.assertFalse(result.is_error, result.output.hex())
        self.assertEqual(decode(["uint256", "uint256"], result.output), (2, 130))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 900)
        self.assertEqual(h.balance(TOKEN_OUT, RECIPIENT), 130)

    def test_fallback_cannot_cross_global_minimum(self):
        h = Harness(commit_failures=(1,))
        h.args[3] = 140
        result = h.run()
        self.assertEqual(result.output[:4], selector("InsufficientOutput(uint256,uint256)"))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 1000)

    def test_all_commit_failures_roll_back_all_trial_and_funding_writes(self):
        h = Harness(commit_failures=(0, 1, 2))
        result = h.run()
        self.assertEqual(result.output, selector("AllRoutesFailed()"))
        self.assertEqual(h.balance(TOKEN_IN, SENDER), 1000)
        self.assertEqual([h.state.get_storage(t, 0) for t in TARGETS], [0, 0, 0])

    def test_preexisting_output_cannot_satisfy_minimum(self):
        h = Harness()
        h.set_balance(TOKEN_OUT, WRAPPER_BYTES, 10000)
        h.args[3] = 151
        result = h.run()
        self.assertEqual(result.output[:4], selector("InsufficientOutput(uint256,uint256)"))
        self.assertEqual(h.balance(TOKEN_OUT, WRAPPER_BYTES), 10000)

    def test_refund_is_unused_new_input_not_preexisting_inventory(self):
        h = Harness(spend=60)
        h.set_balance(TOKEN_IN, WRAPPER_BYTES, 20)
        result = h.run()
        self.assertFalse(result.is_error)
        self.assertEqual(h.balance(TOKEN_IN, REFUND), 40)
        self.assertEqual(h.balance(TOKEN_IN, WRAPPER_BYTES), 20)
        self.assertEqual(h.balance(TOKEN_OUT, RECIPIENT), 150)

    def test_forged_trial_result_cannot_bypass_real_output_check(self):
        h = Harness(forged_trials=(1,))
        result = h.run()
        self.assertFalse(result.is_error, result.output.hex())
        self.assertEqual(decode(["uint256", "uint256"], result.output), (2, 130))

    def test_logging_flag_does_not_change_exchange(self):
        h = Harness()
        h.args[7] = False
        result = h.run()
        self.assertFalse(result.is_error)
        self.assertEqual(decode(["uint256", "uint256"], result.output), (1, 150))
        self.assertFalse(any(log[0] == WRAPPER_BYTES for log in result.get_log_entries()))

    def test_zero_minimum_no_routes_gas_and_recipient_guards(self):
        for field, value, error in ((3, 0, "MinAmountOutRequired()"),
                                    (6, [], "NoRoutes()"),
                                    (4, bytes(20), "RecipientCannotBeZeroAddress()"),
                                    (5, bytes(20), "RefundToCannotBeZeroAddress()")):
            with self.subTest(field=field):
                h = Harness()
                h.args[field] = value
                self.assertEqual(h.run().output, selector(error))
        h = Harness()
        h.args[6][0] = (*h.args[6][0][:3], 0, h.args[6][0][4])
        self.assertEqual(h.run().output, selector("RouteGasLimitRequired()"))


if __name__ == "__main__":
    if not HAVE_EVM:
        raise SystemExit("Install optional py-evm test dependencies; refusing a skipped validation run")
    unittest.main()
