# Feed 提前跟单：停机准备结果与人工启动交接

检查时间：2026-09-15 00:02–00:05，Asia/Singapore（UTC 为 2026-09-14）。
功能代码提交：`3dfb71b`，已按用户指定推送到 `origin/main`。

## 当前状态

**已完成可代办的生产停机准备，但没有启动实盘，也没有开始试运行计时。**
关闭会话后不会自动开始提前跟单，当前也没有旧跟单进程继续运行。

- 先创建 `var/EXECUTION_STOP`（0600），核对账本没有在途执行后停止旧 PID 45757。
  SIGINT 未退出，重新验证命令和工作目录后使用 SIGTERM；最终未发现本项目 run/monitor 实例。
  旧 PID 文件保留，不能把其中的数字当成仍存活的进程，也不要盲目再次 kill。
- 已在原业务 MySQL 应用 008、009、010。四张新表存在且为零行：
  `copy_operation_claims`、`early_trials`、`early_trial_operations`、`early_feed_jobs`。
- 运行账号权限已核对：前三项中的 claim/trial 及 feed jobs 为 SELECT/INSERT/UPDATE，
  early_trial_operations 仅 SELECT/INSERT；没有扩大到私钥库或其他表。
- 关系 1、2、3、4、5、9 的 `execution_providers` 已从 `["local","kyber"]` 改为 `["kyber"]`。
  `allowed_assets=[USDG]`、`trigger_mode=evidenced`、金额、滑点、Gas、预算及其他策略字段均未改。
- 变更事务逐字段比较关系配置，并逐行比较全部 14 行预算和 1 个周期，与停机后快照一致；
  没有清仓、释放原有投入或重置额度。
- 没有代签新配置的风险确认：`live_risk_accepted_at` 保留原值，`updated_at` 因配置变更自动更新，
  六条关系均 `live_risk_acceptance_current=false`。这加上急停是有意保留的启动阻断，不是故障。
- 没有创建 trial、读取真实私钥、发送授权/买卖交易，或启动任何新的 Feed 订阅。

本地恢复/核对快照（0600，Git 忽略，不推送）：

- `var/prelaunch-before-20260914.json`：变更前公开关系配置、预算和周期。
- `var/prelaunch-chain-20260915.json`：只读链上余额、nonce 和逐 Token 授权核对。

不要整表恢复快照：重新运行后账本可能已有进展。回退也须保持急停、确认停机并仅恢复经核对的
配置字段，不覆盖预算/持仓、不重放整个 Docker init、不删表或卷。

## 核验结果

361 项测试通过，含 12 项可选 EVM 测试；pip check、compileall、diff check 通过。
本轮没有新增主网提前交易样本；测试通过不替代人工风险清单或现场故障验收。

execution-audit：healthy，issues 为空；156 个历史 attempts 中 154 confirmed、2 reverted，
prepared/signed-pending/observed_pending/orphaned 均为 0。历史 signed plan 数不是在途数。

只读链上检查（仅代表该时点，不是启动时余额保证）：

- chain ID：4663。
- follower：`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`。
- latest / pending nonce：197 / 197。
- native balance：`5967395738480080` wei；USDG balance：`11929877` raw。
- USDG 对 Kyber Router 的 allowance：`1988700000` raw。
- 36 种现有归因持仓 Token 的链上余额均覆盖账本归因数量。
  其中 27 种 Kyber allowance 为零，28 种不足全部归因持仓量（包含这 27 种）。
  “不足全部持仓”不等于不能进行任何部分卖出，是否足够仍以本次计划输入量检查。

提前分支不新增 approve，因此授权不足会退到严格后备；严格分支在原有门禁下处理有界授权。
本次未自动补授权。新试验必须单列 allowance 拒绝，否则会误把授权限制当成解析没有覆盖。

六条新配置快照如下，风险确认时间本身不参与策略快照哈希：

| 关系 | config_snapshot_hash |
| --- | --- |
| 1 | e590bf27ad4f93132170f3aa7a7b2b49de871233b92972f6721b1d61ae1c1dbc |
| 2 | 8bdea6ea94c555c7e3bf466cd91f7cd6be6f9615a80be422dc10586eb71fc9c6 |
| 3 | 8713db1734c65d85cfb411dfbe4d854e876422cdfb84ed9f410c2b9a638fe518 |
| 4 | 34cd339e5d4fc3d70fb5a48bfd50655c9342897ee52b5f6e7d30b3127d923524 |
| 5 | e5496c0deb86a7f3c59277667094ddb9df48cd5e9e85dc45ef605a46342db2b2 |
| 9 | f153cabf8ad3900efdd7b88ff31b41c17c0fa1af786a029bb8c4201c193ac55b |

## 剩余人工步骤（尚未执行）

1. 阅读并完成 `LIVE_RISK_CHECKLIST.md` 的适用验收，确认钱包、金额、权限、故障处置和看守安排。
   文档仍有未验收项，不能仅凭本轮单测数或“准备完成”声明为无人值守实盘就绪。
   确认同 follower 没有其他主机实例；本机文件锁不能证明这一点。
2. 保持急停，重新运行 `execution-audit --ledger-mysql`、核对余额/nonce，再对每条关系运行
   `relationship-status --relationship-id ID`，核对上表及实际策略。由操作员在业务库中为这六条
   精确关系刷新 `live_risk_accepted_at=CURRENT_TIMESTAMP(6)`，其余字段不变；再次确认六条均
   `live_risk_acceptance_current=true`。如果任何快照变化，应先重新验收，不沿用本页确认。
3. 临近实际启动时才初始化窗口；不要提前一晚运行它消耗 24 小时：

```bash
.venv/bin/sm-copy early-trial-start \
  --trial-id feed-early-20260915 \
  --follower 0x3004ab92565deeea0a2eaa27e40e297bb457e1a6 \
  --relationships 1 2 3 4 5 9 --confirm-risk-checklist
```

该命令不清除急停。`--confirm-risk-checklist` 必须对应实际人工验收，不是自动完成清单。
重复同 ID 不延长窗口，同 follower 不允许换 ID 自动续期。

4. 最终启动由操作员本人执行：按运行手册归档急停文件，沿用原部署环境启动唯一进程。
   以下只是命令示例，助手未执行；应从仓库根目录执行并在离开前核对启动日志：

```bash
nohup .venv/bin/sm-copy run --early-trial-id feed-early-20260915 \
  >> var/log/sm-copy-early-20260915.log 2>&1 < /dev/null &
```

不要同时运行 observe_early_feed.py 或另一份 run/monitor。启动拒绝时不要删账本、改 nonce、
绕过急停或重复启动。24 小时或 100 个提前广播名额先到时只停止新增提前交易，严格分支继续；
停止所有新交易需要急停。广播名额是可能广播的保守上界，不是成功上链数。

5. 有真实新样本后使用只读报告：

```bash
.venv/bin/python scripts/summarize_early_trial.py \
  --trial-id feed-early-20260915 --log var/log/sm-copy-early-20260915.log
```

比较提前于严格证据的广播确认、分段 P50/P95、我方与源交易区块差、已知误跟及未知比例。
`live_execution_send_started` 是发送函数入口，不是精确网络发包时刻；同块不能证明先后。
无新样本时不能报告延时已改善，也不能把旧影子 asset_not_allowed 计为新提前分支失败。
