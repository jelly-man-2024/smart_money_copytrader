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

- 尚未持久化独立区块游标；Feed sequence 不能当 L2 block。断线后仍没有按区块/回执补洞。
- 尚未规范链重查或重组撤销；signals 的 `canonicality` 仍是
  `not_rechecked_for_reorgs`。
- complete/failed 候选目前保留审计记录，尚未定义归档周期，长时运行需测数据库增长。
- V2/V3 factory/pool、V4 settlement recipient、原生 ETH/Gas/退款资金流仍未闭环。
- 被动 Transfer 区块兜底和 Solver 订单证据关联未实现；仅收币仍不能判断为 BUY。
- 本轮分位数只有单个历史恢复样本，不能当延迟 SLA、地域比较或吞吐结论。

下一小步应建立独立的规范区块游标和有界区块补洞：新增只读 `eth_getLogs` 前先扩展允许列表
及拒绝广播回归，区分 live/backfill；随后基于区块哈希复查实现重组撤销/重算。完成采集可靠性
后再推进池/资金流验证、被动入账兜底和 Solver 归属，暂不进入纸面报价或 M3 实盘。
