"""0x v2 AllowanceHolder: exact-input ERC-20 only, no Permit2 or approval calls.

ABI provenance: 0xProject/0x-settler src/allowanceholder/IAllowanceHolder.sol,
src/interfaces/ISettlerTakerSubmitted.sol and AllowanceHolderBase.sol (2026-09-16).
Unknown entrypoints fail closed. Inner swap actions are executed by a registry-
verified Settler; top-level slippage protection must name our actual recipient.
"""
from dataclasses import dataclass
import asyncio
import hashlib
import json
import time
from urllib.parse import urlencode

from eth_abi import decode, encode
from eth_utils import keccak

from .http_pool import JsonConnectionPool
from .kyber import KyberRoute, KyberSwapTransaction, _raw_uint
from .models import address
from .registry import NATIVE, ZERO_X_ALLOWANCE_HOLDER, chain_for

SETTLER_REGISTRY = "0x00000000000004533fe15556b1e086bb1a72ceae"
EXEC_TYPES = ["address", "address", "uint256", "address", "bytes"]
SETTLER_TYPES = ["(address,address,uint256)", "bytes[]", "bytes32"]
SETTLER_SELECTOR = keccak(text="execute((address,address,uint256),bytes[],bytes32)")[:4]


class ZeroExApiError(ValueError):
    pass


def decode_zeroex_swap(data):
    try:
        if not isinstance(data, str) or not 10 <= len(data) <= 2*1024*1024+2:
            raise ValueError()
        raw = bytes.fromhex(data[2:])
        if not data.startswith("0x") or raw[:4] != bytes.fromhex("2213bc0b"):
            raise ValueError()
        outer = decode(EXEC_TYPES, raw[4:])
        operator, token, amount, target, payload = outer
        # Canonical offsets only. Optional outer affiliate suffix is bounded and
        # never forwarded (the bytes length explicitly bounds the nested call).
        canonical = encode(EXEC_TYPES, outer)
        if raw[4:4+len(canonical)] != canonical or len(raw)-4-len(canonical) > 64:
            raise ValueError()
        if operator != target or address(target) == NATIVE or amount <= 0 or payload[:4] != SETTLER_SELECTOR:
            raise ValueError()
        inner = decode(SETTLER_TYPES, payload[4:])
        if encode(SETTLER_TYPES, inner) != payload[4:]:
            raise ValueError()
        (recipient, output, minimum), actions, _ = inner
        if (address(recipient) == NATIVE or address(output) == NATIVE or minimum <= 0
                or not 1 <= len(actions) <= 128 or any(len(a) < 4 for a in actions)):
            raise ValueError()
        return dict(src_token=address(token), dst_token=address(output),
                    dst_receiver=address(recipient), amount_raw=str(amount),
                    minimum_amount_out_raw=str(minimum), settler=address(target))
    except Exception:
        raise ZeroExApiError("unsupported or invalid 0x AllowanceHolder calldata") from None


@dataclass(frozen=True)
class ZeroExTransaction(KyberSwapTransaction):
    def public_evidence(self):
        result = super().public_evidence()
        result.update(provider="zeroex", allowance_spender=ZERO_X_ALLOWANCE_HOLDER,
                      settler=decode_zeroex_swap(self.data)["settler"],
                      deadline_basis="local_send_deadline_not_onchain_expiry")
        return result


class ZeroExAggregatorClient:
    name = "zeroex"
    router = ZERO_X_ALLOWANCE_HOLDER

    @staticmethod
    def router_for(chain_id):
        """AllowanceHolder on one chain; 0x serves several, this one may not."""
        router = chain_for(chain_id).zero_x_allowance_holder
        if router is None:
            raise ZeroExApiError(f"0x has no router for chain {chain_id}")
        return router

    def __init__(self, api_key, timeout=5.0):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("0x API key is not configured")
        self._key = api_key
        self.transport = JsonConnectionPool("https://api.0x.org", timeout=timeout)

    def close(self):
        self.transport.close()

    def _request(self, endpoint, query):
        try:
            result = self.transport.request(path="/swap/allowance-holder/" + endpoint + "?" + urlencode(query),
                headers={"0x-api-key": self._key, "0x-version": "v2", "Accept": "application/json",
                         "User-Agent": "smart-money-copytrader/0.1"}, idempotent=True)
        except Exception as exc:
            raise ZeroExApiError("0x request failed: " + type(exc).__name__) from None
        if not isinstance(result, dict) or result.get("liquidityAvailable") is not True:
            raise ZeroExApiError("0x liquidity unavailable")
        return result

    @classmethod
    def _query(cls, token_in, token_out, amount, chain_id):
        token_in, token_out = address(token_in), address(token_out)
        if NATIVE in {token_in, token_out} or token_in == token_out:
            raise ZeroExApiError("0x requires distinct ERC-20 assets")
        _raw_uint(amount, "input", True)
        cls.router_for(chain_id)  # refuse before asking 0x about an unserved chain
        return dict(chainId=int(chain_id), sellToken=token_in, buyToken=token_out,
                    sellAmount=amount)

    @staticmethod
    def _parse_route(document, query, observed):
        if (address(document.get("sellToken")) != query["sellToken"]
                or address(document.get("buyToken")) != query["buyToken"]
                or _raw_uint(document.get("sellAmount"), "sell amount", True) != query["sellAmount"]):
            raise ZeroExApiError("0x quote identity mismatch")
        output = _raw_uint(document.get("buyAmount"), "buy amount", True)
        tx = document.get("transaction") or {}
        gas = int(_raw_uint(tx.get("gas", document.get("gas")), "gas", True))
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return KyberRoute(query["sellToken"], query["buyToken"], query["sellAmount"], output,
                          gas, ZeroExAggregatorClient.router_for(query["chainId"]), {},
                          observed, digest)

    @staticmethod
    def _route(document, query, observed):
        try:
            return ZeroExAggregatorClient._parse_route(document, query, observed)
        except ZeroExApiError:
            raise
        except (ValueError, TypeError, KeyError):
            raise ZeroExApiError("invalid 0x quote response fields") from None

    async def route(self, token_in, token_out, amount, *, chain_id):
        query = self._query(token_in, token_out, amount, chain_id)
        document = await asyncio.to_thread(self._request, "price", query)
        return self._route(document, query, time.time())

    async def quote(self, token_in, token_out, amount, follower, slippage, deadline,
                    *, chain_id):
        query = self._query(token_in, token_out, amount, chain_id)
        follower = address(follower)
        if follower == NATIVE or type(slippage) is not int or not 1 <= slippage <= 2000:
            raise ZeroExApiError("invalid 0x recipient or slippage")
        query.update(taker=follower, recipient=follower, slippageBps=slippage)
        document = await asyncio.to_thread(self._request, "quote", query)
        try:
            return self._parse_quote(document, query, follower, slippage, deadline)
        except ZeroExApiError:
            raise
        except (ValueError, TypeError, KeyError):
            raise ZeroExApiError("invalid 0x executable quote fields") from None

    def _parse_quote(self, document, query, follower, slippage, deadline):
        amount = query["sellAmount"]
        router = self.router_for(query["chainId"])
        route = self._route(document, query, time.time())
        tx, issues = document.get("transaction"), document.get("issues")
        if not isinstance(tx, dict) or not isinstance(issues, dict):
            raise ZeroExApiError("0x transaction or issues missing")
        if (issues.get("allowance") is not None or issues.get("balance") is not None
                or issues.get("simulationIncomplete") is not False
                or issues.get("invalidSourcesPassed", [])):
            raise ZeroExApiError("0x quote has allowance, balance or simulation issues")
        if address(tx.get("to")) != router or str(tx.get("value")) != "0":
            raise ZeroExApiError("0x transaction target or value mismatch")
        if (document.get("allowanceTarget") is not None
                and address(document["allowanceTarget"]) != router):
            raise ZeroExApiError("0x allowance target mismatch")
        decoded = decode_zeroex_swap(tx.get("data"))
        minimum = _raw_uint(document.get("minBuyAmount"), "minimum output", True)
        if (decoded["src_token"] != route.input_asset or decoded["dst_token"] != route.output_asset
                or decoded["dst_receiver"] != follower or decoded["amount_raw"] != amount
                or decoded["minimum_amount_out_raw"] != minimum
                or not int(route.amount_out_raw)*(10000-slippage)//10000 <= int(minimum) <= int(route.amount_out_raw)):
            raise ZeroExApiError("0x calldata does not protect requested swap")
        return ZeroExTransaction(route.input_asset, route.output_asset, amount, route.amount_out_raw,
            minimum, follower, router, tx["data"].lower(), "0", route.gas_estimate,
            deadline, route.observed_at, route.response_hash, route.response_hash)


async def verify_settler(rpc, data):
    """Current OR previous during API dwell; a paused/unreadable registry fails closed."""
    target = decode_zeroex_swap(data)["settler"]
    async def read(signature):
        raw = await rpc.call("eth_call", [{"to": SETTLER_REGISTRY, "data": "0x" + (
            keccak(text=signature)[:4] + encode(["uint256"], [2])).hex()}, "pending"])
        return address(decode(["address"], bytes.fromhex(raw[2:]))[0])
    results = await asyncio.gather(read("ownerOf(uint256)"), read("prev(uint128)"), return_exceptions=True)
    if isinstance(results[0], BaseException):
        raise results[0]
    if results[0] == NATIVE or (target != results[0] and target != results[1]):
        raise ZeroExApiError("0x Settler not registered or registry paused")
    return {"settler": target, "registry": SETTLER_REGISTRY}
