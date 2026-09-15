import hashlib
import json
import subprocess
import sys
from copy import deepcopy
import unittest
from unittest.mock import patch

from eth_abi import encode
from smart_money import registry as R
from smart_money.call_trace import DiagnosticTraceRpc, saved_call, summarize_trace
from smart_money.decode import KYBER_SWAP_EXECUTION
from smart_money.rpc import ALLOWED_METHODS

WALLET, TOKEN = "0x"+"11"*20, "0x"+"22"*20


def diagnostic():
    description = (R.USDG, TOKEN, [WALLET], [100000], [], [], WALLET, 100000, 10, 512, b"")
    raw = bytes.fromhex("e21fd0e9") + encode([KYBER_SWAP_EXECUTION],
        [(WALLET, R.NATIVE, b"", description, b"")])
    return {"schema_version": 1, "chain_id": 4663, "method": "eth_call", "provider": "kyber",
            "calldata_omitted_size_limit": False, "calldata_sha256": hashlib.sha256(raw).hexdigest(),
            "follower_wallet": WALLET, "input_asset": R.USDG, "amount_in_raw": "100000",
            "minimum_amount_out_raw": "10", "proposal_id": "synthetic",
            "call": {"from": WALLET, "to": R.KYBER_META_AGGREGATION_ROUTER_V2,
                     "data": "0x"+raw.hex(), "value": "0x0", "gas": "0x927c0"}}


def frame():
    call = diagnostic()["call"]
    return {"type": "CALL", "from": call["from"], "to": call["to"], "input": call["data"],
            "gas": "0x927c0", "gasUsed": "0x100", "output": "0x"}


class TraceTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_requires_opt_in_before_reading_log_or_network(self):
        result = subprocess.run([sys.executable, "-m", "smart_money.call_trace",
                                 "--log", "/nonexistent/diagnostic.jsonl",
                                 "--proposal-id", "synthetic", "--block", "10"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no network requests made", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    async def test_opt_in_chain_block_and_generic_allowlist(self):
        client = DiagnosticTraceRpc("https://rpc.invalid")
        self.assertNotIn("debug_traceCall", ALLOWED_METHODS)
        with patch.object(client, "_request") as request:
            with self.assertRaises(PermissionError):
                await client.trace_saved_call(diagnostic(), 10)
            for block in ("latest", "pending", {"blockHash": "x"}, 0, True):
                with self.assertRaises(ValueError):
                    await client.trace_saved_call(diagnostic(), block, allow_rpc=True)
            for method in ("debug_traceCall", "eth_sendRawTransaction", "debug_setHead"):
                with self.assertRaises(PermissionError): await client.call(method, [])
            request.assert_not_called()
        with patch.object(client, "_request", return_value="0x1") as request:
            with self.assertRaises(ValueError):
                await client.trace_saved_call(diagnostic(), 10, allow_rpc=True)
            self.assertEqual(request.call_count, 1)

    async def test_exact_fixed_tracer_no_overrides_and_root_binding(self):
        client = DiagnosticTraceRpc("https://rpc.invalid")
        header = {"number": "0xa", "timestamp": "0x64", "hash": "0x"+"ab"*32}
        with patch.object(client, "_request", side_effect=["0x1237", header, frame(), header]) as request:
            result = await client.trace_saved_call(diagnostic(), 10, allow_rpc=True)
            method, params, _ = request.call_args_list[2].args
            self.assertEqual(method, "debug_traceCall")
            self.assertEqual(params, [saved_call(diagnostic()), "0xa", {
                "tracer": "callTracer", "timeout": "5s", "reexec": 0,
                "tracerConfig": {"onlyTopCall": False, "withLog": False}}])
            self.assertFalse(result["broadcast_performed"])
            self.assertTrue(result["historical_trace_not_original_pending_state"])
        bad = {**frame(), "to": TOKEN}
        with patch.object(client, "_request", side_effect=["0x1237", header, bad]):
            with self.assertRaises(ValueError): await client.trace_saved_call(diagnostic(), 10, allow_rpc=True)
        with patch.object(client, "_request", side_effect=["0x1237", header, frame(), {**header, "hash": "0x"+"cd"*32}]):
            with self.assertRaises(ValueError): await client.trace_saved_call(diagnostic(), 10, allow_rpc=True)

    def test_saved_call_validation_and_gas_bound(self):
        original = diagnostic()
        for key, value in (("chain_id", 1), ("provider", "other"), ("method", "eth_sendRawTransaction"),
                           ("calldata_sha256", "wrong"), ("follower_wallet", TOKEN), ("amount_in_raw", "1")):
            with self.subTest(key=key), self.assertRaises(ValueError): saved_call({**original, key: value})
        for key, value in (("to", TOKEN), ("value", "0x1"), ("nonce", "0x1"), ("data", "0x")):
            bad = deepcopy(original);bad["call"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): saved_call(bad)
        for gas in (True, 0, 2000001, "2000000"):
            with self.assertRaises(ValueError): saved_call(original, gas)
        self.assertEqual(saved_call(original, 2000000)["gas"], hex(2000000))
        self.assertEqual(original, diagnostic())

    def test_failure_paths_sanitized_and_not_every_child_revert_is_root_cause(self):
        child = {**frame(), "error": "execution reverted", "output": "0x08c379a0"+encode(["string"],["Pool failure"]).hex()}
        root = {**frame(), "calls": [child, frame()]}
        rows = summarize_trace(root)
        self.assertEqual([r["path"] for r in rows], ["0", "0.0", "0.1"])
        self.assertFalse(rows[0]["failed"])
        self.assertEqual(rows[1]["failure"]["revert_reason"], "Pool failure")
        child["error"] = "https://rpc.invalid/secret"
        child["output"] = "0x08c379a0"+encode(["string"],["password=hunter2"]).hex()
        output = json.dumps(summarize_trace(root))
        self.assertNotIn("hunter2", output)
        self.assertNotIn("rpc.invalid", output)

    def test_tree_depth_width_and_hex_limits(self):
        for malformed in (None, {}, {**frame(), "type": None},
                          {**frame(), "calls": "bad"}, {**frame(), "input": "0xzz"},
                          {**frame(), "calls": [frame()]*512}):
            with self.assertRaises(ValueError): summarize_trace(malformed)
        root = frame();node = root
        for _ in range(65):
            node["calls"] = [frame()];node = node["calls"][0]
        with self.assertRaises(ValueError): summarize_trace(root)
