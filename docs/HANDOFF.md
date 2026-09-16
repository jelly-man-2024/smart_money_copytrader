# 开发交接：在新工程继续

历史交接记录起始于 2026-09-12，后续进展按日期追加。

## 2026-09-16 签名前 RPC 故障隔离与交易健康日志（已实现，未部署）

用户批准本轮 5 个运行代码文件及测试、交接文档的修改；不修改在线配置、账本或额度，
不解除急停、不停启进程、不访问真实密钥、不签名广播。提交范围仅为下述源码、离线测试和本节文档；
部署状态以运行进程为准。

现场依据为 `var/log/sm-copy-early-20260916.log` 的 12:41:06 新加坡时间事件及此前只读账本核对：
proposal `6bc78c6680e64fe896f9ccae7aa2c73f91aee25771ba9843e9e1f1506616ec1f`
在 prepare 阶段报 `RPC transport failure: HttpPoolError`，当时没有 execution plan，提案已取消、
`100000` raw USDG 预留已释放。但 EarlyRuntime 将 RpcError 按 RuntimeError 一律急停，
后续 Feed 正常仍无法交易。旧包装丢失底层类型和方法，**不能倒推那次一定是超时、429 或旧连接断开**。
后续只读请求恢复正常也不构成原故障精确原因的证明。

实现边界：

- HTTP 池异常保留固定字段：exception_type、reason、phase、reused、status；RPC 层补 method，
  并发等待超时单列 concurrency_wait。日志不保存 URL、请求参数、headers 或任意异常原文。
  失败连接仍淘汰，不增加隐式重试；广播绝不自动重发。池失败细节同时进入原 timings；
  订单的 `live_execution_abandoned.rpc_diagnostic` / `early_handoff_rejected.rpc_diagnostic`
  持久记录该次故障，不依赖 health 最近 8 条时序恰好保留失败样本。
- 只读决策尚未建立提案/调用执行器时的 RpcError，只拒绝本次候选。
  已预留订单仅在 **prepare 阶段、没有计划或从未签名的 prepared 计划取消成功、提案取消成功**
  后，才由执行器发出绑定 proposal ID 的 `CancelledBeforeSigningRpcError` 标记。
  EarlyRuntime 复核提案确实 cancelled 后不新增急停；无标记的执行器 RpcError 仍急停。
  不扩大到签名/复核阶段，不重试旧单，不自动清除已有急停或内存锁定。
- 清理抛异常（包括账本 ValueError）、取消返回失败、未决签名及广播结果未知均不获豁免。
  清理失败报告 `live_execution_cleanup_failed` 或 abandoned/operator_review_required，
  提前试运行保持急停。原同进程 review 阶段的未广播签名清理保留，但不据此豁免 RpcError。
- health 保留旧 `healthy` 字段的 Feed 语义，新增明确的 `feed_healthy` 和 `execution`：
  configured、fault_latched、stop_file_active、state。state 为 disabled / stopped / not_stopped /
  unknown；`not_stopped` **仅表示本地急停未阻断**，不表示 trial、预算、行情等门禁通过。

验证仅使用离线假连接、假执行流水线、内存账本及临时停止文件：覆盖原故障的安全取消、
只读决策失败后下一次候选、错误标记绑定/账本状态、prepared 清理、签名/复核/广播异常、
清理异常/返回失败、传输类型/复用/阶段/HTTP 429 脱敏，以及 Feed 正常但交易 stopped 的日志接线。
新测试先在旧行为下复现失败，修复后全量 518 项测试通过（12 项可选 EVM 跳过；含原有
23 项未跟踪 Relay 采样测试，不纳入此次代码修改）。没有新增实盘成交或延时改善证据。
操作员后续上线前仍须重新核对急停原因、在途状态及 trial 窗口；不能仅删除文件让旧进程恢复。

提交前只读审计结果：执行账本一致性检查通过；活动 execution attempt 中 prepared、signed、
observed_pending、orphaned 均为 0，历史状态为 164 confirmed、2 reverted。提前试运行仍 eligible，
已使用 5/100，early trial 的 reserved proposal 为 0。另有 24 笔已知历史严格路径 reserved proposal
（20 笔预算预留、4 笔持仓预留），本轮保持不动。旧进程仍运行旧代码，本地 `EXECUTION_STOP`
仍有效；因此本次提交推送本身不会恢复交易，部署和解除急停须另行执行并再次审计。

## 2026-09-16 启动前阻塞已处理，等待操作员启动

用户授权检查、处理启动前阻塞并提交推送 main。关系 1/2/3/4/5/9 已使用
`["zeroex","kyber"]`，Feed 7 秒、报价 6 秒及原金额/预算保持不变。
旧进程 PID 74769 已确认属于本项目后停止；`var/EXECUTION_STOP` 仍保留，尚未启动新实盘进程。

已核销下节的单笔未决签名：对照旧版本发送代码与日志，确定
`early intent freshness: feed_intent_expired` 发生在广播 RPC 请求之前；重复链上查询
交易/回执为空、latest/pending nonce=211。结论不只依赖“查不到回执”，还依赖发送前异常位置及
原进程已停止。原 attempt、签名哈希、review、日志和 RPC 证据完整移入对应
`execution_plans.final_review_payload.operator_reconciliation`，移除其活动 attempt，
取消 plan/proposal、释放 nonce 预留及 `100000` raw USDG 的预算预留。
这不是链上退款；没有发出取消/替换交易。原防重 claim 与已消耗试运行名额保持不变。
核对后未决 signed/pending attempt 均为 0；历史 164 confirmed、2 reverted，账本一致性检查通过。

因试运行表对 follower 唯一，本次在原 `feed-early-20260915` 上执行一次明确授权的人工续期，
未新增并行试运行，也未改变程序“禁止自动续期”的逻辑。旧窗口及此次授权记录保存在同一 plan 的
`operator_trial_renewal`。有效至 **2026-09-17 10:24:58（新加坡时间）**，
累计名额仍 100、已消耗 5、剩余 95；不重置预算、持仓或名额。
公开链上检查 USDG 余额 `10929877` raw、0x allowance `10000000` raw，足够当前单笔
`100000` raw USDG；未读取私钥或新增授权。

另发现 **24 笔历史严格路径 reserved proposal**，均无 execution plan、签名哈希及 early_trial_id，
不属于本次单笔核销范围，保持原状；可能占用预算或持仓，需另行调查，不视为未决链上交易。
当前启动恢复不会自动把这些旧预留发送出去。

提交前工作区运行 507 项测试：495 通过、12 项可选 EVM 测试跳过；其中 23 项属于原有
未跟踪的 Relay 采样工具，不纳入本次提交。排除该模块后再次运行本次版本的 484 项：
472 通过、12 项跳过。补充本地路由与 0x 混合配置下
回退时的报价上下文回归。尚无新版本真实端到端成交或真实 SELL 对照结果；完整的
0x 准备失败→Kyber 成功组合流程仍缺专门的 monitor 集成测试，不能将单模块通过视作实盘证明。

## 2026-09-16 聚合器配置已切换，重启仍有阻塞（历史；已由上节处理）

按用户“修改配置，发我重启命令”的要求，仅将 follower
`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6` 的关系 1/2/3/4/5/9 从 `["kyber"]`
改为 `["zeroex","kyber"]`，同事务刷新配置风险确认/更新时间；逐列核对其余字段未变，
重新加载确认报价仍 6 秒。未重启 PID 74769、未清除急停、未重置预算或试运行，也未访问私钥。

启动前检查发现 `var/EXECUTION_STOP` 有效，`feed-early-20260915` 已于 2026-09-16 00:25:03
新加坡时间到期（consumed_slots=5）；不能通过重启续期。
另有未决 signed proposal `d13fef792329f4b767b54bda6819b899ccafbce2da2771a1c51cb572db2334d9`，
hash `0xca1129d275c174241fb470764734a37f63274cd8795340e884bbfc9f12561aaa`。
日志发送入口后报 `early intent freshness: feed_intent_expired`，该异常在广播 RPC 前的检查产生；
本次只读 RPC 查询交易/回执均为空，latest/pending nonce 均 211。空回执本身不证明从未广播。
账本未作取消/释放，试运行未续期；需操作员授权核销及后续试运行安排后再恢复交易，不直接清停重启。

## 2026-09-16 执行延时三项优化已实现（未部署）

新增 0x 单接口构建、`["zeroex","kyber"]` 有界回退、并行预检及交易绑定的 2 秒内存凭据、
RPC/聚合器/独立广播 HTTPS 连接池。保留 Feed 7 秒和报价 6 秒；未修改在线关系、试运行或授权，未重启。
详情、只读对照区块与测量结果、配置及回退说明见
[执行延时优化说明](EXECUTION_LATENCY_2026-09-16.md)。
小样本不支持“0x 必然更快”；真实跟单端到端收益待后续部署验证。

## 2026-09-16 执行延时优化原始待办（历史记录，由上节更新）

用户要求记住：优先评估 0x 的单接口报价＋构建，随后优化执行阶段的网络等待。
本节不构成切换 provider、修改风控、重启或交易许可；本次只修改交接记录。

### 用户确认的本轮方案范围（三项，尚未实施）

1. 验证并接入合适的报价＋交易构建单接口，先比较 0x 与可用的 Kyber 一步接口。
2. 正常路径收敛为一轮最终预检：报价构建完成后，有界并行读取余额、授权、nonce、Gas 等，
   完成实际待发送交易的模拟，再签名并立即发送。签名和发送复用绑定该钱包及完整交易参数的
   检查结果；明显延迟、参数改变或相关状态变化必须使结果失效，重新检查或拒绝。
   失效边界及异常流程须在实现前明确，不表示无限缓存，也不保证链状态在检查后不变。
3. RPC 与聚合器采用有界 HTTPS 持久连接池，复用连接及 TLS 上下文，减少重复建连开销。
   这是连接复用，不是增加 Feed/WebSocket 订阅；保留并发/超时/响应大小限制、TLS 验证和
   失败连接淘汰，不自动重发广播。后续接入的聚合器也纳入同一传输约束。

急停、当前配置、预算/持仓、nonce 串行、防重、时效、交易参数一致性和恢复所需持久化仍保留。
请求优先级、钱包锁范围调整列为候选，尚未加入这三项主改造。细分计时与回归测试用于验证收益，
不预先承诺可节省秒数；本次“加入优化点”只更新方案，没有执行生产切换。

### 聚合接口候选

- 0x Swap v2 `GET /swap/allowance-holder/quote` 返回正式报价及
  `transaction.to/data/value/gas` 等字段，不必先调用 `/price`，也不需单独 build。
  仍须补齐交易封装、验证 Router/recipient/资产/金额/minOut/spender、余额/授权、模拟及 nonce。
  参考报价风控仍可能需要另一请求，不能宣称整个系统只需一次 API 请求。
- 项目之前已进行 0x 查询测试和独立 USDG 授权准备；当前源码
  `paper.py:AGGREGATOR_PROVIDERS` 仍只有 `kyber`，不是已上线的 0x 自动执行 provider。
  本节没有重新核对当前 allowance 或密钥配置，也不将历史授权当作永久足额。
- Kyber 新版 `GET /{chain}/api/v1/swap` 也合并报价与构建，但为白名单 Beta，要求真实执行
  意图；不能直接用于大量不成交的预取/对照请求。旧版 `/{chain}/route/encode` 标为 Legacy、
  不支持 RFQ，并非文档宣告停用；Robinhood 的实际可用性、参数兼容性和延时尚未验证。
- 下一步应做获准范围内的同币对/金额/钱包/滑点对照，比较取得可验证 calldata 的完整耗时、
  价格/minOut、费用和只读模拟通过率。不能用少一次请求推定已节省整个准备阶段。

官方依据（2026-09-16 查阅）：
[0x Swap v2](https://docs.0x.org/docs/upgrading/upgrading-to-swap-v2)、
[Kyber EVM API](https://docs.kyberswap.com/developer-guide/aggregator-api/aggregator-api-specification/evm-swaps)。

### 执行阶段优先级

1. 补各 RPC 的排队/网络耗时、build、模拟、数据库/钱包锁等待、签名本体计时；区分实际请求
   与缓存命中。现有 prepared→signed 包含复核，不是纯签名耗时。
2. 优先有界并行同一轮 `ReadOnlyExecutionPreflight.check` 的五类独立读取：pending nonce、
   native balance、gasPrice、Token balance、allowance。当前均逐条 await；正常 ERC-20 路径
   在 prepare/sign/review 各调用一轮，合计 15 次读取，另有三次交易 eth_call 模拟（不计重试
   和其他模块请求）。目标按上方最新确认方案收敛到一轮并行最终预检；仍受 RPC 并发上限，
   保持失败关闭及完成后的时效复核。
   pending 查询即使并行或 batch，也不等于固定于同一个链上状态快照。
3. RPC、Kyber 客户端目前使用 urllib 请求，没有显式受控的持久连接池；评估有界连接复用，
   对照握手/服务端等待/总耗时。保持方法与域名限制、TLS、响应大小、超时和敏感信息脱敏；
   不增加隐藏重试，广播尤其不得自动重发。
4. 聚合报价已有同操作复用；但 quote_with_reference 和执行预检仍分别读取 Gas 等动态信息。
   核对同一检查阶段内可共享的结果，按原采集时间/费用上限/钱包与交易绑定使用，不跨阶段
   无条件沿用旧余额或 nonce，不刷新旧报价时间。固定交易字段的重复解析也可评估复用。
5. 最新方案包含正常路径模拟随最终预检收敛；尚未实施。链状态会变化，不能因 calldata
   相同就认定多次模拟等价；需定义失效条件、异常退回完整检查，并保留临发送的必要门禁。
6. 同 follower 的执行锁当前覆盖完整 execute_live_serialized，市场数据预取已在锁外。
   如锁等待显著，再评估缩小锁范围；nonce、预算/持仓、防重、签名及广播必须继续正确串行。
   不能简单给同一钱包并行发送或提前释放未决订单占用。

### 本次讨论使用的测量口径

来源为业务库 early_feed_jobs/paper_proposals 与 `var/log/sm-copy-early-20260915.log`；
运行周期起点 1789461014.1386712，查询截止约 1789520536.7100842。未新发交易。
最近 20 个已识别 BUY：Feed→订单响应中位 0.425 秒/P95 0.518 秒；
Feed→识别检查完成（等待并行报价）中位 0.829 秒/P95 1.185 秒；
匹配的严格证据中位 1.133 秒/P95 8.634 秒。19/20 订单响应不晚于报价预取。
当前周期 4 笔成功 Feed 入口 BUY：Feed→广播 ACK 4.31–6.48 秒，中位 5.42 秒；
Feed→本地观察成功结算 4.89–7.02 秒，中位 5.89 秒。它们均在严格证据到达后才广播，
不能把“使用 Feed 入口”写成“早于严格证据广播”。结算观察时间不是精确链上入块时间。
上述为有限历史样本，并非新增优化的收益承诺；各阶段分位数不能直接相加。

## 2026-09-15 Relay 连接复用与固定金额报价并行（已实现，待操作员重启）

只修改源码和离线测试，未修改生产配置、预算、trial、Feed 订阅或运行进程。
Relay 客户端改为最多 4 个独占 HTTPS 连接，复用系统 TLS 验证上下文，响应限制仍为 4 MiB。
禁止重定向；传输失败丢弃连接，本次不隐藏重试，下一次查询建立新连接。
因此服务端关闭空闲连接时仍可能出现一次查询失败，不能保证所有查询低于一秒。
health 的 relay_http_timings 展示最近最多 8 次成功 HTTP 查询的连接复用、池等待、连接、
响应头等待、正文解析和总耗时；不代表订单关联一定成功，也不是累计交易数。

启用原 early trial 时，对语义通过的 BUY 候选并行查询 Relay 与预取 Kyber 两档报价。
仅固定 USDG 买入适用：比例买入金额依赖订单付款，SELL 依赖持仓，因此不猜测预取金额。
预取只查市场路线，不构建、预留、签名、广播，也不制造订单归属证据；正式决策重新验证
原订单、Feed 时效、当前配置/预算/持仓、滑点与价格影响。预取失败仍保留订单证据，
后续正常报价；订单失败即使预取成功也不获得下单许可。

同一交易/聪明钱/follower/配置快照/链/币对/原始金额/排除来源的 Kyber 请求可以共享在途
请求及成功结果，最多缓存 64 条，保留原 API observed_at 并按消费者的报价 TTL 验证。
不同证据阶段只共享市场数据，分别计算源价格风控；不共享决策、预算、nonce 或构建交易。
不把旧响应时间刷新为当前时间；消费时的块头仅为观察上下文，不宣称聚合器报价固定于该块。
失败不缓存；取消释放在途标记；换来源不会命中原路线。
early_quote_prefetch 记录预取耗时/成功/请求次数；early_quote_requests 与
execution_quote_requests 新增 shared_route_reuses，route_requests 只计实际发起请求。
prepare/sign/review 的模拟及余额、授权、nonce、费用与归属检查未删减。

依据：本会话对同一已完成订单（源 tx e70c34…178fc）的 13 次公开串行 GET 对照，
当前 urllib 6 次中位数约 0.88 秒，热连接 5 次中位数约 0.51 秒，另有两次首次建连；
样本存在网络/服务端缓存及顺序影响，不能保证新订单同样提速。
识别决策阶段以减少约 0.5–1 秒为验证目标，不是已测得的端到端收益。
新增 12 项离线测试覆盖连接复用/失败/限制、两路径共享、TTL/配置/换来源隔离、
取消/容量、固定金额边界及并行预取不能绕过订单归属。上线效果需重启后观察真实样本。

## 2026-09-15 执行预检统一使用配置报价有效期（已实现，待操作员重启）

此前 quote_policy 已设为 6 秒，但执行准备、签名前、广播前及 Gas 重试的
ReadOnlyExecutionPreflight 未传入该配置，仍使用 UnsignedExecutionPlan.validate 的默认
2 秒门槛，可能在报价策略检查通过后报 `execution quote is missing or expired`。
现四处调用均显式传入 quote_policy.max_age_seconds，预检结果记录 quote_max_age_seconds。
Feed 7 秒、报价 6 秒配置及其他经济风控不变；超过配置有效期或未来时间戳仍拒绝。
新增 5 项离线回归覆盖 3 秒/6 秒接受、超过 6 秒拒绝、默认兼容性、非法参数及四处配置接线。
452/452 测试通过（含可选 EVM 测试）。本次未签名、广播或重启实盘进程。

## 2026-09-15 Kyber 原调用有界增 Gas 恢复（已实现，未重启）

用户批准修改；本次不修改数据库、运行配置或进程，不签名、不广播、不重发旧提案。
`ExecutionPreparer` 在准备阶段首次 Kyber RPC 模拟回滚（execution_reverted/out_of_gas）后，
保持 calldata、输入量、minOut、收款人、Gas 单价和 deadline 不变，仅提高 gas_limit，
进行一次有界模拟。新上限为原值的 1.5 倍向上取整，最多 2,000,000，并受现有
`max_gas_cost_wei / maxFeePerGas` 约束；没有提升空间则不试。不把通过重试等同于已证实 OOG。

额外模拟前验证余额、allowance、当前费率与费用预算，并在异步检查前后复核原 Feed 意向和报价。
通过后照常执行完整 preflight、时效复核，再预留 nonce、持久化新上限；后续签名/广播沿用该
不可变计划。不对已准备/签名计划补改 Gas。传输错误、余额错误、无输出、低于 minOut 等不触发此恢复。
原路线增 Gas 后仍 RPC 回滚，才由既有换路条件判断是否排除 `uniswap-v4` 再试；替代路线不再增 Gas。
成功路径不加请求；增 Gas 恢复不调用聚合器。成功记录 `preflight.gas_retry` 和
`live_execution_prepared.gas_retry`；二次模拟失败时 `simulation_failure.gas_retry` 保留首轮诊断。

依据为本项目日志 `var/log/sm-copy-early-20260915.log` 中两笔保存的未签名失败调用：

| proposal | 固定重放块 | 原 gas_limit | 1,600,000 / 2,000,000 重放 |
| --- | --- | --- | --- |
| `46b0062011b1b7fb189a14350e6c3989a8058a14c2a3329a2ad546393bd34524` | 63417728 | 1218885 | 均成功，实际使用 1116707 |
| `f63d5b0b966082c9b96cd91e71becd96a2306cf678023ac5df70caacd82384d1` | 63418032 | 1218885 | 均成功，实际使用 1117778 |

两笔均买入 `0x648cf99e79a8e799cdd096364985e7e20d261e18`，0.1 USDG。
原上限重放在 Hook `0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544` 深层结算 OOG，
外层包装为 `Call failed`；USDG transferFrom 成功。重放固定块 hash 分别为
`0x9010c61267af24523dbc1dabcad28fe1e7682c0bc70ae6c2810d53eea0a24212`、
`0x865b7bf21e25a582023e89a563794f6be8fd3b852f7bff3ef98234d12dedd7de`。
这不是原 pending 状态的精确还原，更不是历史实盘成交证明；不推断其他回滚都是 Gas 不足。

测试包含不改 calldata/保护条件、金额费用上限、过期拒绝、失败有界、增 Gas→换路顺序、
一次 nonce/计划持久化与无额外聚合器请求。完整离线回归命令：
`PYTHONPATH=/private/tmp/smart-money-race-evm.eYVxKo .venv/bin/python -m unittest discover -s tests -q`。
本次 447/447 通过（含可选 EVM 测试），`git diff --check` 通过。

## 2026-09-15 用户指定 0x 授权总额度 10 USDG

此前操作员已完成 0.1 USDG 授权（回执 hash
`0x9071715767d4237017064464272c4a49f0b3f469b08d3a18b7c4715279393cd1`）。
按用户最新要求，独立脚本固定 AMOUNT 改为 10000000 raw，确认参数改为
`--execute --confirm-approve-10-usdg`，设置总额度 10 USDG，不是增加 10 USDG。
允许当前 allowance 为 0 或已确认的旧额度 100000；其他非零不足额仍需复核，足额不重复发送。
使用新 journal `var/zeroex-usdg-approval-10000000.jsonl`，保留旧 0.1 USDG 成功及中止记录。
其余钱包、spender、锁、Gas、历史状态与密钥门禁不变；未代用户签名/广播、未重启 monitor。
新增升级金额回归，435 项全量测试通过（含可选 EVM）。下方历史命令仅代表当时版本。


## 2026-09-15 授权 Gas 波动误拦修正

第二个根因：签名前后重新计算 gasPrice×1.2，再以整笔 transaction 相等作复核；正常 Gas 波动
也导致拒绝。inspect 新增 fixed_transaction，只使用原 maxFeePerGas 复核当前 gasPrice、原 Gas
最大成本、余额及策略上限，其他字段必须完全一致。签名前后均走该检查，不改已签名费用或 payload。
新增 4 项回归：正常涨跌、费用不足/余额/策略、nonce/payload 变化和带波动的模拟签名执行流程。
完整 434 项测试（含可选 EVM）通过。

本次只读复核旧 journal：仅 signing_started / signed_not_yet_submitted，无 broadcast_attempt；
hash `0x7e673a0c9314b47368d79eaf2231fdd8829896975cb57a53a48731d5875de53c`
的交易和回执均查询不到，nonce=201、allowance=0、未决历史计划=0。
保持原交易费用的公开预检实际通过。未读取真实密钥、重新签名或广播，未移动/删除旧 journal。
该记录继续阻止普通重跑。操作员可在确认无其他发单后将这一份已核验的中止记录改名归档，
再手动执行一次；不得把此步骤当成广播超时后的通用清记录重试方案。


## 2026-09-15 0x 授权脚本历史状态误拦修正

操作员停止 monitor 后仍遇到 ValueError；未创建授权 journal，未进入密钥访问。
根因：execution_plans 终态仍是 signed，规范回执结果在 execution_attempts；旧判断把
158 条 signed 计划误当未决，实际尝试表是 156 confirmed / 2 reverted。
改为按 plan_id 关联尝试：signed 必须恰好一个 confirmed/reverted，且其余只能是 replaced；
prepared、无尝试、仅 replaced、pending/signed/orphaned、未知/NULL、多个终态继续阻断。
cancelled 只有无尝试才通过，矛盾状态拒绝。只查询固定 follower，不改账本。
新增 3 项回归，真实业务库仅运行 check_execution_history 得到 unresolved_execution_plans=0。
全量 430 项测试通过（含可选 EVM）。本次未读取真实私钥、签名、广播或启动 monitor。


## 2026-09-15 独立 0x USDG 小额授权脚本（未执行）

用户要求准备连接现有私钥库的签名脚本。新增 `smart_money.zeroex_approval`，仅供操作员执行，
未改 monitor 或全局 APPROVAL_SPENDERS。固定 chain 4663、follower
`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`、USDG、spender
`0x0000000000001ff3684f28c67538d4d072c22734`、授权量 `100000` raw（0.1 USDG）。
地址来源为此前 0x 真实报价的 issues.allowance.spender，并核对
[官方合约说明](https://docs.0x.org/docs/core-concepts/contracts)。不是给 Settler 授权。

默认检查：`.venv/bin/python -m smart_money.zeroex_approval --relationship 1`。
默认不连接 key DB；读取启用关系与公开 RPC，检查固定钱包/资产/链、合约代码存在、USDG 6 位精度、
allowance、pending/latest nonce、Gas/ETH 余额、approve eth_call。非零但不足的 allowance 拒绝修改。
足额返回 already_sufficient，不广播；不将既有更高授权擅自调低。

操作员需要先停止 monitor，确保没有其他机器/钱包客户端使用同一钱包发交易，再显式执行：

```bash
.venv/bin/python -m smart_money.zeroex_approval --relationship 1 --execute --confirm-approve-0.1-usdg
```

执行模式复用 runtime_instance_lock（不覆盖 monitor PID），检查该 follower 无未决 execution plan，
使用原 LiveDatabaseSigner/数据库 enabled/live 接受时间与 snapshot/急停门禁。
固定 gas 100000，最大 Gas 费用受关系策略与 10^15 wei 较小值约束。签名前后重检状态，独立恢复
签名 sender 并逐字段比对交易，然后通过原 MainnetBroadcaster 只发送一次，检查规范 receipt 与 allowance。
本地锁不保护其他机器或独立钱包，因此仍要求操作员停止外部发单。

`var/zeroex-usdg-approval-100000.jsonl` 在访问密钥前独占创建；只保存公开交易、nonce、hash、状态，
不保存密钥或 raw signed bytes。超时/中断后不自动重发，也不自动清理记录；先按 hash 查链与 nonce。
授权是独立操作，不生成跟单 lot，不占 early 交易广播名额，也不自动启用 0x 跟单 provider。
测试仅使用假 RPC、假数据库门禁与内存一次性无资金账户；未读取真实密钥、签名或广播授权，未停后台。
验证：427 项全量测试（含可选 EVM）通过，compileall/diff check 通过。实际仅运行默认只读检查，
返回 ready、allowance=0、nonce=201、最大 Gas 费用 `9095280000000` wei，private_key_read=false、
broadcast_performed=false；这些是当次快照，执行时必须重检。


## 2026-09-15 时效再调整：Feed 7 秒、报价 6 秒

按用户最新明确要求，运行时 `EARLY_FEED_MAX_AGE_SECONDS` 从 6 改为 7；纯回放的可配置上限
同步为 7，默认历史回放仍为 3 秒。Feed timestamp 和 received_at 两个时钟均受 7 秒门槛约束，
报价刷新不重置 Feed 时钟。同步更新运行、执行边界及部署检查相关测试的边界断言。
业务 MySQL 已事务更新 follower `0x3004ab92565deeea0a2eaa27e40e297bb457e1a6` 的
enabled mainnet_live 关系 1/2/3/4/5/9：quote_policy.max_age_seconds 5→6，刷新
updated_at/live_risk_accepted_at。锁行核对身份及旧值，逐列确认其他策略字段不变，并通过
实际配置解析。全局 QuotePolicy 默认值未改；未重启、推送、改变额度或访问密钥库。
旧进程不会热加载，新配置与旧 snapshot 不一致会阻止执行；需用户重启后生效。
验证：含既有可选 EVM 依赖的 421 项测试全部通过，compileall 与 diff check 通过。


## 2026-09-15 Kyber 未签名前一次换来源恢复；其他聚合器调研

用户授权先优化 Kyber，再比较其他聚合器；明确没有 0x API Key，本轮先完成优化与调研。
未重启/部署、修改实盘配置、增加授权、读取密钥或广播。以下代码尚未提交推送。

实现入口 `ExecutionPreparer.prepare`、`LiveQuoter.begin_simulation_route_retry`：

- 正常路径不增加请求。仅当前 operation 的 Kyber route 元数据完整且包含 `uniswap-v4`、
  未持久化 execution plan、准备模拟 RPC 明确分类为 `execution_reverted`，才尝试一次替代。
- 只在已有 task-local execution_context 生效（当前 Kyber-only 实盘/early 路径）；无上下文不重试。
  不把普通 `Call failed` 断言为 V4 根因；日志 reason 明确是试探替代而非已确诊。
- 清除该 operation 的 full/reference/route 缓存，给新 full/reference routes 都传
  `excludedSources=uniswap-v4`；新响应任何分支仍有 V4 或元数据缺失均拒绝。构建保留原始
  routeSummary，重新执行完整价格、Gas、模拟、余额/allowance/nonce 与原 Feed 时效检查。
- 最多增加两次 routes（全量与小额参考）、一次 build，以及相应只读 RPC。备选交易的链上 minOut
  不得低于第一次失败交易的 minOut，deadline 不延长；不重置 Feed 接收时间或放宽 quote TTL。
- 仅准备阶段能重试。签名/广播前再次模拟失败仍停止，不改已持久化交易或重试广播。
  两次准备都失败不预留 nonce；既有外层取消逻辑释放 proposal 额度。
- 超时、余额/Gas 错误、缺失/畸形输出、低于 minimum、非 V4 或未知来源不触发此恢复。
  未做全局 V4 禁用、STANDARD 专用限制、跨交易黑名单，也未宣称解决全部代币/全部模拟失败。
  `enableGasEstimation` 保持 false：先前实验证明 true 能拒绝坏路线但不能自动修好它。
- `execution_quote_requests` / `early_quote_requests` 增加 `route_retry`：首轮诊断、原 route hash、
  排除来源、最终 prepared/failed、额外耗时。成功准备的 plan.preflight 同时保存原失败与替代模拟
  通过证据。prepared 不是广播成功；后续签名/回执结果仍看原 execution 账本。

候选调研（2026-09-15 查阅官方资料，不代表本项目已实测）：

| 候选 | 已确认 | 当前结论 |
|---|---|---|
| 0x Swap v2 | 官方支持 Robinhood 4663；quote 返回 calldata，issues 包含 allowance/balance/simulationIncomplete；所有 API 请求需要 Key | 优先对照，缺 Key，未测试或接入 |
| 1inch Classic Swap | 官方支持 Robinhood 4663，可取得 quote/calldata，使用 API Key | 第二候选，未测试或接入 |
| OKX | 仓库有独立客户端，但未接生产 execution provider | 本轮未取得明确的 Robinhood 聚合执行支持证据，不默认可用 |

官方来源：
[0x 链支持](https://docs.0x.org/docs/introduction/supported-chains)、
[0x API 认证](https://docs.0x.org/api-reference/api-overview)、
[0x v2 校验与 issues](https://docs.0x.org/docs/upgrading/upgrading-to-swap-v2)、
[1inch Robinhood 发布说明](https://business.1inch.com/whats-new/robinhood-chain-support)、
[1inch Classic Swap](https://business.1inch.com/portal/documentation/apis/swap/classic-swap/introduction)。

后续比较必须使用同钱包、同 token/方向/输入量/滑点，近同时取各家报价，再以同一编号区块模拟
精确 calldata，记录区块哈希；模拟时状态变化及余额/新 spender allowance 不足分别标记，不能把
缺授权算坏路线，也不能为比较自动 approve 或用状态覆盖冒充实盘成功。覆盖 STANDARD 及其他代币、
BUY/SELL、多种金额和不同时段；记录报价覆盖率、完整校验比例、模拟通过率、含所有请求的 p50/p95
耗时、minOut、Gas、费用与限流。只测 STANDARD 或只有报价价格不构成更换首选的证据。
若候选确实提高可执行覆盖/成功率且延时和成本可接受，再实现其独立的 calldata/recipient/spender
验证适配及主备失败矩阵。新 spender 授权与实盘启用需要操作员确认，签名后不跨聚合器换单。

离线回归：新增 `tests/test_kyber_retry.py` 8 项，覆盖来源过滤及响应违约、未知来源/错误类别拒绝、
operation 缓存隔离、一次上限、已有 plan 不改、原 minimum/deadline 保留和实用准备链路模拟。
完整 421 项通过（默认环境跳过 12 项可选 EVM）；compileall、pip check、diff check 通过。
使用既有隔离可选依赖 `/private/tmp/smart-money-race-evm.eYVxKo` 再跑，421 项全部通过，无跳过。


## 2026-09-15 Kyber 来源过滤与 gas 估算开关只读验证

用户要求先验证参数，不授权上线。本轮未修改生产代码、数据库配置、交易预算或后台进程；
未签名、广播或申请新 API key。沿用 careful 安全约束。仅调用官方 routes/route/build 和
现有 ReadOnlyRpc allowlist 的公开查询/eth_call，所有构建结果只在诊断内存中使用。

条件：chain=4663，follower=`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`，
USDG `0x5fc5360d0400a0fd4f2af552add042d716f1d168` → STANDARD
`0x88ad8ddf1e3898412146a534538d418c6f8a9062`，amountIn=`100000` raw（0.1 USDG），
slippageTolerance=300，deadline 为每次构建时刻+120 秒，模拟 gas=600000，value=0。
预检块 63332143：USDG balance=`11729877`、Router allowance=`1988500000`，输入充足。
每次模拟使用显式历史块号。默认/仅 V2 组模拟后另复查块 hash；V4/排除 V4 组记录模拟前
块 hash，未另做模拟后 canonical 复核。不是 Feed 触发，也没有完整策略/参考报价风控决策。

验证时间：2026-09-15 11:16:42–11:18:06 SGT（route_at 1789442202.231058–1789442285.354462）。
初始简化请求头曾 HTTP 403；使用项目原客户端完整请求头后成功，无需改 endpoint 或凭据。

| GET /routes 参数 | POST /route/build enableGasEstimation | 构建结果 | 自己的 eth_call |
| --- | --- | --- | --- |
| 默认 | false | 成功，V2 | 成功 |
| 默认（复用同一 routeSummary） | true | 成功，V2 | 成功 |
| includedSources=uniswap | false | 成功，V2 | 成功 |
| includedSources=uniswap（复用同一 routeSummary） | true | 成功，V2 | 成功 |
| includedSources=uniswap-v4 | false | 成功，无 Hook V4 | 回滚：Error(string) `Call failed` |
| includedSources=uniswap-v4（复用同一 routeSummary） | true | HTTP 422，API code 4227 | 没有返回可模拟交易 |
| excludedSources=uniswap-v4 | false | 成功，V2 | 成功 |
| excludedSources=uniswap-v4（复用同一 routeSummary） | true | 成功，V2 | 成功 |

共 4 次正式 routes、8 次 build、7 次交易 eth_call：6 模拟成功、1 模拟回滚、1 在 build 阶段
被拒绝。另有 1 次先行成功默认报价探测，不计入该矩阵。所有返回 V2 的池为
`0x90738366a044c13ea0a1393d1761d7625ed3f0a9`，exchange=`uniswap`，poolType=`uniswap-v2`。
V4 对照返回 exchange/poolType=`uniswap-v4`、fee=3015、tickSpacing=30、hookAddress=零地址，
PoolId=`0x15a07a23921a44c99964558bd9c10d9c1807994ec9f9c80645697cbc781c070c`。
此池不同于原始历史失败池；本轮仅确认新的同类无 Hook 路线外层回滚，没有新增 callTracer
核验其内部错误 selector，不将外层 `Call failed` 单独当成内部根因证明。

关键原始值与 provenance（金额均为十进制 raw）：

- 默认及仅 V2 组 route amountOut=`327507242747179264`，build amountOut=
  `327507242747179263`，minimum=`317682025464763885`，四次模拟 output 均为
  `327507242747179264`。默认 route hash
  `9c0187acc48315bf6d8102bb4a1569e7188889de551e261b4a7aad779f853b23`；仅 V2 route hash
  `eeeb80e257e210332d972f26b068806ee301436baed4ebfffcdbeaf50cf09cc8`。
- 默认 false/true 模拟块：63332164 / 63332171；仅 V2 false/true：63332186 / 63332194。
  仅 V2 false 的 calldata SHA-256
  `7c7214af258b96bc933234cfc7661a8e4c439948122f941abd5fae5897b16021`，块 hash
  `0x48c77d14bd86e16fcc7f87c10e145049e9ec847c7e253a30a5348f47530a3f3b`。
- V4 route hash=`86d8219cd910fe828a76c1663da4a4546335ecc3a2ce09eb482cfce532bf15d1`；
  false 构建 calldata SHA-256=`d7ebffd03056120047eef04f79bdeeb2f78958a02ad59e6b12051a3d258382f4`；
  模拟块 63332962/hash `0xf66703a263318e87af8cb5b3bd5dfb3ca3116fc2d5e8a366a687a99a25270aef`；
  RPC code=3，Error(string) `Call failed`。true 构建 HTTP 422/API code 4227，错误正文
  包含 revert/estimation，正文 SHA-256=`a4d7619891987b72ea637ee9f25e593e86055890e977f0d9b434ff435cf167c6`。
  该次没有自动换路成功，没有可供签名的结果。
- 排除 V4 route hash=`d283a9077a56bbc813e71248c3abd01a0ebd35e306085cf97ce22f7cfbbc283b`，
  route amountOut=`342094156359443072`，build amountOut=`342094156359443071`，
  minimum=`331831331668659778`，false/true 模拟 output 均为 `342090420469464742`。
  模拟块分别 63332973 / 63332977；false calldata SHA-256
  `69b0c500dcc436daacc4b59e94807952f3c6b855554ccb6070aef409cafd8174`，true 为
  `d8d68148b76a0a80952f5b02a6b00daefee63a615f1cbda96dfbe34b55819d6e`。

耗时只作为少量串行样本，不是性能基准：默认 routes=700.48ms，build false/true=
953.08/453.54ms；仅 V2 routes=656.53ms，build=525.40/340.34ms；仅 V4 routes=679.34ms，
false build=523.06ms、true 错误返回=411.60ms；排除 V4 routes=205.62ms，build=
299.59/390.00ms，eth_call=65.99/56.69ms。缓存、请求顺序、链状态均不同，不能据此认定
开启 gas 估算更快。第二次 build 复用同一 routeSummary，不把包含第一轮测试的累计耗时当成
独立交易延时。排除 V4 的第一轮 routes+build+eth_call 三项之和约 571.20ms，未计入额外
块查询、Feed、决策、签名或广播，更不是上链时间。

结论与边界：

1. `includedSources=uniswap`、`excludedSources=uniswap-v4` 均已实测返回当前可执行 V2
   路线；可以作为本 Token 的绕行候选。前者只留一个来源，后者排除整个 DEX ID，都不是
   精确单池过滤；后者也可能排除可用的带 Hook V4 池，不能无条件全局应用。
2. `enableGasEstimation=true` 已实测在本次坏 V4 routeSummary 的 build 阶段报错，而非
   自动修好/换路；它能提前发现问题，但不是恢复策略，也不能保证将来上链成功。
3. 当前默认报价已经选择同一 V2 池，故默认与仅 V2 成功组不是“旧坏路线被修复”的证明。
   通过 V4-only 反例和 excludedSources 对照确认来源参数确实影响选路。
4. 仅验证 BUY、0.1 USDG、当前状态；未验证 SELL、其他 Token/金额或完整实盘风控。
   不推断限制来源后价格始终最优或所有交易均可执行。尚未实施生产修复。

参数依据：[Kyber API](https://docs.kyberswap.com/developer-guide/aggregator-api/aggregator-api-specification/evm-swaps)、
[DEX 映射](https://github.com/KyberNetwork/kyberswap-documentation/blob/main/developer-guide/aggregator-api/dex-ids.md)、
[V4 DexType 常量](https://github.com/KyberNetwork/kyberswap-dex-lib/blob/main/pkg/liquidity-source/uniswap/v4/constant.go)。
原始证据为本次官方 HTTPS 响应及只读 RPC 输出；这里只留显式摘要/哈希，未声称保存完整
route/build 响应或可独立重放的全部 calldata。

## 2026-09-15 STANDARD 模拟回滚根因确认：无 Hook V4 路由不满足 Token 转账额度规则

本节更新下一节尚不明确的根因。用户授权继续只读调查；未修改交易代码、白名单、策略、
数据库或后台进程，未签名、广播。诊断通过固定源交易的独立 callTracer（5 秒、reexec=0、
无日志）与已有保存调用 trace 入口完成，不把 callTracer 开放给生产通用 RPC 客户端。

**结论仅针对 proposal `3b53a3713f7f0b7feaf32bf45e50abe599df763155e12c197f9334d8d3128aee`：**
Kyber 构建的 USDG → STANDARD 调用选择无 Hook 的 V4 池；STANDARD 的 PoolManager 转账门控
当时开启，需要注册 TAX_HOOK 在同一交易内先增加对应方向的临时额度。该路由没有调用 Hook，
额度保持 0，因此 PoolManager.take 最终执行 Token.transfer 时触发 `0x206e5f37`。
不再只是“可能存在临时权限机制”的猜测。错误和函数的 Solidity 源码名称仍未知，但调用、
权限、存储读取/写入、额度不足分支及成功路径对照已闭环。

身份与证据来源：

- Token：`0x88ad8ddf1e3898412146a534538d418c6f8a9062`；历史 name()/symbol() 均为 STANDARD。
- PoolManager：`0x8366a39cc670b4001a1121b8f6a443a643e40951`。
- Registry：`0x855c294dd019e9e0d84c058cff7404f5f2ddca4b`；selector `0x8eaa6ac0` 参数为
  bytes32 ASCII `TAX_HOOK`，历史返回 `0xf1ee073811b14359d850825e48d200483200edcd`。
- 成功源交易：`0xde76423d927eb1d10a67668d8b62f1fc9bb5ce210b8ff211ce56af2a56370f27`，
  block 63285068，status=1。失败调用来自现有 JSONL simulation_failure，重放块 63285080，
  calldata hash、块 hash 见下一节。两个历史块的 Token 门控 getter `0x52451a9c` 都为 true，
  TAX_HOOK 登记地址一致。

追踪直接解码 PoolManager.swap 的 PoolKey（不是用 PoolManager 地址代替池身份）：

| 字段 | 我们失败的保存调用 | 源交易最终成功分支 |
| --- | --- | --- |
| currency0 | USDG `0x5fc5360d0400a0fd4f2af552add042d716f1d168` | native ETH，零地址 |
| currency1 | STANDARD | STANDARD |
| fee（原始值） | 330000 | 10000 |
| tickSpacing | 3300 | 200 |
| hooks | 零地址 | `0xf1ee073811b14359d850825e48d200483200edcd` |
| PoolId | `0x53699ebf1f9175df123d440875a107431ee8533cb1c14166c051f0bedd3ad565` | `0xc73f3cd3fb68288e63f008e08ef69caa0437224e420963c6aeb4526178e87ad9` |

失败调用 trace 共 25 帧，swap 路径 `0.2.0.2.0.0.0.0.3`，Token.transfer 失败路径
`0.2.0.2.0.0.0.0.4.0`。需要转出 `243475500362163640` raw，临时额度 `0`，
原错误参数为 `(false, 243475500362163640, 0)`，无额度写入调用。

成功源交易 trace 共 345 帧，必须排除祖先已经回滚的路线试探。最终生效的分支为：

1. `0.1.1.5.2.0.10.0.0.0`：ETH/STANDARD PoolManager.swap。
2. `.1`：PoolManager 调用注册 Hook 的 `0xb47b2fb1`（afterSwap）。
3. `.1.0`：Hook → Token，selector `0xd738ee8c`，参数
   `(false, 1087441258958955336433)`；Token 内部查询 Registry 的 TAX_HOOK 以验证调用人。
4. `0.1.1.5.2.0.10.0.0.3.0`：PoolManager → Token.transfer，向
   `0x0005ea38eb0a69d1253508ebdbdb9ea8cb26b5ef` 转出相同 raw 数量，返回 true。
5. Token 经 wrapper/Relay 最终转给源钱包；这些节点均无回滚祖先，与成功 receipt 相符。

补充交叉证据：源交易的 Kyber 试探中也出现无 Hook USDG/STANDARD 池，fee=10000、
tickSpacing=100，Token.transfer 同样报 `0x206e5f37`；不能将它误作成功源交易的成交池。
同一源交易也出现 Kyber 执行器通过带 TAX_HOOK 池完成额度设置及 Token 转出的局部成功，
虽然该试探整体被 wrapper 回滚。这进一步说明不是 Kyber 执行器地址一概被禁止。

历史 Token runtime 的控制流核对（代码元数据指纹见下一节）：

- `0x1055` 为转账更新前检查。门控打开且 from=PoolManager 时，调用额度消费函数，方向 false；
  to=PoolManager 时对应 true。随后才进入普通余额更新，不是把 ERC20 balance 当成临时额度。
- `0x121b` 按方向取临时存储 key，`0x1229` TLOAD；amount > credit 时构造 `0x206e5f37`，
  否则扣减额度。false 对应 key
  `0x3a9c1f4b7e2d5c8a1f6b9e0d2c4a7f8b3e6d1c9a5f2b8e4d7c0a3f6b9e1d4c2a`。
- selector `0xd738ee8c` 跳至 `0x0c4b`：查询 Registry TAX_HOOK，与 CALLER 比较；
  不匹配回滚 `0x67373552`。通过后读取旧额度、加上入参，并通过 `0x0f7c` TSTORE 写回同一 key。
- 在块 63285080 的只读 eth_call 中，以已登记 Hook 为 from 调用该函数成功；以 Kyber
  executor 为 from 调用相同参数返回 `0x67373552`。这只是虚拟执行权限验证，不是发送交易，
  也不会将额度保留给后续 eth_call 或实盘交易。

项目责任边界：`kyber.py:_parse_build` 读取 API data，仅规范化十六进制大小写；
`execution_prep.py` 同样原样采用 swap.data，没有将带 Hook PoolKey 改成无 Hook。
内部 executor payload 当前不解码，且 build 请求 `enableGasEstimation=false`，项目依靠自己的
签名前 eth_call 检查可执行性。本样本说明收到可报价/可 build 路线，不等于可成功执行；
我们的模拟正确阻止了广播。未知的是 Kyber 上游为何纳入/选中这条不兼容池，而不是本次
EVM 回滚的原因。这里不声称已取得当时上游报价服务内部状态。

处理方向（尚未实施）：对这种门控 Token 排除不满足 Hook 额度规则的具体路由，改取经过
所需 Hook 的路线，或经模拟验证可用的 V2/V3 等替代路线；换路后重做价格/时效/风控检查。
不能只替换 hooks 字段，因为 PoolKey 改变就是另一个池，必须重新报价构建。
不要把所有 V4 池禁用，也不要跳过模拟、放宽滑点或让 follower 直接伪造额度。
此前新 V2 路线模拟成功是另一个时点的证据，不保证原时点可用；未新增实盘修复或自动重试。

## 2026-09-15 独立历史 callTracer 诊断与 V4 Token 转出回滚定位

用户另行同意只读调用追踪后，新增 `smart_money.call_trace` 独立入口，不被交易运行时导入。
生产 `ReadOnlyRpc.call` 白名单不变，仍拒绝通用 debug_traceCall；仅诊断方法允许固定 callTracer。
要求完整已保存的 Kyber unsigned simulation_failure、calldata SHA-256/钱包/Router/金额匹配、
chain 4663、显式历史块号及 `--allow-rpc`。不支持 pending/latest、状态覆盖、任意 JS tracer；
只允许可选 gas 调整至 21000–2000000，串行请求，服务端 5 秒/HTTP 10 秒超时。
追踪前后复核块 hash、根调用绑定，输出有界调用树路径、selector、gas、失败摘要，不输出完整
calldata、任意提供方消息或未知错误参数。失败子调用可能被捕获，不能一律视为整笔失败原因。
无签名、广播、交易重试、配置/预算/账本写入。本轮未重启或推送。

在项目虚拟环境安装完成后，按需执行一次（需要提供方支持该历史状态及 callTracer）：

```sh
.venv/bin/python -m smart_money.call_trace \
  --log var/log/sm-copy-early-20260915.log \
  --proposal-id 3b53a3713f7f0b7feaf32bf45e50abe599df763155e12c197f9334d8d3128aee \
  --block 63285080 --allow-rpc
```

接口依据：[Geth debug_traceCall](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-debug)、
[callTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers)。
历史重放不是原始未固定 pending 状态的完整还原，结果显式标记这一限制。

实际 RPC 追踪上述日志样本，在原报价块 63285080/hash
`0x2fcb8db2d13507bee71b79a9fd1b93d33330b50a925d701ed9769e0443c4bc52`、
原 gas 600000 下复现失败，根 gasUsed=213252。失败传播链为：

1. Kyber Router → executor `0x8f10b468b06c6fd214b65f87778827f7d113f996`。
2. 经 delegate adapter 调用 V4 PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`。
   USDG transferFrom、转入/settle、PoolManager swap 子调用均成功。
3. PoolManager 的 take（`0x0b0d9c09`）调用目标 Token
   `0x88ad8ddf1e3898412146a534538d418c6f8a9062` 的 transfer（`0xa9059cbb`），
   收款人为 executor，金额 `243475500362163640` raw。
4. Token transfer 是该失败传播链最深的失败帧，无下层调用；剩余传入 gas=336891、
   gasUsed=7875，返回 100 bytes 自定义错误 `0x206e5f37`，随后被外层包装为 `Call failed`。
   原错误 SHA-256：`fabc21645030bee24cc6eaf9066020d783de0c4eb85b14268c43e7779feb911a`。

补充只读验证：该块 PoolManager 实际 Token balanceOf 为 `24335925685086405201034910` raw，
显著大于转出量；直接以 PoolManager 为 from 模拟同一 token.transfer，也得到相同错误摘要。
字节码在 selector 常量偏移 4662 附近读取 `TLOAD` 值，与转出量比较，不足则回滚；
错误三词为 `0 / 243475500362163640 / 0`，第一词构造使用双 ISZERO（布尔规范化）。
因此可定位为 Token 自身的交易内临时额度/权限检查拒绝，而非普通 ERC20 余额不足；
具体字段名、额度产生条件和为什么该 Kyber 路径未满足条件尚未取得验证源码，不能声称已确定。
这也不是放宽报价有效期、增加 gas 或降低外层 minimum 可直接解决的问题。

同一源交易 `0xde76423d927eb1d10a67668d8b62f1fc9bb5ce210b8ff211ce56af2a56370f27`
的成功回执中，该 Token 从同一 V4 PoolManager 转给
`0x0005ea38eb0a69d1253508ebdbdb9ea8cb26b5ef`，经包装器/Relay 转至聪明钱钱包。
不能将本样本推广为所有 V4 路径、所有买入均失败，也不能认定该币无法卖出或存在黑名单。
后续新 Kyber V2 报价模拟成功的对照见下一节；其状态和路由均不同，不证明当时切路必然成功。

源码来源核查：该历史 Token runtime 长 6434 bytes，元数据指向 IPFS
`QmVusfrwD8mkNRqmNKr4ZoNGv9TXw1iW8AKfFxB2u37Jmn`，metadata SHA-256
`7084a16cd82023b4e13a21a09b571bce247bc8f46c7d2904bc0e7a05a004ef59`。
本次 Blockscout API 返回 403、Sourcify full_match 返回 404、多处 IPFS 网关访问失败；
未拿到可验证 ABI，故不猜测 `0x206e5f37` 的 Solidity 错误名称。
旧摘要字段 target_data_selector=0x4f1423cc 实际来自 packed target_data 开头地址
`0x4f1423cce26c09fb31d96eafe702fc5e22421f6a`，不能将它当作实际被调用函数；
追踪中的 executor/delegate 函数 selector 是 `0xd9c45357`。

新增 6 项离线测试：CLI 先 opt-in、生产白名单保持拒绝、链/块/原调用校验、固定 tracer
无覆盖、重组检测、有界树及敏感原因脱敏。报价 5 秒配置仍需操作员重启后加载；
诊断成功不表示已发生实盘成交，也不自动启用换路或重报价策略。
验证结果：413 项全量离线测试通过（含临时可选 EVM 依赖）；普通虚拟环境同为 413 项、
跳过 12 项。compileall、pip check、git diff --check 均通过。

## 2026-09-15 报价有效期改为 5 秒；模拟回滚只读对照

用户明确要求先将报价有效期改为 5 秒。已在业务 MySQL 单事务更新 follower
`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6` 的 enabled mainnet_live 关系 1/2/3/4/5/9：
只将 quote_policy.max_age_seconds 从 2.0 改为 5.0，并同步 live_risk_accepted_at/updated_at
记录本次配置确认。逐列校验其他字段完全相同，未操作预算、trial、额度或密钥库。
实际 load_mysql_paper_config 复核六条均为 5.0；代码默认 QuotePolicy 仍为 2.0，不扩大到其他配置。
Feed 仍 6 秒，滑点 300 bps、价格冲击/偏离 500 bps、Gas 上限不变。
未重启 PID 48838；旧进程不热加载，配置复核会因 snapshot 不一致拒绝执行，需操作员重启后生效。
重启同样会加载上一节（下方）的入账补证过滤；不要误称配置已在旧进程中生效。
修改前 execution-audit healthy，prepared=0、signed/pending/orphaned attempt=0，历史 156 confirmed/2 reverted。

针对 proposal `3b53a3713f7f0b7feaf32bf45e50abe599df763155e12c197f9334d8d3128aee` 的
simulation_failure（来源 `var/log/sm-copy-early-20260915.log`，原模拟 1789437428.336926），
本轮仅使用现有 ReadOnlyRpc allowlist 和 Kyber 官方 API 做只读对照，没有生成提案、签名或广播：

- 原输入 USDG `100000` raw，目标 `0x88ad8ddf1e3898412146a534538d418c6f8a9062`，
  Router `0x6131b5fae19ea4f9d964eac0408e4408b66337b5`，内部 call target
  `0x8f10b468b06c6fd214b65f87778827f7d113f996`。原 calldata 3588 bytes，
  SHA-256 `a57acd0a9ccd0fa221b9e8b9123ba903309b20250da0110d4a944e51223c2b1e`。
- 源 receipt status=1，块 63285068，hash `0xba6690bab0a515c519aee204f10904b2d1b496c18f2a65bce2f61972d77f547f`；
  报价块 63285080，hash `0x2fcb8db2d13507bee71b79a9fd1b93d33330b50a925d701ed9769e0443c4bc52`。
  两块余额均 `11729877` raw USDG，Router allowance 均 `1988500000`，均覆盖输入。
- 原调用分别在两个历史块以 600000、2000000 Gas 模拟，四次均 code 3 / Error(string) `Call failed`。
  另确认顶层 ABI roundtrip 完全一致后，只在内存中将外层 minimum 从 `236174385809595060`
  改为 `1`，在报价块/2000000 Gas 下仍同样回滚；未更改生产滑点，也不排除内层价格/限制。
- 新 routes/build 同钱包/币对/金额、300 bps，route_at=1789439169.609123，返回单跳 V2 池
  `0x90738366a044c13ea0a1393d1761d7625ed3f0a9`，新最低输出 `249523491810035478`。
  route response hash `c211c6c25f95d941eeeddcef6cbcd92cc64caaf1b8be5dc33d00be18b12398b7`，
  build response hash `7be7a089b9a67a429941d9286824e26c4e937b332cab844f23fa25de91387d39`。
  在块 0x3c5ea67/hash `0x330e0b08595c7fd846eed38f4a4bc2a82da248545ebfe9979c949b61d1614685`
  以 600000、2000000 Gas 均 eth_call 成功。新池地址字节未出现于旧 calldata，仅为路线差异线索，
  未完成旧内部压缩调用解码，不将字节搜索当成池子归属证明。

结论边界：历史对照复现原失败，且在这些历史状态下不是账户 USDG/Router allowance 不足，
增加外层 Gas 或降低外层 minimum 不能解决。新构建可模拟成功，但状态/路线不同，不能倒推原
pending 时点成功，也不能确定具体哪一跳失败。原交易没有广播 hash，debug_traceTransaction
不能追踪它；现有白名单不允许 debug_traceCall/callTracer，本轮没有绕过或修改白名单。
精确定位内部回滚仍需单独授权的只读调用追踪能力，或取得可验证的内部执行器解码/本地重放。

## 2026-09-15 普通直接转账跳过 Relay 入账补证（未重启）

新增 `receipts.direct_token_transfer_evidence()`，使用已取得的公开交易参数与成功回执，严格匹配
顶层标准 transfer/transferFrom、收到的 token、资金转出方、当前钱包和正数金额。恰好一个 Transfer，
其他日志只允许同 token 标准 Approval；未知/畸形日志、多个 Transfer、Swap、Relay 交付、包装调用、
零地址/自转账等均不跳过。规则不增加 RPC，也不改变原始 receipt 分类或 Feed 提前买卖规则。

monitor 在入账信号首次 emit 前写入 `relay_lookup_skipped`、`relay_lookup_skip_reason` 和完整匹配
依据 `relay_lookup_skip_evidence`，然后跳过 destination-hash 订单查询；每次使用当前回执重算，
不信任旧 skip 标记。保留 INCOMING_TRANSFER/needs_review，不升级 BUY，不生成跟单许可。
没有其他待补证信号时候选正常完成并保存 inclusion；否则整笔交易仍走既有重试。
日志和 health 的 `relay_lookup_skipped` 是本进程处理次数，不是去重交易数。无需数据库迁移，
不删除旧记录、不复活 failed、不重置 attempts；旧 pending/retry 在自然处理时适用规则。

已明确接受的风险：无额外标记的跨链直接 Token 交付也可能被过滤，匹配不是无购买的证明；
离线 relay-associate 仍保留严格订单补证能力。详见 copy_trade_flow.md 3.4。
新增 13 项离线测试覆盖参数/事件匹配、各类反例、真实 monitor 接线的零 Relay 请求、信号持久化、
complete/inclusion、其他信号重试不被吞掉、旧标记不可信及关闭自动关联时不虚报跳过。
本轮未操作生产数据库、预算、配置或后台进程，未提交推送、重启或广播；运行进程尚未加载本改动。
验证：407 项全量测试通过（使用既有临时可选 EVM 依赖）；普通虚拟环境同为 407 项、跳过 12 项。
pip check、compileall、git diff --check 通过。测试全部使用离线数据与模拟提供方，不代表现场延时已改善。

## 2026-09-15 部署代码后台检查（已实现，未重启）

按用户明确选择，把部署代码检查与交易时效解耦，不引入缓存最长有效期。新增
`deployment_monitor.py`，仅在 monitor/run 启用提前通道时创建同进程后台任务，不新增 Feed、
进程或数据库表。第一次检查在提前 worker 启动前进行，之后每轮结束等待 30 秒再检查；
复用既有受限 RPC，查询 chain ID、latest 块及 blockHash/requireCanonical 对应的 code。
RPC 自身超时仍有界，单任务串行检查，不因超时创建并行替代请求。

范围仅为 `relay_race.py` 已登记的包装器 `0x039ec98a76f111092d4751365ff09dd2aec301e8`，
固定 4721 bytes/Keccak/rule；不是任意 Relay Proxy、账户委托或内层 Router 的升级监控。
沿用公开证据 `data/relay_race_runtime_2026-09-14.json` 和原 EVM 测试，不声称取得验证源码或
完成升级能力审计。没有开放新的代码版本；要支持其他代理/实现须另做适配。

- 首次未取得有效代码：该包装提前路径不开放，其他路径及严格后备继续原规则；后台继续尝试。
- 验证通过：候选读取缓存，保存完整证据副本；后续决策/构建/签名/发送仍重验本地语义和停用状态，
  不再调用部署 RPC，也不按部署快照年龄拒绝。不能重置或伪造原采集时间。
- 请求失败/畸形响应/错误链：`deployment_check_failed` 仅记录异常类型、连续失败数和距上次成功
  时间，不输出 RPC URL/错误原文，继续保留已有验证结果。连续失败可能无限延长变化盲区，
  是用户接受的策略，不是假设“查询失败则交易必然失败”。
- 成功取得但代码不匹配（含空代码）：`deployment_code_changed` 将该路径锁为停用，旧意向的
  后续执行复核同样拒绝。后续即使恢复原哈希也不自动解锁；需人工复核后重启。不会自动适配新代码。
- 通过日志 `deployment_check_passed`、锁定后日志 `deployment_check_still_disabled`；启动和 health
  的 `deployment_verification` 报告状态。停止 monitor 时回收后台任务。
- 提案归因新增 `early_deployment_verification`：所用原始 code hash、rule、区块、采集时间、
  provenance、validation_mode；完整代码仍在 early_feed_jobs 快照，现有源失败/不匹配归因照常。
  这些字段支持追溯，并不能仅凭代码变化就证明某一笔误跟。

历史回放及未注入监控器的离线入口仍保留部署快照 3 秒规则，不把当前缓存回填旧交易。
Feed 6 秒、SELL 账户委托 3 秒、报价/策略/持仓、permit、预算、trial、防重和模拟均未放宽。
新增 11 项离线测试及已有 monitor 接线断言，覆盖首次失败后恢复、连续失败保留、代码变化
撤销已有意向/锁定、旧快照复用无 RPC、未来/篡改证据拒绝、历史和账户 TTL、定时/关闭及归因入库。
本轮不改变运行进程、生产配置/数据，不重启、不提交推送、不签名广播；需操作员后续重启加载。
不能把取消部署 TTL 当成已经解决另一笔 Kyber `Call failed` 模拟回滚。
验证结果：394 项全部通过（使用既有临时 EVM 依赖）；普通虚拟环境同为 394 项，跳过 12 项
可选 EVM 测试。pip check、compileall、git diff --check 均通过；没有使用主网调用作为测试。

## 2026-09-15 提前意向窗口改为 6 秒（尚未重启）

用户最终明确选择 6 秒，不是此前讨论的 5 秒。`early_timing.py` 集中定义运行时提前意向年龄，
同一 Feed 接收入口（启用提前通道时）、队列、识别及 VerifiedFeedIntent 重验统一使用该值。
决策/构建/签名前/广播前继续调用原重验接口，未删除检查。源时间及本地收到时间均须在窗口内，
不把本地接收时刻重置为源时间。普通观察器与严格分支的旧 feed_intent shadow 仍为 3 秒；
离线历史 evaluate_candidate 默认仍为 3 秒，运行时显式传 6，避免改变旧回放口径。

报价 TTL、账户/部署快照与策略/持仓快照 TTL、滑点、permit deadline、健康静默时间、预算及
24 小时/100 次提前广播上限均不变。快照过期仍会单独拒绝，不承诺 6 秒以内必能发送。
新提案记录 early_feed_max_age_seconds、early_feed_timestamp、early_decision_exceeds_3s，
最后一项只表示决策时年龄超过旧门槛，不代表实际广播。新启动事件报告 early_feed_max_age_seconds。
本轮没有重放旧失败提案、重建 trial、改变生产配置或预算，也没有停启运行中的实盘程序。
重启须沿用原 trial，由操作员执行；不要将代码修改当成运行进程已加载新阈值。

新增 6 项离线回归覆盖恰好 6 秒、超过 6 秒、双时钟/未来时间、识别接线、执行边界复核、
旧离线默认 3 秒以及报价/快照 TTL 不变。其余旧历史记录保持原时点语义。
全量 383 项测试通过（使用既有临时可选 EVM 依赖）；普通虚拟环境为 383 项、跳过 12 项，
pip check、compileall、diff check 通过。只读重启前审计 healthy=true、prepared=0、
signed/pending/orphaned attempt 均为 0，历史 attempt 为 155 confirmed / 2 reverted。
当次核对仍运行 PID 71847，旧进程未重启，因此尚未加载 6 秒设置；这些数值是检查时快照。

## 2026-09-15 模拟失败现场诊断已实现（本次改动未重启上线）

操作员已自行创建 `feed-early-20260915` 并启动提前实盘；00:33 的只读检查确认 PID 52642、
单一 Feed 连接、六条 Kyber-only 关系和有效风险确认。下节“未启动”保留为更早的停机准备记录，
不是此后运行状态。任何后续接手仍须重新核对进程和日志，不能把这些历史 PID 当成当前状态。

本次按用户同意，仅新增诊断代码和离线测试，未重启/停止进程、改生产配置或迁移业务数据库，
未访问真实密钥或签名/广播。运行中的旧进程不会因修改源码自动获得新日志；需由操作员按现有
运行手册完成安全重启后，新的失败才会携带诊断。不要重建 trial、重置预算或绕过启动审计。

### 现场证据与不能推断的结论

- 原提前买入 proposal：`98a4607fb5af1ec080a4eb1da05e928597154b826c1fcf79bb0197575c9d5bdf`；
  source tx：`0x0439f64de6196e698720fbc942c047598ff259007be3d02fc10a6c68e302d678`。
  依据业务 MySQL 中该 proposal、reservation、claim 的只读查询，以及
  `var/log/sm-copy-early-20260915.log` 第 408–419 行：识别和决策通过，输入 `100000` raw USDG，
  prepare 阶段 `eth_call` 返回错误码 3；提案取消，预算和 operation claim 释放，无 execution plan。
- 当时执行方案在模拟通过后才入库，RPC 层又丢弃了具体 error data；因此缺少原始 calldata 和
  回滚详情，不能声称已确定那次失败根因。
- 经用户明确授权，于约 00:42:50 使用同钱包/币对/输入量向现有 RPC 和 Kyber 官方 API 做
  新报价与只读模拟。原报价块 62951422 和当时 pending 余额均为 `11929877` raw USDG，
  Router allowance 均为 `1988700000`；新方案在 Gas 600000 与 2000000 下都模拟成功，
  输出均为 `76313842399386127938` raw，Router 报告 Gas 用量 `221594`。
  新 build 响应摘要 `9f60f0654967527020a0192afb099c06b123ed0da09879f89095ead24d70604b`。
  源交易回执 status=1，但回执成功本身不等于各用户子操作的资产兑换证明。
  新报价/状态与原现场不同，不能据此倒推原失败一定是 Gas、滑点或路由，也没有补写成实盘成交。

### 新记录和读取方式

`rpc.py` 只为错误保留受限的结构化诊断，RPC 方法白名单不变。`execution_prep.py` 的模拟失败
抛出仍属于 ValueError 的 `AggregatorSimulationError`；现有 monitor 取消/释放路径保持原样，
在原 `live_execution_abandoned` JSONL 事件内追加 `simulation_failure`，不创建新的执行许可。
prepare / sign / review 三个阶段均覆盖；该字段也适用于严格证据分支，不只提前分支。

- 事件关联：proposal、relationship、source event/tx、early_trial_id（严格分支为空）。
- 精确调用：from、to、data、value、gas、`eth_call` 和 `block_parameter=pending`。
  不含 nonce、私钥、签名或 raw signed transaction。JSON-RPC 参数中的 value/gas 保持原协议 hex；
  输入金额、最低输出、计划费用等 `*_raw` 继续为十进制字符串。
- 时间和上下文：模拟开始/结束墙钟、单调时钟耗时；原 unsigned plan 的报价时间/区块号/hash。
  `quote_context_basis=unsigned_plan` 不代表签名复核时新取得的报价；
  `pending_state_pinned=false`、`quote_block_is_simulation_block=false` 明确不能重建原 pending 状态。
  不额外调用 RPC 获取另一个时点的“最新块”冒充模拟块。
- 错误：有界 RPC code、固定 message_category、解出的 Error(string) 原因或 Panic(uint256) 代码；
  未知自定义错误只保留 selector、长度和 SHA-256，不猜 ABI，不倾倒任意响应对象或未知参数。
  传输/响应处理异常仅保留类型；空响应、不可解码响应、输出低于下限也保留现场，仍拒绝执行。
- 大小和脱敏：仅检查最多 8 KiB revert data；原因最多 512 字符，URL/端点凭证/疑似密钥/控制字符
  会整段隐去。未签名 calldata 最多保留 64 KiB，超限 data=null，并明确
  `calldata_omitted_size_limit=true`，仍保存原长度、完整 calldata hash 和 request hash，不能当成
  可完整重放样本。即使已脱敏，日志也包含钱包/交易意向，按业务运行日志权限和保留策略管理。

从仓库根目录只读提取（不要将 data 自动发送到广播接口）：

```bash
jq -c 'select(.event == "live_execution_abandoned" and .simulation_failure != null) |
  {observed_at, stage, proposal_id, relationship_id, source_tx_hash, early_trial_id, simulation_failure}' \
  var/log/sm-copy-early-20260915.log
```

诊断使用现有 stderr JSONL 持久化，不修改数据库 schema；未重定向或删除运行日志会丢失记录，
无法靠现有 proposal 表补回失败前的 calldata。没有自动重试、放宽滑点/Gas/报价 TTL、增加 Feed
订阅或修复其他延迟问题。新增 16 项离线测试，覆盖脱敏/大小边界、三阶段 monitor 接线、
原有清理及不广播；全量 377 项通过（含现有临时依赖中的 12 项可选 EVM 测试），
pip check、compileall、diff check 通过。不能据此宣称原回滚根因已修复或新增记录已在实盘生效。

## 2026-09-15 已推送与停机准备完成（未启动实盘）

按用户明确选择，将提前链路实现、测试和相关公开证据推送到 `origin/main`，功能提交 `3dfb71b`。
361 项测试含可选 EVM 通过，依赖/编译/差异检查通过；个人截图和运行日志未提交。
先建立 0600 急停，审计无在途，精确核对并停止旧 PID 45757；现在没有本项目 run/monitor 实例。
已应用生产迁移 008–010，核对四张空表及最小权限；六条关系 1/2/3/4/5/9 已改为 Kyber-only，
USDG 范围、evidenced 后备、原金额和风控未改，全部预算/周期逐行比较未变化。
风险确认保留原时间并因配置更新失效，等待操作员验收；未创建 trial、未解除急停、未读取真实
私钥或发送授权/买卖。不要把准备完成当作已上线，关闭会话后不会自动测试。

链上 latest/pending nonce 197/197，账本 154 confirmed/2 reverted，无未处理执行；36 种归因
Token 余额覆盖持仓，但 27 种 Kyber allowance 为零，28 种不足全部归因持仓，会影响提前 SELL。
本地快照保存在 Git 忽略的 var/prelaunch-*.json（0600）。完整新快照、证据口径、剩余人工风险
确认、临启动时初始化窗口、后台启动和报告命令见
[EARLY_FEED_PRELAUNCH_2026-09-15.md](EARLY_FEED_PRELAUNCH_2026-09-15.md)。

## 2026-09-14 提前执行接线与上线前验证（未部署、未重启）

本节取代下方“尚未挂接 monitor/构建器”的旧进度。新增 early_runtime.py，把同一 Feed 内部
队列接到原有执行流水线；只有显式 `run --early-trial-id` 才选择已有的操作员窗口。提前构建
使用独立 typed-intent 入口，未把原始 intent 改写为 success，也未放宽旧严格构建入口。
源证据、预算/持仓、试运行、配置及最终发送时效反复核对；现有 allowance 不足走严格后备，
不在提前入口新增授权。原有 Kyber 同操作报价复用继续适用。

严格后备按持久 trial scope 接操作防重，窗口过期/停止或重启不带提前标志也不丢失该 scope。
操作领取会检查升级前旧提案关联的源订单。source_position 核对已接入 signal 写入和我方 fill
事务，支持源先到/后到及重组作废，失败不释放我方真实资产本金。加入本机实例锁/PID 存活检查、
关键异常持久急停和内存锁定；试运行中的已发送交易在窗口结束后继续跟踪，未知重启拒绝自动交易。
新增操作员 early-trial-start 命令（默认需显式确认、要求急停有效，不会清除急停或发送交易）。

日志新增墙钟时点与阶段事件；early_metrics.py / summarize_early_trial.py 只读统计提前于严格
证据的广播确认、分段延时、区块差、已知误跟及未知比例。没有现场新样本，不声称已提速。

361 项测试通过（含可选 EVM），pip check/compileall/diff check 通过；真实 MySQL 隔离测试通过，
两轮各 24 张随机合成表已清理，未改业务表数据。生产审计 healthy，154 confirmed/2 reverted、
无在途 attempt，latest/pending nonce 均 197。六条原配置风险确认有效，但仍是 local+kyber，
新增四张提前表尚不存在，旧 PID 当时仍为 45757。未改生产配置/预算、执行迁移、清除急停、
重启进程、读取真实私钥或广播。实盘启动必须由操作员本人完成，详见 EARLY_FEED_LIVE_HANDOFF.md。

保留限制：单机锁不证明其他主机没有实例；首次 SELL 的 allowance 可能不足；订单请求
single-flight 尚未实现；未知内层路由仍未知；未做新增提前链路的真实主网故障/延时验收。
不要根据单测数直接勾选 LIVE_RISK_CHECKLIST.md 的操作员/现场验收项目。

## 2026-09-14 下一步：已验证 Feed 意向的只读决策与源持仓隔离

新增 `verified_feed_intent.py`：从原始公开 Feed 交易重新解析指定钱包/路径，保存不可变 JSON
证据副本，并在使用时重验新鲜度、语义、归属及所需部署证据。不能仅凭保存的
`recognized_intent=true` 构造许可。此类型不是外部输入认证机制，快照仍须由可信运行时采集。
用于报价的 Signal 保持 `intent/pending`、`copy_eligible=false`；BUY 输入为已核对订单付款额，
SELL 为已签署声明卖出量，不伪造 `actual_*` 或已成交输出。

新增 `early_decision.py`：只读检查关系开关、快照绑定/时效、订单去重、资金资产、源协议、
Kyber-only 提供方、固定/比例金额、预算/归属 lot、原有报价风控。允许动态目标币，但不允许
未受信资金币；跨本金资产缺乏源价格依据时拒绝。SELL 需要明确 confirmed 的源持仓依据。
报价后再次检查时效；受保护最小输出取滑点下限和缩放源订单最低输出的较大值。
输出仅为 `decision_checks_passed`，不预留、不签名、不广播，也不授予完整提前资格。

新增 `source_position.py` 与 Store 防护：intent 买入 lot 的聪明钱持仓依据标 pending，
不把预计输出写成实际 source_position 数量，也不让其参与比例卖出。显式核对接口仅接受同链、
同源 tx、同订单、同聪明钱钱包的严格成功证据，以实际到账量补入依据；失败/资产不匹配标记
source_failed/source_mismatch，本地已买资产和投入本金保持不变。核对前已有本地退出则转人工
复核，不猜剩余源持仓。此处 confirmed 表示证据依据已核对，不代表 L1 finality。

接入边界：上述决策和核对接口尚未挂接 monitor，构建器/签名器的严格阶段门槛没有放宽。
上线前仍需完成类型贯穿 unsigned 构建、严格后备共享 operation claim、源严格证据先于本地
成交时的补核对、后续重组对已确认依据的撤销，以及最终风险清单；不能直接打开提前实盘。
本轮新增 14 项离线测试，全量 343 项通过（含可选 EVM），pip check / diff check 通过。
未新增 Feed 连接、执行生产迁移、重启后台、修改实盘参数或发送交易；没有实盘延时改善结论。

## 2026-09-14 下一步：同一 Feed 连接的内部证据队列接入（默认关闭）

新增 `early_feed_lane.py`、迁移 `010_early_feed_jobs.sql`。monitor 原有 `receive()` 仍是唯一
WebSocket 接收器；候选写入 candidates 后，同时唤醒原回执 dispatcher 并把副本交给内部队列。
没有第二个订阅或新进程，也不需要同时运行独立 observe_early_feed.py。
`monitor/run --early-feed-evidence` 仅显式启用识别/归属证据采集，默认 false，不启用提前交易。
生产 PaperEngine 尚不消费该队列结果，故目前严格分支仍是唯一实盘触发路径。

队列默认容量 32、2 个 worker，不等待回执处理；队列满不阻塞接收，记录严格后备原因。
来源必须是 fresh Feed，入队、处理前后复查 3 秒窗口及 Feed 健康。候选解析/语义先过，再并行
获取所需订单与固定 blockHash 代码证据；过期 permit 不请求订单。收币/字节匹配不构成买入证据。
HTTP 使用有界底层客户端；不因 3 秒窗口到期就不断取消线程并创建替代请求，晚到结果标过期。
新增只读 Relay/RPC 请求，但没有新增 Feed 订阅；这些客户端当前仍共享现有 RPC 并发限制，
订单 single-flight/成功响应复用尚待接入，不能宣称不会遇到服务端限流。

early_feed_jobs 保存 queued/done/expired/failed/interrupted、接收/更新时间和结果快照；候选已
在原 candidates 表持久化，无需另存 raw signed bytes。入队证据写失败不会撤销原候选。
结束或重启将未完成工作标 interrupted，不自动重放到未来交易执行入口；严格回执重试保持原样。
结果的 `recognized_intent` 只表示解析/新鲜度/归属/所需部署证据通过，始终 `copy_eligible=false`；
不能把它当成预算、市场、预检、签名或完整提前资格通过。失败仅记录异常类型，不泄露端点。

新增 13 项离线测试；模拟 monitor 集成确认 connect 只调用一次，回执等待时证据队列能先完成。
全量 329 项测试通过（含可选 EVM），pip check / diff check 通过。未实际联网监听、执行生产迁移、
修改后台启动参数、重启进程或交易。下一步是已验证意向贯穿策略/构建，以及源持仓核对后接执行；
届时必须同时给严格后备接 operation claim，不能只打开提前分支。

## 2026-09-14 下一步：持久试运行门禁完成（未启动试运行）

新增 `early_trial.py`、迁移 `009_early_trials.sql`：固定 24 小时、最多 100 个提前广播名额，
明确绑定 follower 与 relationship 集合。没有默认 trial，也未为生产关系设置 trial ID。
显式 `start_early_trial` 重复同 ID 只读取原窗口/计数，不重置、不延长；scope 改变拒绝，同 follower
更换 ID 也拒绝，避免重启或自动重建变相续期。此 API 本身不是实盘授权，尚无自动启动入口。

带 `early_trial_id` 的提案必须同时拥有 `copy_operation_order_id`，且 source_behavior 为 BUY/SELL。
预算/持仓预留前、执行/授权准备前、签名前、最终复核、广播去重事务及广播 RPC 前分别检查。
广播去重标记、trial operation 记录和名额递增在同一事务内完成；失败全回滚。
第 100 个名额允许发送，第 101 个被拒；普通严格后备不带 trial ID，不受试运行到期影响，仍受
同订单去重与原有安全门禁。已排队任务不能凭入队时的有效状态在到期后广播。

计数口径：`consumed_slots` 是“可能已广播操作的保守上界”，在 RPC 调用前持久化。
如果随后崩溃、超时或最终门禁拒绝，名额不自动退还，避免结果未知时超限；因此不等于实际 RPC
成功数或链上成交数。实际广播成功、提前于严格证据发送和成交数仍需后续时间线/归因单列。
approval 不从此入口计数，重复发送不会增加名额，普通误跟率没有自动停止阈值。

停止 trial 仅阻止新增提前发送，不取消在途订单、不释放其预算、不清仓。名额分配后如 trial
被停止，最终广播复核仍拒绝。时间到期使用持久 expires_at 判断，重启无须重新启动倒计时任务。
SQLite/MySQL 迁移包含两张 trial 表；已有 trial 记录而缺配套表时拒绝迁移，避免遗失状态。

新增 15 项本地测试，覆盖 100 次边界、最后名额并发、精确到期、事务故障、重启、scope、停止后
在途保护及严格后备接手；既有广播/账本迁移测试补到期和 trial 窗口保留检查。
全量 316 项测试通过（含可选 EVM）；未执行生产 MySQL 迁移，未修改生产配置/预算、重启或交易。
下一步仍是独立 Feed 队列、已验证意向贯穿决策/构建及源持仓核对；本节不代表提前实盘已上线。

## 2026-09-14 下一步：原子操作占用与广播去重基础完成（未启用）

新增 `copy_operation.py` 与迁移 `008_copy_operation_claims.sql`。稳定键与离线 Candidate
一致：chain + smart wallet + Relay orderId，再绑定 relationship + follower，不包含 stage、
tx hash 或配置版本。同订单换包装交易、Feed/严格分支切换、配置更新都不能绕开同一操作占用。
这只是去重，不是订单真实性或提前实盘授权。

BUY/SELL 预算/lot 预留事务内先领取 operation；失败一起回滚。使用 proposal 归因中的
`copy_operation_order_id` 显式接入；旧 proposal 不访问新表，生产 PaperEngine 尚不设置此字段。
因此当前没有启用提前模式，也没有改变旧关系的去重语义。上线前还必须完成两分支统一接入、
已有订单的占用衔接与审计，不能只为提前分支设置字段而遗漏严格后备。

已接入的 CLI 发送路径对带此字段的 proposal 在广播前原子写 `broadcast_attempted`；重复发送
请求直接拒绝，重启不清除。网络失败/超时不释放占用。只有从未创建 execution plan 的取消，才
自动释放给严格后备；曾创建 plan 的取消保守保留占用，不能自动认定签名 bytes 从未离开进程。
链上已核实 revert 可释放预算，但不释放操作，避免严格分支重复跟单。仍在途/未知结果禁止通过
普通 proposal cancel 释放预算。后续可另加经验证的未广播 plan 交接，不能绕过此保护。

SQLite 自动兼容建表；生产 MySQL 的 008 尚未执行。SQLite→MySQL 迁移现保留占用记录并逐列
核对冲突；旧数据库无此表且无已接入 proposal 时仍可迁移，有已接入 proposal 却缺表则拒绝。
没有迁移生产数据、修改关系/额度、重启服务、读取真实密钥或发送交易。

验证：新增 15 项本地测试，包含双连接并发、BUY/SELL 回滚、后备接手、重启、未知广播、revert
及旧配置兼容；既有迁移测试增加非空占用表的幂等/冲突核对。全量 301 项通过（含可选 EVM），
`pip check` 与 `git diff --check` 通过。未验证真实 MySQL 并发，不能把 SQLite 结果替代该项。
下一步：持久试运行时间/次数门禁，再接独立 Feed 队列与已验证意向、源持仓核对及归因时间线。

## 2026-09-14 延时优化实现进度：基础链路已改，提前实盘尚未接入

本轮已修改代码，未改业务库、关系配置、预算或后台进程，未访问真实密钥或发送交易。
careful 安全检查约束生产操作；不能把下面的离线测试结果当成已部署或现场延时改善。

- 提前订单归属规则升为 `early-intent-offline-v3`，目标链仅接受整数 4663、字符串 `4663`、
  精确小写 `robinhood`，归因保留原始值和规范化值；浮点、其他链及模糊别名均拒绝。
- `RelayPublicClient.lookup_by_order` 优先用解析出的 request hint 查询 `id`，否则查 `orderId`；
  要求唯一结果并复核 orderId/requestId，仍由独立归属规则核对付款/收款/币种。独立影子采集已
  切换此入口，不再等目标交易 hash 被 API 索引。生产提前队列尚未接入，订单请求合并仍待实现。
- 配置与 PaperEngine 支持 `execution_providers=["kyber"]`；BUY/SELL 跳过本地路线查询。
  旧配置仍默认 local，已有 local/kyber 顺序不自动修改。显式聚合器选择优先于源本地路线。
- Kyber-only live 策略的每次处理获得独立的操作/钱包/配置绑定报价上下文；完整金额与参考量
  routes 并行，并行读取 Gas。未过期时决策、准备、签名、复核复用报价，build 复用完整金额 route。
  普通无等待路径的离线合同测试为 2 次 routes + 1 次 build；余额、allowance、nonce、预算、配置、
  exact-calldata 模拟仍重复核对。过期/金额或信号变化重新查询，不保证所有交易都只有三次请求。
- 新日志 `execution_quote_requests` 记录 routes/build 尝试数、报价复用与刷新数。报价时效不被
  build/复核重置；补参考报价过期检查及准备、签名前、广播复核完成后和最终数据库门禁后的检查。

尚未完成的已批准方案（不要误报为上线）：

1. 持久 Feed 独立队列与新的已验证意向类型，贯穿动态目标、金额、构建、签名门禁；不伪造
   execution success。代码/账户短时快照与订单 single-flight；超过 3 秒转严格证据后备。
2. Feed/严格证据共用、跨配置版本的 operation claim，在同一事务内预留预算/持仓并创建任务；
   已尝试广播或状态未知不能再次跟单，重启不得盲目重发。
3. 我方实际成交独立结算；源持仓基数只由严格证据确认，未知基数不能参与比例跟卖；误跟/未知
   归因与各阶段真实时间持久化，不能用历史签名时间代替广播时间。
4. 持久的 24 小时或 100 次提前分支广播限额（先到为准，BUY/SELL计入，approve/重复不计），
   到期回退严格证据，普通误跟不自动停机、不额外清仓；关键系统故障仍停新单。
5. 核对最新运行实例、账本/余额/nonce 和风险清单，完成新增端到端故障回归后才能切换实盘。

已批准试运行范围为关系 1、2、3、4、5、9，以及 follower
`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`；金额、额度、滑点和 Gas 限制沿用，禁止重置预算。
本节记录批准范围，不代表这些提前模式已启用。试运行开始时间和广播计数尚不存在。
本轮 286 项测试全部通过（使用既有临时目录加载 12 项可选 EVM 测试依赖），`pip check` 和
`git diff --check` 通过。生产虚拟环境依赖未修改。

## 2026-09-14 已保存上下文重建与统计口径纠正

用户指出历史交易已保存，不能仅因空 snapshots 就要求重新采集。核查确认旧导入器硬编码
`snapshots={}`，此前“缺当时证据”的表述未区分未导入与未保存，本节纠正此口径。
新增 `early_history.py`、`replay_early_feed.py --reconstruct-context --mysql --log ...`。
同一只读业务事务读取固定队列八类表；原日志以固定前缀/哈希保留，另核对五个公开归档。
不读当前 relationship/钱包密钥，不用可变余额或 signals.updated_at 反推历史。

145 Feed 的解析通过仍 116 BUY + 18 SELL；结合原始 stale 标记为 116 + 17 = 133/145。
26 SELL 旧 Feed 决策全部已存在、全部 asset_not_allowed。145 笔报价均恢复，但均晚于 Feed 接收；
BUY/SELL 首次完整金额报价完成的中位耗时为 3.805/1.765 秒，并非节省量。
119 BUY 订单归因摘要确实保存，但缺这批订单的完整原始响应/时点；26 SELL 有验签输入和早期身份断言，
无完整历史账户快照。预检、预算预留和 lot 记录存在，但不等于提前时点的完整策略/持仓状态。
市场包保守以不可变记录持久化时间作为可用上界，保留内部 quote 时间；未填造任何其余快照。

报告 `docs/feedback/early_feed_context_replay_2026-09-14.json`，说明见 HOWTO_REPLAY_EARLY_FEED。
完整决策仍无法证明，不是证明没有交易可以前移。接下来针对订单归属、账户/部署预取与动态目标
门禁接入，并行优化聚合器；不要再把历史导入器遗漏笼统称作数据库无数据。
新增 13 tests，全量 274 通过（含 12 EVM）；careful 约束下生产只读，未重启、签名或修改配置。

以下各节为当时的历史状态；关于证据是否保存，以本节实际上下文核查为准。

## 2026-09-14 race 固定字节码适配与历史核验（v2）

公开查询已恢复。取得 chain 4663 包装器完整运行代码，与样本 A 历史块及 Base 同地址代码匹配；
没有取得验证源码，不称源码级审计。证据 `data/relay_race_runtime_2026-09-14.json`。
`relay_race.py` 固定地址/4721 bytes/Keccak，解释九个参数、路线试跑/回退及最低输出；
12 个 Py-EVM 内存字节码测试覆盖回滚、下限、失败回退、伪造试跑错误、退款/既有库存及日志隔离。
依赖只装临时目录，生产 `.venv` 未改变；`requirements-race-tests.txt` 为可选复现依赖。

`early_intent.py` v2 支持该版本 envelope，但内层未知路线仍未知。`early_replay.py` 新增独立
deployment 门禁，必须有当时取得且实际代码重算哈希匹配的快照；代码缺失/变更/未来/过期均拒绝。
`early_shadow.py` race BUY 并行采集该快照；没有接入生产 monitor，也未增加签名或交易执行。
`replay_early_feed.py --audit-race-code` 只做事后代码审计，不补早期快照。`race_audit.py` 最多 256 块、
2 并发、按 blockHash requireCanonical 查 code，结果与提前结果分开。

固定队列 + 对照 381 条无输入错误：Feed BUY 语义 116/119（原 7），SELL 18/26（不变）；
120 个历史块代码全匹配，含 109 Feed 包装 BUY 和 11 backfill。225 个对照仍无候选，
其中 100 个未定性不能视作已证明负例。119 BUY 仍缺当时订单，109 包装 BUY 缺当时代码快照，
26 SELL 缺当时账户快照，不能称历史提前资格通过。报告 `docs/feedback/early_feed_race_replay_2026-09-14.json`。

全量 261 tests 通过（含 12 可选 EVM tests），默认 discover 未装测试依赖会跳过这 12 项。
一次 60 秒独立只读采集收到 1186 帧、1 笔尚不支持的 Multicall3，没有 race 候选，不能报现场资格或提速。
原始 JSONL `docs/feedback/early-shadow-race-smoke-2026-09-14.jsonl`；正常停止，不改业务库/配置或后台进程。
下一步：取得足量现场订单/代码可见时间，补市场/准备快照；直接 0x 的 11 个路径缺口与聚合器去重仍未完成。

以下同日早期记录保留当时状态，包装规则以本节及 EARLY_FEED_REFERENCE 为准。

## 2026-09-14 独立 Feed 影子采集首版（未现场启用）

新增 `early_shadow.py`、`early_shadow_mysql.py`、`observe_early_feed.py` 及 JSONL 汇总命令。
默认不联网，显式启用后只读取 Feed/RPC/Relay/业务 MySQL；不构造 Store、不连接私钥库、不交易。
候选有界排队，订单/账户与业务快照并行采集，早期结果冻结，后续严格证据单独核对。
范围仅识别/归属及策略/预算/lot 快照；市场、构建和签名前预检尚未采集，不能说提前跟单已上线。
配置解析抽出纯函数；业务库连接新增可选 read_timeout，原调用默认 10 秒保持不变。

包装 `0x998b5942` 的公开 selector 查询得到 race(...)，本地哈希验证一致；增加内层路线元数据。
但无法据此验证目标部署、最低输出和路线选择；109 笔语义阻断未解除。进一步公开源码查询遇到
审批服务容量错误，本轮没有获取目标运行字节码/匹配源码，因此没有捏造验证结论。
测试与使用边界见 [操作说明](HOWTO_REPLAY_EARLY_FEED.md#独立实时影子采集首版识别与归属)。
下一步仍需补目标合约证明，现场采集真实订单可见时间，再补报价/准备采集和统一历史覆盖回归。
本轮未运行现场影子进程、未修改后台配置或重启服务、未优化生产聚合器请求数。
新增 17 项离线测试，全量 242 项通过；默认关闭命令确认不联网，固定公开 BUY/SELL 回放结果未放宽。

## 2026-09-14 Feed 提前资格离线实现

新增 `early_intent.py`、`early_replay.py` 与 `scripts/replay_early_feed.py`，不接入生产 monitor。
覆盖三种 Relay Permit2 BUY 结构、账户 Relay SELL、本地 v0.8 UserOp 验签、带时点的订单归属、
动态目标/金额/lot/预算/报价检查和准备快照核对。包装及 0x 未确认语义继续阻止提前资格。
源码依据、完整快照契约和限制见 [EARLY_FEED_REFERENCE](EARLY_FEED_REFERENCE.md)。

固定 156 笔已有跟单记录：145 Feed（119 BUY、26 SELL）均能解析候选；语义层分别通过 7 和 18 笔，
26 SELL 签名全部验证成功。119 BUY 缺当时订单快照，26 SELL 缺当时委托快照，不能报历史提前通过。
原始日志纠正 2 个 stale SELL；11 个 backfill 不计实时覆盖率。125 个非交易/失败非交易对照及
100 个未定性对照没有候选。摘要保存在 `docs/feedback/early_feed_replay_2026-09-14.json`。

新增 25 项测试，全量 225 项通过。未重启进程、改关系/预算、迁移数据库、访问真实密钥或广播。
运行路径的请求数和延时尚未改变。下一步：确认包装语义、补 0x 内层、采集真实时点影子快照；
之后才接入生产任务/归因/幂等事务及独立的聚合器重复请求优化，启用仍需风险清单与单独批准。
参见 [回放操作与结果](HOWTO_REPLAY_EARLY_FEED.md)。

## 2026-09-14 流程与延时只读复核

当前完整流程统一维护在 [copy_trade_flow.md](copy_trade_flow.md)，旧的
[COPYTRADING_FLOW.md](COPYTRADING_FLOW.md) 已改为入口，避免两份正文相互过期。
基线 `c27810c`；运行证据见 [flow_audit_2026-09-14.json](feedback/flow_audit_2026-09-14.json)。
核对了两个候选 worker 等待执行到广播、Relay 空订单重试放大、四轮报价外额外一次 Kyber routes、
同步 MySQL 与同 follower 钱包锁等延时因素，补充了元数据、识别/排除条件和分阶段优化建议。
最近三笔成功 BUY 的 Feed 接收至我方入块约 11.314、12.222、8.412 秒；计时边界及秒级区块时间
误差在新文档中说明，不能把签名持久化时间当广播时间。

本次只改文档并做只读业务库/RPC查询和离线测试；未修改交易代码、配置或后台进程，未访问私钥库、
签名或广播。196 项离线单测通过。下文保留历史里程碑，不能将早期“无实盘代码”描述当作当前状态。

新服务器接手时先按 [SERVER_HANDOFF.md](SERVER_HANDOFF.md) 复现功能基线，
再推进后续只读开发。该文包含测试命令、验收口径、开发优先级和可复制提示词。
已推送的初始代码基线为 `main / 67c86b5`，以服务器实际 HEAD 为准，不要重置后续改动。
下文 `/home/jelly/...` 是原开发机路径；服务器应使用自己的仓库路径，不依赖旧工程目录。
当前配置驱动的信号→动作说明见 [COPYTRADING_FLOW.md](COPYTRADING_FLOW.md)。
67 地址的 2026-09-01 至 2026-09-12 路径统计见
[WATCHLIST_ROUTE_ANALYSIS_2026-09-12.md](WATCHLIST_ROUTE_ANALYSIS_2026-09-12.md)。

## 用户目标与已确认的设计方向

用户要从监控新币转为监控聪明钱地址在 Robinhood Chain 上的真实买卖，再进行跟单。
项目必须与原 `/home/jelly/applet/fomo_sniper` 同级，不能放在它的子目录里。
新工程：`/home/jelly/applet/smart_money_copytrader`。

特别重视排除空投：一次向几百个钱包分发不能伪装成大量共同买入。
4 种模式只是执行入口/身份归属；真正的行为另分 BUY/SELL/CLAIM/TRANSFER/DEPOSIT 等。
纯分发可提前短路；claim + swap 必须保留后面的 swap，不能丢整个批次。
Feed 是已排序交易，领先 RPC 不意味着可以抢在已排序目标交易之前。

## 已完成

- 独立 Python 包、CLI、固定直接依赖、离线样本、测试和完整方案。
- Feed JSON/Base64/Nitro 解包、legacy/type 1/2/4 验签恢复发送者、持续新鲜度/缺口保护。
- 导入 67 地址，支持 Simple7702Account 和 MetaMask 委托账户的已知批量布局。
- EntryPoint/4337 按 sender 解包；回执按 BeforeExecution 和 UserOperationEvent 限定日志范围。
- Relay ApprovalProxy/Router 部分外包装及 Depository；已知 CLAIM、TRANSFER、APPROVAL、LP 分类。
- 部分 V2/V3/Universal Router，新版 V4 单跳参数包含 minHopPriceX36（真实样本已验证）。
- 分发汇总、第三方入账候选、SQLite 幂等记录、回执状态分层。
- 样本中 CRIBS 买/卖已通过 pool/hook、settlement、交易级原生币 state-diff 和 Gas 分离，达到
  `swap_evidenced`；该状态仍不是 finality、盈利承诺或跟单许可。
- 实时只读 smoke 已接通 feed 和 RPC，成功获取一笔外部交付候选的回执；没有发送交易。
- 2026-09-11 新服务器基线和首个 P0 小步见
  `SERVER_VALIDATION_2026-09-11.md`：候选已改为先持久化再进入有界队列，支持重启恢复和
  最多 8 次持久回执退避；关键零值 counters、候选状态与阶段延迟分位数已进入健康日志。
  当前共 177 项测试。保守分类和 `copy_eligible=false` 均未改变。
- 意向、执行、规范链状态已拆为独立字段，Feed 来源单独标记；第三方入账不归属为目标意向。
  SQLite 已有独立 `canonical_l2` 区块游标。RPC safe-head 扫描器已完成首版：首次锚定而不
  扫全链，之后有界逐块续扫，补抓标为 backfill/fresh=false；父哈希不连续时停止。显式重组
  回滚已完成首版：最多回查 64 块找共同祖先，孤块信号只改 canonical_status=orphaned，
  原意向和历史执行结果保留，相关候选重新核对；超过深度则停止。`.env` 只加载 RPC/Feed
  两个键，忽略钱包和私钥变量。
- RPC 允许列表新增只读 `eth_getLogs`。补洞按单个 safe-head 区块查询固定 ERC-20 Transfer
  topic 和 67 个 recipient topic；它只扩大被动入账候选，不把收币判断为 BUY。每区块最多
  接受 10,000 条匹配日志，交易必须存在于同一完整区块响应中。
- V2/V3 单跳以及带完整 address[] 的 V2 多跳现在按回执区块验证 factory 合约代码、
  factory 映射出的 pool、pool 合约代码和 token0/token1；只有对应池 Swap 日志数量精确匹配且
  目标钱包 ERC-20 收支闭环才到 `swap_evidenced`。V3 bytes path 已保存按实际执行顺序排列的
  每跳 token/fee；付费 RPC 已实测 WETH/USDG 的 V2 与 V3 fee=500 池验证通过。
- V4 单跳已解析 `SETTLE/SETTLE_ALL/TAKE/TAKE_ALL`，校验 PoolKey/PoolManager 和已登记 hook
  的历史代码哈希，并要求输入 payer 与输出 recipient 唯一归属目标钱包。受限的只读
  `prestateTracer diffMode` 按交易计算钱包原生余额差；目标钱包为外层 sender 时用 receipt Gas
  单独剥离。两笔真实 V4 样本已闭环到 `swap_evidenced`，但仍 `copy_eligible=false`。
- Depository calldata 的 orderId 必须与同一 UserOperation 范围内的链上存款事件逐字段一致后
  才持久化为 `source_deposit_evidenced`。订单证据表只允许相同 orderId 且相同钱包的显式交付
  证据关联；仅凭 recipient 收币不会关联。Relay 公开 requests/v2 已按真实 orderId 找到不同
  requestId、源交易和 Solana outTx，形成一条真实 API 证据关联；目标链交易尚未用 Solana RPC
  独立复核，因此状态明确为 API reported，而不是链上最终确认。
- 实时意向阶段仍用 latest 委托代码提供低延迟观察；回执取得后使用受限 prestate tracer 获取
  该笔交易执行前的账户 code 并重解码。trace 缺账户或遇到未登记实现时不沿用 latest 猜测；
  RPC 失败进入持久重试。真实 UserOperation 已验证 Simple7702Account prestate code。
- safe-head 扫描命中与 receipt inclusion 哈希一致时，信号升级为 `safe_head_confirmed`，证据
  明确写为“hash 已复核但不是 L1 finality”；重组仍改为 `orphaned` 并删除孤块 Solver 执行证据。
- bundled/UserOperation 不能使用外层交易 Gas 纠正钱包余额。只有 V4 钱包原生余额变化与匹配
  poolId 的 Swap 事件原生币绝对金额一致才允许闭环；hook/Gas 导致不一致时继续 review。
  健康日志新增 pool verification 与 native trace 的独立延迟分位数。
- 60 秒真实监听捕获一笔目标 UserOperation 调用 WETH `deposit()`；现按固定 WETH 地址和 selector
  分类为 `WRAP_NATIVE`，回执要求 call value 与钱包 WETH 净入账完全相同。它不是 BUY，保持
  `copy_eligible=false`。同样支持严格的 WETH `withdraw(uint256)` 为 `UNWRAP_WETH`，不是 SELL。
- health/monitor_finished 只按回执后的最终信号统计 receipt_signals、UNKNOWN、needs_review、
  swap_evidenced 和 unknown_fraction；低延迟 intent 输出单列，避免同一事件两阶段重复计算覆盖率。
- 自动重组回查超过 64 块会继续安全停扫；新增显式 `reconcile-reorg --max-depth 65..100000`
  运维入口，用 RPC 把本地保存的每个候选祖先哈希与规范链逐块比较，只有找到共同祖先才原子
  撤销其后的规范状态并重排候选。没有可信共同祖先时数据库游标保持不动。

## 明确未完成，不能误报为已上线跟单

M1 不读取私钥，不签名、不广播，copy_eligible 恒为 false。
完整聚合器、V3/V4 多跳真实样本、bundled/UserOp 原生币 Gas 归属、
Solver 目标链独立复核尚未实现。
纸面报价、PnL、订单/仓位/预算预留系统已经实现；生产交易执行器仍不存在。
未知输入不会被猜成成交。真实样本 UNKNOWN 是支持边界，不是没有交易。

2026-09-12 dRPC 窗口扫描发现 5,248 个主动交换候选组：3,177 个经过 0x、1,992 个经过
KyberSwap，当前 OKX Router 对观察钱包为 0，直接池调用为 0。最常见的 5,123 组实际是
`Token -> 0x/Kyber -> USDG -> Relay Depository order`，其中 5,122 个回执存款事件逐字段匹配，
1 个 orderId 不匹配继续复核。名称启发式的 meme 子集 1,649 组中有 1,627 组走该 Relay
订单路径。源信号适配应先补 Relay `0x73b7bb2f`、Depository `0x5a1ee3ac`、0x
`0x2213bc0b` 和 Kyber `0xe21fd0e9`；方案 B 的跟单执行端仍可独立使用 OKX 重新报价。
Fomo 官方条款和公开 Web `/swaps/v2` 返回结构也确认 Robinhood Token 使用 Relay 基础设施；
这不表示 Relay 是唯一成交聚合器，目标 UserOperation 的实际内层成交仍主要归属 0x/Kyber。

## 下次开发建议：先把 M2 的确认链路做完整

### 2026-09-12 用户确认的精简 MVP 范围

后续按两个顺序 Goal 交付，不以接齐所有聚合器作为首次可用门槛：

1. **Goal 1：只读信号 MVP**。保留现有 V2/V3/V4/Universal Router 等支持路径；只新增当前
   观察名单的主路径 Fomo/Relay 外层与 0x、Kyber 内层识别。必须完成目标 UserOperation
   归属、Relay 卖出订单闭环，以及被动入账的最小 Solver 交付关联；单纯收币不能判为 BUY。
   复用并回归现有游标、补洞、重试和重组撤销。GMGN、1inch、OKX 源解析和未知 Router 延后，
   但已有安全检查和负例必须保留。Goal 1 全程 `copy_eligible=false`。
2. **Goal 2：跟单 MVP**。Goal 1 验收后再创建；跟单钱包使用 OKX 取得自己的实时报价，不复用
   聪明钱 Relay/0x/Kyber calldata。先接现有 MySQL 策略、额度、周期、lot 与归因账本，进行实时
   纸面跟单和故障恢复验收。签名、广播和小额主网试单仍属于需单独明确授权及风险清单通过的
   M3，不因“精简上线测试”自动开启。

Goal 1 的首次可用门槛不是路由全覆盖，而是主要 Fomo 买卖能被保守识别、未知路径不误跟、
重启/断线不静默漏信号，并能给 Goal 2 输出幂等且可审计的确认信号。

1. 阅读 COPYTRADING_PLAN.md，运行 tests 和 replay。
2. 补 V3/V4 多跳真实样本和原生币完整资金流对应。
3. 为 ETH 净流接只读 trace 或可验证状态差分，分开 Gas、退款与实际成交。
4. 给第三方入账添加区块/Transfer 兜底和订单关联，保守处理未证明的购买关系。
5. 候选持久化、回压不静默丢失、有界回执重试、独立区块游标、断线补洞与显式重组撤销
   已完成首版；深重组已有显式人工恢复入口，仍需生产监控决定何时调用。Feed sequence 不能冒充区块号。
6. 补完以上后再做实时可得报价和纸面跟单，不能用目标历史成交价代替自己的报价。

用户已为只读开发配置 RPC 端点，并已确认配置库与独立 key MySQL 的简单多数据源方案；项目仍
不得索取或导入真实私钥。实盘预算最终值、止损、主网密钥读取、签名和广播尚未授权，必须在
风险证据审阅后由用户再次明确确认。

## 已确认的跟单需求（M2 纸面已实现，M3 仍未授权）

- 触发点支持 Feed 意向、RPC 回执成功和 `swap_evidenced` 三档；默认证据模式，Feed 先影子模拟，
  所有模式都必须重新取得当时可用报价并通过风控。
- 每个聪明钱钱包可选择按其已验证实际输入金额的比例跟单，或按固定金额跟单。比例模式不估算
  聪明钱总资产；无法闭环实际输入金额时不生成 proposal。结果继续受剩余额度及其他风控上限裁剪。
- 每个聪明钱钱包设置独立的手动周期累计投入上限；未回收买入本金、在途和预留共同占用，
  数据库事务防止并发超额。跟卖成交后按对应 lot 的已卖出本金释放额度，部分卖出按比例释放。
- 额度按输入资产分桶：每个聪明钱首版分别配置 USDG 和合并的 ETH/WETH 桶，wrap/unwrap 不新增
  额度；未配置桶的输入资产拒绝跟单。卖出只按 lot 的原始投入本金恢复原桶，所得币种和盈亏不
  放大或缩小恢复额。可选组合 USD 风险上限必须使用新鲜报价，报价缺失时禁止扩大风险。
- 额度周期由用户手动管理；重启时明确选择沿用上次周期或新建重置周期。重置不删除历史，
  在途状态不明时禁止重置。
- 成交记录必须保存聪明钱地址/标签、源 signal/tx、触发节点、时间、源金额、我方报价/成交、
  成本、额度周期和策略版本，用于后续收益归因。

上述金额、额度和独立私钥数据库产品口径已经用户确认。当前已有严格交易构建、离线测试签名、
重启恢复及无广播的最终复核，但没有主网签名或广播能力；最终实盘授权仍需另行明确确认。

纸面跟单 Goal 已于 2026-09-12 开始。第一步已新增三档触发/金额策略 helper，以及手动额度周期、
USDG/ETH_WETH 分桶、事务化 proposal/reservation、取消释放、纸面 BUY order/fill/position lot
归因骨架；详情见 `SERVER_VALIDATION_2026-09-12.md`。随后已增加固定区块的真实 V2/V3/V4
单跳报价、同区块参考报价、年龄/偏离/冲击/slippage/Gas 门控、PaperEngine 决策持久化，以及
SELL lot 预留、部分卖出本金恢复和原始本金资产 realized PnL。组合 USD 汇总和跨资产 Gas
折算仍未实现，但原始本金币种的 realized/unrealized PnL 已可审计。PaperEngine
现已支持按聪明钱实际卖出 token 比例或固定 token 数量创建 SELL 提案；Store 独立核验 BUY
输入资产或 SELL 回款资产与额度桶一致，并且只预留同一聪明钱归因的持仓 lot。
严格 JSON 配置、显式周期 reuse/reset、三档主/影子接入和二次报价 paper fill 已接入 monitor；
影子档不占额度，主档二次报价低于首次 minOut 或风控失败会取消并释放。reuse 启动会恢复同一
策略/主档下的 reserved proposal 并重报价；缺失、孤块或移出允许范围的源信号会释放额度。
`paper-mark` 已能为 open lot 按反向原路径生成固定区块未实现 PnL 快照，Gas 保持单列。V4
多跳 Quoter calldata 已按官方 PathKey ABI 实现并回归，但仍缺目标链真实多跳样本。下一步优先
补纸面运行延迟指标、真实多跳样本和组合层汇总。
纸面配置现强制声明 `allowed_routes`，并将 V3 fee、V4 fee/tickSpacing/hook 和所有中间资产纳入
匹配；V4 hookData 也必须逐跳一致。只允许首尾资产相同但走未批准池的信号会以
`route_not_allowed` 拒绝。`paper-export` 可导出带聪明钱配置标签、决策时不可变 signal 快照和
当前 canonical 状态的完整成交归因记录。
`scripts/validate_paper_readonly.py` 已在 2026-09-12 用实际 RPC 完成 V3 WETH→USDG 的源价、
决策、二次报价 fill 和反向 mark；具体区块与原始金额见当日 SERVER_VALIDATION。
同日还完成带纸面配置的 60 秒 Feed/RPC 联合监听；该窗口零候选，因此只证明接入、补洞、健康
日志和停机，不替代实际 RPC 纸面端到端探针。

## 常用命令

```bash
cd /home/jelly/applet/smart_money_copytrader
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sm-copy replay --extra-fixture data/bulk_distribution.json
.venv/bin/sm-copy monitor --seconds 60
```

原数据在 data/，原研究见同级 fomo_sniper/docs/。原工程用户已修改 sniper/README.md，勿覆盖。

## 原会话能否继续到此目录

本机 `codex resume --help` 已核对支持 `--all` 和 `-C/--cd`。在同一机器/用户、
能访问原会话记录的情况下，可以从 CLI 选择原会话，并指定新工作目录：

```bash
codex resume --all -C /home/jelly/applet/smart_money_copytrader
```

从列表选本次会话；如果客户端提示沿用旧目录还是当前目录，选择新目录。
若已知会话 ID，可以用 `codex resume <会话ID> -C /home/jelly/applet/smart_money_copytrader`。
这是续接运行目录，不承诺桌面应用会永久把原线程移动到另一个项目分组。
当前自动化工具不能替用户切换 IDE 已打开的工作区，也没有执行会话数据库修改。

图形界面/IDE 最稳妥的替代办法：打开新目录，启动会话并发送：

> 阅读 AGENTS.md、docs/COPYTRADING_PLAN.md 和 docs/HANDOFF.md，继续完成 M2 的成交确认链路；保持只读，不发送真实交易。

新会话不会自动继承旧聊天全文，但这份交接和实际代码/样本保存了继续工作的关键上下文。

操作员启动、重启、停止和 execution 状态处置统一记录在 `docs/OPERATOR_RUNBOOK.md`。
多数据源 Goal 的逐项证据、部分验证项和待外部确认项见 `docs/GOAL_ACCEPTANCE.md`，不得只根据
测试总数判断已可上线。

## 本地双钱包链上闭环（2026-09-12）

新增独立 Anvil `local-test` profile、一次性 `LocalUSDG`/`LocalFixedRatePool` 合约和
`scripts/validate_local_copytrade.py`。脚本只接受 loopback HTTP、chain ID 31337 且客户端标识
必须为 Anvil；两个钱包地址每次随机生成，项目不生成、读取或保存其私钥，Anvil 仅在本地模拟
账户能力。当前闭环完成聪明钱 BUY、50% 跟买、聪明钱 SELL、50% 跟卖，并逐回执限定 pool、
Swap topic 与 trader 归属。它是隔离集成测试，不走 Robinhood Feed，也未接业务 MySQL 的真实
跟单关系。两个聪明钱回执现已作为 chain 31337 的 `swap_evidenced` 测试信号接入现有
signal→比例策略→额度→proposal→fill→position→realized PnL 状态机；验证 50% 跟买占用本金、
50% 跟卖只恢复对应本金，归因保留 smart/follower/relationship/source tx。测试使用独立 SQLite
artifact，不写业务 MySQL；下一步是增加可精确清理的 MySQL 测试模式，而不是将测试合约事件
当作主网受支持 Router 证据。

## 多数据源与实盘准备追加状态（2026-09-12）

配置 MySQL 的 `copy_relationships` 已支持 CSV 导入和 `--paper-mysql` 加载；独立 `key_mysql`
位于本机 `127.0.0.1:3309`，空 `wallet_keys` 表的运行账号只有指定列 SELECT。
`OfflineDatabaseSigner` 被 `offline_test` 硬门禁限制，真实密钥尚未导入。未签名执行计划已经具备
pending nonce、余额、Gas、报价年龄和目标 allowlist 的只读预检，并有 SQLite 持久 nonce
reservation；尚未接入真实执行器，RPC 广播继续拒绝。门禁进度见 `LIVE_RISK_CHECKLIST.md`。

执行准备现可为严格 allowlist 内的 V2/V3 exact-input 生成并反解验证 calldata，recipient 固定为
follower；V4 仅支持单跳 native-input 的 swap + SETTLE_ALL + TAKE_ALL，token-input/多跳拒绝。
预检同时检查 ERC-20 balance 与 Router allowance，不生成 approve。该构建器仍未连接广播，
不能视为实盘执行器。

`ExecutionPreparer` 已把 reserved proposal→二次报价/风控→calldata→只读 preflight→持久 nonce
→prepared execution plan 串联；同 proposal 重复调用或 SQLite 重启后复用同一计划，不重复
报价或占 nonce。它没有调用 key source，仍没有 signed/broadcast order。

离线签名协调器现会在签名前再次报价并复查 proposal、规范链状态、配置快照、余额、allowance、
Gas 和 pending nonce；只在 `offline_test` 下访问 key source。签名后只持久化 tx hash，raw signed
transaction 不落库。报价恶化和 nonce 前移已有拒绝回归；仍无广播入口。

只读 `ReadOnlyExecutionTracker` 已接上签名后的公开生命周期：项目只按已知 tx hash 查询 RPC，
逐字段验证 from/to/nonce/chain/type/gas/value/calldata 和 EIP-1559 fee 后，记录
`observed_pending`、规范块内 `confirmed`、`reverted` 或 `orphaned`。显式 replacement 必须替换
当前 active attempt、保持原意图与 nonce，并相对当前 attempt 提高费用；不允许借 replacement
改变收款目标或 calldata。SQLite 只保存公开交易字段和 hash，不保存 raw signed bytes。此协调器
没有广播方法，`eth_sendRawTransaction` 仍不在 RPC allowlist。

配置与账本现支持同一个 smart wallet 对应多个 follower。`PaperConfig.relationships` 保存每条
关系，`policies_for()` 按 smart wallet 返回全部关系；旧的 `wallets` 索引仍保留首条策略以兼容
只读观察范围。每条关系用 follower + relationship ID + smart wallet 派生稳定公开 ledger scope，
预算、proposal、position 和卖出本金恢复都按该 scope 隔离；proposal/decision 的存储幂等键也
加入 relationship，而 API/导出恢复真实 source event 与 smart wallet。MySQL 加载器不再限制一个
进程只能有一个 follower。完整回归已验证两个 follower 同时复制同一 smart wallet 时分别占用
各自额度，且 proposal/归因不冲突。

MySQL 每条 enabled relationship 现可独立声明 strategy version、主/影子触发点、QuotePolicy、
协议/资产/路由。加载时逐行复用严格 JSON 校验并生成该关系自己的 snapshot hash，再聚合观察
范围；monitor 为每条关系创建对应 PaperEngine、PaperExecutor，重启恢复和 position mark 也按
归因选择该关系的 allowlist 与报价策略。不同关系不再被要求与第一行全局配置一致。

`OfflineExecutionSigner` 签名前新增 signable transaction 与 `UnsignedExecutionPlan` 的严格一致性
校验，覆盖 chain ID、to、calldata、value、gas、type、maxFee 和 priority fee；nonce 继续与持久
reservation 核对，from 继续通过密钥派生地址和签名 recovery 双重核对。回归直接篡改 SQLite
transaction calldata，确认在访问签名结果前拒绝。

`scripts/validate_mysql_relationship_isolation.py` 已对本机真实 MySQL 完成进程级演练：临时插入
两个 follower 指向同一 smart wallet，分别加载 `mysql-isolation-v1/v2`、不同触发档和
100/500 bps 滑点，建立两个独立 ledger scope 与预算。结果明确输出 `copy_eligible=false`、
`live_trading=false`；finally 按本次 insert ID 删除 2 行并复查 `remaining=0`。脚本不连接
key DB 或 RPC。

`MySqlRelationshipGate` 会在每次签名前用 runtime SELECT 账号按 relationship ID 重新读取
`enabled=TRUE` 行，并核对 follower、smart wallet 和该行最新 snapshot hash。数据库禁用或策略
修改都会使旧 execution plan 失败关闭。真实 MySQL 演练已先通过两条关系的新连接复核，再由
admin 把其中一条设为 disabled，随后新连接得到 `emergency_stop_rejected=true`；最后仍按插入 ID
清理并确认 remaining=0。此门禁已接入 `OfflineExecutionSigner`，未增加广播能力。

离线密钥访问现在还受三个进程控制共同约束：execution mode 和 signing mode 必须都为
`offline_test`，emergency stop 必须显式为 0；任一缺失均拒绝。每次签名前还检查默认
`var/EXECUTION_STOP`（可通过环境指定其他路径），运行中创建 stop file 会阻止下一次访问密钥。
`require_mainnet_broadcast_enabled()` 当前无条件拒绝，所以设置虚构的 mainnet/broadcast 环境变量
也无法启用主网能力。

签名前最终检查现重新核对 paper 额度账本：BUY 必须仍有 active cycle/budget reservation，金额、
scope、reserved/invested/limit 一致；SELL 必须有同 scope 的 open lot reservations，合计 token
数量等于 proposal。额度归因还必须匹配 execution plan 的 follower/relationship。最终 quote、
risk、余额/allowance/Gas/nonce preflight、额度证据、配置快照和 sender recovery 会与 signed 状态
原子写入 `final_review_payload`；序列化前显式拒绝 private key/raw transaction 字段。

`execution_plans.plan_integrity_hash` 现覆盖 plan/proposal ID、follower、relationship、配置快照、
nonce reservation、完整 transaction/unsigned plan 和初始 preflight。Store 每次读取都会重算；
签名、tracker 和重启恢复都通过该读取路径，因此 SQLite 内容被意外改写会失败关闭。旧数据库的
NULL 行在 schema 迁移时以当时已有内容建立基线；这是完整性/损坏检测，不是对有数据库写权限
攻击者的密码学认证。

尝试进一步做真实 key MySQL 临时密钥演练时，执行审批依据 AGENTS.md“测试不得存储私钥”拒绝，
命令没有启动。对应临时脚本已删除，随后使用 runtime SELECT 只读查询确认 `wallet_keys=0`。现阶段
只保留运行时内存随机无资金密钥和模拟 DB 查询的离线 signer/recovery 测试，不绕过该安全边界。

执行构建门禁又明确要求 source 为 `swap_evidenced`、execution status success、非 orphaned、行为
为 BUY/SELL/TOKEN_SWAP、exact-input 且路径完整允许；UNKNOWN、needs_review、失败/未知执行状态
现在都有显式拒绝回归。

新增 `ReadOnlyPreBroadcastReviewer`，仅接受调用方内存中的已签名 bytes，不保存也不输出 raw。
它先匹配持久 signed tx hash、恢复 follower sender、解码 type-2 交易并逐字段匹配 immutable plan，
随后重新查询 relationship gate、预算/position reservation、实时报价/risk 和余额/allowance/Gas/
pending nonce；最后要求 network pending nonce 与签名 nonce 完全相等。返回证据固定
`broadcast_performed=false`，类中没有发送方法。篡改一个 raw byte 和 nonce 前移已有拒绝回归。

新增 CLI `execution-audit`，只读汇总 execution plan、nonce reservation 与公开 attempt，并检查
完整性损坏、reservation 缺失、身份/状态错配、signed attempt 缺失及多个 active attempts。
单条损坏不会中断整份报告；输出不含 raw transaction 或私钥，并继续明确
`copy_eligible=false`、`live_trading=false`。操作步骤见 `docs/OPERATOR_RUNBOOK.md`。
审计把 consistency 与 coverage 分开：空账本可以内部健康，但固定返回
`end_to_end_evidenced=false`；attempt 按 signed/pending/confirmed/reverted/replaced/orphaned 汇总，
只有规范链成功 receipt 才提供单样本端到端证据，避免把“没有记录”误报为完整链路通过。

execution audit 进一步逐条复算 attempt 身份：hash/nonce/from/to/chain/type/gas/value/calldata 必须
与 immutable plan 一致，原始 attempt 费用必须完全相同，replacement 必须引用同 plan 的已 replaced
parent 且相对 parent 逐级提价，final 状态必须有合法 block number/hash。正常多级生命周期保持
healthy，任一公开字段、费用或 parent 链损坏均结构化报告。

远程配置 MySQL 与 key MySQL 连接现在都要求 HOST、PORT、USER、PASSWORD、DATABASE、SSL_CA
完整显式设置；缺少任一项会在连接函数调用前失败，不会把本机 Docker 的默认账号密码或 3308/
3309 端口带到远程服务器。连接异常继续只暴露异常类型，回归用包含伪密码的服务端错误确认密码
不会进入上层异常。

配置严格校验新增零地址边界：启用配置的 smart wallet 与 follower wallet 都不得为零；CSV 的零
follower 占位行必须保持 disabled。该限制不影响 `allowed_assets`/route 中用零地址表示 native
asset。这样误启用占位行会在 monitor 加载阶段失败，而不会继续生成零地址纸面归因。

`relationships-import` 写入口也在数据库连接前拒绝零 follower 与 CSV 中的零 smart wallet。
已有 67 条 disabled 零 follower 历史占位不删除，继续保留来源；后续导入必须提供真实非零公开
跟单地址，避免新增永久不可启用的占位数据。

离线签名增加显式 `recover_signed` 重启恢复。由于 raw signed transaction 按安全要求不落库，
进程若在签名后、交给调用方前退出，可在重新打开 SQLite 后重跑 relationship/budget/quote/RPC
检查，并对 immutable transaction 确定性重签；仅当唯一 attempt 仍为 `signed` 且新 hash 等于
持久 hash 才返回 bytes。attempt 一旦被 RPC 观察或 nonce 状态推进即拒绝，恢复不能充当 replacement
或绕过 pre-broadcast reviewer。测试仍只使用内存随机无资金密钥。

`OfflineSignedExecution` 不再使用可被 `asdict()` 展开的 dataclass，而是 slots 专用容器；默认 repr
仅含公开 plan/proposal/hash/preflight，隐藏 raw 字段和值，`vars()` 也失败关闭。raw 仍可由明确访问
属性的调用方在内存中交给 reviewer，但不得记录或持久化。

新增 `key-status --wallet`，让操作员在不进入签名路径时验证独立 key MySQL 的公开元数据。SQL
仅选择 `wallet_address,enabled`，不读取私钥列；输出明确 `private_key_read=false`，并继续受三重
offline_test/stop-file 和远程 TLS 配置门禁。localhost 空库实际查询公开测试地址得到 found=false。

新增 `execution-track` CLI，把已完成的 `ReadOnlyExecutionTracker` 暴露为实际运维入口。它先验证
RPC chain ID，再按 proposal 的原始 signed hash 或显式 replacement hash 查询交易、receipt 和规范
块，只输出/持久化公开状态并固定 `broadcast_performed=false`。replacement CLI 必须同时提供新
hash 与被替换 hash，底层继续要求相同意图/nonce 及逐级提价。服务器只读检查未发现 anvil/geth/
ganache/hardhat 二进制或相关 Docker 镜像，因此本轮没有下载、安装、启动或伪称真实测试链演练。
CLI 进程级回归已从磁盘 SQLite 的 signed plan 出发，经 chain ID 检查和 RPC 模拟查询推进到
`observed_pending`，关闭后重开确认状态持久；错误链与缺失 signed plan 均在查询交易前拒绝，输出
不含 raw/private 字段。这是 CLI plumbing 证据，仍不是实际 EVM 广播证据。

## 2026-09-12 多数据源配置进展

新实盘准备 Goal 已开始。Docker MySQL 8.4 在本机绑定 `127.0.0.1:3308`；
`copy_relationships` 一行保存跟单钱包公开地址、聪明钱和唯一策略/额度。CSV 的 67 个聪明钱
已作为 `enabled=0` 的零地址占位模板导入。CLI 的 `relationships-import` 可针对真实跟单钱包
公开地址导入，`--paper-mysql` 可代替 JSON 加载启用关系。此段为 Goal 启动时记录；当前已有独立
key MySQL、离线测试签名与实盘前风控，但仍不读取真实私钥、不做主网签名或广播。

2026-09-12 用户决定将 SQLite 运行状态与收益分析账本迁移到业务 MySQL。第一步已新增
`docker/mysql/init/003_runtime_ledger.sql`：20 张 InnoDB 表覆盖 signals/candidates/chain cursor、
Solver 证据、paper 额度/提案/订单/成交/持仓/已实现盈亏/估值以及 execution nonce/plan/attempt。
原始金额和正负 PnL 继续使用十进制字符串列；私钥表不在该 schema。已在本机 MySQL 8.4 实际
应用并确认 20 张表存在。`smart_money_runtime` 对 `copy_relationships` 仍只有 SELECT，对账本表
只有 SELECT/INSERT/UPDATE/DELETE。当前应用的 `Store` 尚未切换到 MySQL，本步骤不能称为迁移
完成；下一步是实现 MySQL Store 事务适配、SQLite→MySQL 可重复迁移及双后端一致性回归。

第二步新增 `ledger-migrate --sqlite ... --confirm-source-sha256 ...`。迁移要求停写、源 SHA-256
精确匹配且无非空 WAL；目标写入在单一事务内按外键顺序进行。已有主键逐列一致时可安全重跑，
存在任何字段冲突则整笔回滚且不覆盖。单测覆盖首次插入、幂等重跑和冲突回滚。服务器 `var/`
存在多个不同阶段的历史 SQLite，而没有唯一名为 `observer.sqlite3` 的生产账本，因此尚未擅自
选择并合并其中任意一个；需要在运行时切换前明确正式源或把它们按独立归档处理。

第三步新增 `MySqlStore` 与受限 SQL 方言适配。monitor/replay、paper cycle/mark/export、reorg、
execution audit/track 和普通 export 均可显式传 `--ledger-mysql`；默认仍是 SQLite，且不存在双写
路径。MySQL 连接日常 autocommit，Store 的 `BEGIN IMMEDIATE` 区段转换为显式事务，事务内 SELECT
增加 `FOR UPDATE`，SQLite upsert/ignore 和 JSON event-id join 做受限转换。真实 MySQL 演练验证了
signal/candidate 幂等、候选领取、额度预留、BUY fill、持仓和带聪明钱 relationship 归因的收益
导出；首次演练发现 JSON/ASCII collation 冲突，修复为显式 ASCII/`ascii_bin` 后通过。每轮 UUID
测试数据均在 finally 精确删除，复查残留为 0。尚需继续覆盖规范链重组和 execution 生命周期的
真实 MySQL 写路径，才能完成后端切换验收。

后续真实 MySQL 演练已补齐剩余核心写路径：部分 SELL 从 250 token 归因 lot 卖出 100，按整数
比例释放原始本金 40，使 invested 从 100 降至 60，并保存 `realized_pnl_raw=19`；固定金额始终是
十进制字符串。规范链使用 UUID 派生且预查不存在的随机高度，验证 safe-head 信号在回退后变为
orphaned、candidate 重新可领取。execution 路径验证 nonce=7 持久预留、prepared plan、公开 signed
hash、observed_pending、confirmed 及 audit end_to_end_evidenced=true；没有密钥、签名或广播。所有
UUID 行、随机规范块和 canonical cursor 清理后综合残留为 0。接下来只剩正式运行账本源选择、
实际迁移/新库启用和 60 秒 MySQL monitor 验收，不应再称核心 Store 方法缺失。

随后已完成业务 MySQL 的 60 秒真实主网只读 monitor 切换验收（未启用 paper relationship）：
1100 feed frames、5233 decoded、4126 stale skipped、569 backfill blocks，发现并完成 5 个 candidate，
最终 pending/queued/retry/failed 均为 0；RPC、worker、backfill、frame、account code 错误及重连均为
0。5 个信号全是第三方被动入账，全部保持 needs_review/copy_eligible=false，没有误判 BUY。进程结束
后直接查询 MySQL：signals=5、copy false=5、needs_review=5、complete candidates=5、canonical cursor
=60984943、canonical blocks=570、paper fills=0、execution attempts=0。该业务 MySQL 现可作为新的
运行账本起点；服务器旧 SQLite 均为带测试名称的历史验证库，没有唯一正式生产源，继续原样保留
为归档而未混入新账本。收益数据可直接通过 `paper-export --ledger-mysql` 查询；当前为 0 是因为尚
未启用任何真实跟单关系，不是迁移失败。
依据 [官方 CLI 参考](https://developers.openai.com/codex/cli/reference/) 和本机帮助。

## 2026-09-12 精简版 Goal 1 完成

已新增 Relay + 0x/Kyber 卖出识别和严格回执闭环，以及被动入账与保存的 Relay 订单响应精确
关联后才升级 BUY 的离线逻辑。新增阶段 `relay_sell_evidenced`、`relay_buy_evidenced` 均保持
`copy_eligible=false`；`sm-copy relay-associate` 只处理本地文件和账本，不联网、不自动下单。
60+ 秒只读监听现场闭合 3 笔 Relay SELL（含 0x、Kyber），纯入账负例未判 BUY。实现、验证、
临时日志位置和剩余工作见 `docs/SERVER_VALIDATION_2026-09-12.md` 最后一节。

随后 Relay 官方 `requests/v2?hash=` 证明该入账存在唯一跨链 BUY 订单。实现允许源链付款人与
Robinhood 收币钱包不同，但分别严格绑定 request user/origin depositor 与目标 recipient/order
payment/outTx/fill/local receipt；真实正例升级为 `relay_buy_evidenced`，篡改收款人的负例拒绝。
重复关联幂等、重组撤销和跨 UserOperation 禁止拼接均已有回归。Relay 响应提示 requests/v2 将
退役，后续需兼容 v3。最终 `pip check`、153/153 unittest、13 笔 replay 和 diff check 通过；
Goal 1 已停止在只读信号层，等待用户讨论并另建 Goal 2。

## Goal 2 当前进度（2026-09-12，进行中）

Goal 2 已接入 `relay_buy_evidenced`、`relay_sell_evidenced` 两个精确触发档，并允许 0x、Kyber、
Relay Solver 作为源协议。源交易归因与跟单报价路径已经分离：聚合器信号只决定来源、方向、
资产和已验证金额；纸面执行必须从 relationship 的 `allowed_routes` 唯一选出本地 V2/V3/V4
路径，首次决策和成交前二次报价均重新选择并核对相同路径。缺路径或多条路径同时匹配均拒绝。

SELL 新增原始本金资产选择。系统先按 relationship ledger scope、token 和未被其他 proposal
预留的 lot 选择唯一可覆盖卖出量的 `principal_asset`，再用 Token→principal_asset 路径报价。
因此 ETH 出资 BUY、聪明钱卖成 USDG 时，不会把 USDG raw 与 wei 直接相减，而是纸面退出为
ETH、恢复 ETH_WETH 原始本金并以 wei 记录 realized PnL。若 USDG 与 ETH 两类 lot 都能独立覆盖
同一卖出量，则以 `attributed_principal_asset_ambiguous` 安全拒绝。`paper-mark` 同样会先恢复
聚合器 BUY 使用的本地执行路径再反转估值。全量回归当前为 157/157。

本轮 `pip check`、compileall、158 项 unittest 与 13 笔历史 replay 已通过，replay 继续保持
`copy_eligible=false`。沙箱内主网调用无响应；获准只读联网后，固定区块 paper BUY、二次报价
fill 和反向 mark 探针成功，证明此前是执行环境网络边界而非 RPC 客户端超时缺陷。随后使用临时
无私钥策略完成 60 秒 Feed/RPC/SQLite 纸面监听：11 个候选、11 个回执最终 complete，3 次临时
RPC 错误均经持久重试恢复，队列/重试/失败最终为 0。该窗口仅形成 1 条外部入账 needs_review，
没有命中临时策略的 Relay BUY/SELL，paper decision/fill 为 0；因此它证明监听与恢复健康，但不
替代真实 Relay 信号到 paper fill 的端到端样本。单元回归与固定区块探针共同覆盖该逻辑闭环。

## 2026-09-13 动态目标资产语义修正

`copy_relationships.allowed_assets` 现在解释为可信本金、结算币和中间路由币集合，不再是
可以买入的 token 白名单。只有已成功执行且达到 `swap_evidenced`、`relay_buy_evidenced` 或
`relay_sell_evidenced` 的信号，BUY 输出 token / SELL 输入 token 才作为动态目标从静态检查中
排除；未知中间币仍拒绝，feed intent、UNKNOWN、needs_review 和失败交易不会获得动态放行。
SELL 后续仍必须命中同 relationship 的已有归因 lot，不能借此卖出钱包中其他来源的 token。

`allowed_routes` 允许配置为空。直连或仅经过可信中间币的已证实 V2/V3/V4 源路径，可以跟随
新出现的 meme token，而不要求事先枚举其合约；聚合器/Relay 源在 OKX 动态构建接入完成前仍需
可执行的本地路径，否则报价阶段安全拒绝。字段名为保持 MySQL 向后兼容暂不改动，README 与流程
文档已明确新语义。

同日增加受控 `mainnet_live` 小额测试入口：独立 `MainnetBroadcaster` 没有修改 `ReadOnlyRpc`
allowlist。最初使用多组进程开关和单独风险 JSON；当前已收敛为数据库驱动的 `sm-copy run`，并在
读取 key 前和广播瞬间重新加载 enabled `mainnet_live` 行，核对 follower/relationship/config
snapshot，同时保留全局 stop file。广播器再次
恢复 raw transaction sender、校验 chain ID 和本地 hash，RPC 返回 hash 不一致即拒绝。

`sm-copy run` 固定使用 MySQL 配置/账本、Relay 关联和自动额度周期复用，当前接受恰好一条
`run_mode=mainnet_live` 关系；enabled smart wallet 自动进入监听集合。签名前重新
加载 enabled relationship，随后进行多轮报价、余额/allowance/Gas/nonce 检查、签名、复核、广播
和 180 秒公开回执跟踪。已预留/已签名状态在重启时仅报警，避免自动重发。

本入口仍缺 confirmed 回执的真实余额差分→position/PnL 结算、自动 approve 和 OKX monitor 集成，
因此只能用于人工看守的极小额主网测试，不能称为正式无人值守版本。现有本机 MySQL 已应用
`004_mainnet_live_relationships.sql`；67 条旧关系保持 `paper/disabled`，没有擅自启用任何钱包。

## 2026-09-13 Fomo/Relay 主网最小闭环准备

针对 relationship 78 的实际 Fomo BUY，monitor 可选 `--relay-auto-associate`：只对目标链被动入账
候选查询 Relay 官方公共订单，严格绑定源 payer/inTx、目标 recipient/outTx/payment 和本地 receipt。
当前真实样本源为 Solana USDC；只对登记的 `(chain_id, currency)` 映射到本地 USDG，其他跨链资产
不推断。若同一目标 receipt 有方向匹配的 V3 Swap，则在该历史区块核对 factory、token0/token1、
fee/code 后保存本地路径；若严格 Relay 被动交付 receipt 没有 Swap，则只在配置 V3 Factory 的四个
标准 fee tier 中，以 relationship 实际计划输入量做同区块直接池报价，并逐池核对 code、pair 和 fee
后择优。两者都不复用聪明钱 calldata，也不是任意多跳/多协议寻路。

统一 `evidenced` trigger 接受严格的 direct swap、Relay BUY 或 Relay SELL 三种证据，feed_intent 与
receipt_success 仍只能作为 shadow。confirmed live receipt 已补规范块重查、follower ERC-20 净差额
核验以及归因 lot/PnL/额度结算。新增 `mainnet-approve-usdg`；操作员选择将 allowance 固定为该
relationship USDG 周期总预算的200倍，软件额度仍为原上限。它拒绝无限授权、已有部分授权、
pending nonce、配置变化或任一 live gate 缺失，测试结束需要撤销。

实际 MySQL 关系仍 `enabled=0`，当前 snapshot 为
`aa11cd2486b97f3064cd5f7c744f519f3dc6c079d4dbc4e909e25fe488c2f647`。follower 公共余额已只读核对，
key 元数据已确认存在/enabled，但没有选择或输出私钥列。历史 Fomo 回补闭合 1 笔 Relay BUY 并保留
1 笔普通被动入账负例；2 USDG 实时报价/预算探针到 allowance 门禁正确停止。最终 169/169 tests、
13 笔/27 信号 replay、pip check、compileall、diff check 通过。依据项目规则，真实授权/广播仍需
完成操作员风险清单，不能因代码入口存在而自动启用。

## 2026-09-13 主网跟卖自动授权与比例语义

第一笔真实 BUY 已成功结算后，SELL 改为按来源持仓比例映射，而不是复制聪明钱的 Token raw 数量。
例如本次 smart wallet 买到 `69405773920665976786` raw，而 follower 买到
`50303912913447597330` raw；smart wallet 全卖时 follower 卖自己的全部归因 lot，smart wallet
卖50%时 follower 卖自己的50%。旧 lot 若尚无新增的 source position 字段，会从原始已保存 BUY
signal 的 `actual_output_credit_raw` 恢复基数；无法恢复时保持拒绝，不猜测。

V2/V3 SELL 的输入 Token allowance 现在由 monitor 自动管理，不再要求逐笔人工确认。它先重新验证
enabled relationship、follower/smart/snapshot，再以该 relationship ledger scope 的当前 open lot
总量作为授权上限；approve 模拟成功、规范 receipt 成功且最终 allowance 足额后，才重新报价并发送
SELL。钱包中没有对应归因 lot 的同币余额不进入授权上限。171 项单测、pip check 与 diff check
通过；实际恢复 live 前仍需核对 MySQL lot、链上两侧余额和急停状态。

同日主网 SELL 已形成完整闭环。smart wallet 的 0x + Relay 卖出不能直接提供 follower 可复用的
执行 calldata；系统从原跟买 lot 恢复此前验证过的 V3 fee=3000 路径并反向执行。follower 精确
授权并卖出全部 `50303912913447597330` raw Token，收到 `1993796` raw USDG；proposal/attempt/fill、
lot、额度和归因均已结算，链上 Token 余额与对应 V3 allowance 均为0。详细 hash、区块、PnL 和日志
见 `docs/SERVER_VALIDATION_2026-09-13.md` 的“第一笔真实 SELL 闭环”。本轮结束后 relationship 78
已禁用、`var/EXECUTION_STOP` 已恢复，未保留自动实盘运行进程。

配置语义需特别注意：`allowed_protocols` 是来源识别与本地执行协议的准入集合，不是聚合报价器的
“遍历协议列表”。0x/Relay/Kyber 只提供来源归因；本地执行仍需唯一、可验证的 V2/V3/V4 route。
当前从 Relay 目标 receipt 自动发现的仅是 V3；SELL 可复用并反转原 BUY lot 保存的 V2/V3/V4 路径。
跨多个协议主动枚举池、比较报价并选路仍是后续独立的 routing 模块工作，不能把“协议允许”误作
“已实现该协议的自动寻路”。

第二轮主网测试前又修正 allowance 阈值：`minimum_required_raw` 是本次 proposal 的输入量，
`amount_raw` 是不足时才写入链上的有界授权目标。USDG 现有 allowance 足够本次买入时不会为了恢复
到预算×200而逐笔重复 approve；meme Token 现有 allowance 足够本次部分卖出时，也不会仅因它小于
全部归因持仓而补授权。不足时仍分别补到预算×200或本 relationship 的全部归因 open position。
172/172 回归通过，relationship 78 与急停仍保持关闭。

第二轮 Fomo/Relay 主网买卖也已形成闭环。BUY 被动入账 receipt 无 Swap，系统先以 Relay 唯一订单
证明归因，再按跟单实际2 USDG发现并验证 fee=500 直接 V3 池；follower 实际买到
`9161024053399471` raw Token。smart wallet 全卖后，SELL receipt 自身验证到 fee=3000 V3 路径，
follower 按归因比例卖出全部上述 Token并收回 `1998190` raw USDG。该轮 lot 已 closed，预算
invested/reserved 均归零，realized PnL 为 `-1810` raw USDG（不含 Gas），Token 余额/allowance 为0；
执行审计4笔历史 swap attempt 全部 confirmed 且无 issue。修复后174/174单测、pip check、diff check
通过。结束状态仍为 relationship 78 disabled、stop file active、无 monitor；公开 hash、区块、Gas、
最终余额及日志见 `docs/SERVER_VALIDATION_2026-09-13.md`。

第三轮继续验证了 Kyber 来源的 Relay SELL。源 SELL 的 Token debit 与 Relay USDG deposit 均严格
闭合，但 relationship 78 初始遗漏 `kyber`，系统先按 `protocol_not_allowed` 安全拒绝且没有广播。
补入 Kyber 后 snapshot 变为
`f51aa692e2535866c6c6eae8b448a479b556e88ccf2800f223e220765a22ed58`；decision/proposal/账本 source
幂等键现同时绑定配置 snapshot，允许配置修正后安全重放同一事件并保留原拒绝记录。修复提交
`24faa62` 已推送，175/175 tests 与 `pip check` 通过。

重放没有执行 Kyber calldata，而是反转 BUY lot 中已验证的 V3 fee=10000本地路径。follower 的
Token approve 与 SELL 分别为
`0x3baa61de842a5a65d391f57557ae6c62def0962b7a9ff964389de77fe9a6a611`、
`0x4bc67f6160424387e5d2c6d03a6102ce69abf7853c6e3113f1127c2a250a4ea8`；全部归因 Token 卖出后收回
`1918286` raw USDG，lot closed，10 USDG预算全部恢复，realized PnL 为 -0.081714 USDG（不含 Gas）。
执行审计6个 plan/attempt 全部 confirmed 且无 issue，链上 Token余额/allowance 均为0。

按使用者要求，monitor 当前继续常驻于 screen
`smart_money_mainnet_live_78_kyber_retry2`，日志
`var/mainnet_live_78_20260913_kyber_retry2.log`；relationship 78 enabled，急停文件暂存为
`var/EXECUTION_STOP.continuous-kyber-20260913`。启动时必须显式传
`--watchlist data/mainnet_test_watchlist.csv`，否则默认 watchlist 不包含测试 smart wallet，进程会在
任何签名之前退出。运维接手前应重新检查 screen/PID、日志最后一条 health、执行审计、余额/nonce；
需要停止时先恢复 `var/EXECUTION_STOP`，再禁用 relationship 78 并终止精确 monitor PID。

第四轮在同一常驻进程中再次完成 Relay BUY→Kyber/Relay SELL 闭环。源 BUY
`0xf1b298eedcb00ba7bf82261ecea23e9ad8669f915025c6627808589d29693306`
经两次 RelayNotReady 持久重试后关联成功；follower 以2 USDG通过已验证 V3 fee=3000池买入
`38533793488645720533` raw Token，交易为
`0xf1db6b9611e2b6d2843449b9d0b1d6ea9e93817a7901732c0d0f373d95dc81a6`。
源全卖后，系统从唯一归因 lot 反转同一路径，先做有界 Token approve，再由
`0x05ef728f046f4269ba534eb107a2e62af664e3cf530994617b48c5efa8e30c7f`
卖出全部 follower 归因 Token并收回 `1988313` raw USDG。

本轮 realized PnL 为 -0.011687 USDG（不含 Gas），lot closed，10 USDG额度全部恢复，链上目标
Token余额/allowance均为0，latest/pending nonce均为13；执行审计8/8 confirmed且无 issue。monitor仍
运行于 `smart_money_mainnet_live_78_kyber_retry2`，relationship 78仍enabled，完整公开证据继续写入
`var/mainnet_live_78_20260913_kyber_retry2.log`。

第五轮继续完成另一组 Relay BUY→Kyber/Relay SELL。follower BUY
`0x867e5806e98d470a905b67509060f009e9cc4071892e26a5ed2434fad0bd2b0c`
以2 USDG买到 `3396809340185972048` raw Token；自动有界授权后，follower SELL
`0x61051d5f1bd056db1018237c711bf81c725c208b07bf5ae513e079225934afc5`
卖出全部归因持仓并收回 `1988018` raw USDG。realized PnL为 -0.011982 USDG（不含Gas），lot closed，
10 USDG额度全部恢复，链上目标Token余额/allowance均为0。执行审计10/10 confirmed且无issue；
monitor仍在原screen常驻，relationship 78保持enabled。

## 2026-09-13 数据库驱动启动收敛与本机停机

使用者要求日常配置只维护 `copy_relationships` 与独立库的 `wallet_keys`。实盘逐关系授权现由
`enabled=TRUE/run_mode='mainnet_live'` 以及行内确认时间共同承担；每次读取密钥和广播前都会重新
加载该行，核对 follower、relationship ID、完整 snapshot，并要求确认时间不早于行更新时间。任何
配置变化都会使旧确认失效，需在同一条数据库 UPDATE 刷新。已删除单独风险确认 JSON及 mainnet
execution/signing/broadcast/chain ID 环境开关要求，仍保留全局 `var/EXECUTION_STOP` 急停、数据库
enabled 状态、密钥 enabled 状态、配置快照、报价/余额/Gas/allowance/nonce 与链上 hash 门禁。

新增 `docker/mysql/init/006_database_live_acceptance.sql` 和固定入口 `.venv/bin/sm-copy run`：自动使用
MySQL 配置与账本、Relay 关联、复用活动额度周期，
第一次才创建周期；新增 relationship 自动初始化独立额度，修改额度保留 invested/reserved，低于已
占用值时拒绝启动。enabled relationship 中的 smart wallet 自动并入监听集合，不再另改 CSV。
固定入口现会加载全部 enabled `mainnet_live` relationship。启动时逐条检查数据库确认、配置快照
和对应 follower 的 key 元数据；任一 enabled 实盘关系不合格会整体失败关闭。同一 follower 的
allowance、nonce、签名与广播使用进程内钱包锁串行，不同 follower 可并行。同一聪明钱关联多条
关系时分别执行，单条失败会带 relationship/follower 记录且不阻断其他关系。

本机原 relationship 78 monitor 已按使用者要求停止，`var/EXECUTION_STOP` 已恢复且权限为0600；账本
审计10/10 attempts confirmed，无 pending/signed/orphaned/reverted。改造后177/177 unittest、13笔回放、
`pip check`、compileall 和 diff check 通过；未读取真实私钥、未签名或广播新交易。

## 2026-09-13 跟单流程文档 v2.0

`docs/COPYTRADING_FLOW.md` 已按提交 `20f2512` 重写为当前实现，不再把实盘流水线描述为未来工作，
也不再保留单 relationship、本机旧数据库状态和“monitor 不自动授权”等过期说明。新版从
`sm-copy run` 启动开始，覆盖全部 enabled 关系加载、逐关系门禁、多 follower 分发、同 follower
nonce/授权串行、纸面/实盘分支、有界授权、签名广播、回执结算、行为矩阵和当前代码入口；同时
明确进程内锁不能保护重复实例，以及路由、V4/Permit2、复杂结算和深重组等剩余边界。

## 2026-09-13 Kyber 聚合器执行提供方（阶段 1）与 nonce 释放修复

Mac mini 接入 6 个真实聪明钱后，7 笔已证实买入只有 1 笔可跟，其余卡在 V4 池、非本工厂池或多跳路径，
设计与证据见 `docs/AGGREGATOR_ROUTE_DESIGN.md`。本次实现其阶段 1：新增 `src/smart_money/kyber.py`
（固定官方域名、Router 白名单、顶层 calldata 反解），`copy_relationships.execution_providers`
（迁移 007，默认 `["local"]`，进入配置快照），决策引擎按提供方顺序回退，执行准备器以 Kyber 构建交易
的输出为风控基准并在签名前、广播前以 follower 身份 `eth_call` 模拟；授权 spender 白名单加入 Kyber
Router；聚合器买入的 lot 卖出时同样走 Kyber。运维手册"执行路径提供方"一节记录门禁与 SQL。

首批实盘尝试跑通了路由选择、USDG 有界授权、构建、计划与模拟，但签名前二次报价因
`adverse_price_deviation_exceeded` 被拒：这批新币在聪明钱买入后数秒内即超过 3% 偏离上限，是否放宽
`quote_policy` 由操作员决定。过程中暴露原有缺陷：签名被拒后 `prepared` 计划和 reserved nonce 残留，
后续计划比网络 pending nonce 多 1 并在广播前全部被拒。现已改为在同一进程内自动取消从未签名的计划、
以及审核阶段被拒且从未广播的已签名计划，nonce 行置为 `released` 并改为高位哨兵值（原 nonce 记录在
`final_review.released_nonce`），事件 `live_execution_abandoned` 携带原因；`copy_execution_error` 和
`paper_decisions.payload.quote_error` 现在保存具体错误文本。本机账本中 3 条残留已按此清理，审计
healthy。188/188 unittest、pip check、13 笔回放、diff check 通过。

## 2026-09-14 Relay 订单确认跨链卖出与 Pons Swap 识别

操作员截图核实两个问题：跟卖弱、回补落后。回补已改为地址过滤的范围日志扫描（c749bfa）。跟卖弱的
根因是聪明钱 `0x1cfbe3af…9be09` 在 Pons V2 池（factory `0x7ed598bc…ec7e`，池
`0x9febd871…2c0b`）卖出，该池 Swap 事件签名不在识别表，信号停在 `needs_review`
（`relay_sell_evidence_not_uniquely_closed`），而 Relay 订单显示 USDG 已桥到该聪明钱固定的 Solana
地址 `4zFEFU8g…PW3j`。操作员确认这类跨链卖出应视为聪明钱卖出。

实现：`receipts.SWAPS` 加入 Pons V2 Swap topic；`RelayPublicClient.lookup_requests_by_hash` 允许
同一哈希返回多个订单；`solver.relay_confirmed_sell` 按订单号、用户、存款来源、卖出币种数量、输入交易
逐项与本地回执比对后才升级为 `relay_sell_evidenced`；回执规则在未闭合时也记录
`relay_deposit_amount_raw` 供交叉校验；`sm-copy run` 的 receipt 工作线程对未闭合 Relay 卖出自动
查询并升级，事件 `relay_sell_order_confirmed` / `relay_sell_confirmation_rejected` /
`relay_lookup_pending`，计数器 `relay_sell_confirmed`。真实样本
`data/relay_sell_evidence_2026-09-13.json`（含另一用户的打包订单）作为测试夹具。196/196 unittest。
