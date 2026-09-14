# Feed 提前跟单：实现与操作员切换交接

2026-09-14。本文件描述尚未部署的工作树，不是实盘已启动报告。

## 当前结论

已接通同一 Feed 接收器中的提前队列 → 重新验证的意向 → 原有金额/预算规则 → Kyber 报价与
构建 → 预检 → 签名复核 → 持久发送防重门禁。正常入口仍为严格证据；只有显式选择一个已由
操作员启动的 trial，才连接提前执行回调。观察字段 `copy_eligible` 不承担交易授权职责。
测试中的签名只使用内存生成、无资金的临时密钥；没有访问真实私钥或发送真实交易。

本轮验证：361 项单元/集成测试通过（包括可选 EVM），pip check、compileall、diff check 通过。
真实 MySQL 使用随机 `earlytest_` 前缀的独立表验证并发单赢家、预算原子性、取消/后备交接、
重开连接防重、试运行窗口不重置、源证据先于本地成交、源重组作废持仓依据；每轮 24 张合成
测试表均清理，未写现有业务表数据。不能将这些测试替代真实主网提前交易表现或全部异常演练。

生产只读核对结果（仅代表检查时点）：

- 关系 1、2、3、4、5、9，follower `0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`，
  均为 enabled mainnet_live/evidenced，行内风险确认有效；执行提供方仍是 `["local","kyber"]`。
- 每条关系 USDG 周期上限 `10000000` raw、ETH_WETH 上限 `1` raw。未改金额、额度、Gas、滑点或周期。
- 业务库尚无 copy_operation_claims、early_trials、early_trial_operations、early_feed_jobs。
- execution-audit healthy：156 attempts，154 confirmed、2 reverted，无 prepared/signed-pending/
  observed_pending/orphaned 待处理项。这里 signed-pending 指 attempt 状态，不是历史签名总数。
- 主网 latest/pending nonce 均为 197；native 余额 `5967395738480080` wei，USDG `11929877` raw。
- 原进程 PID 文件指向 45757，当时仍存活。此 PID 不能直接作为之后的停机目标，必须重新核对。

因此没有执行生产迁移、更新关系、清除急停、重启旧进程或开始提前实盘，没有新延时效果可报告。
最终会启动真实自动交易的操作须由操作员本人执行。

## 新链路的安全边界

1. 原始 Feed 交易重新解析，唯一钱包/路径、订单或 UserOp 归属、固定部署/委托证据必须通过。
   不从 `recognized_intent=true` 布尔值或普通收币事件推导交易许可。意向始终是 intent/pending。
2. 从 Feed 时间和接收时间计算的 3 秒窗口贯穿决策、准备、签名前复核和最终发送检查；晚到、
   断线、过期或证据不足转严格后备。快照完成时间不能掩盖缓慢采集过程。
3. 提前只走 Kyber，复用同一操作/钱包/配置绑定的报价上下文。常规无等待合同为两次 routes
   （完整金额与参考量并行）加一次 build；超时、过期、输入变化仍刷新。没有删除余额、nonce、
   allowance、配置、预算或 exact-calldata 模拟检查，不能保证所有交易只有三次 HTTP 请求。
4. BUY 按已核对订单付款额/固定规则计算，SELL 按声明卖出量映射到已确认的本关系 source lot。
   动态目标币可通过，但本金资产仍必须受信；跨本金资产缺价格依据、未知 source lot 都拒绝。
5. **提前入口不新增 approve**。现有 Kyber allowance 不足就交给严格分支，不等待一笔授权后
   仍称为立即跟单。因此第一次卖出某个新 Token 可能仍不能提前；报告应单列此拒绝原因。
6. 提前与严格分支共用 chain + smart wallet + orderId + relationship + follower 操作键。
   新版本会检查旧提案关联的源订单，避免升级后重复下单；试运行过期、停止或重启不传提前参数，
   已记录 trial scope 的严格分支仍保留操作级去重。曾创建计划的取消保守保留同订单占用。
7. 我方成交和源交易结果独立。先成交的早期 BUY lot 标 pending，不拿预计输出当源持仓；
   严格证据先到或后到均可补核对实际数量。源失败/不匹配不释放我方已投入本金；源重组清除
   可用比例依据并保留资产和防重记录，恢复后须人工复核，不能自动把旧依据重新放行。
8. 原有 signer/reviewer 的严格构建入口没有放宽；提前使用独立 typed-intent 构建入口，
   recipient、router、金额、value 和链上最低输出继续逐字段验证，源最低输出不容许减一舍入。
9. 单实例文件锁加现有 PID 存活检查，防止同机重复启动；锁不是跨主机分布式锁。操作员须确认
   没有其他机器复制同一 follower；不要同时启动独立影子 Feed 收集脚本。
10. 广播结果不明、结算异常、重组及持续跟踪失败会触发内存锁定和持久急停，不自动清除。
    提前试运行进程中的已发送订单在窗口过期后仍跟踪；源失败不触发额外清仓。重启存在未知
    执行或未结提前提案时拒绝继续自动执行，交操作员核对，不盲目重签重发。

## 操作员切换顺序（尚未执行）

先完成并记录 `LIVE_RISK_CHECKLIST.md` 的操作员验收，复核当前代码、原金额规则、钱包和配置快照。
`--confirm-risk-checklist` 是操作员声明，不会自动完成验收，也不是由测试总数推导出来的结论。

1. 核对 PID、命令和工作目录，确认唯一旧实例；创建 `var/EXECUTION_STOP`，再停止那个精确 PID，
   等待退出并重新做 execution-audit。不删除账本、不重置预算、不手工改 nonce。
2. 使用有相应 DDL/GRANT 权限的业务库管理员，检查后执行迁移 008、009、010。不要重放整个
   docker init，不删 volume，不操作独立私钥库。核对四张新表及运行账号权限。
3. 只修改这六条关系的执行提供方，同一 UPDATE 刷新风险确认；其他策略与金额字段不动：

```sql
UPDATE copy_relationships
SET execution_providers = JSON_ARRAY('kyber'),
    live_risk_accepted_at = CURRENT_TIMESTAMP(6)
WHERE id IN (1,2,3,4,5,9)
  AND follower_wallet = '0x3004ab92565deeea0a2eaa27e40e297bb457e1a6'
  AND enabled = TRUE AND run_mode = 'mainnet_live';
```

确认正好六条目标关系，逐条 `relationship-status --relationship-id ID` 保存新快照；重新核对原
预算的 invested/reserved/limit 均未重置，以及链上余额、allowance、pending nonce。

4. 急停仍保持有效时，由操作员显式初始化窗口：

```bash
.venv/bin/sm-copy early-trial-start \
  --trial-id feed-early-20260914 \
  --follower 0x3004ab92565deeea0a2eaa27e40e297bb457e1a6 \
  --relationships 1 2 3 4 5 9 --confirm-risk-checklist
```

此命令只记录固定窗口，不清除急停、不重置预算、不广播。重复同 ID 不延长窗口；同 follower
不允许换 ID 自动续期。窗口从此命令首次成功开始，不是从首次成交开始。

5. 最终安全检查通过后，操作员本人按运行手册归档急停文件，沿用原部署环境启动唯一进程：

```bash
.venv/bin/sm-copy run --early-trial-id feed-early-20260914
```

若交给后台任务管理器，确保 stdout/stderr 持久保存到本轮独立日志；程序会持有实例锁并写
`var/sm-copy.pid`。出现另一个存活 PID、未知在途记录、缺表、配置/范围不匹配时启动会拒绝。
不要为了绕过拒绝直接删 PID 文件、解除急停或修改已签名账本状态。

24 小时或 100 个提前广播名额先到即停新增提前交易，严格分支继续。名额是网络调用前持久
预留的“可能广播上界”，失败不退款，不等于 100 笔成功上链；approval 不占名额。
若要停止所有新交易，应重新创建急停文件，不能仅等待提前窗口到期。

## 怎样看效果

```bash
.venv/bin/python scripts/summarize_early_trial.py \
  --trial-id feed-early-20260914 --log var/log/本轮日志文件.log
```

只读业务账本和指定日志，无私钥或广播能力。查看：

- early_feed_counters 与 early_handoff_rejected：识别/排队/归属/决策/allowance 各层为什么没进入发送。
- early_quote_requests：routes/build 次数、复用和刷新；不能只凭配置认为已减少实际请求。
- feed→decision、decision→prepared、prepared→signed、发送函数入口→广播确认、feed→本地结算
  观察时间的 P50/P95。这些是记录的墙钟延迟，不冒充交易真正进入区块的精确秒数。
- 广播确认是否早于本地严格证据可用时间，以及我方与源交易的区块高度差。
  广播确认不等于上链；同块不证明具体先后，不推断“没有优化时同笔交易会多慢”。
- matched/source_failed/source_orphaned/source_mismatch/unknown。未知不算正确，也不自动算错误；
  已知误跟数除以有广播确认的提前提案数，另列未知比例。广播未确认/结果不明另列，不冒充成功。

原进程日志中最近可读到的一份旧健康摘要：live_execution_ms 10 个样本，P50 8240.59ms、
P95 11803.607ms。它是旧实现的一个历史窗口，不是本次更新后的表现，也不是逐笔对照实验。
实际是否改善必须等待操作员切换后有足量新样本，再分 BUY/SELL、授权需求和路径做对比。
