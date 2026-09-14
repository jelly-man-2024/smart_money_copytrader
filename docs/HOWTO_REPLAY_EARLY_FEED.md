# How to 回放 Feed 提前跟单覆盖率

用已保存的数据验证解析、归属、决策和准备四层覆盖，不运行跟单进程。
规则、字段和限制见 [接口参考](EARLY_FEED_REFERENCE.md)。

## 前提

在本项目目录，已有 `.venv` 和项目依赖。文件回放无需 RPC、MySQL 或私钥。
业务库回放需要既有业务 MySQL 只读访问能力；不要设置私钥库变量，不运行 `sm-copy run`。

## 步骤

1. 回放两笔已保留来源的公开交易：

   ```bash
   .venv/bin/python scripts/replay_early_feed.py \
     --input data/early_feed_public_samples_2026-09-14.json --evaluate --summary
   ```

   预期两笔均解析到候选，SELL 签名验证数为 1；由于缺少当时订单/账户快照，最终决策不会通过。
   `live_enabled` 和 `copy_eligible` 始终为 false。省略 `--summary` 可查看逐笔原因。

2. 只读复跑固定的 156 笔历史队列及对照：

   ```bash
   .venv/bin/python scripts/replay_early_feed.py --mysql \
     --cohort data/early_feed_baseline_2026-09-14.json \
     --log var/log/sm-copy.log --controls --evaluate --summary
   ```

   MySQL 路径只在 `START TRANSACTION READ ONLY` 内查询公开业务账本，结束 rollback/close。
   不构造会初始化或迁移数据的 Store，不读 wallet_keys。`--log` 用原始意向的 stale 标志纠正
   后续被重写的 candidate fresh 字段，同时记录日志前缀 SHA-256。

3. 核对新测试及全量回归：

   ```bash
   .venv/bin/python -m unittest discover -s tests -v
   ```

   正常准备链路使用合成快照测试，包含精确 calldata、模拟失败、nonce 变化、未来报价、预算、
   比例持仓及重复消费。测试不会广播。合成快照通过不代表历史交易通过。

## 命令参数

| 参数 | 默认/作用 |
|---|---|
| --input PATH / --mysql | 必选其一；离线 JSON / 只读业务库 |
| --cohort PATH | 可选，仅 MySQL；`{"tx_hashes":[...]}` 固定选择集合，上限 1000 |
| --limit N | MySQL 最多选 N 笔 proposal，默认 200，范围 1..1000 |
| --log PATH | 可选；原 monitor JSONL，只读打开时已存在的前缀 |
| --controls | 仅 MySQL；每个类别按 hash 排序最多 100 个对照，不是随机抽样 |
| --evaluate | 开启离线决策检查；不改变任何运行配置 |
| --summary | 省略逐笔 rows，保留分组、原因及来源 |
| --audit-race-code | 额外启用只读 RPC：事后核验 wrapper 历史区块代码，不产生提前快照 |

输入文件最多 64 MiB、1000 个 case。输出到 stdout；不自动落库或覆盖文件。
退出码 0 表示报告成功且输入无错误，不表示交易可以跟；1 表示逐笔输入错误；2 表示输入/查询失败。

## 历史基线（v1，保留对比）

[机器可读摘要](feedback/early_feed_replay_2026-09-14.json)保存于 2026-09-14 UTC。

| Feed 类别 | 候选解析 | 语义层通过 | 历史归属层通过 | 最终提前决策通过 |
|---|---:|---:|---:|---:|
| BUY | 119/119 | 7/119 | 0，119 笔缺当时订单快照 | 0，可用性尚未证明 |
| SELL | 26/26 | 18/26 | 0，26 笔缺当时账户快照 | 0，可用性尚未证明 |

26 笔 SELL 签名均验证成功，但签名不替代账户实现快照；其中 2 笔原意向已 stale。
BUY 109 笔包装语义未验证、3 笔直接 0x 未完整解析；SELL 8 笔 0x 未完整解析。
另 11 笔补扫 BUY 不进入实时分母。后续资产对/订单核对 156 笔均匹配，不作为提前通过证据。

100 个已记录分发、14 个其他非交易、11 个失败非交易对照没有产生候选；另 100 个未定性
样本也未产生候选，但不能当作已证明负例。尚未得到独立验证的真实失败换币对照。
旧运行日志在这批记录中仅有 26 个 SELL 意向，Feed 影子决策全部 asset_not_allowed。

不存在可据此报告的实盘误跟率或端到端提速结果。下一步优先补包装语义并采集实时、带取得时间的
订单/账户/策略/持仓/报价影子快照，再做同一套完整检查；不通过放宽归属来追求高覆盖率。

## race 版本复跑（v2，2026-09-14）

[完整机器报告](feedback/early_feed_race_replay_2026-09-14.json) 保留旧版报告不覆盖。
规则版本 `early-intent-offline-v2`，同一 156 笔队列，加 225 个有来源的对照，共 381 条、无非法输入。

| Feed 类别 | 语义/参数规则通过 | 其中 race 缺当时部署快照 | 缺当时归属证据 | 最终提前决策 |
|---|---:|---:|---:|---:|
| BUY | 116/119（此前 7/119） | 109 | 119 | 0，不能证明历史提前可用 |
| SELL | 18/26（未变） | 不适用 | 26 | 0，不能证明历史提前可用 |

109 笔包装 BUY 全部符合新增约束；额外只读核验了 120 个历史区块（109 Feed + 11 backfill），
代码全部匹配固定 Keccak。**这是今天取得的事后证明**，未写入这些案例的 snapshots，不补造早期时点。
另有 3 BUY + 8 SELL 的直接 0x 内层仍不支持。125 个已分类非交易/失败非交易与 100 个未定性对照
依然没有候选；未定性不能算已证明负例。原日志仍有 2 个 stale SELL，不能因为代码支持就忽略。

```bash
.venv/bin/python scripts/replay_early_feed.py --mysql \
  --cohort data/early_feed_baseline_2026-09-14.json \
  --log var/log/sm-copy.log --controls --evaluate --audit-race-code --summary
```

`--audit-race-code` 最多 256 个不同历史区块、2 并发；固定 chain 4663、地址及代码哈希，
失败/缺块/不匹配单列。只使用既有端点，不读私钥库；不加此参数仍不进行任何代码 RPC 查询。
deployment 对非 race 路径为 not_applicable，不是匹配代码的成功记录。

包装器的 12 项实际字节码测试使用可选依赖，不改生产依赖。复现时装入新的临时目录：

```bash
RACE_TEST_DEPS=$(mktemp -d)
.venv/bin/python -m pip install --no-deps --only-binary=:all: \
  --target "$RACE_TEST_DEPS" -r requirements-race-tests.txt
PYTHONPATH="$RACE_TEST_DEPS" .venv/bin/python tests/test_relay_race_evm.py -v
PYTHONPATH="$RACE_TEST_DEPS" .venv/bin/python -m unittest discover -s tests -v
```

全量 261 项通过（含 12 个 EVM 测试）。未安装可选依赖时，普通 discover 会明确跳过 12 项；
单独运行 `test_relay_race_evm.py` 则直接失败退出，防止把跳过误报为字节码验证成功。
这使用 Py-EVM Cancun 内存状态及合成对手方；不是主网模拟、Gas 性能证明或完整合约安全审计。

## 已保存历史上下文重建（2026-09-14，纠正缺失口径）

前面的 v1/v2 回放导入器直接给每条案例设置 `snapshots={}`，没有重建已有辅助记录。
因此那些报告中的 `snapshot_missing` 只表示**没有传入评估器**，不能直接推断数据库没有保存。
原始报告保留；本轮新增 `--reconstruct-context`，实际读取固定队列相关的八类业务表与原日志。

```bash
.venv/bin/python scripts/replay_early_feed.py --mysql \
  --cohort data/early_feed_baseline_2026-09-14.json \
  --log var/log/sm-copy.log --evaluate --reconstruct-context --summary
```

结果见 [逐笔重建报告](feedback/early_feed_context_replay_2026-09-14.json)。`--summary` 仍保留每笔证据摘要、
来源行/主键与哈希；不加时输出完整字段清单。此开关不调用 RPC/Relay，不查私钥或当前配置，
在同一 READ ONLY 事务中读取，每项查询超过 10,000 行即失败而非截断后继续。数据库 session 须为 UTC。

本次读取：156 个候选、234 个信号、156 个提案、338 个决策、156 个执行计划、26 个 Solver 证据、
154 个成交和 69 条关联 lot 预留记录。队列仍是 145 Feed + 11 backfill，后者不纳入提前覆盖分母。

| 判断层 | BUY | SELL | 合计 |
|---|---:|---:|---:|
| Feed 原始交易 | 119 | 26 | 145 |
| 新规则解析通过 | 116 | 18 | 134（92.4%） |
| 解析且原始新鲜度通过 | 116 | 17 | 133（91.7%） |
| 旧程序实际记录的 Feed 决策 | 0 | 26 | 26 |
| 旧 Feed 决策接受 | 0 | 0 | 0 |

26 个旧 SELL 决策全部拒于 `asset_not_allowed`：旧动态目标资产放行依赖严格执行证据，
仅有 Feed intent 不能获得放行。不能将其解释为“未识别卖出”，也不能因为修复这一关就断言其余门禁全过。
两笔原始 stale SELL 中，一笔属于 18 笔受支持路径，另一笔已在 0x 未支持集合中，故 134 降为 133，不是 132。
134/133 是已跟单历史样本内的潜在意向覆盖，**不是归属验证通过、可发单率或全量准确率**。

| 已保存内容 | 实际核查结果 | 是否可直接作为提前输入 |
|---|---|---|
| 原始 calldata、Feed 时间 | 145 笔齐全 | 可重放解析及新鲜度 |
| 买入订单归因 | 119 笔有 request/order、来源付款等摘要 | 未找到这批订单的完整提前响应；摘要不含完整 orderData、来源状态及响应取得时间 |
| 卖出账户身份 | 26 笔有原始 UserOp 可验签、早于严格证据的 intent 日志 | 日志只有 `latest_not_historical` 等断言，缺完整 code/block/取得时间组合 |
| 报价与参考报价 | 145 笔均恢复，保留各自 observed_at | 全部在 Feed 接收后；可以比较旧流程，不能挪到接收瞬间 |
| 策略、预算、lot、预检 | 有策略哈希/归因、实际预检、预留后的预算或可变 lot | 不等于完整历史策略/急停、前置持仓/并发预留快照，不能用当前余额反推 |
| race 代码 | 上轮已验证固定版本与历史部署 | 事后审计保留，不伪装为当时已采集 |

复核的三个公开 Relay 响应夹具均不匹配这批交易/orderId；`account_codes.json` 也没有这批钱包。
报告列出具体归档及哈希。缺失结论限定于本次读取的业务表、指定日志与五个公开归档，不泛指一切外部存档。

恢复到 `snapshots.market` 的报价保持原内容、关系/配置绑定与真实时间。完整市场包还包含最后取得的 Gas，
因此其 `observed_at` 保守取不可变提案/决策行写入时间，而不是提前的 quote 时间。
原报价自己的时间不改；未来、畸形或跨关系记录不会被补成提前通过。完整决策仍为 `unverifiable`，
这意味着**还不能证明全部门禁可在当时通过**，不意味着证明所有交易都不能前移。

旧流程 Feed 接收至首次完整金额报价完成：BUY 中位数 3.805 秒（1.873–18.799），
SELL 中位数 1.765 秒（0.923–8.649）。这是旧流程累计耗时，不是聚合器单请求耗时或前移后的节省量。
样本 A：接收 epoch `1789361362.685537`，首次报价 +2.947 秒，主决策持久化 +3.542 秒，
执行准备计划持久化 +6.802 秒。决策内包含严格信号，只能证明严格证据最迟在该决策前已取得；
旧日志无墙钟时间，且 `signals.updated_at` 可被后续覆盖，所以不报告精确首次严格识别时间或虚构提前秒数。

新增 13 项历史重建测试；全量 274 项通过（含临时隔离依赖中的 12 个 EVM 测试）。
后续应针对这些真实缺口接入提前归属及动态目标门禁，再记录连续时点的通过/拒绝原因；无需从零重新验证已有解析覆盖。
本轮未改生产触发点、聚合器请求数或后台进程。

## 故障处理

- `order_snapshot_missing/account_snapshot_missing`：评估输入未提供该快照；先运行上下文重建区分未导入、部分保存和确实未找到，不能把今天查到的响应回填为过去取得。
- `wrapper_semantics_unverified`：旧 v1 的包装阻断原因；v2 分为参数检查与独立 deployment 门禁。
- `deployment_snapshot_missing/race_runtime_code_mismatch`：当时部署证据不足/代码不匹配；不能用事后审计替代。
- `zero_x_output_and_minimum_unparsed`：直接 0x 路径仍不支持，保留严格证据后备。
- `feed_was_not_fresh/decision_time_missing`：过期或时间缺失，不能猜测提前收益。
- 数据库连接失败：核对既有业务连接环境及权限。工具不会输出凭据，也不会切换到私钥数据库。
- `invalid_records > 0`：用完整报告定位错误 index，检查原输入；错误必须计入验收，不能静默剔除。
- 数量变化：hash 清单只固定源交易，不是数据库不可变快照。新增关系、账本变更或缺失记录可能改变结果；
  对照也可能随库变化。比较时同时保留捕获时间、选择口径、日志摘要和原始公开输入。

## 验证边界

运行进程、数据库关系、预算和触发模式均未改动；没有读取真实私钥、签名或广播。
现有生产主路径仍使用严格证据。聚合器三次请求目标属于后续独立改造，本次没有减少运行中的 API 请求。

## 独立实时影子采集（首版：识别与归属）

新增入口默认关闭，**不是提前下单开关**。本轮已完成一次 60 秒独立只读采集，未重启后台。
使用既有业务库只读凭据及两个 endpoint 环境变量，不需要私钥库、执行器或签名设置。

```bash
# 不联网、不连库：输出 shadow_enabled=false
.venv/bin/python scripts/observe_early_feed.py

# 明确开启独立只读连接，60 秒后停止；输出路径必须不存在
.venv/bin/python scripts/observe_early_feed.py --enable-shadow --seconds 60 \
  --output docs/feedback/early-shadow-first.jsonl

# 采集结束后离线汇总，不使用历史最终成交倒填提前判断
.venv/bin/python scripts/summarize_early_shadow.py docs/feedback/early-shadow-first.jsonl
```

首版的范围是：Feed 候选 → 并行取得 Relay 订单（BUY）或按区块哈希取得账户代码（SELL），
同时读取当时关系配置、预算、持仓及重复消费信息，race 路径还采集独立部署代码快照
→ 固定提前评估 → 独立轮询已有严格证据。
**尚不采集市场报价、构建或签名前预检**，因此不能用首版报告宣称完整提前资格或真实提速。
那 109 笔包装路径现已通过参数规则，但缺当时部署/订单证据，不因此成为历史提前成功。

- `candidate_started`：候选、路径及规则版本；开始后未完成的采集可在 counters 中看到。
- `early_case`：原始公开交易参数、收到时间、查询起止时间、实际取得的快照、固定评估结果。
  `capture_errors` 只保留错误类型；超时/空订单不会填成有效证据。金额为十进制字符串。
- `reconciliation`：晚到的严格证据与匹配标签；不写回 early_case。同一候选最多轮询约 30 秒，
  首次 matched 后仍可记录 orphaned。此时间窗口不保证发现之后的重组，也不代表 L1 finality。
- `run_finished`：队列溢出、过期 Feed、未完成采集/核对等计数。缺少该行表示运行未正常收尾。

默认 2 个采集 worker、32 项候选队列、256 项核对队列、64 MiB 文件上限；慢请求不拖住 Feed
接收事件循环。出现序号缺口停止本次采集，不自动重连或把补发当成新鲜意向。队列满会丢弃并计数。
时长限制针对 Feed 采集；退出时已在执行的只读线程还要等其有界连接/读取超时，不保证精确到秒。
文件独占创建，磁盘写失败或大小超限停止采集。数据库查询始终 READ ONLY，退出 rollback/close；
单个快照超过读取时间或行数上限会失败，而不是截断后声称预算、持仓或去重完整。

关系钱包清单在启动时读取；运行中新加的聪明钱需另开一次采集。已有钱包的关系及配置每次重读，
按 relationship/config hash 绑定，不能跨关系借持仓。stop 状态只反映此影子进程环境及共享停机文件，
不声称读取了后台进程环境；默认停机环境值仍按关闭执行处理，请勿为了提高覆盖率更改停机门禁。

汇总按 BUY/SELL + route_kind 分组，分母是已记录的候选×关系；解析失败及队列丢弃另列，
不是全部链上交易覆盖率。p50/p95 是 Feed 接收到**采集评估结束**，不是下单或上链延时。
pending 不算误判；`actual_misfollow_rate=null`，因为没有真实影子跟单。报价/准备尚缺时决策不会通过。
BUY 查询复用现有按目标交易 hash 的 Relay 接口；若该索引直到入块才可见，将直接暴露其晚到，
目前未声称已实现按 requestId 提前查单或源链付款独立验证。JSONL 请使用专用汇总命令，
不要当成旧 replay 命令的 JSON cases 文件输入。

本轮 60 秒 [原始只读采集](feedback/early-shadow-race-smoke-2026-09-14.jsonl)：1186 帧、1 笔相关交易，
为尚未支持的 Multicall3 `0x82ad56cb` 路径，保留 unsupported_path，未生成提前候选。
队列无残留、正常停止；没有命中 race BUY，因此**没有**现场订单/部署可用性或延时收益结论，
也不能把这笔未知路径说成已证明非交易。部署采集分支由离线完整代码/错误代码快照回归覆盖。
