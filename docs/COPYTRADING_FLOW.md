# 当前跟单流程与行为矩阵

更新时间：2026-09-12。

本文说明当前代码如何从聪明钱链上活动产生信号，以及业务 MySQL 中一条
`copy_relationships` 配置会让跟单钱包采取什么动作。当前 `run_mode` 只允许 `paper`：所谓
“跟买、跟卖、成交”均为使用实时 RPC 报价写入业务账本的纸面操作，不读取主网私钥、不签名、
不广播交易，所有信号继续保持 `copy_eligible=false`。

## 1. 一条跟单关系代表什么

一行 `copy_relationships` 唯一表示：

```text
一个跟单钱包 follower_wallet
        ×
一个聪明钱钱包 smart_wallet
        ×
这一对钱包唯一采用的一套策略
```

同一个聪明钱可以被多个跟单钱包跟随；每一行都有独立 relationship ID、配置快照、额度、
proposal、持仓 lot 和收益归因。某行 `enabled=false` 时只保留配置，不参与监听后的纸面决策。

当前主要配置字段：

| 配置 | 当前含义 |
|---|---|
| `trigger_mode` | 主触发点，只能是 `feed_intent`、`receipt_success` 或 `swap_evidenced` |
| `shadow_trigger_modes` | 只记录比较结果，不占额度、不产生成交 |
| `usdg_rule_*` | 聪明钱以 USDG 买入时，跟单使用固定金额或输入金额比例 |
| `eth_rule_*` | 聪明钱以 ETH/WETH 买入时，跟单使用固定金额或输入金额比例 |
| `sell_rule_*` | 聪明钱卖出时，跟单使用固定 token 数量或聪明钱实际卖出数量比例 |
| `*_budget_limit_raw` | 本手动周期内、按聪明钱关系隔离的 USDG 与 ETH/WETH 累计投入上限 |
| `allowed_protocols` | 当前策略允许报价的 V2/V3/V4 协议集合 |
| `allowed_assets` | 跟单决策允许涉及的资产合约集合 |
| `allowed_routes` | 允许的完整资产路径及 V3 fee、V4 fee/tickSpacing/hook/hookData |
| `quote_policy` | 报价年龄、价格偏离、价格影响、滑点、Gas 和最小输出限制 |
| `strategy_version` | 写入历史归因的策略版本；修改配置不会倒推改写旧成交 |

金额始终按链上原始整数处理并以十进制字符串保存。`ratio_ppm=100000` 表示 10%，
`ratio_ppm=500000` 表示 50%，`ratio_ppm=1000000` 表示 100%。

### 本机数据库当前实际状态

2026-09-12 UTC 只读查询本机业务 MySQL：`copy_relationships` 共 67 行，`enabled=true` 为 0 行，
`enabled=false` 为 67 行。因此服务器当前虽然持续观察清单地址，但没有任何一条正在生效的跟单
关系；聪明钱发生任何行为都只会进入观察账本，不会创建 paper proposal 或 paper fill。

本文后续所称“当前跟单动作”，是指某行经过使用者核对并设置 `enabled=true` 后，现有代码会按照
该行配置采取的动作。README 中的 `config/paper.example.json` 只是示例，不是本机当前启用策略。

## 2. 从聪明钱交易到当前纸面成交

```text
Feed 实时意向 [N1] ───────────┐
                              ├─ 恢复真实发送者/解智能账户和 UserOp [N3]
RPC 独立区块补漏 [N2] ────────┘
                                      ↓
                         解码行为与内部调用路径 [N4]
                                      ↓
                      RPC 回执、池归属和资金流核对 [N5-N8]
                                      ↓
               intent / execution_observed / swap_evidenced
                         / needs_review / failed [N9]
                                      ↓
                  查询所有 enabled 的匹配 relationship [N10]
                                      ↓
              触发档 + 协议/资产/路由 + 金额规则检查 [N11-N13]
                                      ↓
                  决策时实时报价及第一轮风险检查 [N14-N15]
                                      ↓
              在业务账本事务中创建 proposal 并预留额度 [N16]
                                      ↓
                    成交前第二次报价及同一套风控 [N17]
                                      ↓
             当前：写 paper order/fill/position/收益归因 [N18-N20]
             未来 M3：准备/签名/广播/跟踪 [N21-N24]
```

Feed 和 RPC 不是二选一。Feed 用于更早观察意向；RPC 用于补洞、取得回执和核对链上事实。
主触发点决定何时尝试跟单，不能取消后续重新报价和风险检查。

### 流程节点代码索引

下面的行号对应当前提交附近的位置；后续改代码后行号可能变化，优先按类名/函数名搜索。

| 节点 | 实现入口 | 具体职责 |
|---|---|---|
| N0 进程总入口 | [`cli.py:155 monitor()`](../src/smart_money/cli.py#L155) | 组装 RPC、Feed、Decoder、队列、Store、PaperEngine 和后台任务 |
| N1 Feed 接收 | [`cli.py:471 receive()`](../src/smart_money/cli.py#L471)、[`feed.py:127 envelopes()`](../src/smart_money/feed.py#L127) | 接收 WSS 帧、拆 Nitro envelope、检查序号缺口与消息新鲜度 |
| N1 原始交易恢复 | [`feed.py:24 signed_transactions()`](../src/smart_money/feed.py#L24)、[`feed.py:57 decode_raw()`](../src/smart_money/feed.py#L57) | 解析签名交易、恢复真实 sender；不使用 header sender 代替钱包身份 |
| N2 RPC 区块补洞 | [`cli.py:436 backfill()`](../src/smart_money/cli.py#L436)、[`backfill.py:57 BlockScanner.scan_once()`](../src/smart_money/backfill.py#L57) | 按独立 safe-head 游标扫描完整区块和目标 Transfer 日志，补 Feed/重启缺口 |
| N2 重组处理 | [`backfill.py:130 BlockScanner.reconcile_reorg()`](../src/smart_money/backfill.py#L130)、[`cli.py:544 reconcile_reorg()`](../src/smart_money/cli.py#L544) | 找共同祖先、标记孤块信号并重新核对候选；深重组需要显式命令 |
| N3 候选持久化/调度 | [`store.py:1321 put_candidate()`](../src/smart_money/store.py#L1321)、[`cli.py:100 dispatch_pending()`](../src/smart_money/cli.py#L100) | 先落盘再入有界队列，支持重启恢复和有界重试 |
| N3/N4 身份与行为解码 | [`decode.py:52 Decoder`](../src/smart_money/decode.py#L52)、[`decode.py:57 Decoder.decode()`](../src/smart_money/decode.py#L57) | 区分 direct/self account/bundled/third party，递归解析已知账户、Router 与内部调用 |
| N4 BUY/SELL 判定 | [`decode.py:21 side()`](../src/smart_money/decode.py#L21) | 依据报价资产方向区分 BUY、SELL、TOKEN_SWAP；不是看到 Swap 字样就判断 |
| N5 候选处理主链 | [`cli.py:334 worker()`](../src/smart_money/cli.py#L334) | 解码 intent、查询回执、执行 prestate/池/资金流核验并保存升级后的信号 |
| N6 池归属验证 | [`pools.py:28 verify_signal_pools()`](../src/smart_money/pools.py#L28) | 验证 factory、池地址、token 对、V4 manager/PoolKey/hook 等证据 |
| N7 ETH 资金流 | [`native_flows.py:8 verify_native_flows()`](../src/smart_money/native_flows.py#L8) | 使用只读状态差分/trace 分开实际原生币流、退款与 Gas |
| N8 回执归属与资产流 | [`receipts.py:29 transfers()`](../src/smart_money/receipts.py#L29)、[`receipts.py:55 operation_scopes()`](../src/smart_money/receipts.py#L55)、[`receipts.py:76 enrich()`](../src/smart_money/receipts.py#L76) | 计算钱包 ERC-20 delta、隔离各 UserOperation 日志、生成第三方入账/分发并决定证据等级 |
| N9 信号模型与 ID | [`models.py:35 Signal`](../src/smart_money/models.py#L35)、[`store.py:1502 put()`](../src/smart_money/store.py#L1502) | 保存 intent/execution/canonical 状态、原始整数字符串、理由和证据；禁止高证据被低证据覆盖 |
| N10 MySQL 关系加载 | [`mysql_config.py:125 load_mysql_paper_config()`](../src/smart_money/mysql_config.py#L125)、[`paper_config.py:125 load_paper_config()`](../src/smart_money/paper_config.py#L125) | 只加载 enabled 行，逐 relationship 严格校验并生成配置快照 |
| N10 业务 MySQL 后端 | [`mysql_store.py:99 MySqlStore`](../src/smart_money/mysql_store.py#L99)、[`cli.py:52 runtime_store()`](../src/smart_money/cli.py#L52) | 让既有 Store 合约运行于业务 MySQL；`--ledger-mysql` 时启用 |
| N11 信号分派到关系 | [`cli.py:263 paper_observe()`](../src/smart_money/cli.py#L263) | 找出该聪明钱的全部 relationship，选择 BUY/SELL 规则并分别执行主/影子触发 |
| N12 触发档判断 | [`paper.py:135 trigger_allowed()`](../src/smart_money/paper.py#L135) | 检查 feed_intent、receipt_success、swap_evidenced 的对应条件及孤块/失败状态 |
| N12 路径范围判断 | [`paper.py:30 signal_route_key()`](../src/smart_money/paper.py#L30)、[`paper.py:154 scope_reason()`](../src/smart_money/paper.py#L154) | 将协议、完整资产路径、fee/hook 参数与 relationship allowlist 精确匹配 |
| N13 跟单金额 | [`paper.py:83 budget_bucket()`](../src/smart_money/paper.py#L83)、[`paper.py:115 planned_input_amount()`](../src/smart_money/paper.py#L115) | 选择 USDG 或 ETH/WETH 桶，按固定金额或聪明钱已验证实际输入比例计算 |
| N14 实时报价 | [`quotes.py:159 LiveQuoter`](../src/smart_money/quotes.py#L159)、[`quotes.py:177 quote_with_reference()`](../src/smart_money/quotes.py#L177) | 在固定区块取得 V2/V3/V4 报价和小额参考报价，不复用聪明钱成交价 |
| N15 风控评估 | [`quotes.py:72 validate_quote()`](../src/smart_money/quotes.py#L72)、[`quotes.py:108 assess_quote()`](../src/smart_money/quotes.py#L108) | 检查年龄、资产、源价格偏离、价格影响、滑点、Gas 与最小输出 |
| N16 BUY 决策/预留 | [`paper.py:266 PaperEngine.propose_buy()`](../src/smart_money/paper.py#L266)、[`store.py:680 reserve_paper_proposal()`](../src/smart_money/store.py#L680) | 保存 decision，并在同一事务创建 proposal、检查和预留对应额度 |
| N16 SELL 决策/预留 | [`paper.py:321 PaperEngine.propose_sell()`](../src/smart_money/paper.py#L321)、[`store.py:1080 reserve_paper_sell()`](../src/smart_money/store.py#L1080) | 只预留同 relationship、同 token、同本金桶的可用 position lot |
| N17 二次报价/纸面成交 | [`paper.py:385 PaperExecutor`](../src/smart_money/paper.py#L385)、[`paper.py:397 execute()`](../src/smart_money/paper.py#L397) | 对 reserved proposal 再报价；恶化或异常则取消，满足条件才写纸面 fill |
| N18 BUY 订单与持仓 | [`store.py:882 fill_paper_buy()`](../src/smart_money/store.py#L882) | 原子写 BUY order/fill/position lot，额度由 reserved 转为 invested |
| N19 SELL 与本金恢复 | [`store.py:1153 fill_paper_sell()`](../src/smart_money/store.py#L1153) | 消耗 lot reservation、按卖出比例减少持仓、恢复原本金并写 realized PnL |
| N20 收益与归因导出 | [`store.py:1023 paper_trades()`](../src/smart_money/store.py#L1023)、[`store.py:1265 paper_realized_pnl()`](../src/smart_money/store.py#L1265) | 导出 smart/follower/relationship/source signal/策略快照及 BUY/SELL 收益明细 |
| N21 未签名交易准备 | [`execution_prep.py:36 build_execution_plan()`](../src/smart_money/execution_prep.py#L36)、[`execution_pipeline.py:120 ExecutionPreparer.prepare()`](../src/smart_money/execution_pipeline.py#L120) | 为有限受支持路径构建 follower 自己的 calldata，检查 nonce/余额/allowance/Gas；当前未接 monitor |
| N22 离线测试签名 | [`execution_pipeline.py:184 OfflineExecutionSigner`](../src/smart_money/execution_pipeline.py#L184) | 只在三重 `offline_test` 门禁下测试；主网密钥读取和主网签名仍拒绝 |
| N23 广播前复核 | [`execution_pipeline.py:281 ReadOnlyPreBroadcastReviewer`](../src/smart_money/execution_pipeline.py#L281) | 对内存 signed bytes 重查关系、额度、报价、交易字段和 nonce，固定不广播 |
| N24 公开交易跟踪 | [`execution_receipts.py:42 ReadOnlyExecutionTracker`](../src/smart_money/execution_receipts.py#L42)、[`execution_receipts.py:88 observe()`](../src/smart_money/execution_receipts.py#L88) | 对外部提供的 tx hash 跟踪 pending/confirmed/reverted/replaced/orphaned；没有发送函数 |

阅读主线时建议先看 N0、N1、N3/N4、N5、N8、N11、N16、N17、N18/N19；再按需要深入池验证、
补洞/重组和尚未接入的 execution preparation。完整数据库表定义在
[`docker/mysql/init/001_copy_relationships.sql`](../docker/mysql/init/001_copy_relationships.sql) 和
[`docker/mysql/init/003_runtime_ledger.sql`](../docker/mysql/init/003_runtime_ledger.sql)。

### 对照阅读的回归测试

| 想验证的规则 | 测试位置 |
|---|---|
| 真实样本 BUY/SELL | [`test_observer.py:627`](../tests/test_observer.py#L627)、[`test_observer.py:635`](../tests/test_observer.py#L635) |
| claim + swap 不丢 swap | [`test_observer.py:200`](../tests/test_observer.py#L200) |
| bundle 中他人的 Swap 不归因 | [`test_observer.py:395`](../tests/test_observer.py#L395) |
| 被动收币不算买入 | [`test_observer.py:427`](../tests/test_observer.py#L427) |
| 230 地址分发只生成一条负例 | [`test_observer.py:692`](../tests/test_observer.py#L692) |
| 报价、决策与额度原子预留 | [`test_observer.py:868`](../tests/test_observer.py#L868) |
| 多 follower 跟同一 smart 时隔离 | [`test_observer.py:2220`](../tests/test_observer.py#L2220) |
| BUY fill 与归因 lot | [`test_observer.py:2579`](../tests/test_observer.py#L2579) |
| SELL 只卖归因 lot 并恢复本金 | [`test_observer.py:2610`](../tests/test_observer.py#L2610) |
| 重组保留 intent、撤销规范链证据 | [`test_observer.py:2945`](../tests/test_observer.py#L2945) |
| 本地链地址/chain ID 安全边界 | [`test_local_copytrade.py:18`](../tests/test_local_copytrade.py#L18)、[`test_local_copytrade.py:43`](../tests/test_local_copytrade.py#L43) |

### 三种触发档

| 主触发点 | 何时允许进入决策 | 当前风险和用途 |
|---|---|---|
| `feed_intent` | Feed 中已解出新鲜、明确的 exact-input 交易意图 | 最快，但聪明钱可能最终失败；当前适合作影子比较 |
| `receipt_success` | 目标交易或目标 UserOperation 已成功，且不是模糊/待复核行为 | 比 Feed 稳妥，但不一定已有完整资产交换证据 |
| `swap_evidenced` | 池、Swap、钱包输入扣款和输出入账形成当前支持范围内的闭环 | 当前默认主触发，也是最保守的第一版选择 |

示例配置的主触发是 `swap_evidenced`，`feed_intent` 和 `receipt_success` 是 shadow。也就是说，
前两档即使判断“可以”，也只写影子 decision；只有证据闭环档可以预留额度并写纸面成交。

## 3. 聪明钱发生不同交易时，跟单钱包怎么处理

| 聪明钱链上情况 | 系统分类/证据 | 当前跟单动作 |
|---|---|---|
| 使用支持的 Router 买币，实际支出 USDG 并收到目标 token | `BUY/swap_evidenced` | 读取 USDG 买入规则，报价和风控通过后按固定金额或比例纸面跟买，占用 USDG 桶额度 |
| 使用支持的 Router 买币，实际支出 ETH/WETH 并收到目标 token | `BUY/swap_evidenced` | 读取 ETH/WETH 买入规则，ETH 与 WETH 共用一个额度桶；wrap 本身不新增额度 |
| token A 明确兑换 token B，业务上不能归成普通报价币买卖 | `TOKEN_SWAP/swap_evidenced` | 只有输入资产属于已配置买入桶且协议/资产/路由允许时才按买入逻辑处理，否则不创建 proposal |
| 卖出此前买入的 token，收到 USDG 或 ETH/WETH | `SELL/swap_evidenced` | 按 sell rule 计算数量，只预留这条 relationship 归因的 open lot；没有足够归因持仓就拒绝 |
| 只卖出一部分 | `SELL/swap_evidenced` | 按实际卖出比例减少对应 lot，只恢复该部分原始投入本金；不按卖出收入或利润放大额度 |
| 卖出数量超过跟单钱包由该聪明钱产生的持仓 | `SELL`，但持仓不足 | `attributed_position_insufficient`，不卖其他聪明钱或用户自己持有的 token |
| 单纯收到 token、空投或批量分发 | `INCOMING_TRANSFER` 或 `BULK_DISTRIBUTION` | 不跟买；收币不是买入证据 |
| 单纯向外转 token | `TRANSFER` | 不跟卖；转账不是卖出证据 |
| 领取奖励，没有兑换 | `CLAIM` | 不操作 |
| 同一批调用先 claim 后 swap | `CLAIM` + 独立的 BUY/SELL/TOKEN_SWAP | 保留两个动作；claim 不跟，满足证据和策略的 swap 可进入决策 |
| ETH↔WETH 包装/解包 | `WRAP_NATIVE` / `UNWRAP_NATIVE` | 不视为买卖，不产生跟单；ETH/WETH 额度仍属于同一桶 |
| approve、Permit2 或其他授权 | `APPROVAL` / `AUTHORIZATION` | 不跟单，也不会因为聪明钱授权而替跟单钱包自动无限授权 |
| 加减流动性、NFT 头寸操作 | `LIQUIDITY` / `LIQUIDITY_OR_POSITION_CALL` | 不按普通买卖跟单 |
| Relay/Solver 源链存款 | `INTENT_DEPOSIT` | 不跟买；存入报价资产不能证明最终买了什么 |
| 目标链只看到 Solver 向聪明钱交付 token | `EXTERNAL_DELIVERY_CANDIDATE` 或 `needs_review` | 未有唯一订单、付款和目标成交归属时不跟买 |
| bundle 中别人的 UserOperation 发生 Swap | 目标 UserOp 没有对应 Swap/资金流 | 不归给目标聪明钱，不跟单 |
| 外层交易成功，但目标 UserOperation 失败 | `failed` | 不跟单 |
| 有 Swap 日志，但池、token、recipient 或钱包资金流不一致 | `needs_review` | 不跟单，等待增加解析支持或人工调查 |
| 未知聚合器、未知 selector、未知账户实现或未知 hook | `UNKNOWN/needs_review` | 不猜测、不跟单 |
| 聪明钱交易已进入孤块 | `canonical_status=orphaned` | 不创建新 proposal；尚未成交的纸面预留在恢复检查时释放，历史观察意向仍保留 |

## 4. BUY 金额如何决定

先根据聪明钱实际输入资产选择桶：USDG 使用 `usdg_rule_*`，ETH/WETH 使用
`eth_rule_*`。没有对应桶或实际输入金额无法闭环时不生成 proposal。

### 固定金额

假设配置：

```text
usdg_rule_mode = fixed
usdg_fixed_amount_raw = 1000000
```

如果 USDG 是 6 位小数，每次符合条件的 USDG 买入计划使用 1 USDG；聪明钱买 100 USDG 或
10,000 USDG 都不改变本次计划金额。实际仍受剩余额度和报价风控限制。

### 比例金额

假设配置 `eth_ratio_ppm=100000`（10%）：

```text
聪明钱经回执确认实际投入 0.8 ETH
跟单计划金额 = 0.8 × 10% = 0.08 ETH
```

比例基数是该笔交易中已经验证的钱包实际输入，不是聪明钱总资产，也不是 calldata 中未经执行
确认的最大值。

如果计划金额超过该 relationship 当前桶的可用额度，当前实现拒绝该 proposal，不能通过减少
安全检查强行成交。手动周期重启选择 `reuse` 时沿用已用/预留额度；选择 `reset` 时建立新周期，
旧成交和归因不会删除。有未确定在途预留时禁止重置。

## 5. SELL 如何保护不同来源持仓

每次纸面 BUY 都创建独立 position lot，并保存：

- 跟单钱包与 relationship ID；
- 聪明钱地址、标签和源 signal/tx；
- 原始投入资产与本金；
- 实际获得 token 数量；
- 策略版本和额度周期。

聪明钱卖出时，当前系统只从同一 ledger scope、相同 token、相同本金桶的 open lot 中按时间顺序
预留。跟卖成交后按 `卖出 token 数量 ÷ 该 lot 卖出前 token 数量` 计算应恢复的原始本金。

例如跟买 lot 使用 0.05 ETH 获得 50 token；之后跟卖 25 token：

```text
剩余 token = 25
恢复 ETH 额度 = 0.025 ETH
继续占用的原始本金 = 0.025 ETH
```

卖出实际收到 0.02、0.025 或 0.03 ETH，都只恢复 0.025 ETH 原始本金；差额进入 realized PnL，
不会改变额度恢复数量。Gas 单独记录，不混入 token 数量。

## 6. 协议、资产和路径配置如何影响动作

聪明钱信号覆盖面可以宽于实际跟单范围。系统可能确认聪明钱确实通过某个聚合器买了 token，
但只要当前 relationship 没允许其协议、资产或完整路径，就以相应原因拒绝：

- `protocol_not_allowed`；
- `asset_not_allowed`；
- `route_not_allowed`。

当前纸面实现使用信号对应的允许路径做实时固定区块报价，不会把聪明钱历史成交价当作跟单成交
价。未来主网可以进一步设计“同路径执行”或“在受支持池中重新选最优路径”，但目前没有实现
跨路径自动寻路，不能把这一计划写成已具备能力。

## 7. 报价失败或市场变化时

第一次报价通过后，系统事务化预留额度并创建 proposal；纸面成交前会做第二次报价。以下任一
情况都会拒绝或取消，不写成功 fill，并释放尚未消耗的买入额度/卖出持仓预留：

- 报价缺失或超过 `max_age_seconds`；
- 相对聪明钱已验证成交价格恶化超过 `max_adverse_deviation_bps`；
- 价格影响超过 `max_price_impact_bps`；
- 第二次报价低于第一次计算的 slippage `minimum_amount_out_raw`；
- 预计 Gas 超过 `max_gas_cost_wei`；
- 输出小于 `min_amount_out_raw`；
- 信号在重启恢复时已经缺失、孤块或移出允许范围。

影子触发不会占用额度，因此也不存在需要释放的真实 proposal reservation。

## 8. 当前明确不会做的事情

- 不因为钱包收到 token 就跟买。
- 不因为钱包转出 token 就跟卖。
- 不把 bundle 中其他人的 Swap 归给聪明钱。
- 不复制聪明钱的原始 calldata、nonce、recipient、deadline 或授权。
- 不自动 approve，更不会自动无限授权。
- 不处理未知路径后强行下单。
- 不读取主网私钥、不签名、不广播主网交易。
- 不承诺成交、收益、最终性或所有协议覆盖。

## 9. 当前配置下的一条完整示例

假设一条 enabled relationship 配置如下：

```text
主触发：swap_evidenced
影子触发：feed_intent、receipt_success
ETH/WETH 买入：聪明钱实际输入的 10%
USDG 买入：固定 5 USDG
跟卖：聪明钱已验证卖出 token 数量的 10%
ETH/WETH 周期额度：0.5 ETH
USDG 周期额度：100 USDG
允许：V3，WETH/USDG/TOKEN-A，指定 fee 的完整路径
```

运行结果示例：

1. Feed 看见聪明钱计划用 1 ETH 买 TOKEN-A：只写 `feed_intent` 影子判断，不占额度。
2. 回执成功但资金流尚未闭环：只写 `receipt_success` 影子判断，不占额度。
3. 确认聪明钱实际支出 1 ETH、收到 TOKEN-A，且路径完全匹配：重新报价，计划跟买 0.1 ETH。
4. 0.1 ETH 未超过剩余 0.5 ETH 额度：事务化预留 0.1 ETH 并创建 proposal。
5. 第二次报价仍满足 minOut、滑点、影响和 Gas：当前写一笔 paper BUY fill 和 position lot。
6. 聪明钱后来卖出其 40% TOKEN-A；若 10% sell rule 计算出的数量在该 lot 可用范围内，当前只
   纸面卖出对应归因 lot，不动其他 TOKEN-A。
7. 跟卖后按该 lot 实际卖出比例恢复原 ETH 本金，卖出收入与成本进入 realized PnL。

如果第 3 步确认聪明钱走的是未允许的池、未知 hook 或 Solver 交付候选，流程停在观察/拒绝，
不会为了“必须跟上聪明钱”而越过安全边界。

## 10. 从当前纸面流程到主网还缺什么

纸面 fill 目前不是链上成交。进入主网 M3 前，还要完成并单独验收：受支持 Router/路径的主网
交易构建、独立密钥安全、精确授权、广播瞬间最终复核、交易发送、pending/replacement、真实
回执与余额变化核对、账实不符全局停止，以及操作员确认的极小额额度。完成这些之前，业务表的
`run_mode` 仍只能是 `paper`。
