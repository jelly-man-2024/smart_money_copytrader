# 当前跟单流程与行为矩阵

文档版本：v2.0

更新时间：2026-09-13

适用代码：`main` 分支 `20f2512` 及后续提交。

本文描述当前数据库驱动的纸面/实盘跟单流程。v2.0 已替换早期“仅纸面、单条实盘关系、签名广播
属于未来 M3”的说明；当前固定入口 `.venv/bin/sm-copy run` 会加载业务 MySQL 中全部 enabled
关系，其中 `run_mode=paper` 写纸面成交，`run_mode=mainnet_live` 在全部门禁通过后可读取对应
follower 的独立 key MySQL 记录、签名并广播真实交易。

观察信号的 `copy_eligible` 仍固定为 `false`。它表示观察层不自行授权交易；实盘资格来自独立的
relationship 配置、证据等级、风险检查和签名/广播门禁，不能通过修改信号字段开启实盘。

## 1. 配置、钱包和运行边界

一行 `copy_relationships` 唯一表示：

```text
一个跟单钱包 follower_wallet
        ×
一个聪明钱钱包 smart_wallet
        ×
这一对钱包唯一采用的一套策略
```

同一个聪明钱可以被多个跟单钱包跟随；同一个跟单钱包也可以跟随多个聪明钱。每条关系拥有独立
relationship ID、配置快照、额度、proposal、归因持仓 lot 和收益记录。

一个 `sm-copy run` 进程会加载全部 enabled 关系：

- 启动时逐条校验 enabled 实盘关系的运行模式、数据库确认时间、配置快照和 follower key 元数据；
- 任一 enabled 实盘关系启动校验失败，整个进程失败关闭，不静默遗漏配置；
- 同一聪明钱匹配多条关系时，各关系分别计算金额、额度、报价和归因；
- 单条关系运行时失败会记录 relationship/follower，其他关系仍继续；
- 同一个 follower 的授权、nonce、签名和广播使用同一把进程内钱包锁串行；
- 不同 follower 使用不同钱包锁，可以并行执行。

进程内锁不等于跨进程分布式锁。同一部署不得同时启动两个操作相同 follower 的 `sm-copy run`
进程，否则授权交易和外部 nonce 变化仍可能竞争。

主要数据库配置字段：

| 配置 | 当前含义 |
|---|---|
| `enabled` | 是否由固定任务加载；false 只保留配置和历史 |
| `run_mode` | `paper` 写纸面成交；`mainnet_live` 可进入真实执行分支 |
| `live_risk_accepted_at` | 实盘逐关系确认时间，必须不早于当前行 `updated_at` |
| `trigger_mode` | 主触发点；实盘只接受 `swap_evidenced` 或统一的 `evidenced` |
| `shadow_trigger_modes` | 只记录比较结果，不占额度、不创建真实执行 |
| `usdg_rule_*` | 聪明钱以 USDG 买入时，使用固定金额或输入金额比例 |
| `eth_rule_*` | 聪明钱以 ETH/WETH 买入时，使用固定金额或输入金额比例 |
| `sell_rule_*` | 按固定数量或聪明钱已验证卖出比例计算本地跟卖量 |
| `*_budget_limit_raw` | 按 relationship 隔离的净累计投入上限 |
| `allowed_protocols` | 允许作为信号来源/本地执行依据的协议集合 |
| `allowed_assets` | 可信本金、结算币和中间路由币；不是目标 meme token 白名单 |
| `allowed_routes` | 可选本地路径以及 V3 fee、V4 PoolKey/hook 参数 |
| `quote_policy` | 报价时效、偏离、价格影响、滑点、Gas 和最小输出限制 |
| `strategy_version` | 写入交易归因的策略版本，不倒推改写历史成交 |

原始金额始终使用整数，并作为十进制字符串保存。`ratio_ppm=100000` 表示 10%，
`ratio_ppm=1000000` 表示 100%。

## 2. 启动流程

```text
                         .venv/bin/sm-copy run [N0]
                                      ↓
                  从 MySQL 加载全部 enabled relationship [N1]
                                      ↓
              合并聪明钱监听集合 + 校验 Robinhood chain ID [N2]
                                      ↓
             逐实盘关系检查 STOP、确认时间、快照和 key 元数据 [N3]
                                      ↓
            自动复用额度周期；为新 relationship 初始化独立额度 [N4]
                                      ↓
                为每条关系创建 Engine/执行流水线和钱包锁 [N5]
                                      ↓
                       启动 Feed、RPC 补洞和候选 worker
```

`sm-copy run` 固定使用 MySQL 配置、MySQL 运行账本、Relay 自动关联和自动额度周期。enabled
relationship 的 smart wallet 会自动加入 CSV 基础监听集合，不再为每个新关系维护独立 watchlist。

重启默认复用活动额度周期，不重置累计投入。新增 relationship 会创建新的独立额度 scope；修改额度
会保留 invested/reserved，若新上限小于当前占用值则拒绝启动。

实盘关系必须同时满足：

1. `enabled=TRUE`；
2. `run_mode='mainnet_live'`；
3. `live_risk_accepted_at >= updated_at`；
4. follower 在独立 `wallet_keys` 中存在且 `enabled=TRUE`；
5. 全局 `var/EXECUTION_STOP` 不存在；
6. 业务 MySQL、key MySQL、RPC、余额、Gas、allowance、nonce、报价和路由检查通过。

修改 `copy_relationships` 后必须在同一条 SQL 中刷新
`live_risk_accepted_at=CURRENT_TIMESTAMP(6)`，然后重启。运行中的旧进程在签名前重新加载该行；配置
快照已经变化时会拒绝旧执行。修改 `wallet_keys` 后重启即可。

## 3. 信号监听、补洞和证据确认

```text
Feed 实时交易意向 [N6] ───────────┐
                                  ├─ 恢复 sender、智能账户与 UserOp 身份 [N8]
RPC 独立区块游标补洞 [N7] ────────┘
                                              ↓
                              解码行为和内部调用路径 [N9]
                                              ↓
                   RPC 回执 + UserOp + 池归属 + ERC-20/ETH 资金流 [N10]
                                              ↓
             intent / execution_observed / swap_evidenced / needs_review / failed
                                              ↓
                         持久化 signal、候选状态和规范链证据 [N11]
```

Feed 和 RPC 不是二选一：Feed 提供更早的意向，RPC 独立游标负责重启/断线补洞、回执、规范区块和
资金流证据。Feed 意向即使可解码，也不代表交易最终执行成功。

RPC 补洞按 safe head 前进持久化游标；父块不匹配时寻找共同祖先、孤立旧规范链证据并重新处理
候选。超过自动深度的重组需要显式 `reconcile-reorg`，不能静默把旧块继续当规范链。

关键归属原则不变：

- 收到 Transfer 本身不是买入；
- 转出 Transfer 本身不是卖出；
- bundle 中别人的 Swap 不归给目标聪明钱；
- claim 与 swap 是两个动作，不能为了简化而丢弃 swap；
- 未知 selector、账户实现、池、hook、recipient 或资金流保持 `UNKNOWN/needs_review`。

## 4. 从信号到每条跟单关系

```text
严格信号证据
     ↓
查找这个 smart_wallet 的全部 enabled relationship [N12]
     ↓
每条关系独立检查 trigger / protocol / asset / route [N13]
     ↓
按固定金额或比例计算跟单输入 [N14]
     ↓
固定区块实时报价 + 偏离/冲击/滑点/Gas 检查 [N15]
     ↓
事务化创建 proposal，并预留 BUY 额度或 SELL 归因持仓 [N16]
     ↓
                 ┌── run_mode=paper ── 二次报价后写 paper fill [N17]
                 │
                 └── run_mode=mainnet_live ── 进入真实执行 [N18-N24]
```

多条匹配关系会分别运行上述决策。不同 follower 可以并行；相同 follower 可以先分别形成独立
proposal，但进入真实授权和交易发送时会按 follower 串行。

主触发语义：

| 触发点 | 含义 | 实盘状态 |
|---|---|---|
| `feed_intent` | 新鲜 Feed 中已解出的意向，可能最终失败 | 仅适合 paper/shadow |
| `receipt_success` | 目标交易/UserOp 成功，但未必已有完整兑换闭环 | 仅适合 paper/shadow |
| `swap_evidenced` | 池、钱包扣款、输出入账和 Swap 形成严格闭环 | 支持 |
| `evidenced` | 接受 direct swap、严格 Relay BUY 或 Relay SELL 证据 | 支持 |

## 5. 真实执行分支

```text
获得 follower 钱包锁 [N18]
       ↓
把来源交易映射为跟单钱包自己的本地 V2/V3/V4 路径 [N19]
       ↓
检查 Router allowance；不足时发送有界 approve 并等待规范回执 [N20]
       ↓
再次报价，构建 immutable transaction，检查余额/Gas/allowance/nonce [N21]
       ↓
持久化 nonce reservation 和 execution plan
       ↓
重新查询 relationship + 读取对应 key + 签名 + 恢复 sender [N22]
       ↓
广播前再次核对关系、额度、报价、交易字段和 pending nonce [N23]
       ↓
独立 MainnetBroadcaster 发送 eth_sendRawTransaction 并核对返回 hash [N24]
       ↓
释放 follower 钱包锁，后台跟踪 pending/confirmed/reverted/orphaned [N25]
       ↓
confirmed 后按规范 receipt 的 follower ERC-20 净差额结算 lot/PnL [N26]
```

钱包锁覆盖授权到跟单交易广播的完整 nonce 敏感区间。授权确认后才构建并签署跟单交易；不同钱包
互不等待。广播后下一笔同钱包交易可使用 RPC pending nonce 和持久 nonce reservation 继续排队，
回执跟踪在后台进行。

来源是 0x、Kyber 或 Relay/Solver，不代表跟单钱包复制聚合器 calldata。当前只把严格来源证据映射
为自己的已验证 V2/V3/V4 exact-input 路径；无法找到支持路径就拒绝。Relay 被动交付还必须先唯一
关联订单/付款/recipient，再验证本地池，不能把普通收币升级成买入。

当前授权策略：

- USDG allowance 不足本笔 BUY 时，目标授权额为该 relationship USDG 预算上限的 200 倍；
- 200 倍 allowance 不会放大软件预算，BUY proposal 仍受 relationship 净累计投入上限限制；
- SELL 的 meme token allowance 不足时，只授权该 relationship 当前全部归因 open position；
- 链上 allowance 属于 `钱包 × Token × Router`，多个 relationship 可能共享，但账本额度和持仓
  仍严格按 relationship 隔离；
- 不使用无限授权；现有 allowance 足够本笔输入时不重复发送 approve。

## 6. BUY、额度和 SELL 归因

BUY 先按输入资产选择额度桶：USDG 使用 USDG 桶，ETH/WETH 共用 ETH/WETH 桶。目标 meme token
不需要预先写入 `allowed_assets`；本金、结算币和所有中间币必须在其中。

固定金额示例：`usdg_fixed_amount_raw=2000000` 且 USDG 为 6 位小数时，每次计划买入 2 USDG，
不随聪明钱成交规模变化。

比例示例：聪明钱经回执确认实际投入 0.8 ETH，`ratio_ppm=100000`，计划输入为 0.08 ETH。比例基数
是已验证的实际输入，不是 calldata 最大值或钱包总资产。

额度表示 relationship 的净累计投入：

```text
可用额度 = 上限 - invested - reserved
```

BUY 预留后减少可用额度；BUY confirmed 后 reserved 转为 invested。SELL confirmed 后只按实际卖出
占该归因 lot 的比例恢复原始本金，不按卖出收入或利润扩大额度。

例如跟买 8 USDG，之后卖出该 lot 的 75%，恢复 6 USDG，本关系 invested 为 2 USDG，可继续投入
8 USDG。Router allowance 是另一层链上授权，卖出不会恢复 USDG allowance；下一次 BUY 会检查
现有 allowance，不足时先补到有界目标。

每次 BUY lot 保存 follower、relationship、smart wallet、源 signal/tx、策略快照、本金资产、
投入金额和获得 token。SELL 只能使用同一 relationship、同一 token 的 open lot，不会卖出：

- 另一个聪明钱关系产生的同名 token；
- 用户自行持有的 token；
- 被动入账但没有严格 BUY 归因的 token。

## 7. 聪明钱行为矩阵

| 聪明钱情况 | 信号/证据 | 当前动作 |
|---|---|---|
| 支持的 V2/V3/V4 路径买入，扣款和入账闭环 | `BUY/swap_evidenced` | 每条关系独立报价；paper 写纸面成交，live 走本地验证路径真实买入 |
| 0x/Kyber 聚合器交易且回执闭环 | `BUY/SELL` 严格证据 | 不复制源 calldata；存在本地 V2/V3/V4 路径才执行 |
| Relay/Solver BUY，源付款与目标交付唯一关联 | `relay_buy_evidenced` | 按本地已验证路径跟买；只有入账而无订单关联则拒绝 |
| Relay/Solver SELL，token debit 与结算存款唯一闭合 | `relay_sell_evidenced` | 只卖该关系已有归因 lot，并使用本地退出路径 |
| 卖出此前归因 token | `SELL` 严格证据 | 按固定/比例规则跟卖，不超过本关系可用 lot |
| 卖出超过该关系持仓 | 持仓不足 | `attributed_position_insufficient`，不动其他资产 |
| 单纯收币、空投或批量分发 | Transfer/Distribution | 不跟买 |
| 单纯转出 token | Transfer | 不跟卖 |
| claim 后又 swap | `CLAIM` + 独立 Swap | claim 不操作；swap 满足证据与策略时可跟 |
| ETH/WETH wrap/unwrap | `WRAP_NATIVE/UNWRAP_NATIVE` | 不作为买卖，额度桶不重复计算 |
| approve/Permit2 | `APPROVAL/AUTHORIZATION` | 不复制聪明钱授权 |
| 加减流动性/NFT 头寸 | `LIQUIDITY` | 不按普通买卖跟单 |
| bundle 中他人的 Swap | 目标 UserOp 无对应资金流 | 不归因、不跟单 |
| 外层成功但目标 UserOp 失败 | `failed` | 不跟单 |
| 池、token、recipient、hook 或资金流不一致 | `needs_review` | 不跟单 |
| 未知聚合器/selector/账户实现 | `UNKNOWN` | 不猜测、不跟单 |
| 来源交易进入孤块 | `canonical_status=orphaned` | 不创建新执行；撤销规范链证据并重新核对 |

## 8. 当前代码索引

行号对应 v2.0 更新时的代码；后续修改后优先搜索表中的函数或类名。

| 节点 | 代码入口 | 职责 |
|---|---|---|
| N0 固定任务 | [`cli.py:1169 main()`](../src/smart_money/cli.py#L1169)、[`cli.py:317 monitor()`](../src/smart_money/cli.py#L317) | 解析 `run` 并组装全部组件 |
| N1 MySQL 配置 | [`mysql_config.py:126 load_mysql_paper_config()`](../src/smart_money/mysql_config.py#L126) | 加载全部 enabled 行并逐行生成 snapshot |
| N2 自动监听集合 | [`cli.py:79 monitoring_watchlist()`](../src/smart_money/cli.py#L79) | 合并 CSV 来源与 enabled smart wallets |
| N3 实盘启动门禁 | [`cli.py:125 validate_live_relationships()`](../src/smart_money/cli.py#L125)、[`execution_controls.py:34 _risk_acceptance()`](../src/smart_money/execution_controls.py#L34)、[`key_source.py:89 live_key_record_status()`](../src/smart_money/key_source.py#L89) | 逐关系确认、快照和 key 元数据检查 |
| N4 额度周期 | [`cli.py:92 prepare_runtime_budget_cycle()`](../src/smart_money/cli.py#L92) | 自动复用周期并初始化/更新关系额度 |
| N5 钱包锁与流水线 | [`cli.py:141 live_wallet_execution_locks()`](../src/smart_money/cli.py#L141)、[`cli.py:346`](../src/smart_money/cli.py#L346) | 同 follower 串行，不同 follower 隔离 |
| N6 Feed 接收 | [`cli.py:879 receive()`](../src/smart_money/cli.py#L879)、[`feed.py:127 envelopes()`](../src/smart_money/feed.py#L127) | 接收 WSS、检查 frame/sequence/freshness |
| N7 RPC 补洞/重组 | [`cli.py:844 backfill()`](../src/smart_money/cli.py#L844)、[`backfill.py:57 scan_once()`](../src/smart_money/backfill.py#L57)、[`backfill.py:130 reconcile_reorg()`](../src/smart_money/backfill.py#L130) | 独立游标、Transfer 日志、规范链回查 |
| N8/N9 身份与解码 | [`feed.py:24 signed_transactions()`](../src/smart_money/feed.py#L24)、[`decode.py:56 Decoder`](../src/smart_money/decode.py#L56) | 恢复 sender，隔离账户/UserOp 并解析行为 |
| N10 回执证据 | [`receipts.py:76 enrich()`](../src/smart_money/receipts.py#L76)、[`pools.py:94 verify_signal_pools()`](../src/smart_money/pools.py#L94)、[`native_flows.py:8 verify_native_flows()`](../src/smart_money/native_flows.py#L8) | 回执、池、ERC-20 与 ETH 资金流核验 |
| N11 候选/信号持久化 | [`store.py:1492 put_candidate()`](../src/smart_money/store.py#L1492)、[`store.py:1673 put()`](../src/smart_money/store.py#L1673)、[`cli.py:661 worker()`](../src/smart_money/cli.py#L661) | 重试、幂等、证据升级和规范状态 |
| N12 多关系分发 | [`paper_config.py:71 policies_for()`](../src/smart_money/paper_config.py#L71)、[`cli.py:629 safe_paper_observe()`](../src/smart_money/cli.py#L629) | 将一个信号独立分发给全部匹配关系并隔离错误 |
| N13-N16 策略决策 | [`paper.py:211 trigger_allowed()`](../src/smart_money/paper.py#L211)、[`paper.py:238 scope_reason()`](../src/smart_money/paper.py#L238)、[`paper.py:379 propose_buy()`](../src/smart_money/paper.py#L379)、[`paper.py:445 propose_sell()`](../src/smart_money/paper.py#L445) | 触发、路径、金额、报价和额度/持仓预留 |
| N17 纸面成交 | [`paper.py:552 PaperExecutor`](../src/smart_money/paper.py#L552)、[`store.py:884 fill_paper_buy()`](../src/smart_money/store.py#L884)、[`store.py:1303 fill_paper_sell()`](../src/smart_money/store.py#L1303) | 二次报价及纸面 lot/PnL |
| N18 同钱包串行 | [`cli.py:559 execute_live()`](../src/smart_money/cli.py#L559) | 获取 follower 锁后执行完整 nonce 敏感区间 |
| N19 本地执行路径 | [`paper.py:89 execution_quote_signal()`](../src/smart_money/paper.py#L89)、[`quotes.py:162 LiveQuoter`](../src/smart_money/quotes.py#L162) | 将严格来源证据映射到本地报价路径 |
| N20 有界授权 | [`approval.py:33 approve_relationship_token()`](../src/smart_money/approval.py#L33)、[`approval.py:146 approve_relationship_usdg()`](../src/smart_money/approval.py#L146) | 检查 allowance、发送 approve 并确认规范回执 |
| N21 交易准备/nonce | [`execution_pipeline.py:94 ExecutionPreparer`](../src/smart_money/execution_pipeline.py#L94)、[`store.py:224 reserve_execution_nonce()`](../src/smart_money/store.py#L224) | 二次报价、preflight、计划和 nonce reservation |
| N22 主网签名 | [`execution_pipeline.py:289 LiveExecutionSigner`](../src/smart_money/execution_pipeline.py#L289)、[`key_source.py:185 LiveDatabaseSigner`](../src/smart_money/key_source.py#L185) | 重查关系、精确读取 key、签名并恢复 sender |
| N23 广播前复核 | [`execution_pipeline.py:385 LivePreBroadcastReviewer`](../src/smart_money/execution_pipeline.py#L385) | 重查关系、预算、报价、字段和 pending nonce |
| N24 独立广播 | [`broadcast.py:28 MainnetBroadcaster`](../src/smart_money/broadcast.py#L28) | 仅在最终门禁后发送并核对本地/远端 hash |
| N25 回执跟踪 | [`cli.py:462 track_live()`](../src/smart_money/cli.py#L462)、[`execution_receipts.py:42 ReadOnlyExecutionTracker`](../src/smart_money/execution_receipts.py#L42) | 跟踪 pending、confirmed、reverted、replacement、orphaned |
| N26 实盘结算 | [`live_settlement.py:38 settle_confirmed_execution()`](../src/smart_money/live_settlement.py#L38) | 按规范 receipt 的 follower 净差额更新 lot/PnL |

核心多关系回归：

- `test_mysql_relationship_rows_reuse_strict_paper_validation`
- `test_same_smart_wallet_relationships_have_isolated_budget_and_proposals`
- `test_database_run_validates_every_live_relationship_and_locks_per_follower`
- `test_nonce_reservations_are_persistent_idempotent_and_gap_free`
- `test_usdg_approval_is_200x_budget_bounded_and_relationship_gated`
- `test_dynamic_sell_token_approval_is_position_bounded_and_confirmed`

测试文件：[`tests/test_observer.py`](../tests/test_observer.py)。

## 9. 日常配置和运行

新增或修改关系后，在同一条 SQL 中设置确认时间：

```sql
UPDATE copy_relationships
SET enabled = TRUE,
    run_mode = 'mainnet_live',
    live_risk_accepted_at = CURRENT_TIMESTAMP(6)
WHERE id = ?
  AND follower_wallet = '0x跟单钱包'
  AND smart_wallet = '0x聪明钱钱包';
```

启动前逐关系核对：

```bash
.venv/bin/sm-copy relationship-status --relationship-id ID
```

输出应显示 `live_risk_acceptance_current=true`。后台启动、重启和日志查看见
[`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)。固定前台命令是：

```bash
.venv/bin/sm-copy run
```

启动日志应为每条关系输出 `live_key_ready`，并输出一次 `live_relationships_ready`，其中包含
relationship 数量和不同 follower 数量。

## 10. 当前限制和剩余风险

- 只有进程内 follower 锁，没有跨主机/跨进程租约；禁止重复启动操作相同钱包的实例。
- 主动本地执行仅覆盖已实现且验证的 V2/V3/V4 路径；`allowed_protocols` 不等于自动实现所有寻路。
- 0x、Kyber、Relay/Solver 当前主要是来源归因，不能直接复制其 calldata；OKX 客户端尚未接入
  monitor 动态执行。
- V4 token-input/Permit2、复杂 native 结算、手续费币和非标准 ERC-20 仍不是完整支持路径。
- pending 超时、外部钱包交易、replacement 和长时间 RPC 故障仍需要执行审计与人工处置。
- 自动重组恢复有深度上限，深重组需要显式规范链对账。
- 实盘回执结算当前以受支持 ERC-20 exact-input 净差额为主，复杂费用和资产流仍可能拒绝为
  `needs_review`。

这是一套有界、失败关闭的跟单执行流程，不承诺成交速度、协议全覆盖、盈利或 L1 最终性。
