# Feed 提前资格评估：规则与接口

状态：2026-09-14 已实现离线模块，并接入 monitor 的默认关闭内部证据队列；
**提前结果未接入 PaperEngine 或实盘执行器**。
默认不运行提前决策；离线命令的 `--evaluate` 只启用回放检查，不启用跟单。
另有独立 `observe_early_feed.py --enable-shadow` 入口采集新交易的识别/归属与业务快照，
不启动 monitor 或交易准备。首版缺市场/预检快照，不能据此报告完整提前资格。
所有结果保持 `copy_eligible=false/live_enabled=false`。

现有 monitor/run 的 `--early-feed-evidence` 使用同一 Feed 连接分流，不启动第二个订阅；
只采集识别/归属证据并写入 early_feed_jobs。它不是提前下单开关，不要另开第二个实盘进程使用。
生产库使用前需显式应用 010 迁移；本轮未应用或启用。原独立影子脚本仍保留作只读诊断工具。

当前订单规则版本为 `early-intent-offline-v3`：目标链允许精确的 `4663`、`"4663"` 和
`"robinhood"`，保留原始值，不接受其他别名。独立采集按 request hint 查询 `id`，无 hint 时查
`orderId`，再核对唯一订单身份；不等待目标交易 hash 被索引。旧历史报告仍保留其当时规则版本。

入口：[操作说明](HOWTO_REPLAY_EARLY_FEED.md)、[总流程](copy_trade_flow.md)。

## 1. 分层结果

```text
当时保存的交易参数 → 候选解析 → 语义 + 部署代码快照 + 归属 + 新鲜度
                                            ↓
                           关系/金额/持仓/预算/市场检查
                                            ↓
                             交易准备快照一致性检查

后续成交证据 ─────────────────────────→ 单独核对结果
```

后续成交记录不传给提前评估函数。`pass` 表示该层通过；`reject` 表示已知条件不满足；
`unverifiable` 表示历史资料缺失或在决策时尚不可用。缺资料不得按现在的状态补成历史通过。
`deployment` 是 v2 新增的 race 包装器代码门禁；其他路径记 `not_applicable`，不冒称完成代码核验。
语义层 pass 表示参数符合该版本规则，仍须部署、归属及其余门禁全过才有离线 decision_passed。
准备层检查保存或模拟的构建/预检结果，不实际调用构建器、预留 nonce、模拟主网或签名。

历史导入注意：旧 MySQL 导入器默认 `snapshots={}`，故 `snapshot_missing` 本身只说明评估输入未提供，
不是数据库内容审计。现可用 `--reconstruct-context` 读取已存决策/归因/报价/预检与原日志，
保留完整、部分、晚到及无时间的不同情况。恢复的市场包用不可变提案/决策写入时间作为保守可用上界，
不把 quote 时间冒充其后 Gas 读取完成时间。结果及真实缺口见 [历史重建](HOWTO_REPLAY_EARLY_FEED.md#已保存历史上下文重建2026-09-14纠正缺失口径)。

## 2. 路径和排除规则

包装 selector 新增线索：公开库返回 `race(address,uint256,address,uint256,address,address,
(address,address,uint256,uint256,bytes)[],bool,bytes)`，本地重新计算 selector 确实为 `0x998b5942`。
这仅增强了 ABI 布局线索；库中的 `hasVerifiedContract` 不等于链 4663 上目标部署已经验证。
保留的响应和边界见 [证据](../data/relay_wrapper_signature_hint_2026-09-14.json)。
后续已取得目标运行字节码，按固定哈希执行离线 EVM 测试，核实试跑/回退/最低输出行为。
这不是公开源码验证或全面安全审计。109 笔历史 Feed BUY 通过新增参数规则及事后代码核验，
但缺当时快照，仍不能报提前可下单。`metadata.routes` 保留路线地址、整数、selector、calldata 哈希；
能严格解码的 Kyber 内层另附 description；Feed 中所有 `selected_or_executed` 仍是 unknown。

| 路径 | 解析内容 | 提前限制 |
|---|---|---|
| Relay Proxy `0x0a2b8f36` → 包装 `0x998b5942` | solver 输入、最低输出、交付/退款钱包、路线、订单尾缀和请求提示 | 仅固定字节码版本；必须有当时取得且匹配的 deployment 快照，并独立通过订单归属 |
| 同一 Proxy → Kyber `0xe21fd0e9` | 输入/输出币、数量、接收人、最低输出 | 要求描述与 Permit2/cleanup 一致，已观察 flags=512，订单另行验证 |
| 同一 Proxy → 0x `0x2213bc0b` | operator、输入币/数量、内部 selector、cleanup 收款币 | 输出及最低输出尚未完整解析，保持不支持 |
| EntryPoint → Simple7702Account → Relay → Kyber | 独立 UserOp、卖出数量、USDG 存款订单、最低输出 | 本地验签通过且有当时账户委托快照才能通过归属 |
| 同一账户路径 → 0x | 卖出输入候选及关联存款 | 内部输出/价格限制未解析，保持不支持 |

BUY 首版只支持单 Permit2 输入、恰好 approve/swap/cleanup 三个调用、单一全余额交付、
USDG 输入且输出不是报价资产。approve 必须对应输入币与实际换币目标；allowFailure/native value
不支持。外层 ABI 重编码必须一致，尾部只接受恰好 32 字节订单号；不是 metadata/requestId。

SELL 按 UserOp 分组，一个操作内必须有唯一交易和唯一存款，未解释动作阻止提前触发。
claim + swap 保留 swap；不是按收款人数丢弃整笔。Relay 可选失败子调用不能获得提前资格。
已观察 `0xf9e4bab4` ABI 后的 32 字节尾缀必须等于存款订单号。

### 2.1 race 包装器：验证范围与参数

唯一支持的部署地址：`0x039ec98a76f111092d4751365ff09dd2aec301e8`，chain ID 4663。
运行代码 4721 bytes，Keccak 为
`0xf5ba65338ab45430556c6b876e875413a9a9f94866ef40553b65f4025e947dcd`。
保存的 [代码与样本 A 日志](../data/relay_race_runtime_2026-09-14.json) 同时记录历史/观察区块哈希；
BaseScan 同地址代码相同，但源码未验证，不能据此声称该部署由 Relay 官方发布。

| ABI 位置 | 本版本解释 | 提前解析约束 |
|---|---|---|
| 1 / 2 | tokenIn / 声明输入量 | USDG，金额等于外层 Permit2 输入；不等于源链付款，也不保证最终全额消耗 |
| 3 / 4 | tokenOut / 最低输出量 | 输出非报价币且与 cleanup 相同；最低输出必须大于 0，其含义以代码匹配为前提 |
| 5 / 6 | recipient / refundTo | 均须为外层 RelayRouter；最终由唯一 cleanup 交给目标钱包 |
| 7 | 路线数组 | 1..256 条，每项 target、approvalTarget、native value、gas limit、calldata |
| 8 | 是否输出包装器路线事件 | true/false 都可；不改变兑换条件，也不禁止内层路由自己的日志 |
| 9 | 附带 bytes | 合约将它作为事件附带数据；本项目仅支持 66 字节 ASCII requestId 提示，仍须与独立订单匹配 |

路线 target/approvalTarget 不得为零或包装器自身；native value 必须为 0、gas limit 必须正数、
calldata 至少含 4-byte selector。外层原有 allowFailure、金额、接收者、批准 spender、尾缀校验均保留。
不因为路径包含多个候选路由就认定为多笔买入，也不猜测未知内层 selector 的业务含义。
这里支持的是**已验证包装器约束下的整笔输出意向**，不是宣布其内部任意路径已经完整解析。

该版本的行为为：逐条试跑 → 回滚试跑状态并取得输出量 → 选输出最高者 → 正式执行；
正式执行失败或输出低于其试跑结果时，撤销该次执行并选择下一条。没有成功候选会失败；
所有候选及每次回退都受全局最低输出限制。比较的是本次输出余额增量，不把包装器既有库存算成产出；
未花完的本次输入退给 refundTo，既有输入库存不计入本次退款。

样本 A：路线 0/1/2 的试跑输出依次为 `7762125719652894138818343`、
`7764216653179950981297921`、`7762125719652894138818343` raw，获选的是 index 1。
这些是 **receipt 阶段**的 RouteTrialResult/RouteRaceWon，不能回填为 Feed 当时已知的赢家或成交额。
试跑内部的状态及日志会回滚；包装器在外层另写试跑结果事件，两者不可混淆。

`tests/test_relay_race_evm.py` 在内存 Py-EVM Cancun 环境直接执行未改动的运行代码。
代币/路由对手方是使用同一 EVM 回滚日志的合成模型，覆盖失败、低输出、回退、伪造试跑错误、退款、
事件开关及既有库存，不模拟历史流动性、不签名、不连接 RPC。故障注入不是对真实路由策略的断言。
这些测试不覆盖特殊税费代币的最终净到账、不等于 Nitro Gas/时延基准，也不保证任意内层路由安全。

实时影子采集用 `eth_getCode(address,{blockHash,requireCanonical:true})` 读取部署；评估重新计算完整
code 的 Keccak，不信任调用方提供的 hash 字段。地址/链/规则版本/代码不符、快照缺失、未来或过期均拒绝。
这是观察区块的代码，不冒充源交易执行前精确状态。`--audit-race-code` 只在事后核验历史区块，
不会创建 deployment 快照或改变任何提前结果。

## 3. 身份与 Relay 归属

### 账户签名

空 initCode、空 paymasterAndData、65 字节签名的 UserOp，按 ERC-4337 v0.8 的 EIP-712
摘要恢复账户地址，检查低 s 和 v。签名绑定链、EntryPoint、nonce、完整 callData、Gas 字段。
未知初始化/签名方案不尝试其他摘要“碰到一个能恢复的地址”。

摘要根据官方 [UserOperationLib](https://github.com/eth-infinitism/account-abstraction/blob/releases/v0.8/contracts/core/UserOperationLib.sol)
和 [EntryPoint](https://github.com/eth-infinitism/account-abstraction/blob/releases/v0.8/contracts/core/EntryPoint.sol)
实现，并以独立 EIP-712 编码器、历史公开签名交叉测试。
[Simple7702Account](https://github.com/eth-infinitism/account-abstraction/blob/releases/v0.8/contracts/accounts/Simple7702Account.sol)
使用该摘要恢复账户本身；不是恢复 bundler。

有效签名不证明执行成功、Gas 足够或 nonce 可执行。提前归属还要求在决策前取得的账户代码
快照指向已登记 SimpleAccount。该快照是观察位置，不冒充精确交易前状态。
本次没有完成部署字节码与源码的可复现编译比对，运行接入前仍需校验部署身份。

### Relay 买入

`associate_order` 不读取目标 outTx 入账或 settlement fills，避免要求目标成交后才能匹配。
要求唯一订单、有效且不同的 requestId/orderId、orderId 与 calldata 尾缀一致，orderData.output.chainId
明确为 4663（缺字段不能用最终 receipt 回填），目标钱包及
唯一 payment 的代币/收款人匹配，有正最低交付量；存在包装 request 提示时也要一致。
订单状态仅接受 success/pending，来源 payer 必须等于 request user，来源交易必须唯一标为成功。
首版仅使用已登记 Solana USDC → 本链 USDG 的同精度映射；未知映射拒绝。

这是用户选择的**订单关联标准**。它不证明 Solana 付款人与 Robinhood 钱包由同一人控制，
也不能排除第三方出资但订单合法交付给该钱包的情形。普通 Transfer、只有 solver 身份或
地址字节命中不足以通过。源链 receipt 未独立复核的限制单独保留。

solver 输入与订单来源付款分别保存。比例买入使用订单来源付款；不能因数值相近而混用。
pending 订单必须提供上述完整来源证据，否则失败关闭；历史未保存的 pending 响应不伪造。

## 4. 决策与保护

- 默认关闭评估；显式离线启用后仍不生成实际 proposal。
- 仅 Feed，原始 fresh=true；源时间到决策及接收到决策均不得超过 3 秒，拒绝未来时间。
  买入 Permit2 deadline 必须未过期。补扫不计入实时覆盖率。
- 已通过归属和语义检查的 BUY 输出或 SELL 输入可作为动态目标；资金资产必须可信。
- 固定买入使用配置，比例买入用订单来源付款。超过单笔/剩余预算拒绝，不静默改金额。
- 卖出只使用同一关系的 lot，按声明卖出量映射源剩余基数及我方剩余持仓。基数缺失或不足、
  预留占用、多个本金资产、另一关系的 lot 均拒绝。没有实际来源买入量的早期 lot 不猜测基数。
- SELL 退出本金不是源信号的 USDG 时，首版缺少跨资产价格比较依据，保持无法验证。
- 复用现有 `assess_quote`：完整/参考报价、原始意向价格下限、冲击、Gas、滑点检查。
  两份报价独立检查新鲜度；构建 minOut 不低于市场滑点下限与按我方数量缩放的意向下限。
- 订单操作键为链/钱包/orderId 的 SHA-256，不含 stage、配置版本或 bundler 外层 tx hash。
  关系键再加入 relationship/follower。已消费键拒绝，防止早期/严格后备及重新打包重复跟单。
  当前只检查提供的历史消费快照；没有新增生产数据库唯一约束或预留事务。

## 5. Python 接口

实现位于 `smart_money.early_intent` 与 `smart_money.early_replay`。

| 接口 | 输入 → 输出 |
|---|---|
| `parse_candidates(tx: Transaction, wallet: str)` | `ParseResult(candidates, reasons)`；不接受 receipt |
| `Candidate.to_dict()` | 路径、声明金额、原始字段、语义 blockers、operation_key、规则版本 |
| `Candidate.relationship_key(relationship_id, follower)` | 跨触发阶段稳定的关系操作键 |
| `userop_digest(op, chain_id=4663, entrypoint=R.ENTRYPOINT)` | 受支持 UserOp 的 32 字节摘要 |
| `userop_signature_valid(op)` | 受支持签名是否恢复为 sender；不签名 |
| `associate_order(candidate, observation, at)` | 订单归因字典；不满足条件抛 ValueError |
| `evaluate_candidate(candidate, at, snapshots, enabled=False)` | 各层状态、金额计划请求、归因；固定非实盘 |
| `transaction_from_record(record)` | 保存的公开 Transaction 字段 → Transaction |
| `replay_cases(cases, enabled=False)` | 分组计数、逐笔结果、错误、事后核对；重复记录去重 |
| `reconcile(candidate, truth)` | matched/pending/source_failed/source_orphaned/资产、金额或订单不匹配 |

金额仅用十进制字符串和整数计算，最大 uint256；时间为有限的非负 Unix 秒，禁止 NaN。
候选 calldata 最大 256 KiB，批次数量上限 256，容器深度上限 12；未知或畸形结构不产生资格。
`fingerprint` 为 JSON 排序序列化 SHA-256，仅用于完整性和关联，不是身份认证。

### 回放输入和快照

顶层为 `{"cases": [...]}`。每项包含 `transaction`、`wallet`，可选 `decision_at`、
`relationship_id`、`record_id`、`provenance`、`expected_side`、`snapshots`、`truth`。
可选 `strict_evidence_observed_at` 只用于事后比较；仅当提前决策通过且两个取得时刻完整时计算
`decision_lead_ms`，它不是实际广播/入块提速。没有时间数据时为 null。
transaction 使用项目保存的 Transaction 字段，data 为 0x 十六进制，value 为十进制字符串。
decision_at 默认 received_at；两者缺失时只报告解析，不能完成提前评估。

每个快照为 `{"observed_at": Unix秒, "provenance": "来源", "payload": {...}}`。
observed_at 是**本机取得响应的时间**，不是源交易区块时间。JSON 由受信任的记录导入器提供，
不能将用户提交的布尔值或时间戳作为实盘授权。本工具没有快照真实性认证能力。

| 快照名 | payload 必需内容 | 时效 |
|---|---|---|
| order | 保存的 Relay requests 文档 | 决策前取得 |
| account | wallet、chain_id、code、block_hash | 不超过 3 秒 |
| deployment | race 专用：contract、chain_id、完整 code、block_hash、rule=`relay-race-pinned-runtime-v1` | 决策前取得，不超过 3 秒 |
| policy | enabled、stop_active、relationship_id、follower、smart_wallet、config_snapshot_hash、allowed_assets、allowed_protocols、execution_providers、buy_rule、sell_rule、max_input_raw、quote_policy | 不超过 3 秒 |
| portfolio | 与 policy 一致的四个关系/配置字段；source_orphaned、consumed_operation_keys；BUY 的 budget_available_raw；SELL 的 lots | 不超过 3 秒 |
| market | 相同四个绑定字段；quote、reference、gas_price_wei | 不超过 3 秒，报价还受 QuotePolicy 时效约束 |
| preparation | relationship_key、config_snapshot_hash、transaction、transaction_sha256、valid_until、preflight | 不超过 2 秒且 valid_until 未到期 |

金额规则沿用 `AmountRule`：fixed + fixed_amount_raw，或 proportional + ratio_ppm（百万分比）。
quote_policy 是现有 `QuotePolicy` 字段；quote/reference 是现有 `Quote.to_dict()` 字段。
lots 每项包含 lot_id、created_at、relationship_id、token、principal_asset、token_remaining_raw、
reserved_raw，以及比例卖出所需 source_remaining_raw，按 created_at/lot_id 排序。

准备 transaction 至少含 chainId/from/to/data/value/nonce；preflight 必须绑定相同
transaction_sha256 和 network_pending_nonce，simulation/balance/allowance/gas/nonce/configuration/budget
全部为 true。该层只说明保存的预检结果与确切交易一致，不证明此刻可以广播。

## 6. 归因与后续阶段

源失败与我方执行失败不同。pending 表示未核对，不算误跟；确认方向、资产、订单或 SELL
输入数量不匹配才记录对应差异。当前只有离线核对结果，没有写入生产跟单归因表。

历史数据不足的路径需后续采集带取得时间的实时影子证据。生产接入、原子任务交接/消费键、
早期 lot 核对、实时误跟归因仍是下一阶段。聚合器已实现显式 Kyber-only 配置和操作级短时复用，
离线验证正常无等待路径为 2 routes + 1 build；不减少余额/nonce/预算等安全复核，未修改运行配置。
不能把本模块存在解释为后台延时已改善。试运行范围已获批准，启用仍须完成上述接入和风险清单。
