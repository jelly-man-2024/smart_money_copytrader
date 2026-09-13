"""Authenticated OKX swap-data client with strict EVM transaction validation."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

from .models import address
from .registry import CHAIN_ID, NATIVE, OKX_ROUTER

OKX_API_ORIGIN = "https://web3.okx.com"
OKX_SWAP_PATH = "/api/v6/dex/aggregator/swap"
OKX_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


class OkxError(RuntimeError):
    pass


def _raw_uint(value, name: str, positive: bool = False) -> str:
    if not isinstance(value, str) or not value.isdecimal():
        raise OkxError(f"invalid OKX {name}")
    if (positive and int(value) <= 0) or (not positive and int(value) < 0):
        raise OkxError(f"invalid OKX {name}")
    return value


def _okx_asset(asset: str) -> str:
    normalized = address(asset)
    return OKX_NATIVE if normalized == NATIVE else normalized


@dataclass(frozen=True)
class OkxSwap:
    chain_id: int
    input_asset: str
    output_asset: str
    amount_in_raw: str
    amount_out_raw: str
    minimum_amount_out_raw: str
    follower_wallet: str
    to: str
    value_raw: str
    data: str
    gas_limit: int
    gas_price_wei: str
    observed_at: float
    response_hash: str

    def public_evidence(self) -> dict:
        return {
            "provider": "okx", "chain_id": self.chain_id,
            "input_asset": self.input_asset, "output_asset": self.output_asset,
            "amount_in_raw": self.amount_in_raw,
            "amount_out_raw": self.amount_out_raw,
            "minimum_amount_out_raw": self.minimum_amount_out_raw,
            "follower_wallet": self.follower_wallet, "to": self.to,
            "value_raw": self.value_raw, "data": self.data,
            "gas_limit": self.gas_limit, "gas_price_wei": self.gas_price_wei,
            "observed_at": self.observed_at, "response_hash": self.response_hash,
        }


class OkxSwapClient:
    """Fetch one exact-input transaction; credentials and raw responses are never exposed."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None,
                 passphrase: str | None = None, project_id: str | None = None,
                 allowed_routers: frozenset[str] | None = None,
                 timeout: float = 10.0):
        self.api_key = api_key or os.environ.get("SMART_MONEY_OKX_API_KEY")
        self.secret_key = secret_key or os.environ.get("SMART_MONEY_OKX_SECRET_KEY")
        self.passphrase = passphrase or os.environ.get("SMART_MONEY_OKX_PASSPHRASE")
        self.project_id = project_id or os.environ.get("SMART_MONEY_OKX_PROJECT_ID")
        if not all(isinstance(item, str) and item for item in (
                self.api_key, self.secret_key, self.passphrase)):
            raise ValueError("OKX API credentials are incomplete")
        self.allowed_routers = frozenset(address(item) for item in (
            allowed_routers or frozenset({OKX_ROUTER})))
        if not self.allowed_routers:
            raise ValueError("OKX router allowlist is empty")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("invalid OKX timeout")
        self.timeout = float(timeout)
    def _headers(self, timestamp: str, path_with_query: str) -> dict:
        message = timestamp + "GET" + path_with_query
        signature = base64.b64encode(hmac.new(
            self.secret_key.encode(), message.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Accept": "application/json", "User-Agent": "smart-money-copytrader/0.1",
        }
        if self.project_id:
            headers["OK-ACCESS-PROJECT"] = self.project_id
        return headers

    def _request(self, path_with_query: str, timestamp: str) -> dict:
        request = urllib.request.Request(
            OKX_API_ORIGIN + path_with_query,
            headers=self._headers(timestamp, path_with_query), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise OkxError("OKX response limit")
            document = json.loads(raw)
        except OkxError:
            raise
        except Exception as exc:
            raise OkxError(f"OKX transport failure: {type(exc).__name__}") from None
        if not isinstance(document, dict):
            raise OkxError("invalid OKX response")
        return document

    @staticmethod
    def _parse(document: dict, input_asset: str, output_asset: str,
               amount_raw: str, follower_wallet: str, observed_at: float,
               response_hash: str, allowed_routers: frozenset[str]) -> OkxSwap:
        if document.get("code") != "0" or not isinstance(document.get("data"), list) \
                or len(document["data"]) != 1:
            raise OkxError("OKX swap request was not successful and unique")
        item = document["data"][0]
        router, tx = item.get("routerResult"), item.get("tx")
        if not isinstance(router, dict) or not isinstance(tx, dict):
            raise OkxError("OKX swap response is incomplete")
        try:
            chain = int(router.get("chainIndex"))
            from_token = address(router["fromToken"]["tokenContractAddress"])
            to_token = address(router["toToken"]["tokenContractAddress"])
            sender, target = address(tx.get("from")), address(tx.get("to"))
            gas = int(_raw_uint(tx.get("gas"), "gas", positive=True))
            data = tx.get("data")
        except (KeyError, TypeError, ValueError):
            raise OkxError("invalid OKX swap identity fields") from None
        expected_from = address(input_asset)
        expected_to = address(output_asset)
        if from_token == address(OKX_NATIVE):
            from_token = NATIVE
        if to_token == address(OKX_NATIVE):
            to_token = NATIVE
        if (chain != CHAIN_ID or from_token != expected_from or to_token != expected_to
                or sender != address(follower_wallet) or target not in allowed_routers):
            raise OkxError("OKX swap identity is outside the allowlist")
        from_amount = _raw_uint(router.get("fromTokenAmount"), "input amount", True)
        to_amount = _raw_uint(router.get("toTokenAmount"), "output amount", True)
        minimum = _raw_uint(tx.get("minReceiveAmount"), "minimum output", True)
        value = _raw_uint(tx.get("value"), "value")
        gas_price = _raw_uint(tx.get("gasPrice"), "gas price", True)
        if (from_amount != amount_raw or int(minimum) > int(to_amount)
                or (expected_from == NATIVE and value != amount_raw)
                or (expected_from != NATIVE and value != "0")):
            raise OkxError("OKX swap amounts do not match the request")
        if (not isinstance(data, str) or not data.startswith("0x")
                or len(data) < 10 or len(data) > 1024 * 1024 * 2 + 2):
            raise OkxError("invalid OKX swap calldata")
        try:
            bytes.fromhex(data[2:])
        except ValueError:
            raise OkxError("invalid OKX swap calldata") from None
        return OkxSwap(
            chain, expected_from, expected_to, amount_raw, to_amount, minimum,
            sender, target, value, data.lower(), gas, gas_price, observed_at,
            response_hash)

    async def swap(self, input_asset: str, output_asset: str, amount_raw: str,
                   follower_wallet: str, slippage_bps: int) -> OkxSwap:
        input_asset, output_asset = address(input_asset), address(output_asset)
        follower_wallet = address(follower_wallet)
        _raw_uint(amount_raw, "requested amount", positive=True)
        if (input_asset == output_asset or not isinstance(slippage_bps, int)
                or not 1 <= slippage_bps <= 5000):
            raise ValueError("invalid OKX exact-input swap request")
        params = [
            ("chainIndex", str(CHAIN_ID)), ("amount", amount_raw),
            ("fromTokenAddress", _okx_asset(input_asset)),
            ("toTokenAddress", _okx_asset(output_asset)),
            ("slippagePercent", f"{slippage_bps / 100:.2f}"),
            ("userWalletAddress", follower_wallet), ("swapMode", "exactIn"),
        ]
        query = urllib.parse.urlencode(params)
        path_with_query = OKX_SWAP_PATH + "?" + query
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z")
        document = await asyncio.to_thread(self._request, path_with_query, timestamp)
        observed_at = time.time()
        response_hash = hashlib.sha256(json.dumps(
            document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return self._parse(document, input_asset, output_asset, amount_raw,
                           follower_wallet, observed_at, response_hash,
                           self.allowed_routers)
