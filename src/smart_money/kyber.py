"""Bounded KyberSwap aggregator client for the follower's own execution quotes.

The client never reuses smart-wallet calldata. It asks Kyber for a route and a
transaction for the follower's exact planned input, then verifies the response
against an allowlisted router, the requested asset pair, the follower as the
only recipient, and a decodable top-level swap description. The inner executor
payload stays opaque; execution safety therefore also relies on the pre-signing
``eth_call`` simulation performed by the execution pipeline.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
import time
import urllib.parse
from .http_pool import JsonConnectionPool

from eth_abi import decode
from eth_abi.exceptions import DecodingError

from .decode import KYBER_SWAP_EXECUTION
from .models import address
from .registry import KYBER_META_AGGREGATION_ROUTER_V2, NATIVE

KYBER_API_ORIGIN = "https://aggregator-api.kyberswap.com"
KYBER_API_HOST = "aggregator-api.kyberswap.com"
KYBER_CHAIN_SLUG = "robinhood"
KYBER_SWAP_SELECTOR = "0xe21fd0e9"
KYBER_CLIENT_ID = "smart-money-copytrader"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CALLDATA_HEX = 2 * 1024 * 1024 + 2


class KyberApiError(ValueError):
    """Kyber transport, contract or validation failure; treated as an unavailable quote."""


def _raw_uint(value, name: str, positive: bool = False) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if (not isinstance(text, str) or not text.isdecimal()
            or (positive and int(text) <= 0) or int(text) >= 2 ** 256):
        raise KyberApiError(f"invalid Kyber {name}")
    return text


@dataclass(frozen=True)
class KyberRoute:
    input_asset: str
    output_asset: str
    amount_in_raw: str
    amount_out_raw: str
    gas_estimate: int
    router: str
    route_summary: dict
    observed_at: float
    response_hash: str

    def sources(self) -> frozenset[str]:
        """Only trust complete, bounded hop metadata; unknown layout stays unknown."""
        routes = self.route_summary.get("route")
        if not isinstance(routes, list) or not 1 <= len(routes) <= 64:
            return frozenset()
        sources = set()
        for branch in routes:
            if not isinstance(branch, list) or not 1 <= len(branch) <= 64:
                return frozenset()
            for hop in branch:
                source = hop.get("exchange") if isinstance(hop, dict) else None
                if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", source):
                    return frozenset()
                sources.add(source)
        return frozenset(sources)


@dataclass(frozen=True)
class KyberSwapTransaction:
    input_asset: str
    output_asset: str
    amount_in_raw: str
    amount_out_raw: str
    minimum_amount_out_raw: str
    recipient: str
    to: str
    data: str
    value_raw: str
    gas_estimate: int
    deadline: int
    observed_at: float
    response_hash: str
    route_response_hash: str

    def public_evidence(self) -> dict:
        return {
            "provider": "kyber", "router": self.to,
            "input_asset": self.input_asset, "output_asset": self.output_asset,
            "amount_in_raw": self.amount_in_raw, "amount_out_raw": self.amount_out_raw,
            "minimum_amount_out_raw": self.minimum_amount_out_raw,
            "recipient": self.recipient, "gas_estimate": self.gas_estimate,
            "deadline": self.deadline, "observed_at": self.observed_at,
            "response_hash": self.response_hash,
            "route_response_hash": self.route_response_hash,
        }


def decode_kyber_swap(data: str) -> dict:
    """Decode the top-level Kyber ``swap`` description; the executor payload stays opaque."""
    if (not isinstance(data, str) or not data.startswith("0x")
            or len(data) < 10 or len(data) > MAX_CALLDATA_HEX
            or data[:10].lower() != KYBER_SWAP_SELECTOR):
        raise ValueError("calldata is not a Kyber router swap")
    try:
        args = bytes.fromhex(data[10:])
        execution = decode([KYBER_SWAP_EXECUTION], args)[0]
    except (ValueError, DecodingError, OverflowError):
        raise ValueError("Kyber swap calldata cannot be decoded") from None
    call_target, approve_target, target_data, desc, _client_data = execution
    (src_token, dst_token, src_receivers, src_amounts, fee_receivers, fee_amounts,
     dst_receiver, amount, minimum, flags, _permit) = desc
    if (len(src_receivers) != len(src_amounts) or len(fee_receivers) != len(fee_amounts)
            or max(len(src_receivers), len(fee_receivers)) > 16
            or amount <= 0 or minimum <= 0 or minimum >= 2 ** 256):
        raise ValueError("invalid Kyber swap description")
    if src_amounts and sum(int(item) for item in src_amounts) != int(amount):
        raise ValueError("Kyber source amounts do not sum to the swap amount")
    if any(int(item) != 0 for item in fee_amounts):
        raise ValueError("Kyber swap carries unexpected fees")
    return {
        "call_target": address(call_target), "approve_target": address(approve_target),
        "target_data_selector": "0x" + bytes(target_data[:4]).hex(),
        "src_token": address(src_token), "dst_token": address(dst_token),
        "dst_receiver": address(dst_receiver), "amount_raw": str(int(amount)),
        "minimum_amount_out_raw": str(int(minimum)), "flags": str(int(flags)),
    }


class KyberAggregatorClient:
    """Read-only HTTPS client pinned to Kyber's official aggregator origin."""

    name = "kyber"

    def __init__(self, endpoint: str = KYBER_API_ORIGIN, chain: str = KYBER_CHAIN_SLUG,
                 client_id: str = KYBER_CLIENT_ID, timeout: float = 5.0,
                 allowed_routers: frozenset[str] = frozenset(
                     {KYBER_META_AGGREGATION_ROUTER_V2})):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or parsed.hostname != KYBER_API_HOST
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Kyber endpoint must be the official HTTPS API origin")
        if not isinstance(chain, str) or not chain.isalnum():
            raise ValueError("invalid Kyber chain slug")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("invalid Kyber timeout")
        self.endpoint = endpoint.rstrip("/")
        self.chain = chain
        self.client_id = client_id
        self.timeout = float(timeout)
        self.allowed_routers = frozenset(address(item) for item in allowed_routers)
        self.transport = JsonConnectionPool(self.endpoint, timeout=self.timeout,
                                            max_bytes=MAX_RESPONSE_BYTES)

    def close(self):
        self.transport.close()

    @property
    def router(self) -> str:
        return KYBER_META_AGGREGATION_ROUTER_V2

    def _request(self, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = f"{self.endpoint}/{self.chain}/api/v1/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "x-client-id": self.client_id,
                   "User-Agent": "smart-money-copytrader/0.1"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        try:
            parsed = urllib.parse.urlsplit(url)
            document = self.transport.request("POST" if payload else "GET",
                path=parsed.path + ("?" + parsed.query if parsed.query else ""),
                body=payload, headers=headers)
        except KyberApiError:
            raise
        except Exception as exc:
            raise KyberApiError(f"Kyber request failed: {type(exc).__name__}") from None
        if not isinstance(document, dict) or str(document.get("code")) != "0":
            raise KyberApiError("Kyber request was not successful")
        data = document.get("data")
        if not isinstance(data, dict):
            raise KyberApiError("Kyber response is incomplete")
        return document

    @staticmethod
    def _hash(document: dict) -> str:
        return hashlib.sha256(json.dumps(
            document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _parse_route(self, document: dict, input_asset: str, output_asset: str,
                     amount_in_raw: str, observed_at: float) -> KyberRoute:
        data = document["data"]
        summary = data.get("routeSummary")
        if not isinstance(summary, dict):
            raise KyberApiError("Kyber route summary is missing")
        try:
            router = address(data.get("routerAddress"))
            token_in = address(summary.get("tokenIn"))
            token_out = address(summary.get("tokenOut"))
        except (TypeError, ValueError):
            raise KyberApiError("invalid Kyber route identity fields") from None
        if (router not in self.allowed_routers or token_in != input_asset
                or token_out != output_asset):
            raise KyberApiError("Kyber route identity is outside the allowlist")
        if _raw_uint(summary.get("amountIn"), "route input amount", True) != amount_in_raw:
            raise KyberApiError("Kyber route input does not match the request")
        amount_out = _raw_uint(summary.get("amountOut"), "route output amount", True)
        gas = int(_raw_uint(summary.get("gas"), "route gas estimate", True))
        return KyberRoute(input_asset, output_asset, amount_in_raw, amount_out, gas,
                          router, summary, observed_at, self._hash(document))

    async def route(self, input_asset: str, output_asset: str,
                    amount_in_raw: str, *, excluded_sources: tuple[str, ...] = ()) -> KyberRoute:
        input_asset, output_asset = address(input_asset), address(output_asset)
        if NATIVE in {input_asset, output_asset}:
            raise ValueError("Kyber execution currently supports ERC-20 pairs only")
        if input_asset == output_asset:
            raise ValueError("Kyber route assets must differ")
        _raw_uint(amount_in_raw, "requested amount", positive=True)
        if (not isinstance(excluded_sources, tuple) or len(excluded_sources) > 16
                or any(not isinstance(s, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", s)
                       for s in excluded_sources)):
            raise ValueError("invalid Kyber source exclusions")
        query = {
            "tokenIn": input_asset, "tokenOut": output_asset, "amountIn": amount_in_raw,
        }
        if excluded_sources:
            query["excludedSources"] = ",".join(excluded_sources)
        document = await asyncio.to_thread(self._request, "routes", query)
        route = self._parse_route(document, input_asset, output_asset, amount_in_raw, time.time())
        if excluded_sources and (not route.sources() or route.sources().intersection(excluded_sources)):
            raise KyberApiError("Kyber route did not satisfy source exclusions")
        return route

    def _parse_build(self, document: dict, route: KyberRoute, follower_wallet: str,
                     deadline: int, observed_at: float) -> KyberSwapTransaction:
        data = document["data"]
        try:
            router = address(data.get("routerAddress"))
        except (TypeError, ValueError):
            raise KyberApiError("invalid Kyber build router") from None
        if router not in self.allowed_routers or router != route.router:
            raise KyberApiError("Kyber build router is outside the allowlist")
        if _raw_uint(data.get("amountIn"), "build input amount", True) != route.amount_in_raw:
            raise KyberApiError("Kyber build input does not match the route")
        amount_out = _raw_uint(data.get("amountOut"), "build output amount", True)
        gas = int(_raw_uint(data.get("gas"), "build gas estimate", True))
        value = _raw_uint(data.get("transactionValue", "0"), "transaction value")
        if value != "0":
            raise KyberApiError("Kyber ERC-20 swap must not carry native value")
        calldata = data.get("data")
        try:
            decoded = decode_kyber_swap(calldata)
        except ValueError as exc:
            raise KyberApiError(str(exc)) from None
        if (decoded["src_token"] != route.input_asset
                or decoded["dst_token"] != route.output_asset
                or decoded["dst_receiver"] != follower_wallet
                or decoded["amount_raw"] != route.amount_in_raw
                or int(decoded["minimum_amount_out_raw"]) > int(amount_out)):
            raise KyberApiError("Kyber swap calldata does not match the request")
        return KyberSwapTransaction(
            route.input_asset, route.output_asset, route.amount_in_raw, amount_out,
            decoded["minimum_amount_out_raw"], decoded["dst_receiver"], router,
            calldata.lower(), "0", gas, deadline, observed_at, self._hash(document),
            route.response_hash)

    async def build(self, route: KyberRoute, follower_wallet: str, slippage_bps: int,
                    deadline: int) -> KyberSwapTransaction:
        follower_wallet = address(follower_wallet)
        if follower_wallet == NATIVE:
            raise ValueError("zero follower wallet is forbidden")
        if not isinstance(slippage_bps, int) or not 1 <= slippage_bps <= 2000:
            raise ValueError("invalid Kyber slippage tolerance")
        if not isinstance(deadline, int) or deadline <= int(time.time()):
            raise ValueError("invalid Kyber deadline")
        document = await asyncio.to_thread(self._request, "route/build", None, {
            "routeSummary": route.route_summary, "sender": follower_wallet,
            "recipient": follower_wallet, "slippageTolerance": slippage_bps,
            "deadline": deadline, "source": self.client_id, "enableGasEstimation": False,
        })
        return self._parse_build(document, route, follower_wallet, deadline, time.time())
