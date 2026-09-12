# 开发交接：在新工程继续

更新时间：2026-09-11。

新服务器接手时先按 [SERVER_HANDOFF.md](SERVER_HANDOFF.md) 复现功能基线，
再推进后续只读开发。该文包含测试命令、验收口径、开发优先级和可复制提示词。
已推送的初始代码基线为 `main / 67c86b5`，以服务器实际 HEAD 为准，不要重置后续改动。
下文 `/home/jelly/...` 是原开发机路径；服务器应使用自己的仓库路径，不依赖旧工程目录。

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
  当前共 106 项测试。保守分类和 `copy_eligible=false` 均未改变。
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

## 下次开发建议：先把 M2 的确认链路做完整

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
