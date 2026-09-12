from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import address

CHAIN_ID = 4663
NATIVE = "0x0000000000000000000000000000000000000000"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
ENTRYPOINT = "0x4337084d9e255ff0702461cf8895ce9e3b5ff108"
SIMPLE_ACCOUNT = "0xe6cae83bde06e4c305530e199d7217f42808555b"
METAMASK_ACCOUNT = "0x63c0c19a282a1b52b07dd5a65b58948a07dae32b"
V2_ROUTER = "0x89e5db8b5aa49aa85ac63f691524311aeb649eba"
V2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"
V3_ROUTER = "0xcaf681a66d020601342297493863e78c959e5cb2"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
V3_QUOTER = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
V4_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
KNOWN_V4_HOOK_CODE_HASHES = {
    # Observed unchanged at both imported fixture blocks and 2026-09-11 latest.
    "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044":
        "0xc21b1e6c1b45403e81a581f22ed6d9c747997af1cfdac1b1dc9f4b1d346a10db",
}
RELAY_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
RELAY_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
DEPOSITORY = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
RIPE_CLAIM = "0x2d3cb2b39289f402187d7dc9b609ead6646f2506"
POSITION_MANAGERS = {
    "0x73991a25c818bf1f1128deaab1492d45638de0d3",
    "0x58daec3116aae6d93017baaea7749052e8a04fa7",
}
QUOTE_ASSETS = {NATIVE, WETH, USDG}


def load_watchlist(path: str | Path) -> dict[str, dict]:
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "real_evm" not in (reader.fieldnames or []):
            raise ValueError("watchlist requires real_evm column")
        for row in reader:
            key = address(row["real_evm"].strip())
            if key in result:
                raise ValueError(f"duplicate watchlist address: {key}")
            result[key] = row
    if not result:
        raise ValueError("empty watchlist")
    return result


def delegation(code: str) -> str | None:
    code = code.lower()
    if len(code) == 48 and code.startswith("0xef0100"):
        implementation = "0x" + code[8:]
        if implementation in {SIMPLE_ACCOUNT, METAMASK_ACCOUNT}:
            return implementation
    return None


def snapshot_delegations(path: str | Path) -> dict[str, str]:
    """Historical replay only. Live monitoring reads eth_getCode instead."""
    data = json.loads(Path(path).read_text())
    return {address(a): impl for a, code in data.items() if (impl := delegation(code))}
