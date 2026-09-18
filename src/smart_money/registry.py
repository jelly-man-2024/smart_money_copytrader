from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import address

# The zero-address native sentinel is chain-agnostic (0x0 means "native asset").
NATIVE = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class ChainRegistry:
    """Per-chain contract and asset registry.

    Robinhood Chain (4663) and Arc (5042) differ structurally: Arc has no
    Relay cross-chain solver, no ERC-4337 EntryPoint bundling, and no WETH
    (its quote/gas asset is USDC, exposed both as native and as an enshrined
    ERC-20). Chain-specific fields are therefore optional; a consumer that
    reads one must confirm the chain provides it.

    New multi-chain code resolves a registry with ``chain_for(chain_id)``.
    The module-level constants below alias the Robinhood registry so existing
    single-chain call sites keep working unchanged.
    """

    chain_id: int
    name: str
    # Which allowlisted environment variable carries this chain's HTTPS RPC.
    rpc_env: str = ""
    # --- Assets ---
    weth: str | None = None            # canonical wrapped native (RH); Arc has none
    # A chain's native asset has two scales when its ERC-20 form differs: Arc's
    # gas asset is USDC at 18 decimals natively and 6 through the enshrined
    # ERC-20. Robinhood's ETH/WETH are both 18, so its divisor is 1.
    native_decimals: int = 18
    native_erc20_decimals: int = 18
    usdg: str | None = None            # RH settlement stablecoin
    usdc_erc20: str | None = None      # Arc enshrined USDC ERC-20 (6 decimals)
    quote_assets: frozenset[str] = frozenset()
    # --- Account abstraction (RH only) ---
    entrypoint: str | None = None
    simple_account: str | None = None
    metamask_account: str | None = None
    # --- DEX ---
    v2_router: str | None = None
    v2_factory: str | None = None
    v3_router: str | None = None
    v3_factory: str | None = None
    v3_quoter: str | None = None
    universal_router: str | None = None
    v4_manager: str | None = None
    v4_quoter: str | None = None
    v4_state_view: str | None = None
    v4_position_descriptor: str | None = None
    known_v4_hook_code_hashes: dict[str, str] = field(default_factory=dict)
    position_managers: frozenset[str] = frozenset()
    # --- Relay cross-chain solver (RH only) ---
    relay_proxy: str | None = None
    relay_router: str | None = None
    depository: str | None = None
    ripe_claim: str | None = None
    relay_usdg_equivalents: frozenset[tuple[int, str]] = frozenset()
    # --- Aggregators / execution ---
    zero_x_allowance_holder: str | None = None
    kyber_router: str | None = None
    kyber_chain_slug: str | None = None
    okx_router: str | None = None
    okx_approval: str | None = None
    permit2: str | None = None


    @property
    def native_to_erc20_divisor(self) -> int:
        """Scale factor between the native and ERC-20 forms of the native asset."""
        if self.native_decimals < self.native_erc20_decimals:
            raise ValueError(f"chain {self.chain_id} has an inverted native scale")
        return 10 ** (self.native_decimals - self.native_erc20_decimals)

    @property
    def settlement_asset(self) -> str | None:
        """The chain's quote/settlement stablecoin (RH: USDG; Arc: USDC)."""
        return self.usdg if self.usdg is not None else self.usdc_erc20

    @property
    def native_erc20(self) -> str | None:
        """ERC-20 form of the chain's native asset.

        Robinhood Chain wraps native ETH as WETH; Arc's native gas asset is USDC
        itself, exposed as the enshrined ERC-20. A chain that offers neither
        returns None and cannot quote the native sentinel.
        """
        return self.weth if self.weth is not None else self.usdc_erc20

ROBINHOOD = ChainRegistry(
    chain_id=4663,
    name="robinhood",
    rpc_env="ROBINHOOD_RPC_URL",
    weth="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    usdg="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    quote_assets=frozenset({
        NATIVE,
        "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    }),
    entrypoint="0x4337084d9e255ff0702461cf8895ce9e3b5ff108",
    simple_account="0xe6cae83bde06e4c305530e199d7217f42808555b",
    metamask_account="0x63c0c19a282a1b52b07dd5a65b58948a07dae32b",
    v2_router="0x89e5db8b5aa49aa85ac63f691524311aeb649eba",
    v2_factory="0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
    v3_router="0xcaf681a66d020601342297493863e78c959e5cb2",
    v3_factory="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
    v3_quoter="0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
    universal_router="0x8876789976decbfcbbbe364623c63652db8c0904",
    v4_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
    v4_quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
    known_v4_hook_code_hashes={
        # Observed unchanged at both imported fixture blocks and 2026-09-11 latest.
        "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044":
            "0xc21b1e6c1b45403e81a581f22ed6d9c747997af1cfdac1b1dc9f4b1d346a10db",
    },
    position_managers=frozenset({
        "0x73991a25c818bf1f1128deaab1492d45638de0d3",
        "0x58daec3116aae6d93017baaea7749052e8a04fa7",
    }),
    relay_proxy="0xccc88a9d1b4ed6b0eaba998850414b24f1c315be",
    relay_router="0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
    depository="0x4cd00e387622c35bddb9b4c962c136462338bc31",
    ripe_claim="0x2d3cb2b39289f402187d7dc9b609ead6646f2506",
    relay_usdg_equivalents=frozenset({
        # Relay's Solana mainnet chain id and canonical USDC mint. Both this
        # asset and Robinhood USDG use six decimals; the source identity
        # remains evidence.
        (792703809, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    }),
    zero_x_allowance_holder="0x0000000000001ff3684f28c67538d4d072c22734",
    kyber_router="0x6131b5fae19ea4f9d964eac0408e4408b66337b5",
    kyber_chain_slug="robinhood",
    okx_router="0x6e2a35a7ad683cf634d91492d73bb7ff774c6919",
    okx_approval="0x42170295f1173c9e5874ea9d00c6d137e1a4f53d",
    permit2="0x000000000022d473030f116ddee9f6b43ac78ba3",
)

ARC = ChainRegistry(
    chain_id=5042,
    name="arc",
    rpc_env="ARC_RPC_URL",
    # Arc has no wrapped native and no separate stablecoin: USDC is the native
    # gas token (18 decimals) and is also exposed as an enshrined ERC-20 at the
    # address below (6 decimals). Both forms share one balance; the ERC-20 form
    # is canonical for quoting/execution. Quote/settlement asset is USDC itself.
    weth=None,
    usdg=None,
    usdc_erc20="0x3600000000000000000000000000000000000000",
    native_decimals=18,
    native_erc20_decimals=6,
    quote_assets=frozenset({NATIVE, "0x3600000000000000000000000000000000000000"}),
    # Arc watchlist audit at block 0x143122e found 18 EIP-7702 delegations,
    # all pointing at the canonical ERC-4337 Simple7702Account deployment;
    # its 3,639-byte runtime exactly matched Ethereum at the same address.
    # The EntryPoint was first recorded as absent because its Arc code hash
    # differs from Robinhood's. That test is wrong when a chain id is an
    # immutable: the two runtimes are 21,738 bytes each and differ in exactly
    # 34 bytes — a 32-byte EIP-712 domain separator and a two-byte chain id,
    # reading 0x13b2 (5042) here and 0x1237 (4663) there. Same contract.
    # Verified 2026-09-18, after a watched wallet's sell arrived as a
    # UserOperation and could not be decoded without it.
    entrypoint="0x4337084d9e255ff0702461cf8895ce9e3b5ff108",
    simple_account="0xe6cae83bde06e4c305530e199d7217f42808555b",
    metamask_account=None,
    # Uniswap v4 dominates Arc volume; v2/v3 addresses are added if a decoded
    # smart-money swap needs them. All values below were confirmed on-chain via
    # eth_getCode against ARC_RPC_URL (chain id 5042) on 2026-09-16.
    v2_router=None,
    v2_factory=None,
    v3_router=None,
    v3_factory=None,
    v3_quoter=None,
    universal_router="0x4fca4a51ab4f23a7447b3284fbd7d73289a89fb1",
    v4_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
    v4_quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
    v4_state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    v4_position_descriptor="0x516b8a945700d6bbfdedaa6dcfc4586ba60b8707",
    known_v4_hook_code_hashes={},
    position_managers=frozenset({"0x6049c9a0e26405c0985f9e3685c87d0ae917f82b"}),
    # Relay IS deployed on Arc, at the same addresses as Robinhood Chain and
    # with byte-identical code (verified 2026-09-17 by comparing keccak of
    # eth_getCode on both chains); Relay's public API lists chain 5042 with
    # depositEnabled, and lookup_by_destination_hash resolves real Arc
    # deliveries. The depository at 0x4cd00e38… was read as a DIFFERENT contract
    # (same length, different hash) and left unset; that reading was wrong for
    # the same reason as the EntryPoint above — 8,628 bytes each, 34 differing,
    # two of them the chain id. A watched wallet's cross-chain sell was then
    # observed depositing Arc USDC into it, which is exactly the evidence
    # relay_confirmed_sell consumes. Registered 2026-09-18.
    relay_proxy="0xccc88a9d1b4ed6b0eaba998850414b24f1c315be",
    relay_router="0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
    depository="0x4cd00e387622c35bddb9b4c962c136462338bc31",
    ripe_claim=None,
    relay_usdg_equivalents=frozenset({
        # Relay's Solana chain id and canonical USDC mint. Arc settles in USDC
        # at six decimals and so does Solana, so this funding normalization is
        # an identity of scale, not an operator-approved conversion.
        (792703809, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    }),
    # Execution venues: 0x lists chain 5042 and routes it live (verified
    # 2026-09-17: /sources returns 16 Arc sources, and 6/6 probed active Arc
    # tokens quoted with liquidity through Uniswap_V4). AllowanceHolder sits at
    # the same universal address as on Robinhood Chain AND its bytecode hash
    # matches, so this is the same contract, not a CREATE2 namesake.
    # Kyber's aggregator uses the "arc" chain slug (router address confirmed in
    # Phase 3). OKX not present.
    zero_x_allowance_holder="0x0000000000001ff3684f28c67538d4d072c22734",
    kyber_router=None,
    kyber_chain_slug="arc",
    okx_router=None,
    okx_approval=None,
    permit2="0x000000000022d473030f116ddee9f6b43ac78ba3",
)

CHAINS: dict[int, ChainRegistry] = {
    ROBINHOOD.chain_id: ROBINHOOD,
    ARC.chain_id: ARC,
}


def native_to_erc20_amount(raw: int, chain: ChainRegistry) -> tuple[int, int]:
    """Restate a native-denominated amount in the ERC-20 form's scale.

    Returns the converted amount and the remainder that does not survive the
    change of scale. On Arc a native amount counts USDC in 18 decimals while the
    enshrined ERC-20 counts the same balance in 6, so anything below 0.000001
    USDC is dust the ERC-20 form cannot express; it is returned rather than
    silently dropped. Chains whose two forms share a scale convert to themselves.
    """
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError("native amount must be a non-negative integer")
    divisor = chain.native_to_erc20_divisor
    return raw // divisor, raw % divisor


def chain_for(chain_id: int) -> ChainRegistry:
    """Resolve the registry for a chain id, or raise on an unsupported chain."""
    try:
        return CHAINS[chain_id]
    except KeyError:
        raise ValueError(f"unsupported chain id: {chain_id}")


# --- Backward-compatible module-level aliases (Robinhood Chain) ---------------
# Existing single-chain call sites import these names directly. They resolve to
# the Robinhood registry so this refactor is behavior-preserving. New code that
# must work across chains should read fields off ``chain_for(chain_id)`` instead.
CHAIN_ID = ROBINHOOD.chain_id
WETH = ROBINHOOD.weth
USDG = ROBINHOOD.usdg
ENTRYPOINT = ROBINHOOD.entrypoint
SIMPLE_ACCOUNT = ROBINHOOD.simple_account
METAMASK_ACCOUNT = ROBINHOOD.metamask_account
V2_ROUTER = ROBINHOOD.v2_router
V2_FACTORY = ROBINHOOD.v2_factory
V3_ROUTER = ROBINHOOD.v3_router
V3_FACTORY = ROBINHOOD.v3_factory
V3_QUOTER = ROBINHOOD.v3_quoter
UNIVERSAL_ROUTER = ROBINHOOD.universal_router
V4_MANAGER = ROBINHOOD.v4_manager
V4_QUOTER = ROBINHOOD.v4_quoter
KNOWN_V4_HOOK_CODE_HASHES = ROBINHOOD.known_v4_hook_code_hashes
RELAY_PROXY = ROBINHOOD.relay_proxy
RELAY_ROUTER = ROBINHOOD.relay_router
ZERO_X_ALLOWANCE_HOLDER = ROBINHOOD.zero_x_allowance_holder
KYBER_META_AGGREGATION_ROUTER_V2 = ROBINHOOD.kyber_router
PERMIT2 = ROBINHOOD.permit2
OKX_ROUTER = ROBINHOOD.okx_router
OKX_APPROVAL = ROBINHOOD.okx_approval
DEPOSITORY = ROBINHOOD.depository
RIPE_CLAIM = ROBINHOOD.ripe_claim
POSITION_MANAGERS = ROBINHOOD.position_managers
QUOTE_ASSETS = ROBINHOOD.quote_assets
RELAY_USDG_EQUIVALENTS = ROBINHOOD.relay_usdg_equivalents


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
