# 聚合器执行路径接入设计

日期：2026-09-13。状态：阶段 1（Kyber 提供方）已实现，见 `src/smart_money/kyber.py`、
`docker/mysql/init/007_execution_providers.sql` 与 `docs/OPERATOR_RUNBOOK.md` 的
"执行路径提供方"一节；阶段 2（Relay/OKX 回退与双源校验）和阶段 3 未实现。
基线代码：`main @ 20f2512`。链：Robinhood Chain 4663。读者：开发者与操作员。
实现中与提案的差异：风控评估以聚合器构建交易返回的输出为基准（它才是链上 minReturn 保护的
数字），路由报价只提供冲击参考；广播前模拟采用 follower 身份的 `eth_call` 并校验 Router 返回量，
而非 prestate 差额追踪。

把跟单执行从"只走本工厂 V3 直连池"扩展为"本地可验证路径优先、聚合器兜底"，在保留全部
失败关闭门禁的前提下，覆盖这批聪明钱实际交易的 V4 池、跨工厂池和多跳路径。

## 1. 问题：链路是通的，路径覆盖不够

2026-09-13 在 Mac mini 上用 relationship 1 完成了一轮真实 BUY → SELL 跟单闭环，说明信号识别、
Relay 订单归因、额度、门禁、签名、广播和结算都能工作。随后把 6 个真实聪明钱接入后，同一天出现
7 笔已证实（`relay_buy_evidenced`）的买入，只有 1 笔生成了跟单。其余 6 笔全部以 `quote_unavailable`
被拒，没有花钱，但也没有跟上。

| 源交易 | 聪明钱 | 投入 USDG | 目标 Token | Solver 实际成交场所 | 本工厂 USDG 直连池 | 结果 |
|---|---|---:|---|---|---|---|
| 0xfe5389a1… | 0x89909912… | 3.000000 | 0x385f4f8a…1e18 | V3 池 0x5d37b1d8… | fee 3000，有流动性 | 已跟买 2 USDG |
| 0x0aca0041… | 0x89909912… | 2.000000 | 0x98096d17…1e18 | Kyber 经 V3 池 0x6f95ac65…（非本工厂） | fee 10000，流动性 0 | quote_unavailable |
| 0xa4abd8a7… | 0x1cfbe3af… | 350.000000 | 0xba516ef0…d064 | V4 PoolManager | 无 | quote_unavailable |
| 0xd02fe5c7… | 0x365a446a… | 500.000000 | 0x462dff4b…674d | V3 池 0x52e65b17… + V4 | fee 10000，流动性 0 | quote_unavailable |
| 0xf8099c26… | 0x365a446a… | 500.000000 | 0x462dff4b…674d | V3 池 0x16679e2a… + V4 ×3 | fee 10000，流动性 0 | quote_unavailable |
| 0x69f06fc8… | 0x365a446a… | 50.000000 | 0x50346f91…fb2d | 回执无标准 Swap 事件（疑为发射台合约） | 无 | quote_unavailable |
| 0x441f711a… | 0x1cfbe3af… | 101.000000 | 0xbedbccd1…da9e | 回执无标准 Swap 事件（疑为发射台合约） | 无 | quote_unavailable |

数据来自 `var/log/sm-copy.log`、`paper_decisions` 表和只读 RPC 核对。USDG 为 6 位小数。

### 根因

当前执行端只接受能在本地逐字段验证的 V2 / V3 / V4 exact-input 路径。对 Relay、0x、Kyber 来源的
信号，本地路径只有两种来源，且都只看 V3：

- Relay 交付回执里若有方向匹配的 Swap 日志，从中提取并经配置的 V3 工厂验证
  （`pools.discover_v3_execution_route`）。
- 回执没有 Swap 时，主动向配置的 V3 工厂查 100 / 500 / 3000 / 10000 四个费率的"本金 → Token"
  直连池（`quotes.discover_v3_route`）。

不查 V2 工厂，不查 V4 PoolManager，不做经 WETH 的多跳，不识别其他工厂的池。另外即使能发现 V4 池，
实盘执行对"Token 输入"的 V4 交易因 Permit2 语义未验收而显式拒绝。而这批聪明钱买的币，流动性恰恰
集中在 V4 池、其他工厂的池或需要中转的路径上。

## 2. 探针证据：聚合器能覆盖今天全部被拒代币

对今天前 4 个被拒代币，以 100000 raw（0.1 USDG）输入做只读询价，两个公开接口都返回了可执行路径。
OKX 客户端仓库里已有（`src/smart_money/okx.py`），但需要 API 凭据，本次未测；
`docs/WATCHLIST_ROUTE_ANALYSIS_2026-09-12.md` 记录 OKX Router 在观察钱包中的链上使用为 0，其覆盖尚未证实。

| 目标 Token | Kyber 报价输出 raw | 跳数 | Relay 报价输出 raw | Relay 冲击 |
|---|---:|---:|---:|---:|
| 0xba516ef0…d064 | 4867845972491450187776 | 1 | 4860543058505190342656 | -0.58% |
| 0x50346f91…fb2d | 12457915840192948731904 | 2 | 12438679314721513930752 | -1.84% |
| 0x462dff4b…674d | 124089774257670619136 | 1 | 123902668198756810752 | -0.39% |
| 0x98096d17…1e18 | 2764737373026171392 | 1 | 2767139732711991414 | -0.01% |

- Kyber：`https://aggregator-api.kyberswap.com/robinhood/api/v1/routes`，无需密钥，Router 固定为
  `0x6131b5fae19ea4f9d964eac0408e4408b66337b5`（仓库已把它登记为 Kyber 来源协议）。
- Relay：`https://api.relay.link/quote`，同链 4663 → 4663，返回 approve + swap 两步，swap 的 `to`
  为 Relay 合约 `0xccc88a9d1b4ed6b0eaba998850414b24f1c315be`。
- 两家报价相互印证，偏差在 0.3% 以内。

## 3. 目标与非目标

- 目标：以 USDG 为本金的 BUY 和对应 SELL，在本地路径不可用时改由聚合器报价并执行；保留关系门禁、
  额度、急停文件、配置快照复核、nonce 预留、ERC-20 净差额结算等全部失败关闭机制。
- 目标：每次拒绝都留下可排查的原因文本，而不只是异常类型。
- 非目标：复用聪明钱或 Relay solver 的 calldata；无人值守运行；ETH 本金路径；V4 本地执行的 Permit2
  集成（聚合器覆盖后优先级下降）。

## 4. 总体设计：路径提供方按优先级回退

把"如何拿到一条可执行路径"抽成 `RouteProvider` 接口，按关系配置的顺序逐个尝试，全部失败才返回
`quote_unavailable`。本地可验证路径永远排第一，因为它是唯一能逐字段核对 calldata 的方式。

```text
现状： 源信号 → 回执 Swap 提取(仅 V3) → V3 工厂直连池 → quote_unavailable（今天 6/7 在此结束）
方案： 源信号 → 本地路径(回执/预配置/V3 工厂，逐字段可验证)
              → Kyber 提供方(公开 API，Router 白名单)
              → Relay 提供方(quote 接口，合约白名单)
              → OKX 提供方(现有客户端，需凭据)
              → quote_unavailable（附带每个提供方的失败原因）
```

### 接口

```python
class RouteProvider(Protocol):
    name: str                      # "local" | "kyber" | "relay" | "okx"
    async def quote(self, token_in, token_out, amount_in_raw,
                    follower_wallet, slippage_bps) -> ProviderQuote

@dataclass(frozen=True, slots=True)
class ProviderQuote:
    provider: str
    amount_out_raw: str            # 报价输出
    minimum_out_raw: str           # 提供方给出的 minOut
    tx_to: str                     # 必须在 Router 白名单内
    tx_data: str                   # 黑盒 calldata，仅顶层可反解
    tx_value_raw: str              # ERC-20 输入时必须为 0
    gas_estimate: int
    observed_at: float             # 用于 max_age_seconds 时效门禁
    response_hash: str             # 原始响应的 SHA-256，写入计划
```

现有的 `OkxSwapClient` 已经按这个形状在校验响应（链、币对、sender、Router 白名单、金额一致、
minOut 不超过报价），可以直接适配成第三个提供方。Kyber 提供方参照它实现，并像 `relay_api.py`
一样固定主机名、超时和响应大小上限。

### 配置

新增迁移 `docker/mysql/init/007_execution_providers.sql`：

```sql
ALTER TABLE copy_relationships
  ADD COLUMN execution_providers JSON NOT NULL DEFAULT ('["local"]')
    AFTER allowed_routes;
```

默认值让现有关系行为完全不变；操作员显式写入 `["local","kyber","relay"]` 才启用聚合器。该字段进入
配置快照，因此修改它必须在同一条 UPDATE 里刷新 `live_risk_accepted_at`，这正好把"是否接受聚合器
执行"变成一次显式的逐关系确认。已初始化的 volume 需由数据库管理员执行该迁移一次，规则同 006。

## 5. 报价阶段

- 时效：沿用 `quote_policy.max_age_seconds`（当前 2 秒）。聚合器往返通常几百毫秒，超时即视为该
  提供方失败并回退。
- 源价偏离：沿用 `assess_quote` 的 `max_adverse_deviation_bps`，参考价是聪明钱 Relay 订单里的实际
  成交价（`actual_input_debit_raw / actual_output_credit_raw`）。
- 价格冲击：沿用现有做法，向同一提供方再询一个缩小金额的参考报价，比较单位价格。
- 双源校验（可选，默认开）：若配置了两个以上聚合器，取首选方报价与次选方报价比较，偏差超过
  `max_price_impact_bps` 则拒绝。今天四个样本两家偏差均在 0.3% 内。
- 成交前二次报价：保留现有"决策时报价、签名前再报价"的两段式，第二次报价仍需通过原始 minOut。

## 6. 执行阶段：安全模型的替换项

这是方案里唯一在安全模型上做实质让步的地方，需要操作员明确认可。本地路径能把自建 calldata 用
ABI 反解逐字段核对；聚合器 calldata 是黑盒，只能用四道替代门禁共同兜底。

| 检查项 | 本地路径（现状，保留） | 聚合器路径（新增） |
|---|---|---|
| 交易目标地址 | V2 / V3 Router 常量 | 提供方 Router 白名单：Kyber 0x6131b5fa…、Relay 0xccc88a9d…、OKX 0x6e2a35a7…；ERC-20 输入时 value 必须为 0 |
| calldata 核对 | 自建后 ABI 反解，逐字段等于计划 | 顶层参数反解：Kyber `swap` 的 srcToken / dstToken / dstReceiver = follower / amount / minReturnAmount（仓库已有 `0xe21fd0e9` 解码）；内部执行数据视为黑盒 |
| 最小输出 | 自算 minOut | 取 min(提供方 minOut, 自算下限)，且必须 ≥ 报价 × (1 − max_slippage_bps) |
| 广播前模拟 | 只读预检余额、allowance、Gas | 新增：用已有的只读 `debug_traceCall` prestateTracer diffMode 模拟整笔交易，要求 follower 仅 USDG 减少且等于投入量、目标 Token 增加且 ≥ minOut、无其他资产流出；Gas 估算 ≤ `max_gas_cost_wei` |
| 授权 | 有界授权给 V2 / V3 Router | 同一套有界授权逻辑，spender 参数化为提供方 Router：USDG 上限为周期预算 × 200，SELL 授权等于归因持仓 |
| 计划完整性 | `plan_integrity_hash` 覆盖 calldata | 不变，另存 provider、response_hash、报价快照 |
| 结算 | 规范回执的 follower ERC-20 净差额 | 不变 |
| 失败处理 | quote_unavailable | 逐提供方回退；全部失败仍失败关闭，并记录每个提供方的原因文本 |

### 跟卖

SELL 不再依赖"反转买入时保存的路径"。对经聚合器买入的 lot，在归因里记录 provider；卖出时按归因
数量向提供方重新询 Token → USDG 报价，做同样的有界授权、模拟和 minOut 校验。本地路径买入的 lot
保持现有反转逻辑不变。

### 并发与 nonce

沿用 20f2512 引入的按 follower 串行锁：授权和交易在同一钱包内顺序发送，不同 follower 并行。
聚合器路径不改变这一点。

## 7. 附带修复

- 可诊断性：`paper_decisions.payload` 目前只存 `quote_error_type`，把异常消息和每个提供方的失败原因
  一并写入，排查时不必离线复现。
- 监听范围：`sm-copy run` 固定带上 67 个 CSV 地址，它们的每笔被动入账都会触发最多 8 次 Relay 查单。
  接入聚合器后外部 API 压力上升，建议给 run 增加"只监听 enabled 关系聪明钱"的选项，或把 CSV 地址
  降为不做 Relay 关联的纯观察。
- 事件：新增 `route_provider_selected` 和 `route_provider_failed` 两个日志事件，带 provider、耗时和
  原因，便于统计各提供方的命中率与延迟。

## 8. 分阶段计划与验收

### 阶段 0：可诊断性与影子报价（约 0.5 天）

写入拒绝原因文本；实现 Kyber 提供方的 quote，只记录不执行。让 run 在每次 `quote_unavailable` 时
顺带询一次 Kyber 并记录结果。

验收：跑满一天后统计"已证实买入中影子报价成功"的比例，作为覆盖率基线。今天的样本是 4 / 4。

### 阶段 1：Kyber 实盘路径（约 2 到 3 天）

`RouteProvider` 接口、007 迁移、顶层 calldata 反解、模拟差额门禁、参数化 spender 的有界授权、SELL
走提供方、单测覆盖白名单拒绝与模拟拒绝、13 笔回放不变。

验收：先以 `run_mode=paper` 跑 6 条关系至少 24 小时，纸面成交与源价偏差在策略内；再对 1 条关系做
0.1 到 1 USDG 的看守实盘，至少 3 笔 BUY / SELL 闭环，链上余额与账本一致，`execution-audit` 无 issue。

### 阶段 2：第二提供方与双源校验（约 1 到 2 天）

接 Relay quote 作为回退并启用双源偏差校验；如提供 OKX 凭据，把现有 `OkxSwapClient` 适配为第三提供方。

验收：人为关闭首选方时自动回退并留痕；双源偏差超阈值时拒绝并留痕。

### 阶段 3：V4 本地执行（可选，暂缓）

Permit2 集成与 Token 输入 V4 验收。聚合器已覆盖 V4 流动性后，这一项只在需要摆脱外部 API 依赖时再做。

## 9. 风险与需要操作员决定的事

- calldata 黑盒。这是核心让步。缓解手段是 Router 白名单、顶层参数反解、minOut 下限和广播前模拟四道
  门禁叠加，其中模拟是真正的兜底，必须作为硬门禁而非告警。
- 外部 API 依赖。可用性、限流和延迟都会影响命中率，但不影响安全：任何失败都回落到
  `quote_unavailable`。Kyber 公开接口建议带 `x-client-id` 并遵守其限流；Relay 已在用。
- 更多 spender 授权。follower 将对 Kyber、Relay、OKX 的 Router 持有有界授权，测试结束按现有惯例
  撤销为 0。
- 小额下的 Gas 占比。当前单笔 0.1 USDG，一笔交易约 0.0000133 ETH Gas，聚合器多跳路径 Gas 更高。
  建议实盘验收时单笔不低于 1 USDG，否则收益被 Gas 吞没会干扰对逻辑正确性的判断。

需要决定：是否接受第 6 节的安全模型替换。接受则同步更新 `docs/LIVE_RISK_CHECKLIST.md` 与
`docs/OPERATOR_RUNBOOK.md` 中"只为已验证精确路由构建 exact-input"的表述，并在各关系的同一条 UPDATE
中写入 `execution_providers` 与新的 `live_risk_accepted_at`。
