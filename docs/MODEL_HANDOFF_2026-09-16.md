# 当前运行与开发交接（面向后续模型，2026-09-16）

本文是给下一位模型的短版、可独立阅读交接。它说明当前代码、正在运行的进程、Feed 提前跟单规则、
最近修复、已知问题和下一步。更早的文档保留了历史过程，其中的 PID、测试数、配置和“尚未部署”
结论可能已经过时；发生冲突时，应先以当前代码、只读账本审计和最新运行日志重新核对。

快照时间：**2026-09-16 17:07 SGT（UTC+8）**。运行状态会继续变化，接手后必须先执行第 2 节检查。

## 1. 一页结论

- 仓库：`/Users/claw/smart_money_copytrader`，独立于 `fomo_sniper`。
- 分支：`main`；写本文前 `HEAD == origin/main == be48076`。
- 当前进程：PID `69564`，PPID `1`，命令为
  `.venv/bin/sm-copy run --early-trial-id feed-early-20260915`。
- 当前日志：`var/log/sm-copy-early-20260916-rpc-fix.log`。
- 当前急停文件 `var/EXECUTION_STOP` 不存在；它此前被归档为
  `var/EXECUTION_STOP.before-rpc-fix-20260916`。不要因为看到归档文件而误判急停仍生效。
- 当前代码已经包含 `be48076`。旧交接中“修复尚未部署”的说法已经失效。
- 关系 `1/2/3/4/5/9` 使用同一个 follower，执行提供方为 `zeroex` 首选、`kyber` 备选。
- Feed 提前时效为 **7 秒**，报价时效为 **6 秒**；最终预检凭据最多复用 **2 秒**。
- 当前进程健康、Feed 健康、队列为空，没有活跃的签名/广播/待确认交易。
- 重启后尚未出现满足全部门禁的 enabled-wallet 提前交易，因此不能宣称新链路已经取得真实
  Feed→广播延时样本，也不能宣称 0x 已在真实跟单中优于 Kyber。
- 健康日志中的 `live_errors=24` 是启动恢复时发现的 **24 个历史 reserved proposal**，不是本轮
  新发生 24 笔交易失败；它们没有 execution plan、签名或广播，需另行审计，不能直接删除或释放。
- 当前最有价值的下一步不是继续放宽门禁，而是等待或定位一笔 enabled-wallet、受支持路径的
  Feed 候选，完整记录 Feed→证据→报价/构建→预检→广播→回执的分段时间。

## 2. 接手后的只读检查

先阅读根目录 `AGENTS.md`、[README](../README.md)、[总流程](copy_trade_flow.md) 和本文。
不要先改配置、重启、清除急停、释放预算或发送交易。

```bash
cd /Users/claw/smart_money_copytrader

git status --short
git rev-parse HEAD
git rev-parse origin/main

cat var/sm-copy.pid
ps -p "$(cat var/sm-copy.pid)" -o pid,ppid,etime,command

if test -e var/EXECUTION_STOP; then
  echo "execution stop: ACTIVE"
else
  echo "execution stop: inactive"
fi

jq -c 'select(.event=="health") |
  {observed_at,healthy,feed_healthy,execution,queued,counters,
   early_feed_counters,latency_ms,coverage}' \
  var/log/sm-copy-early-20260916-rpc-fix.log | tail -1

.venv/bin/sm-copy execution-audit --ledger-mysql
```

`execution-audit` 是只读账本检查。注意其顶层历史 `signed` 计数不代表现在仍有已签交易；判断在途
状态要看 `attempt_statuses.signed`、`observed_pending`、`prepared`、`orphaned` 等当前状态字段。

若 PID 不存在、命令不匹配、出现急停、审计不健康、存在未知在途交易或日志中的
`execution.state` 不再是 `not_stopped`，先停止推断并做只读根因调查。不要为了“恢复运行”删除 PID、
急停或账本记录。

## 3. 2026-09-16 17:07 的运行快照

最新健康记录：

| 指标 | 值 | 解释 |
|---|---:|---|
| `healthy` / `feed_healthy` | `true` / `true` | 观察和 Feed 循环健康 |
| execution | configured、未锁定、未急停 | 仅代表本地停止控制状态 |
| queue | `0` | 没有候选排队 |
| Feed candidates | `40` | 本进程累计 Feed 候选 |
| early jobs | `40 enqueued / 40 done` | 提前队列没有积压 |
| receipts | `145` | 本进程处理的回执次数 |
| live prepared/signed/broadcast/confirmed | 全部 `0` | 重启后尚无实盘执行 |
| worker errors | `2` | 早期瞬态错误；进程已自恢复 |
| Feed reconnect/frame/drop | `0/0/0` | Feed 连接未见重连、帧错误或丢队列 |
| strict receipt signals | `36` | 其中 Relay BUY evidenced `15` |
| unknown / needs review | `3 / 16` | 不会自动当成买卖跟单 |

当前运行延时统计：

| 阶段 | P50 | P95 | P99 |
|---|---:|---:|---:|
| queue wait | 0.105 ms | 0.295 ms | 1.178 ms |
| receipt RPC | 54.047 ms | 220.429 ms | 508.244 ms |
| account prestate RPC | 90.110 ms | 310.082 ms | 537.887 ms |
| candidate processing | 539.148 ms | 910.561 ms | 1675.469 ms |

这些是候选/严格证据处理统计，不是已成功跟单的端到端延时。当前没有新的 live broadcast，不能用
它们回答“跟单延时已经是多少”。

稍早的数据库只读快照显示：重启后 33 个 `early_feed_jobs` 全部结束，没有 expired/failed；其中一个
relationship 2 钱包候选被解析为 `unsupported_path`，其余没有形成受支持的 enabled-wallet 提前意向。
之后日志计数已增长到 40；接手者应重新查询数据库后再引用数量。

## 4. 实盘范围与当前配置

follower：`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`

| Relationship | Smart wallet |
|---:|---|
| 1 | `0x1cfbe3af88266ccca29372661f45261c7d19be09` |
| 2 | `0x365a446a2c2f59428e5daebfa2bc23d0e4d8b289` |
| 3 | `0xad9016bc32efdb0b2a1aff66c1a1349b13b91217` |
| 4 | `0xd50a8cf2005b2f9d8237d58aec79bba7645eb9ab` |
| 5 | `0x95b0746316368ca24e5a58da7fe961d791cb6829` |
| 9 | `0x89909912c58e2182d92b1a8638d6ff8d965e173b` |

关键参数：

- `execution_providers = ["zeroex", "kyber"]`，按关系配置，0x 优先、Kyber 备选。
- 固定测试买入为 `100000` raw USDG，即 0.1 USDG。金额必须继续以十进制字符串保存。
- 试运行最多 24 小时或 100 个提前广播名额，以先到者为准。名额是网络调用前的持久预留上界，
  不等于成功成交数，失败也不自动返还。
- 试运行 `feed-early-20260915` 在上次核对时将于 **2026-09-17 10:24:58 SGT** 到期，已消费
  5/100；这是历史快照，继续运行前必须重新读取 trial 状态，不能自动延期或重置。
- 0x AllowanceHolder 的 USDG 授权曾人工设置为 `10000000` raw，即 10 USDG。不要假设 SELL token
  已授权，也不要在运行时自动扩容授权。
- Feed 7 秒和报价 6 秒贯穿最终发送门禁；延长某一阶段不能让旧 Feed 或旧报价“续命”。

不要读取或打印 `.env`、私钥数据库列、签名原文、raw transaction、带凭据 URL 或 API Key。

## 5. 当前架构

```text
单一 Feed WebSocket
  ├─ 保存/分发候选 ─→ 同进程 EarlyFeedLane（队列 32、worker 2）
  │                    ├─ ABI/路径/钱包识别
  │                    ├─ Relay 订单或 UserOp 归属
  │                    ├─ 新鲜度、部署代码、策略、预算、持仓检查
  │                    └─ EarlyRuntime 决策与原子预留
  │
  └─ receipt/backfill ─→ 严格证据通道（提前通道的后备与事后核对）

通过执行门禁
  → 操作级防重/额度预留
  → 0x 单接口 quote+transaction；必要时 Kyber routes+build
  → 一轮并行 RPC 预检 + exact-calldata 模拟
  → 2 秒、交易绑定、单次消费的 PreflightTicket
  → 签名时复核 → 发送前最终 fence → 广播
  → receipt 跟踪、settlement、来源结果归因
```

提前通道复用同一个 Feed 连接。不要再启动一个独立 `sm-copy` 或独立影子 Feed 进程；除了可能触发
订阅限制，还会增加重复执行和账本竞争风险。

主要代码入口：

- `src/smart_money/cli.py`：`run` 编排、Feed/严格通道、live execution、健康日志。
- `src/smart_money/early_feed_lane.py`：同 Feed 的持久 early job、队列、resolver。
- `src/smart_money/early_runtime.py`：提前决策、试运行名额、预留和执行交接。
- `src/smart_money/early_intent.py`、`verified_feed_intent.py`、`early_decision.py`：候选解析、归属、决策。
- `src/smart_money/decode.py`、`receipts.py`、`solver.py`、`relay_api.py`：ABI/回执/Relay 证据。
- `src/smart_money/zeroex.py`、`kyber.py`、`quotes.py`：聚合器与报价校验。
- `src/smart_money/execution_pipeline.py`、`execution_prep.py`：准备、模拟、签名、发送门禁。
- `src/smart_money/http_pool.py`、`rpc.py`：HTTP/TLS 连接池和 RPC allowlist。
- `src/smart_money/runtime_safety.py`：单实例、PID 和运行安全控制。
- `src/smart_money/store.py`、`mysql_store.py`、`copy_operation.py`、`early_trial.py`、
  `source_position.py`：账本、防重、试运行、预算和持仓 lot。

## 6. Feed 提前下单必须同时满足什么

“Feed 解析到了聪明钱地址”只是候选，不是交易许可。提前执行要求：

1. 原始 Feed 数据在源时间和本地接收时间两个口径下都不超过 7 秒。
2. 目标链、合约身份、selector 和 ABI 参数属于已支持且严格解码的路径。
3. 候选唯一归属于某个 enabled relationship 的 smart wallet。
4. Relay BUY 必须取得唯一订单并核对 requestId/orderId、calldata 尾缀、目标链、目标钱包、付款、
   token 映射和来源状态；UserOp SELL 必须通过签名和账户委托/代码检查。
5. 固定 Relay race 包装器的部署代码由后台监控验证。交易线程复用验证结果，不逐笔做 3 秒 TTL
   查询；代码哈希变化会停用该路径，暂时 RPC 失败只记录告警并保留最后一次成功状态。
6. BUY 的资金资产受信，动态目标 token 符合策略；SELL 能映射到同一关系、未孤立、未耗尽的
   attributed source lot。
7. 关系配置、允许资产/协议、金额规则、周期预算、持仓、报价、滑点、Gas、余额、allowance、nonce
   和 exact transaction 模拟全部通过。
8. trial 仍有效且有名额；操作键没有被 early/strict/旧 proposal 消费或占用。
9. 签名前和发送前的配置、急停、来源、新鲜度、报价和预检凭据仍有效。
10. 只有确定广播结果才进入正常跟踪；签名后或发送结果不明时失败关闭，不能自动改走第二家重复发单。

selector 是 calldata 的前 4 字节，用来选择目标合约 ABI 函数。它只能在目标代码身份已验证时说明
“可能调用哪个函数”，不能单独证明聪明钱身份、函数成功、token 已兑换或某个收款人是购买者。

Relay Proxy 的 `0x0a2b8f36` 顶层交易通常由 solver 发起，聪明钱可能只是最终 cleanup 收款人。
完整的 Relay 订单、固定包装器语义、付款和目标钱包一致时，可把它归属为跨链买入意向；只有一条
Transfer 或地址字节命中时，仍不能认定为聪明钱买入。

## 7. 哪些情况明确不提前跟单

- 普通 ERC-20 转账、赠送或空投；incoming Transfer 单独出现始终不是购买证据。
- selector/目标代码未知，ABI 不能严格重编码，路径带未解释动作或可选失败语义不安全。
- 聪明钱地址只出现在无业务含义字段中，或候选能匹配多个钱包/订单。
- Relay order 查不到、字段不完整、requestId/orderId/尾缀不一致、来源付款或目标链不匹配。
- Feed 超过 7 秒、报价超过 6 秒、Permit2/deadline 已过期，或最终预检票过期。
- 不在六个 enabled relationship 中。严格通道观察到其他钱包交易，不会因此跟单。
- BUY 的本金资产/映射不受信，目标 token 被策略禁止，预算不足或已存在相同操作。
- SELL 没有本关系可归因的未平仓 lot、源剩余基数不足、lot 被预留/孤立，或输入 token 未授权。
- 只有 receipt 的 Swap，但不能证明该 swap 属于 bundled transaction 中被监控的钱包。
- receipt 是被动收币且 Relay 归属未补齐；它保留为 incoming/needs_review，而不是 BUY。
- 0x/Kyber 返回内容不能验证、模拟回滚、Gas/余额/allowance/nonce/配置检查失败。
- 急停、执行 fault latch、trial 到期/耗尽、在途状态不确定或账本原子 claim 失败。

SELL 的 `attributed_position_insufficient` 不等于 follower 钱包链上余额必然为 0。它表示当前关系的
可归因 lot 不足或不可用；项目有意不把人工转入、其他关系买入或无法证明来源的余额拿去卖。

## 8. 最近两次关键提交

### `178822d`：执行延时优化

- 接入 0x `GET /swap/allowance-holder/quote`，同一个响应同时提供报价和可验证交易，不再单独 build。
- 仍保留小金额参考报价，因此正常 0x 路径是两次聚合器请求，不是总共一次请求。
- 0x 不可用或准备阶段出现明确、未签名且可安全回退的路由/授权问题时，最多切换一次 Kyber。
- 将 nonce、余额、Gas、token balance、allowance 和 0x registry 检查并行化，只做一轮最终模拟。
- 使用与 proposal、配置、来源和完整交易绑定的 2 秒 `PreflightTicket`，供 sign/review/send 复用。
- RPC、0x、Kyber 和广播使用有界 HTTPS 连接池；广播连接独立，不自动重发交易请求。

公开只读小样本中，热连接 0x/Kyber 报价构建约 0.33–0.39 秒，预检/模拟通常再花几十到一百多
毫秒。它不是实盘统计；详见[执行延时优化记录](EXECUTION_LATENCY_2026-09-16.md)。

### `be48076`：安全隔离准备阶段 RPC 故障

根因样本是在签名前、prepare 阶段发生 `RPC transport failure: HttpPoolError`。当时没有 plan、签名或
广播，proposal 和 `100000` raw USDG 预留已安全取消；旧逻辑却把所有 `RpcError` 都视为执行结果
不确定并触发全局急停。

修复只在以下条件全部满足时避免全局停机：失败发生于 prepare、没有已签 plan、proposal/计划取消
成功、取消标记与当前 proposal 绑定，且账本确认状态为 cancelled。取消失败、清理异常、签名/广播
后的不确定性仍然失败关闭并触发停止。结构化诊断只记录固定安全字段，不写 URL、参数、API Key 或
RPC 原始错误文本。健康日志也拆分为 `healthy/feed_healthy` 与 `execution` 状态。

该修复已经在当前 PID `69564` 中运行，不要再按旧交接重复“部署”。

## 9. 已知问题与待完成工作

按优先级处理：

1. **取得一笔真实可执行样本。** 当前最大的证据缺口不是实现，而是重启后没有 enabled-wallet 的
   supported early candidate。记录 Feed 到达、归属完成、决策、quote/build、preflight、sign、广播
   ACK、receipt 和严格证据完成时间，分别计算延时；不要拿严格通道候选处理 P50 代替端到端延时。
2. **只读调查 relationship 2 的 `unsupported_path`。** 较早样本 tx hash 为
   `0x5f43910a0689b2ea3ae3384602e7d1221063168ec278a38d70e79d23d54794d5`。先从保存的 Feed 参数、selector、
   target 和已知 ABI 判断它是应支持的新路径、普通转账还是应继续拒绝；根因明确前不要放宽解析器。
3. **单独审计 24 个历史 reserved proposal。** 它们没有 early trial ID、execution plan、签名或广播。
   需要追溯对应严格信号、预算预留和取消条件。不要把启动恢复告警当成新失败，也不要批量释放。
4. **观察瞬态 RPC/Relay 故障。** 本轮出现过少量 backfill/candidate/deployment/Relay API 错误，进程已
   自恢复。若再次出现，利用 `be48076` 的结构化阶段/池耗时诊断判断是连接池等待、建连、响应还是
   解析故障；不要仅凭异常类名猜测。
5. **验证 0x 实盘效果和 Kyber fallback。** 当前没有新 live quote/broadcast 样本。必须分别统计
   provider、BUY/SELL、冷/热连接、首次/重试、成功/回滚，不能只报平均数。
6. **补 SELL 覆盖。** 区分无 attributable lot、lot 数量不足、allowance 不足、报价/模拟失败和钱包
   实际余额不足。链上有余额不代表项目被允许出售。
7. **Relay pre-feed 实验仍是未跟踪文件。** 工作区已有用户文件
   `scripts/validate_relay_prefetch.py`、`tests/test_relay_prefetch_probe.py`。不要覆盖、删除或顺手提交；
   先审查其来源、用途和测试边界，再决定是否纳入项目。

## 10. 修改与验证纪律

- 诊断请求只做只读调查；用户明确要求修复后才改代码。
- 使用 `apply_patch` 编辑，不覆盖用户未跟踪文件。
- 保留 RPC method allowlist；不要把 `eth_sendRawTransaction` 加入普通 `ReadOnlyRpc`。
- 不把 receipt 含 Swap 推断成 bundled transaction 中每个钱包都换币。
- 不把 incoming Transfer 推断成购买；保留 claim-then-swap 子调用；未知路径保持 unknown。
- intent、observed execution 和 confirmed asset exchange 是不同状态，均不等于 L1 finality 或盈利。
- raw amount 始终使用整数并序列化为十进制字符串。
- 不在测试中读取真实私钥、广播主网交易或更改在线关系/预算/trial。
- 不自动重试签名/广播后的未知结果，不自动换 provider 发送第二笔。
- 代码修改后运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
git diff --check
```

本次工作区完整回归为 518 项，其中 12 项因可选 EVM 依赖缺失而跳过；接手时的工作区包含
未跟踪测试，实际发现数量可能不同，必须报告本次真实输出，不能复制旧数字。

任何重启、急停变更、配置修改、授权或实盘动作都需要用户明确授权。重启前后都做
`execution-audit`，核对精确 PID/命令，并确认只有一个 follower 实例和一个 Feed 连接。

## 11. 下一里程碑的完成定义

至少取得一笔属于关系 `1/2/3/4/5/9`、路径受支持、7 秒内完成归属且通过风险门禁的实时 Feed
候选，并形成以下可核验记录：

- 原始 Feed 元数据、wallet、selector、解析路径和 operation key；
- Relay/UserOp 归属字段以及每个拒绝/通过理由；
- quote provider、请求耗时、quote age、minOut、build/calldata 校验；
- 并行预检各 RPC、模拟、签名和广播 ACK 时间；
- 最终 receipt、我方实际 token 变化、源交易严格证据和误跟归因；
- 与旧链路按同口径比较的 P50/P95，至少按 BUY/SELL 和 provider 拆分。

若采样期内没有合格候选，正确产物是覆盖报告：多少 Feed 候选、多少命中 enabled wallet、各自在哪个
门禁被排除、哪些路径值得新增解析。不能为了产生“成功跟单”而放宽普通转账、归属或防重规则。

## 12. 进一步阅读

- [当前跟单流程、路径和延时分析](copy_trade_flow.md)
- [Feed 提前规则与接口](EARLY_FEED_REFERENCE.md)
- [Feed 提前执行历史交接](EARLY_FEED_LIVE_HANDOFF.md)
- [执行阶段三项延时优化](EXECUTION_LATENCY_2026-09-16.md)
- [操作手册](OPERATOR_RUNBOOK.md)
- [完整历史交接](HANDOFF.md)
- [来源与证据](PROVENANCE.md)

## 13. 可直接交给下一位模型的提示词

```text
请先阅读 AGENTS.md、README.md、docs/MODEL_HANDOFF_2026-09-16.md、
docs/copy_trade_flow.md 和 docs/EXECUTION_LATENCY_2026-09-16.md。

先只读核对当前 git HEAD、PID/命令、急停文件、最新 health 日志和
`.venv/bin/sm-copy execution-audit --ledger-mysql`。不要读取或输出 .env/私钥，
不要改在线配置、trial、预算或账本，不要重启，不要发送交易，也不要启动第二个 Feed 进程。

确认运行状态后，优先分析：
1. 是否出现关系 1/2/3/4/5/9 的 supported early candidate；
2. Feed→归属→决策→报价/构建→预检→广播/回执各阶段真实耗时；
3. relationship 2 的 unsupported_path 样本是否应新增解析；
4. 24 个历史 reserved proposal 的来源和安全处置条件。

先给出带日志/数据库证据的结论和建议。只有我明确要求实施后才修改代码；修改时使用
apply_patch，保留所有用户未跟踪文件，并运行完整测试、compileall 和 git diff --check。
```
