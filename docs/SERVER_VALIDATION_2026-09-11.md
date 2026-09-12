# 服务器验证记录（2026-09-11）

## 范围与环境

- 验证时间：2026-09-11 15:15–15:26 UTC。
- 仓库：`/home/kuai/applet/smart_money_copytrader`；接手时工作树干净。
- 实际基线：`main` / `552ce46b69995fd805150090d8c1e34d43aca9b3`，未重置到旧基线。
- 系统：Linux x86_64；Python 3.10.12；AMD EPYC 4244P，6 核/12 线程；内存 61 GiB，
  验证前可用约 55 GiB。
- 边界：只使用公开 WSS Feed 和 HTTPS RPC 的只读方法；没有私钥、签名、广播、实盘、
  Telegram、部署或常驻服务操作，也没有 Git 提交/推送。

## 原始功能基线

按 `requirements.lock` 新建 `.venv` 并安装 editable 包。首次沙箱内安装因 DNS 受限失败；
经正常联网权限重跑同一锁定命令后成功，没有改版本。`pip check` 退出 0，未发现损坏依赖。

运行：

```bash
.venv/bin/python -m pip install -r requirements.lock -e .
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sm-copy replay --extra-fixture data/bulk_distribution.json --db :memory:
```

结果：

- 原始 46 项 unittest 全部通过，退出 0。
- 回放 13 笔，输出 27 个信号，退出 0。行为计数为 TRANSFER 1、CLAIM 1、
  APPROVAL 12、UNKNOWN 6、SELL 1、BUY 1、LIQUIDITY 1、
  EXTERNAL_DELIVERY_CANDIDATE 1、INTENT_DEPOSIT 2、BULK_DISTRIBUTION 1。
- 27/27 个信号均为 `copy_eligible=false`。
- 230 个收款人、32 个清单收款人的负例只输出 1 条 BULK_DISTRIBUTION，未生成买入。
- 6 条 UNKNOWN 都明确为 `unsupported_contract_or_method`：两条未知合约方法及两组 Relay
  内部 `0x2213bc0b` / `0x73b7bb2f`。它们没有被猜成兑换。
- 历史 BUY/SELL 均因原生币净流、非零 hook 和 V4 settlement recipient 尚未闭环而保持
  `needs_review`，不代表已确认成交。

基线日志：

- `var/server-validation-2026-09-11-pip-check.log`
- `var/server-validation-2026-09-11-unittest.log`
- `var/server-validation-2026-09-11-replay.jsonl`
- `var/server-validation-2026-09-11-replay.stderr.log`

## 60 秒实时只读监听

运行：

```bash
.venv/bin/sm-copy monitor --seconds 60 --db var/server-smoke-2026-09-11.sqlite3
.venv/bin/sm-copy export --db var/server-smoke-2026-09-11.sqlite3
```

实际网络窗口约为 15:17:09–15:18:09 UTC，退出 0。链 ID 校验通过，出现 1 次
`feed_connected`，所有周期健康记录均为 `healthy=true` 且队列长度为 0。最终计数：

- connections 1，frames 3,477；追历史跳过 48,218 笔，解码新鲜交易 8,431 笔。
- candidates 0，receipts 0；queue_drops 0，worker_errors 0，account_code_errors 0，
  receipt_unavailable 0，reconnections 0，frame_errors 0。
- SQLite 导出和信号 stdout 均为空。

旧基线只在 Counter 中输出出现过的键；上述错误项为 0 的依据是整段健康/最终日志中未出现
对应事件，且队列持续为 0。没有候选只证明本次采集链路健康，不证明候选解析、四类模式、
路由覆盖或实时回执链路全部通过；离线回放补充了功能覆盖，但不是实时覆盖。

实时日志：

- `var/server-smoke-2026-09-11-health.log`
- `var/server-smoke-2026-09-11-signals.jsonl`
- `var/server-smoke-2026-09-11.sqlite3`

## 本轮 P0 只读改动

基线后发现并修复：原接收器在确认内存队列有空间之前就把哈希加入 `seen`；队列满时该候选
被丢弃，后续重复 Feed 消息也不会再入队。现在候选先以原始整数十进制字符串落入 SQLite，
再由有界 dispatcher 入队：

- 新增 `pending / queued / retry / complete / failed` 候选状态与幂等 tx hash。
- 重启时自动把未完成的 `queued` 恢复为 `pending`；队列容量不足时待办仍在数据库。
- 回执未到或 RPC 瞬时失败时持久重试，指数退避为 2–60 秒，最多 8 次；耗尽后明确记为
  `failed` 并增加 `candidate_retry_exhausted`，不无限请求公共 RPC。
- 健康/结束日志显式输出关键零值 counters、候选各状态，以及队列等待、回执 RPC、候选处理
  的有界 p50/p95/p99 单机单调时钟指标。
- 确定性解码/回执错误进入 `failed`，不循环重试；安全分类和 RPC 允许列表未改变。

新增 4 项回归测试，总数 50：容量为 1 的队列不丢第二个候选、处理中断后的重启恢复、
晚到回执的持久有界退避、指标样本上限与分位数。改后 50 项全部通过，13 笔回放仍为
原 27 个信号和原行为分布。

同日继续按确认后的语义拆分 `intent_status / execution_status / canonical_status`，并记录
`observation_source`。Feed 主动调用的意向不会因失败或重组被删除；第三方入账明确为
`intent_status=not_attributed`。新增独立 `canonical_l2` 游标表和禁止静默倒退检查，为显式
重组处理预留入口；尚未声称 RPC 扫描器已经完成。新增 1 项游标测试后共 51 项通过。

改后最终离线日志：

- `var/server-validation-2026-09-11-pip-check-post-p0.log`
- `var/server-validation-2026-09-11-unittest-post-p0.log`
- `var/server-validation-2026-09-11-replay-post-p0.jsonl`
- `var/server-validation-2026-09-11-replay-post-p0.stderr.log`

改后另做 15 秒只读监听：1 次连接、3,011 帧、跳过 48,864 笔历史、解码 2,241 笔；
所有显式错误/重连/丢弃计数为 0，候选状态全为 0。日志在：

- `var/server-smoke-2026-09-11-post-p0-health.log`
- `var/server-smoke-2026-09-11-post-p0-signals.jsonl`
- `var/server-smoke-2026-09-11-post-p0.sqlite3`

为覆盖真实回执恢复路径，另把仓库已有的 `0x542ba9…67611` 历史交付样本放入一个全新测试
数据库，再运行 5 秒只读 monitor。启动时 1 个 pending 被派发并取得公开历史回执，最终为
complete 1、receipts 1、全部错误 0；回执 RPC 148.057 ms。输出仍为保守的
EXTERNAL_DELIVERY_CANDIDATE / needs_review，`fresh=false`、`copy_eligible=false`，没有把补抓
历史伪装成实时机会。日志在：

- `var/server-p0-historical-recovery-2026-09-11-health.log`
- `var/server-p0-historical-recovery-2026-09-11-signals.jsonl`
- `var/server-p0-historical-recovery-2026-09-11.sqlite3`

## 剩余风险与下一步

- 独立区块游标与 RPC 可见交易补洞已在下方续作完成；Feed sequence 仍不能当 L2 block。
- 尚未规范链重查或重组撤销；signals 的 `canonicality` 仍是
  `not_rechecked_for_reorgs`。
- complete/failed 候选目前保留审计记录，尚未定义归档周期，长时运行需测数据库增长。
- V2/V3 factory/pool、V4 settlement recipient、原生 ETH/Gas/退款资金流仍未闭环。
- 被动 Transfer 区块兜底已在下方续作完成首版；Solver 订单证据关联仍未实现。
- 本轮分位数只有单个历史恢复样本，不能当延迟 SLA、地域比较或吞吐结论。

下一小步是基于区块哈希复查实现显式重组回退和孤块状态修正；下方被动入账续作新增
`eth_getLogs` 并保留广播拒绝回归。完成采集可靠性后再推进池/资金流验证、
Solver 归属与纸面报价，暂不进入 M3 实盘。

## Safe-head 补洞续作

在 `eb527b2` 推送后继续完成首版 RPC 区块补洞，不新增 RPC 方法：只使用允许列表已有的
`eth_blockNumber` 和 `eth_getBlockByNumber`。首次启动在 `latest-2` 锚定游标而不扫描全链；
之后每轮最多推进 20 个完整区块。候选先持久化，统一进入既有回执队列，并标为
`observation_source=backfill`、`fresh=false`。父哈希不匹配时停止扫描并报告，不静默改写游标。

`.env` 现在自动加载，但只接受 `ROBINHOOD_RPC_URL` 和 `ROBINHOOD_FEED_URL`；其他键包括
私钥变量均忽略。文件继续由 Git 忽略，endpoint 与 key 不写入日志。

使用已配置付费 RPC 的 15 秒只读验证：游标从 60,384,084 推进至 60,384,224，共扫描
140 个 safe-head 区块；0 backfill error、0 reorg、0 queue drop。Feed 命中 1 个第三方交付
候选并成功取得回执。随后同库重启 5 秒，游标续进至 60,384,304，补抓 80 个区块并由 RPC
发现 1 个候选；该候选成功取回执且保持 backfill/fresh=false、not_attributed、needs_review、
copy_eligible=false。日志未包含 URL 或 key：

- `var/server-backfill-paid-rpc-2026-09-11-health.log`
- `var/server-backfill-paid-rpc-2026-09-11-signals.jsonl`
- `var/server-backfill-paid-rpc-2026-09-11-restart-health.log`
- `var/server-backfill-paid-rpc-2026-09-11-restart-signals.jsonl`

新增 safe-head 初始化/补抓、父哈希异常停止、安全 `.env` 白名单 3 项测试，总数 54。
随后完成显式重组首版：保存规范区块链条，父哈希异常时最多回查 64 块寻找共同祖先；孤块
信号改为 `canonical_status=orphaned` 和 `canonicality=orphaned_by_reorg`，但保留
`intent_status=observed` 与当时观察到的 execution_status。孤块候选清零重试次数后重新查回执；
找不到共同祖先则停止扫描。区块历史和游标在同一 SQLite 事务提交。

新增重组语义回归后共 55 项测试。使用上一版数据库和付费 RPC 再运行 5 秒，旧游标成功补建
链锚点并续扫 80 块，0 backfill error、0 reorg、0 queue drop；日志为
`var/server-reorg-migration-2026-09-11-health.log`。没有人为制造真实链重组。

仍未完成：超过 64 块后的人工恢复工具和长期规范确认升级。

## 被动 Transfer 补洞续作

RPC 允许列表新增只读 `eth_getLogs`，广播拒绝仍由原安全测试覆盖。每个 safe-head 区块只查询
ERC-20 `Transfer(address,address,uint256)`，recipient topic 是 67 个观察地址的 OR 条件；每块
最多接受 10,000 条匹配日志，命中的 transactionHash 必须存在于同一次完整区块响应。
地址无需出现在 calldata。命中只持久化第三方候选，最终仍由回执产生 INCOMING_TRANSFER 或
EXTERNAL_DELIVERY_CANDIDATE，`intent_status=not_attributed`，绝不直接生成 BUY。

付费 RPC 5 秒兼容测试接受该过滤条件：首轮扫描 39 块，0 backfill error、0 reorg、0 drop；
窗口内无被动候选。修复退出时批次统计少报后，同库再运行 5 秒，游标恰好前进 54 块且
`backfill_blocks=54`，仍为 0 错误。新增“地址只在 recipient topic”回归后共 56 项测试。
日志：`var/server-passive-logs-2026-09-11-health.log` 和
`var/server-passive-logs-2026-09-11-restart-health.log`。短窗口零候选不证明完整召回覆盖。

## V2/V3 pool 归属续作

新增回执区块历史状态验证。V2/V3 信号不再只凭 Router calldata 和任意同协议 Swap topic：

- V2 factory 固定为链上 Router `factory()` 返回的
  `0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f`；V3 factory 为
  `0x1f7d7550b1b028f7571e69a784071f0205fd2efa`。
- 在回执 blockNumber 查询 factory/pool 合约代码、`getPair`/`getPool` 映射和 pool 的
  `token0()`/`token1()`；专用 Router 还必须在该区块返回预期 factory。
- 对应池的 Swap 日志必须逐跳精确匹配，且同一归属范围只能有一个已知交易意图、没有
  UNKNOWN，目标钱包的输入 ERC-20 净减少和输出 ERC-20 净增加才标为 `swap_evidenced`。
- 校验缺失、伪造 pool、错误币对、额外 Swap 或原生币流均保持
  `needs_review`；`copy_eligible` 仍恒为 false。

付费 RPC 只读实测：区块 60,396,859 验证 WETH/USDG V2 pool
`0x8803c117ccae7b5146297876c2a25df135141c4d`；区块 60,396,988 验证 V3 fee=500 pool
`0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a`。两个检查均通过 factory、代码和币对验证，
未签名或广播交易。新增 V3 bytes path 每跳 token/fee 的执行顺序保留；新增 5 项
pool/归属/path 正负测试后共 61 项，全部通过；`pip check` 与
`git diff --check` 通过。当前没有独立历史 V2/V3 成交样本，因此链上合约查询通过不等于
真实钱包成交归属已经完成样本覆盖。

本轮 13 笔历史回放仍输出 27 个信号，行为分布与基线完全一致，27/27 均为
`copy_eligible=false`。日志：

- `var/server-validation-2026-09-11-replay-pools.jsonl`
- `var/server-validation-2026-09-11-replay-pools.stderr.log`

## V4 PoolKey、hook 与 settlement 续作

Universal Router V4 action 现在保留 `SETTLE/SETTLE_ALL/TAKE/TAKE_ALL` 的币种、原始整数金额、
payer 和 recipient。回执确认要求 PoolKey 排序与 poolId 哈希一致、PoolManager 在回执区块有代码、
非零 hook 的历史代码哈希属于登记值，并且输入 payer 与输出 recipient 均唯一指向目标钱包。
只有对应 PoolManager/poolId Swap 日志和钱包 ERC-20 收支也闭环时才可到 `swap_evidenced`。

付费 RPC 对仓库两笔真实样本做了只读历史检查：区块 54,284,317 和 54,243,173 的共同 poolId
为 `0xf642c48c7b32c71a577274950bc56fce3fbd5253d570eee920b80d86ad00d30b`，hook
`0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` 的历史代码哈希均为
`0xc21b1e6c1b45403e81a581f22ed6d9c747997af1cfdac1b1dc9f4b1d346a10db`；两笔的 settlement
payer/recipient 也均唯一匹配观察钱包。由于交易分别以原生币作为输入或输出，尚未拆分 Gas、
退款和交易净流，最终仍是 `needs_review`、`copy_eligible=false`，没有把合约结构验证误报为完整
资产交换证明。新增 4 项 V4 正负回归后总计 65 项测试通过。

## Solver order 证据续作

Depository calldata 中的 orderId 现在必须与同一目标钱包/UserOperation 日志范围内的存款事件
逐字段匹配（depositor、token、原始整数 amount、orderId）。匹配成功才记录
`source_deposit_evidenced` 并持久化到独立订单证据表。交付关联接口要求唯一源存款、完全相同的
orderId 和 wallet；错误 orderId、只同钱包收币、钱包不一致或多源歧义均不关联。

新增真实存款事件篡改负例和订单关联正负测试后共 67 项通过；`pip check` 与 diff 检查通过。
13 笔回放仍为原 27 个信号和原行为分布，27/27 `copy_eligible=false`。日志：

- `var/server-validation-2026-09-11-replay-v4-solver.jsonl`
- `var/server-validation-2026-09-11-replay-v4-solver.stderr.log`

当前证据只证明源链/本链 Depository 存款。仓库没有目标链交付中携带同一公开 orderId 的样本，
且未授权访问需要 API key 的 Relay 历史订单接口，所以尚未把任何第三方入账升级为 Solver BUY；
这部分保持 `not_attributed/needs_review`。

回执 worker 同时补上历史账户代码重查：拿到 receipt 后按 blockNumber 的块末状态重新读取
delegation code 并重建信号，失败进入既有持久重试。证据明确标为
`receipt_block_end_not_transaction_prestate`，不把块末状态伪装为交易执行前状态。

随后使用付费 RPC/Feed 做 10 秒只读兼容监听：连接 1 次、594 帧、解码 1,639 笔、safe-head
补扫 81 块；所有 worker/RPC/backfill/reorg/reconnect/drop 错误计数为 0。窗口内候选为 0，
因此只证明监听与补洞兼容，不证明本轮真实候选回执路径被实时触发。日志：

- `var/server-m2-v4-solver-smoke-2026-09-11-health.log`
- `var/server-m2-v4-solver-smoke-2026-09-11-signals.jsonl`
- `var/server-m2-v4-solver-smoke-2026-09-11.sqlite3`

## 原生币 state-diff 与 Gas 分离续作

RPC 允许列表新增只读 `debug_traceTransaction`，但参数在客户端被固定为
`prestateTracer + diffMode=true`：自定义 tracer、callTracer 或脚本参数均直接拒绝，响应仍受
16 MiB 上限约束。每笔含原生币的交易读取目标钱包交易前后余额。若目标钱包也是外层 sender，
用 `gasUsed * effectiveGasPrice` 从余额净变化中剥离外层 Gas；不是外层 sender 时不擅自套用整笔
交易 Gas。trace 未显式包含钱包、receipt 缺 Gas 字段或结构异常时继续 `needs_review`。

付费 RPC 对两笔真实 V4 样本验证结果：

- SELL `0x781ddd…b4daa`：卖出原始 token 数量 `13439190770000000000000000`，扣除外层 Gas 后
  原生币净增加 `239440185155403918`。
- BUY `0xd69b89…a571`：扣除外层 Gas 后原生币净减少 `260000000000000000`，token 净增加
  `13439190772929155460933628`。

两笔均同时满足 pool/hook、settlement recipient、Swap 日志和资产净流，现为
`swap_evidenced`，但 `copy_eligible=false`。摘要日志：
`var/server-validation-2026-09-11-v4-native-trace.jsonl`。新增 Gas 分离、原生币闭环和任意 tracer
拒绝测试后总计 70 项通过；`pip check` 与 diff 检查通过。此结论只覆盖目标钱包为外层 sender
的真实样本；bundled/UserOperation 的 Gas 付款来源仍需独立证明。

## safe-head 规范状态升级

区块扫描和候选 receipt inclusion 的 blockNumber/blockHash 一致后，相关信号现在从
`unconfirmed` 升级到 `safe_head_confirmed`，canonicality 明确为
`safe_head_hash_rechecked_not_l1_finality`，不宣称 L1 finality。若后续父哈希不连续并找到共同
祖先，既有逻辑仍将孤块信号改成 `orphaned`；本轮同时删除孤块上的 Solver 存款/交付执行证据，
避免已撤销订单继续被关联。查询使用 tx_hash 和 inclusion block 索引，不逐块全表扫描 signals。

新增 safe-head 升级和重组删除订单证据测试后共 72 项通过。最终 13 笔离线回放仍输出 27 个
信号且行为分布不变，27/27 `copy_eligible=false`：

- `var/server-validation-2026-09-11-replay-native-canonical.jsonl`
- `var/server-validation-2026-09-11-replay-native-canonical.stderr.log`

bundled/UserOperation 原生币路径另加限制：目标钱包不是外层 transaction sender 时绝不使用整笔
交易 Gas 做修正。当前仅当目标钱包 state-diff 原生余额变化与同一 UserOperation 范围、匹配
poolId 的 V4 Swap 事件原生币绝对金额一致，才可继续资金流闭环；金额不一致（包括 hook fee、
账户付款或 Gas 来源无法分离）保持 `needs_review`。新增不一致负例后共 73 项测试通过。
实时健康指标同时新增 `pool_verification_rpc_ms` 和 `native_trace_rpc_ms` 独立分位数，避免慢 trace
被笼统隐藏在候选总耗时里。

另在全新数据库注入上述两笔仓库历史候选并运行 20 秒完整 monitor：2/2 候选均取得 receipt、
完成历史账户代码、pool/hook、settlement 和 native trace 验证，最终 2 个交易信号为
`swap_evidenced`；候选状态 complete=2、failed/retry/error/drop/reconnect 均为 0。同期补扫 178 块。
两样本的候选处理耗时 p50 97.853 ms、p95/p99 264.148 ms；native trace p50 32.227 ms、
p95/p99 66.666 ms。样本数只有 2，不能视为 SLA。stdout 包含意向与后续更新共 8 行，8/8
`copy_eligible=false`。日志：

- `var/server-m2-v4-native-worker-2026-09-11-health.log`
- `var/server-m2-v4-native-worker-2026-09-11-signals.jsonl`
- `var/server-m2-v4-native-worker-2026-09-11.sqlite3`

## V4 多跳 ABI 续作

按官方 `IV4Router.ExactInputParams/ExactOutputParams` 和 `PathKey` 布局新增 action 0x07/0x09。
exact-in 正向恢复每跳，exact-out 按 Router 逆向执行语义还原为逻辑输入到输出顺序；每跳保留
tokenIn/tokenOut、fee、tickSpacing、hook、hookData、PoolKey 和 poolId，以及每跳价格约束。
回执必须出现每一个且仅一个预期 PoolManager/poolId Swap 事件，任一缺失、额外或重复均 review。

新增 exact-in、exact-out 解码和“全部池事件/缺一池事件”正负测试后共 76 项通过。当前验证使用
严格构造 ABI 回归，仓库及当前短时窗口没有观察钱包的真实 V4 多跳样本，因此不能声称真实样本
覆盖；出现未知 action 或无法完整恢复路径时仍输出 UNKNOWN/needs_review。

改后 13 笔回放仍为 27 个信号和相同行为分布，27/27 `copy_eligible=false`：

- `var/server-validation-2026-09-11-replay-v4-multihop.jsonl`
- `var/server-validation-2026-09-11-replay-v4-multihop.stderr.log`

## 真实 Relay orderId 到 requestId/outTx 续作

官方公开 `/intents/status/v3` 使用 requestId；把链上 orderId `0x3ccc6f52…8826f` 直接作为
requestId 查询得到 unknown，验证二者不能混用。随后使用公开、无需 API key 的弃用接口
`/requests/v2?orderId=...&includeOrderData=true&limit=2` 取得唯一成功记录：

- Relay requestId：`0x17888118…832c9`，与 orderId 不同；
- user 与链上 depositor：`0x0a6ebe…119e`；
- 源链 4663、USDG、amount `173943711`、源交易 `0xffa84e…f277` 全部与已验证存款一致；
- 目标链 792703809（Solana），outTx `5M8GEDm1…CLL96`，API stateChanges 报告接收
  USDC `173879072`。

新增独立紧凑样本 `data/relay_order_evidence_3ccc6f52.json`，保留查询来源与日期，不修改旧样本。
解析器要求唯一请求、成功状态、源交易/链/币/金额/depositor/depository 全匹配、requestId 不得
冒充 orderId、outTx 必须同时存在于 settlement fills，并且只有一个目标 recipient 正向 FT
余额变化。关联成功后仍标记 `api_reported_not_independently_rechecked`，没有把 Relay API 响应
冒充 Solana 最终性。新增真实关联和 ID 混用负例后共 78 项测试通过。

## 60 秒实时监听与 WETH wrap 缺口修复

使用付费 RPC/Feed 做 60 秒只读监听，实际捕获 1 个 feed 候选并完成 receipt，不再是零候选窗口：
连接 1 次、1,019 帧、解码 7,608 笔、safe-head 补扫 528 块；candidate/dispatch/receipt/complete
均为 1，所有 retry/failed/worker/RPC/backfill/reorg/reconnect/drop 错误为 0。候选处理 33.089 ms，
receipt RPC 11.96 ms。最终区块哈希被 safe-head 复核，数据库信号为 `safe_head_confirmed`。

该候选是观察钱包 `0x0a6ebe…119e` 的 UserOperation 调用固定 WETH 合约 `deposit()`，call value
与 WETH 净入账均为 `5441263369668645163`。原实现保守输出 UNKNOWN；现新增固定地址+selector
的 `WRAP_NATIVE` 以及 `UNWRAP_WETH`，并要求 wrap value/WETH credit 或 unwrap amount/WETH debit
完全相等。两者是资产形态转换，不是 BUY/SELL。

用同一真实交易在新数据库重跑 10 秒：candidate complete=1、所有错误为 0，最终行为
`WRAP_NATIVE/execution_observed`、金额完全闭环、`copy_eligible=false`。新增 wrap/unwrap 解码及
金额不匹配负例后共 81 项测试通过。日志：

- `var/server-m2-final-smoke-2026-09-11-health.log`
- `var/server-m2-final-smoke-2026-09-11-signals.jsonl`
- `var/server-m2-final-smoke-2026-09-11.sqlite3`
- `var/server-m2-wrap-recovery-2026-09-11-health.log`
- `var/server-m2-wrap-recovery-2026-09-11-signals.jsonl`
- `var/server-m2-wrap-recovery-2026-09-11.sqlite3`

健康输出新增最终回执信号覆盖统计，不把前置 intent 和 receipt 更新重复计数。用同一真实 wrap
候选验证：receipt_signals=1、unknown=0、needs_review=0、swap_evidenced=0、unknown_fraction=0；
该信号是已验证的非交易 WRAP，所以没有错误计入 swap。新增去重统计测试后共 82 项通过。日志：

- `var/server-m2-coverage-2026-09-11-health.log`
- `var/server-m2-coverage-2026-09-11-signals.jsonl`
- `var/server-m2-coverage-2026-09-11.sqlite3`

回执身份验证进一步从 receipt block 末状态升级为交易执行前状态：固定允许
`prestateTracer`（无自定义脚本），从交易 prestate 读取目标账户 code；trace 未出现账户或 code
不是登记的 7702 实现时不沿用 latest 结果。真实 wrap UserOperation 的 prestate 明确返回
`0xef0100` 加 Simple7702Account 地址。新数据库端到端重跑后 account_prestate_missing=0，
最终信号 evidence 为 `transaction_prestate_trace`；prestate RPC 35.644 ms，候选总处理
69.518 ms。新增已知/未知 prestate 负例后共 83 项通过。日志：

- `var/server-m2-prestate-2026-09-11-health.log`
- `var/server-m2-prestate-2026-09-11-signals.jsonl`
- `var/server-m2-prestate-2026-09-11.sqlite3`

## 深重组显式恢复与最终离线回归（17:04 UTC）

自动回查 64 块找不到共同祖先时仍安全停扫，不自动猜测游标。新增只读运维命令：

```bash
.venv/bin/sm-copy reconcile-reorg --db var/observer.sqlite3 --max-depth N
```

`N` 限制为 65–100000。命令先校验 chain ID，再仅用允许列表内的 `eth_getBlockByNumber`
逐块对比数据库保存的哈希；只有找到规范共同祖先才调用既有 rewind，孤块信号改为 orphaned、
相关候选重新入队、Solver 孤块执行证据删除。未找到共同祖先时不会修改游标。构造 70 块深
重组回归证明默认 64 块失败后游标保持 100，显式深搜找到块 30 后才回退并清除后续链条。

当前工作树最终离线检查：84/84 unittest 通过，`pip check` 为 No broken requirements found，
`git diff --check` 通过。13 笔历史回放仍输出 27 个信号，行为计数为 TRANSFER 1、CLAIM 1、
APPROVAL 12、UNKNOWN 6、SELL 1、BUY 1、LIQUIDITY 1、EXTERNAL_DELIVERY_CANDIDATE 1、
INTENT_DEPOSIT 2、BULK_DISTRIBUTION 1；全部 `copy_eligible=false`。离线 replay 不调用历史
pool/trace RPC，因此其中 V4 BUY/SELL 仍保守为 needs_review；其在线闭环证据见本报告前述
两笔真实 V4 worker 验证，不能把离线状态误报成失败或自动跟单许可。

## M2 只读信号 Goal 验收矩阵

| 要求 | 当前证据与结论 |
| --- | --- |
| 持久游标、重启、回压、回执重试 | SQLite cursor/candidates、先落盘后派发、8 次有界退避；重启、队列满、晚到回执测试通过。 |
| 断线补洞与历史/实时隔离 | safe-head 完整区块 + 单区块 Transfer recipient 日志扫描；backfill 强制 `fresh=false`，真实监听游标持续推进。 |
| 规范链重查与重组 | inclusion hash 升级、64 块自动共同祖先、孤块 orphan/requeue/Solver 撤销，以及显式深重组恢复测试通过；不宣称 L1 finality。 |
| 身份和逐操作归属 | tx 签名 sender、登记的交易 prestate 7702 实现、EntryPoint/UserOperation 日志范围；他人 Swap、伪 EntryPoint、失败 UserOp 负例通过。 |
| V2/V3/V4 池及资金流 | factory/pool/token/code、V4 PoolKey/hook/settlement、逐池事件和钱包 ERC-20/native 差分验证；两笔真实 V4、V2/V3 RPC 检查及伪池/错 recipient/缺池负例通过。 |
| 被动入账 | Transfer recipient 扫描可建候选，但仅输出第三方入账/分发语义，不把收币当 BUY；230 人分发真实负例通过。 |
| Solver 关联 | 源存款事件逐字段一致，公开 Relay 订单中 orderId/requestId、源 tx、钱包、金额和 outTx 显式关联；目标 Solana 仅标 API reported，未伪称独立最终性。 |
| 健康、延迟和覆盖率 | health/finished 含队列、重试、错误、重连、补洞、reorg、各阶段 p50/p95/p99 和 receipt 去重覆盖率；60 秒真实窗口取得候选和回执。 |
| 安全边界 | RPC 固定只读允许列表；仓库没有签名/广播接口；所有构造、历史与实时信号 `copy_eligible=false`，未知或证据不足路径为 UNKNOWN/needs_review。 |

该矩阵验收的是当前明确支持路径的可靠、保守信号生成，不代表完整聚合器覆盖或盈利能力。
尚缺真实 V3/V4 多跳观察样本、bundled/UserOp 中一般化的 Gas 付款归属、Relay 目标链独立 RPC
复核；这些输入当前均不会越过 needs_review，属于后续覆盖增强，不构成已支持路径的静默误判。
报价、模拟账本、PnL、订单/仓位/预算和真实交易明确不在本 Goal 内。
