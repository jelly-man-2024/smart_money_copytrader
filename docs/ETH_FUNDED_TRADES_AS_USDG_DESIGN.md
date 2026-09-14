# ETH/WETH 交易按 USDG 计价跟单设计（草案，待操作员确认）

状态：设计稿，未实现。2026-09-14 由操作员提出需求：`copy_relationships` 的 `eth_*` 规则不再使用，
聪明钱用 ETH/WETH 买卖时，先把 ETH/WETH 金额换算成 USDG，再按 `usdg_*` 规则跟单并占用 USDG 额度；
卖出得到 ETH/WETH 时同样换算成 USDG。

## 1. 现状与动机

- 本机账本里已证实的聪明钱买入 688 笔，其中 565 笔用 USDG、86 笔用原生 ETH（`token_in` 为零地址）、
  37 笔用其他代币；已证实卖出 318 笔全部换回 USDG。ETH 买入约占 12.5%，目前一笔都不能跟。
- 现有关系 `allowed_assets = [USDG]`，ETH/WETH 出资的信号在 `scope_reason` 就被 `asset_not_allowed`
  拒绝；`eth_rule_mode=fixed / 1 wei / 预算 1 wei` 只是形式上的占位。
- 决策引擎按出资资产分桶：`budget_bucket(token_in)` 决定用 `USDG` 还是 `ETH_WETH` 的买入规则和预算，
  lot 的 `principal_asset` 跟随 follower 实际支付的资产。两套预算、两种 PnL 单位。

## 2. 关键决策：follower 用什么资产执行

| 方案 | follower 买入支付 | 预算与 lot | 需要 ETH 库存 | 评价 |
|---|---|---|---|---|
| A（推荐） | 始终用 USDG（本地 V3 路径或 Kyber `USDG -> token`） | 单一 USDG 桶，lot 本金 USDG | 只需 gas | 与"占用 USDG 额度"一致，PnL 单位统一 |
| B | 跟随聪明钱用 ETH，金额按 USDG 等值折算 | USDG 桶记等值，lot 本金 ETH | 需要 ETH 余额 | 一个桶里混两种资产，卖出结算、审计都要双币核对 |

本设计按方案 A 展开。ETH/WETH 只出现在"聪明钱那一侧"的换算里，follower 侧从头到尾是 USDG。

## 3. 目标行为

### 3.1 聪明钱用 ETH/WETH 买入

1. 信号仍需达到 `swap_evidenced` 或 `relay_buy_evidenced`，取证逻辑不变。
2. 新增换算步骤：把 `actual_input_debit_raw`（ETH/WETH 数量）按信号所在区块的价格换算成
   `source_input_usdg_raw`，写入证据 `funding_valuation`（见第 4 节）。换算失败则拒绝，原因
   `funding_valuation_unavailable`，不回退到 ETH 规则。
3. 规模：`usdg_rule_mode=fixed` 直接用 `usdg_fixed_amount_raw`；`proportional` 用
   `source_input_usdg_raw * ratio_ppm / 1e6`。
4. 执行路径：与 USDG 买入相同，`USDG -> token`，本地已验证路径优先，其次 Kyber。
5. 价格偏离：用换算后的 USDG 价格与 follower 报价比较（现有 `validate_quote` 的公式不变，只是把
   源信号的 `token_in` 视作 USDG、`actual_input_debit_raw` 视作 `source_input_usdg_raw`）。
6. 预算：只占用 `USDG` 桶。lot 的 `principal_asset = USDG`，与现有 USDG 买入完全一致。

### 3.2 聪明钱卖出换回 ETH/WETH

1. 卖出规模仍按 lot 比例（`sell_rule` 作用于聪明钱卖出的代币数量），不需要换算。
2. follower 把 lot 卖回 lot 的本金资产（USDG），这是 `propose_sell` 现有行为。
3. 价格偏离：把聪明钱得到的 ETH/WETH `actual_output_credit_raw` 换算为
   `source_output_usdg_raw`，用它和 follower 的 USDG 报价比较。现有代码在输出资产不同的情况下
   退化为 `assess_market_quote` 并标注 `not_comparable_output_asset_changed`，本设计改为可比较。
4. 释放 USDG 额度、记 USDG 实现盈亏，与现有卖出一致。

### 3.3 不在范围内

- 聪明钱代币换代币（`TOKEN_SWAP`）、以其他代币出资的 37 笔买入，继续按现有规则拒绝。
- follower 用 ETH 执行（方案 B）。
- 历史已拒绝的 ETH 买入不回补执行。

## 4. ETH/USDG 换算的价格来源

顺序尝试，全部失败即拒绝：

1. **本地 V3 池，历史区块报价**：用工厂有界发现（现有 `discover_v3_execution_route` 的逻辑）找
   `WETH/USDG` 池，按信号的 `block_hash` 用 Quoter `eth_call` 报出 `WETH -> USDG` 的精确输出。
   这是与聪明钱成交同一区块的价格，无时间偏差。原生 ETH 按 1:1 视作 WETH。
2. **Kyber 路由报价（当前价）**：`WETH -> USDG` 的 route 接口输出。只在本地池不存在时使用，证据里
   标注 `valuation_basis=aggregator_current_price` 并记录与信号区块的时间差；超过
   `quote_policy.max_age_seconds` 的差距直接拒绝。

证据字段（写进信号 `evidence.funding_valuation` 和决策 payload）：

```json
{
  "asset": "0x0bd7…ad73",
  "amount_raw": "41000000000000000",
  "usdg_raw": "100992295",
  "basis": "v3_pool_at_signal_block",
  "pool": "0x…", "fee": 3000,
  "block_number": 1234567, "block_hash": "0x…",
  "observed_at": 1789350000
}
```

换算本身只影响规模和价格比较，不产生任何链上动作。

## 5. 配置与数据库

- 新增迁移 `008_funding_normalization.sql`：
  - `copy_relationships` 增加 `funding_normalization VARCHAR(32) NOT NULL DEFAULT 'none'`，
    取值 `none`（现状）或 `eth_as_usdg`（本设计）。
  - `eth_rule_mode / eth_fixed_amount_raw / eth_ratio_ppm / eth_budget_limit_raw` 改为可空，
    `funding_normalization='eth_as_usdg'` 时要求这四列为 NULL，加载器发现非空即拒绝启动，避免两套
    规则并存造成歧义。列先保留不删，等运行稳定后再出迁移删除。
- `funding_normalization` 进入配置快照哈希，改动后必须在同一条 UPDATE 里刷新
  `live_risk_accepted_at = CURRENT_TIMESTAMP(6)`。
- `allowed_assets` 语义不变，仍是 follower 执行路径允许出现的资产。`eth_as_usdg` 模式下，
  `scope_reason` 把源信号里的 ETH/WETH 视为"已归一的出资资产"，不再要求它出现在 `allowed_assets`。
- `mysql_config.rows_to_document` 输出 `funding_normalization`；`ETH_WETH` 桶在该模式下不再配置预算，
  `paper_budgets` 只剩 `USDG` 行。

启用 SQL 示例（对现有 6 个关系）：

```sql
UPDATE copy_relationships
SET funding_normalization = 'eth_as_usdg',
    eth_rule_mode = NULL, eth_fixed_amount_raw = NULL,
    eth_ratio_ppm = NULL, eth_budget_limit_raw = NULL,
    live_risk_accepted_at = CURRENT_TIMESTAMP(6)
WHERE enabled = 1 AND run_mode = 'mainnet_live';
```

## 6. 代码改动点

| 文件 | 改动 |
|---|---|
| `paper_config.py` | `WalletPaperPolicy.funding_normalization` 字段与校验；`eth_as_usdg` 时 `buy_rules` 只含 `USDG` |
| `mysql_config.py` | 读取新列，校验 `eth_*` 为空，进入快照 |
| `paper.py` | 新增 `normalize_funding(signal, valuation) -> Signal`：复制信号，`token_in`（买）或 `token_out`（卖）改为 USDG，`actual_input_debit_raw` / `actual_output_credit_raw` 换成 USDG 等值，原值保留在 `evidence.funding_valuation`；`planned_input_amount`、`propose_buy`、`propose_sell`、`scope_reason` 使用归一后的信号做规模、预算桶和价格比较；执行信号的 `token_in` 用 USDG 选路 |
| `quotes.py` | `LiveQuoter.value_in_usdg(asset, amount_raw, block_hash)`：V3 池历史报价优先，Kyber 当前价回退 |
| `cli.py` | `paper_observe` 中 ETH/WETH 出资的买入在 `eth_as_usdg` 模式下走 `USDG` 规则；卖出到 ETH/WETH 时先换算再比较 |
| `store.py` | 无结构变化；`principal_asset` 已按 follower 实付资产记录 |
| `docker/mysql/init/008_*.sql` | 迁移 |
| `tests/test_observer.py` | 归一函数、比例规则按 USDG 等值、价格偏离用换算值、换算失败拒绝、`eth_*` 非空拒绝启动、快照哈希变化 |
| `docs/OPERATOR_RUNBOOK.md`、`COPYTRADING_FLOW.md` | 新模式说明与启用 SQL |

## 7. 风险与边界

- **换算价格偏差**：本地池历史报价与聪明钱成交同块，偏差来自池深度而非时间；Kyber 回退是当前价，
  ETH 短时波动会直接进入偏离判断，因此设置最大时间差并记录 basis。
- **原生 ETH 与 WETH 视作等价**：Robinhood 链 WETH 合约 `0x0bd7…ad73` 为 1:1 包装，证据中保留原资产
  地址，不丢信息。
- **follower 不持有 ETH 敞口**：聪明钱在 ETH 买入后，若 ETH/USDG 汇率变动，follower 的 USDG 本位盈亏
  与聪明钱的 ETH 本位盈亏会有差异。这是方案 A 的固有属性，不是缺陷。
- **fixed 模式下换算只影响价格比较**：`usdg_fixed_amount_raw` 不依赖换算，但换算失败仍拒绝，
  因为没有可比较的源价格就无法做偏离门禁。
- **Kyber 卖出路径**：ETH 出资买入的 lot 本金是 USDG，卖出走 `token -> USDG`，与现有 Kyber 卖出一致。

## 8. 验收

1. 单测全绿；`config-snapshot` 哈希在 `funding_normalization` 变化时变化。
2. 用账本中一笔真实 ETH 买入（86 笔之一）做离线回放：换算值、规模、偏离 bps 全部落在决策 payload。
3. 小额实盘：开启 `eth_as_usdg`，等待一笔聪明钱 ETH 买入，确认 follower 以 USDG 成交、USDG 桶扣减、
   lot `principal_asset=USDG`；随后聪明钱卖出（无论换回 USDG 还是 ETH），确认 lot 关闭并释放额度。
