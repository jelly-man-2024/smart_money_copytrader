# 服务器验证记录（2026-09-12）

## 纸面跟单 Goal：账本第一步

- 工作目录：`/home/kuai/applet/smart_money_copytrader`；基线 HEAD 仍为
  `eb527b2`，进入本轮前已有 M2 未提交改动，全部保留。
- 本轮只增加纸面策略和 SQLite 账本骨架，没有私钥、签名、广播、真实订单或 RPC 写方法。
- 新增三档触发条件的保守门控，以及比例/固定金额和 USDG、ETH/WETH 额度桶计算。比例模式只
  接受回执资金流给出的 `actual_input_debit_raw`；固定模式也必须有明确支持的输入资产桶。
- 新增手动额度周期、每钱包/资产桶预算、事务化 proposal/reservation。金额以十进制字符串
  存储并用 Python 任意精度整数核算，测试覆盖超过 SQLite 64 位整数范围的原始金额。
- proposal 幂等，额度检查把 reserved 和 invested 同时计入；创建 proposal 与增加预留在
  `BEGIN IMMEDIATE` 事务中完成。存在活动预留时禁止重置周期。底层账本还会独立验证输入/
  回款资产与额度桶一致，不信任调用方提供的 bucket。
- 新增取消 proposal 释放额度，以及纸面 BUY 成交原子地把 reserved 转为 invested，同时创建
  paper order、fill 和带聪明钱归因的 position lot。重复提交同一 fill 不重复占用额度。

验证命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

第一步结果：89/89 unittest 通过；`git diff --check` 通过。新增 5 项纸面策略/账本回归，覆盖
触发拒绝、实际输入比例、分桶、超限、幂等、重启恢复、带在途预留禁止重置、取消释放以及
BUY fill/lot 归因。

## 真实时点报价、风险门控与 SELL 账本

- 新增官方 Robinhood 部署的 V3/V4 Quoter 地址，V2 使用已登记 Router 的
  `getAmountsOut`。所有报价先取得最新 header，再把 `eth_call` 固定到该区块号；记录
  protocol、来源合约、block number/hash、本地观察时间和原始输入/输出整数。
- V2/V3 将 native 映射为 WETH 参与路径报价；V3 支持已解出的有界多跳 path；V4 当前只支持
  已验证单跳 PoolKey/hookData，多跳继续拒绝，不用不完整路径报价。
- 同一区块同时取得计划金额报价和 1% 小额参考报价，以整数有理数计算预估价格冲击；同时检查
  相对聪明钱实际成交价的不利偏离、报价年龄、最小输出、滑点 `minOut` 和 Gas 上限。
  `eth_gasPrice` 是本轮唯一新增 RPC 方法，仍为只读；V2/V3/V4 使用保守 gas unit floor，
  V4 另参考 Quoter 返回值并增加固定 overhead。
- PaperEngine 已把触发、金额、报价、风控和事务额度预留串联。接受和拒绝决定都按
  source event/trigger/strategy 幂等持久化；报价超时、偏离、冲击、Gas 或额度失败均不预留。
- 新增 SELL position reservation，只能占用同一聪明钱、同一 token、同一原始额度桶的 open
  lots，不能出售其他钱包或其他归因仓位。模拟成交按卖出 token 比例释放每个 lot 的原始本金，
  更新 invested 额度，并单独保存本金资产 realized PnL 与分摊 gas wei；不同币种不相加。
- PaperEngine 已增加 SELL 决策入口：比例模式使用回执验证的聪明钱实际卖出 token 数量，固定
  模式使用配置的 token 原始整数；持仓不足时拒绝，不跨钱包或跨额度桶补卖。

2026-09-12 实际只读 RPC 探针（固定输入 `1000000000000000`）：

- V2 WETH→USDG：block 60798155，output `2504897`。
- V3 fee=500 WETH→USDG：block 60797909，output `2511112`。
- V4 ETH→CRIBS：block 60798041，output `246105865042814718725027`，
  Quoter gas estimate `79735`。
- V3 同区块主/1%参考报价：block 60798920，分别为 input/output
  `1000000000000000/2510707` 与 `10000000000000/25107`；gasPrice `103088000` wei，
  此样本整数计算价格冲击为 0 bps。单次样本不是 SLA 或盈利证明。

本阶段结束时 106/106 unittest 通过。新增覆盖包括 V2/V3/V4 calldata 与固定区块、报价过期/
资产错配/不利偏离/价格冲击/Gas/slippage、PaperEngine 接受/拒绝和 SELL lot 归因、取消释放、
部分卖出本金恢复及 PnL，以及资产桶错配拒绝和引擎级比例跟卖预留。

## 可运行纸面配置与影子触发

- 新增严格、无秘密字段的 `config/paper.example.json`；未知字段、重复钱包/资产、主/影子触发
  重叠、额度与买入规则缺桶均拒绝。默认主触发 `swap_evidenced`，Feed/receipt 为 shadow。
- monitor 只有显式提供配置并选择 `--paper-cycle-action reuse|reset` 才启用纸面模式；reuse 会
  核对活动周期额度与配置完全一致，reset 要求 cycle id/reason。普通 monitor 行为不变。
- 主档通过会建立预留并再次实时报价；第二次报价仍须通过风控且不低于首次 `minOut` 才写入
  paper order/fill/position，否则取消并释放预留。影子档只写 decision，不占额度。
- BUY/SELL fill 均保存 Gas wei；DEX 报价输出已含池费，当前无法独立观测的 fee 字段记 0，
  不把它误报为链上实际手续费。
- reuse 启动会查找同一策略/主触发下仍为 reserved 的提案，从持久化 signal 恢复完整路由并
  重新报价；源信号缺失、移出允许范围、已孤块或报价失败时取消提案并释放额度。
- `paper-mark` 会反转原 BUY 的已验证 V2/V3/V4 路径，为每个 open lot 写入固定区块、报价来源、
  毛回款、剩余本金、原本金币种未实现 PnL 和单列 Gas 的不可变快照。V4 多跳 Quoter ABI 已按
  官方 `QuoteExactParams`/`PathKey[]` 实现并做 calldata 回归，但尚缺目标链真实多跳池样本。
- 配置新增必填 `allowed_routes`：V2 精确到资产路径，V3 另包含每跳 fee，V4 另包含每跳 fee、
  tickSpacing、hook 和 hookData。方向反转视为同一路由；更换中间资产、fee、spacing、hook 或
  hookData 均拒绝。
- `paper-export` 逐笔导出 paper fill，并带聪明钱、源 signal/tx、触发档、策略版本、决策报价、
  成交原始金额、Gas、聪明钱配置标签、决策时不可变 source signal 快照和当前源信号
  canonical 状态；便于重组后同时审计“当时为何成交”和“之后规范链状态如何变化”。

真实 RPC 纸面端到端命令：

```bash
.venv/bin/python scripts/validate_paper_readonly.py
```

2026-09-12 实测通过。脚本以 V3 fee=500 WETH→USDG、固定输入
`1000000000000000` 做同轮源价、主决策、二次报价 fill 和反向 position mark：源价区块
60819475、Quoter `0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7`；paper fill 输出
`2509823`、估算 Gas `30274800000000` wei；mark 区块 60819477、毛回款
`999000020180517` WETH raw、未实现 PnL `-999979819483` WETH raw，mark Gas 单列
`30274800000000` wei。结果中 `copy_eligible=false`、`live_trading=false`。内存数据库在进程
关闭后销毁；该样本证明本轮只读链路可运行，不证明收益、SLA 或所有池均可报价。

## 60 秒纸面监听接入验收

使用临时、未提交的配置把清单首个公开地址加入纸面钱包范围，并显式新建额度周期；配置仍以
`swap_evidenced` 为主档、Feed/receipt 为影子档。实际运行完整输出位于
`/tmp/smart-money-paper-live-rerun-2026-09-12.log`，数据库位于
`/tmp/smart-money-paper-live-rerun-2026-09-12.sqlite3`。

- `monitor_started.seconds=60.0`，最终正常输出 `monitor_finished`，全程
  `live_trading=false`。
- Feed 健康且一次连接：1,238 frames，跳过 5,089 笔旧回放，解码 4,910 笔新鲜交易；
  `reconnections=0`、`frame_errors=0`。
- 独立规范链游标从 block 60823819 推进到 60824402，共补查 583 个 safe-head 区块；
  `backfill_errors=0`、`reorg_detected=0`。
- 本时间窗没有命中观察地址：candidates、receipts、signals 和 paper decisions/fills 均为 0；
  队列为 0，候选 pending/queued/retry/failed 均为 0，worker/纸面错误均为 0。

这次验收证明纸面配置、额度周期、Feed、RPC 补洞、健康日志和正常停机已一起运行；由于没有
候选，它不能单独证明候选→回执→报价→fill 链路。该链路由上面的实际 RPC 纸面端到端探针和
106 项离线回归共同覆盖，不能把“零候选”误报为完整成交验证。

## 尚未完成

- V4 多跳真实链样本和跨 V2/V3/V4 混合路径报价；真实报价的持续样本和延迟分位数。
- 纸面执行延迟的持续分位数和更丰富的进程崩溃注入测试。
- 组合 USD 风险换算以及跨资产 Gas 成本折算未实现；realized/unrealized PnL 均只在原始本金币种
  内计算，Gas 单独保留 wei。
- 当前 `copy_eligible=false` 不变；本文件记录的是开发中的纸面账本，不是实盘能力。

## 多数据源实盘准备 Goal：MySQL 配置第一步

- 新增 MySQL 8.4 Docker Compose，仅绑定本机 `127.0.0.1:3308`，未占用服务器已有的 3306、
  3307 数据库；容器 healthcheck 通过。
- 初始化单表 `copy_relationships`，唯一键为 `(follower_wallet, smart_wallet)`；一行包含双方
  公开地址/标签、唯一触发策略、USDG 与 ETH/WETH 规则和额度、跟卖规则、报价风控及精确路由。
- 新增 MySQL 配置读取和 `--paper-mysql`，数据库行仍复用严格 PaperConfig 校验；启用行若来自
  多个跟单钱包、同一聪明钱重复或公共风控/路由不一致会拒绝启动。
- `relationships-import` 已把 CSV 的 67 个不重复聪明钱导入，跟单钱包为明确的零地址占位符，
  全部 `enabled=0`；实际查询为总数 67、启用数 0、不同聪明钱 67。
- 增加 PyMySQL 1.1.2 锁定依赖和 MySQL/离线 signer 回归；全量测试增至 109 项并通过。
- 本机运行账号实际授权仅为 `SELECT smart_money.copy_relationships`；导入使用独立可写账号。
  MySQL 8.4 `caching_sha2_password` 通过 TLS 连接，本机回环接受容器自签名证书，远程主机没有
  `SMART_MONEY_MYSQL_SSL_CA` 时会在连接前拒绝。
- 临时启用一条模板后，`paper-cycle reset --paper-mysql` 用只读账号成功加载 1 个钱包并建立
  SQLite 周期；随后已恢复为 67 条全部禁用。MySQL 行生成的配置保存 follower wallet、关系 ID
  和 SHA-256 配置快照哈希，PaperEngine 将其写入不可变成交归因。
- 当前表没有私钥字段，运行仍无签名/广播。独立私钥数据库属于新 Goal 后续步骤，必须先完成
  脱敏、权限和离线签名风险门控。

独立私钥 MySQL 已作为第二个实例初始化在 `127.0.0.1:3309`；`wallet_keys` 当前为 0 行，
没有导入任何真实或测试私钥。运行账号实际授权仅为 address/private_key/enabled 三列 SELECT。
`OfflineDatabaseSigner` 只有 `SMART_MONEY_SIGNING_MODE=offline_test` 才能查询，远程连接强制 CA；
它要求按公开地址精确命中一条 enabled 记录、重新派生地址一致，并严格限定 chain 4663 type-2
交易字段。单测使用运行时随机无资金密钥完成离线签名与 sender recovery，密钥没有写盘或输出。
该模块尚未接入 observer/paper/RPC，RPC 广播仍拒绝，不能据此宣称具备实盘执行能力。

新增 `UnsignedExecutionPlan` 和 `ReadOnlyExecutionPreflight`：严格限定 chain 4663、允许目标、
calldata/value、正整数输入、minOut、EIP-1559 fee、报价区块/hash/年龄和 deadline；通过只读
`eth_getTransactionCount(..., pending)`、`eth_getBalance(..., pending)`、`eth_gasPrice` 与 ERC-20
`balanceOf` 检查 pending nonce、Gas 上限和余额。`eth_getTransactionCount` 已加入只读 RPC
allowlist，广播方法仍未加入。

SQLite 新增持久 `execution_nonce_reservations`，以 follower/chain/nonce 唯一，proposal 幂等，
`BEGIN IMMEDIATE` 分配第一个可用 nonce；状态只能按 reserved→signed→broadcast→confirmed 或
受控 release 转移。回归覆盖连续分配、重复 proposal、关闭重开恢复、非法状态跳转、token/native
余额、Gas 和报价过期。全量测试当前为 111 项。

新增 exact-input 执行 calldata 构建器。V2 根据 native/token 方向选择
`swapExactETHForTokens`、`swapExactTokensForETH` 或 `swapExactTokensForTokens`，V3 支持单跳
`exactInputSingle` 和有界多跳 `exactInput`；recipient 强制为 follower，amountIn/minOut/deadline
来自已核验计划，只接受 `swap_evidenced`、非孤块、精确路由 allowlist。回归把生成 calldata
重新 ABI 解码核对字段。ERC-20 预检还增加
`allowance(follower, router)`，避免只有余额但 Router 无授权时生成必然 revert 的计划。
全量测试当前为 112 项。

V4 执行范围随后扩展到严格的单跳 native-input：只接受已 `swap_evidenced` 的 PoolKey、完整
hookData、精确 route allowlist，以及零 hook 或登记过历史代码哈希的 hook；生成 Universal
Router `V4_SWAP`，内部固定 exact-input swap + `SETTLE_ALL` + `TAKE_ALL`，value 等于 amountIn，
deadline/minOut 固定，输出 recipient 由调用者即 follower 解析。回归将 calldata 交回项目 Decoder
确认方向、金额和 settlement。V4 token-input/多跳继续拒绝，避免猜测 Permit2 或未验收组合；
系统也不生成 approve/无限授权交易，只检查 V2/V3 已有 Router allowance。

新增默认只停在 prepared 状态的 `ExecutionPreparer`，按顺序执行：读取 reserved proposal 和
不可变 follower/relationship/config hash 归因、二次实时报价与风险评估、构建 V2/V3 calldata、
只读余额/allowance/Gas/pending nonce 预检、事务 nonce reservation、持久 execution plan。
`execution_plans` 以 proposal 唯一且只保存公开字段、未签名 transaction 和 preflight 证据；不含
私钥或签名。回归覆盖首次准备、重复调用不重复报价/nonce、关闭 SQLite 后重开恢复。为兼容已有
JSON 账本，无 relationship 的旧 PaperEngine ID 算法保持不变。全量测试当前为 113 项。

新增 `OfflineExecutionSigner`，仍受 `SMART_MONEY_SIGNING_MODE=offline_test` 硬门禁。它只接受
prepared plan，并重新核对 proposal/source canonical 状态、relationship/config hash、nonce
reservation；随后重新报价和评估偏离/冲击/slippage，再复查余额、allowance、Gas 和 pending
nonce。报价跌破原 calldata minOut 或 pending nonce 已越过预留值均拒绝。测试通过后使用运行时
随机无资金账户完成 type-2 签名和 sender recovery；SQLite 原子地把 plan/nonce 改为 signed，
只保存公开 tx hash，不保存 raw signed transaction 或私钥。模块仍无广播能力。全量测试 114 项。

新增 `execution_attempts` 持久状态和 `ReadOnlyExecutionTracker`。它不会广播，只查询调用方提供或
离线签名阶段记录的 hash；RPC 返回交易必须逐字段匹配已持久计划。replacement 只能针对当前
active attempt，保持相同 from/to/nonce/chain/type/gas/value/calldata，且 max fee/priority fee
不得降低并至少一项提高。receipt 只有在同高度规范块 hash 与 receipt blockHash 相同时才记录
成功或 revert；hash 不一致记录 orphaned，未查到交易/回执分别保持 not_observed/pending。
回归同时核对 nonce 状态 signed→broadcast→confirmed，以及账本不含 raw signed transaction。
2026-09-12 再次运行全量 unittest：117 项全部通过；该项是离线 RPC 模拟，不是测试链或主网
广播证明。`eth_sendRawTransaction` 仍被默认 RPC allowlist 拒绝。

随后移除 MySQL 运行时“一进程只能有一个 follower / 同一 smart 只能出现一次”的限制。
配置增加完整 relationship 列表；同一 smart wallet 的多个 follower 分别使用由
follower + relationship ID + smart wallet 派生的 ledger scope，隔离预算、proposal 幂等、仓位
和跟卖本金恢复。内部 scope 不替代公开归因，查询与导出仍返回真实 follower、relationship、
smart wallet 和 source event。新增回归用两个 follower 对同一个 smart wallet 同时生成 BUY，
证明得到两个 proposal、各自只预留自己的 `1000000` USDG 额度。全量 unittest 增至 118 项并
全部通过；尚未以两条真实 enabled MySQL 关系运行实时窗口。

MySQL 加载进一步改为逐 relationship 严格校验：每行独立保存 strategy version、trigger/shadow
modes、QuotePolicy、协议/资产/精确路由及 snapshot hash。监听、二次报价成交、重启恢复和持仓
估值均按 proposal/position 归因选择对应关系配置，不再复用第一行的全局策略。新增模拟 MySQL
回归同时加载两条相同 smart wallet 关系，分别使用 `strategy-a/swap_evidenced/100 bps` 与
`strategy-b/receipt_success/500 bps`，验证两组值和快照保持独立。全量 unittest 增至 119 项并
通过；仍需实际启用两条本机 MySQL 测试关系做进程级演练。

离线签名前新增 `transaction ↔ UnsignedExecutionPlan` 逐字段复核。测试在 prepared plan 的 SQLite
JSON 中把 calldata 改成 `0xdeadbeef`，即使打开 `offline_test` 也以
`signable transaction does not match execution plan` 拒绝；恢复原记录后才可完成随机无资金密钥
签名。结合 nonce reservation、配置 snapshot 比对和 sender recovery，避免预检对象与实际签名
对象不一致。最终 `pip check`、compileall、119 项 unittest 和 `git diff --check` 全部通过。

实际运行 `.venv/bin/python scripts/validate_mysql_relationship_isolation.py` 连接本机
`127.0.0.1:3308`：临时创建两个 follower 指向同一 smart wallet，运行时只读账号成功加载 2 条
关系；策略版本为 `mysql-isolation-v1/v2`，滑点为 100/500 bps，两个 ledger scope 不同且预算均
成功配置。输出明确为 `copy_eligible=false`、`live_trading=false`。finally 只按本轮 insert ID
删除 2 行，随后查询得到 `remaining=0`。该演练未连接 key DB、RPC 或 Feed，也没有签名/广播。

新增 `MySqlRelationshipGate`：离线签名协调器在签名前用新 runtime 连接按 relationship ID 读取
`enabled=TRUE` 行，并逐项匹配 follower、smart wallet、relationship ID 与 snapshot hash。
演练脚本在两条关系成功通过 gate 后，用 admin 将第一条临时关系设为 `enabled=FALSE`，下一次
runtime 查询立即以固定错误拒绝，输出 `emergency_stop_rejected=true`；cleanup 再次为
`remaining=0`。单测还覆盖 snapshot 改变拒绝和签名协调器实际调用 gate。该机制不是主网授权，
也不提供签名广播入口。

进程控制新增 fail-closed 三门禁：`EXECUTION_MODE=offline_test`、
`SIGNING_MODE=offline_test`、`EMERGENCY_STOP=0` 缺一不可；默认状态为停止。签名前还逐次检查 stop
file，测试在运行期创建临时 `EXECUTION_STOP` 后确认立即拒绝。另以看似启用 mainnet/broadcast
的全部环境变量测试，主网 gate 仍固定返回 `not implemented or authorized`。全量测试由 120 项
增至 121 项；没有新增 RPC 广播方法。

SQLite `execution_plans` 新增可迁移的 `final_review_payload`。离线签名成功时，最终重新报价/risk、
余额、allowance、Gas、pending nonce、relationship snapshot、sender recovery 和额度 reservation
证据与 signed 状态在同一事务保存。BUY 需 active cycle/budget reservation 且账本金额不超限；
SELL 需 open position reservations 合计等于 proposal。回归把 BUY reservation 改为 released 后，
签名在报价和密钥访问前拒绝；另验证 final review 出现 private key/raw transaction 字段即拒绝。
全量测试预期增至 122 项；这仍是签名前证据，不等同广播瞬间复核。

execution plan 新增 `plan_integrity_hash`，以规范 JSON 对 plan/proposal ID、follower、relationship、
配置快照、nonce reservation、transaction/unsigned plan 和初始 preflight 计算 SHA-256。正常计划
关闭 SQLite 后可恢复；在同一连接篡改 calldata，或重启后篡改 preflight，下一次读取均以
`execution plan integrity mismatch` 拒绝。旧 NULL 行仅在首次 schema 迁移时按已有数据补基线，
故此机制用于发现损坏/意外改写，不宣称能阻止拥有 SQLite 写权限的攻击者同时重算哈希。

拟进行“向 key MySQL 临时写入随机无资金密钥再删除”的演练时，安全审批因 AGENTS.md 明确禁止
测试存储私钥而拒绝，进程未启动；临时脚本随后删除，没有绕过执行。通过 key runtime 只读账号
执行 `SELECT COUNT(*) FROM wallet_keys` 得到 `0`，确认数据库仍为空。离线签名证据继续来自不落盘
的内存随机密钥与模拟数据库读取。

执行 builder 进一步显式拒绝 needs_review、execution unknown、orphaned 和 UNKNOWN behavior；只有
`swap_evidenced + execution success + supported trade + exact-input + allowlist` 才进入构建。新增
边界断言复用现有 builder 回归，测试总数不变。

本轮收尾再次运行锁定环境：`pip check`、compileall、122 项 unittest、`git diff --check` 均通过。
13 笔历史回放仍输出原行为分布：TRANSFER 1、CLAIM 1、APPROVAL 12、UNKNOWN 6、SELL 1、BUY 1、
LIQUIDITY 1、EXTERNAL_DELIVERY_CANDIDATE 1、INTENT_DEPOSIT 2、BULK_DISTRIBUTION 1；所有输出继续
`copy_eligible=false`，`replay_finished.live_trading=false`。

新增只读 `ReadOnlyPreBroadcastReviewer`。它对内存 raw signed bytes 计算 hash 并匹配持久记录，恢复
sender，解码 type-2 字段并匹配 execution plan，再重新执行 MySQL relationship、paper reservation、
quote/risk、余额、allowance、Gas 和 pending nonce 检查；pending nonce 必须与签名 nonce 完全相等。
回归确认正常 review 返回 `broadcast_performed=false`，修改 raw 最后一个字节会 hash mismatch，nonce
从 7 前移到 8 会拒绝。review 不持久 raw bytes，也没有 RPC broadcast 调用。

新增 `execution-audit` 运维检查与 `docs/OPERATOR_RUNBOOK.md`。审计覆盖健康 signed 状态、plan
完整性篡改以及 nonce 身份错配；即使单条记录损坏也返回结构化 issue 而不终止整份报告。
初始定向运行 3 项新增测试全部通过，完整锁定环境验证见本文件后续记录。

本轮完整验证结果：`pip check` 为 `No broken requirements found`，compileall 通过，125/125 项
unittest 通过，`git diff --check` 通过。CLI 对既有只读监听 SQLite 执行 audit，返回 plans=0、
attempts=0、issues=[]、healthy=true，同时明确 `copy_eligible=false`、`live_trading=false`；该空
execution 账本只证明审计命令可运行，不证明签名后生命周期完整。13 笔历史 replay 再次通过，
行为分布保持 TRANSFER 1、CLAIM 1、APPROVAL 12、UNKNOWN 6、SELL 1、BUY 1、LIQUIDITY 1、
EXTERNAL_DELIVERY_CANDIDATE 1、INTENT_DEPOSIT 2、BULK_DISTRIBUTION 1，所有信号继续
`copy_eligible=false`。

随后强化 audit 的证据语义：新增固定 attempt 状态分布、coverage 维度和
`end_to_end_evidenced`。空 execution 账本仍可 `healthy=true`（不存在内部矛盾），但明确返回
`has_plans=false`、`end_to_end_evidenced=false`；模拟 tracker 取得规范链成功 receipt 后才为 true。
新增空账本断言，并在既有 confirmed 生命周期回归中验证 coverage；定向 5 项测试通过。随后
`pip check`、compileall、126/126 项 unittest 与 `git diff --check` 全部通过。对既有空 execution
账本实际执行 CLI，输出六种 attempt 计数均为 0、全部 coverage 为 false，同时保持
`healthy=true`、`end_to_end_evidenced=false`、`copy_eligible=false`。

双 MySQL 远程连接审计发现并修复了本机默认凭据回退风险。配置库与 key 库对非 localhost 现均
要求显式 HOST/PORT/USER/PASSWORD/DATABASE/SSL_CA，缺项时不会调用 PyMySQL。新增回归还让模拟
服务端异常包含伪密码，确认上层错误只含异常类型、不泄漏凭据；3 项定向测试通过。

随后锁定环境完整验证：`pip check`、compileall、128/128 项 unittest 和 `git diff --check` 通过。
受限进程首次连接 localhost MySQL 得到脱敏 `OperationalError`；只读检查确认两个 Docker MySQL
容器均 healthy，获准在宿主网络重新运行公开关系隔离脚本后成功：两条 relationship、两个独立
scope/预算、策略版本和 100/500 bps 滑点均保持隔离，禁用门禁立即拒绝。finally 删除本轮 2 条
临时公开关系并复查 `remaining=0`。脚本没有连接 key DB、RPC、签名或广播。

补齐离线签名后的重启窗口：新增 `recover_signed`，重新打开 SQLite 后重做 relationship、budget、
quote/risk、余额/allowance/Gas/pending nonce 检查，确定性重签 immutable transaction，并要求 raw
对应 hash 与已持久 hash 完全一致。回归确认恢复 bytes 与原签名逐字节相同、attempt 不重复插入；
将该 attempt 推进为 RPC observed 后，nonce 状态变化使恢复立即拒绝。测试密钥仍仅为运行时随机
无资金账户，不写入 key MySQL、SQLite 或日志。

签名材料日志审计发现 dataclass 即使 `repr=False` 仍可能被结构化日志器通过 `asdict()` 展开。
`OfflineSignedExecution` 已改为 slots 专用容器：repr 只含公开字段，既不出现 raw 字段名也不出现
raw hex，`vars()` 失败关闭。签名、recovery 和 pre-broadcast API 保持显式属性访问；定向离线签名/
重启恢复测试通过。随后 `pip check`、compileall 和 128/128 项全量 unittest 通过；
`git diff --check` 通过，RPC 源码仍没有发送方法或 `eth_sendRawTransaction`。

实际 MySQL 最小权限复核（只读 `SHOW GRANTS`）：配置 runtime 用户只有全局 `USAGE` 与
`smart_money.copy_relationships` 的表级 `SELECT`；key runtime 用户只有全局 `USAGE` 与
`wallet_keys(wallet_address, private_key_hex, enabled)` 三列的列级 `SELECT`。同时仅执行
`COUNT(*)` 确认 key 表为 0 行，没有查询私钥列的任何值。客户端提示命令行密码方式不适用于生产；
本次密码来自容器内部环境且未出现在输出，生产仍须由部署平台 secret 注入。

配置地址边界新增 fail-closed 校验：任何启用策略中的零 smart wallet 或零 follower wallet 都在
加载阶段拒绝；`allowed_assets` 和 route 中的零地址仍合法表示 native asset。新增回归分别覆盖
两个关系地址拒绝，并确认示例配置继续包含合法 native asset；2 项定向测试通过。

完整验证为 `pip check`、compileall、129/129 项 unittest 通过。随后再次连接 localhost 配置
MySQL 运行隔离演练：67 条 disabled CSV 占位没有进入加载结果，两条非零临时 follower 正常加载
并保持独立策略/额度，禁用 gate 继续立即拒绝；finally 清理 2 条且 `remaining=0`。

重启 audit 新增 attempt 级一致性：核对 hash/nonce/from/to/chain/type/gas/value/calldata，原始费用
与 plan 相同，replacement 引用已 replaced parent 并逐级提价，final attempt 具备合法区块证据。
回归直接篡改 attempt nonce、费用和 parent，三类问题均使 `healthy=false`；正常 replacement 链仍
保持 healthy。2 项定向测试通过。

完整验证为 `pip check`、compileall、130/130 项 unittest 和 `git diff --check` 通过。对既有只读
监听 SQLite 再运行 `execution-audit`，兼容通过；该库没有 execution plan/attempt，因此输出仍
明确 `healthy=true` 但 `has_plans=false`、`end_to_end_evidenced=false`，没有把空记录算作链路证明。

CSV 写入口进一步收紧：保留已有 disabled 零 follower 历史行，但新的 `relationships-import` 在
建立数据库连接前拒绝零 follower 或 watchlist 中的零 smart wallet。新增测试确认两种拒绝均不
调用 MySQL；合法 native asset 零地址配置测试继续通过。2 项定向测试通过。

完整验证为 `pip check`、compileall、131/131 项 unittest 和 `git diff --check` 通过。只读统计实际
配置库得到 `total_rows=67, zero_follower_rows=67, enabled_rows=0`，证明旧 CSV 来源行未删除或
改写且仍全部禁用；客户端的命令行密码提示同前，不代表密码出现在输出。

新增只读 `key-status --wallet` 元数据探针。单测确认默认停止时不连接数据库，放行后 SQL 只包含
`wallet_address,enabled` 且不出现 `private_key`。实际连接 localhost 空 key DB 查询公开保留地址
`0x2222...2222`，返回 `found=false, enabled=false, private_key_read=false, read_only=true`；没有
选择或写入私钥列。CLI help 与定向测试通过。

本轮完整验证：`pip check`、compileall、132/132 项 unittest 与 `git diff --check` 通过。交接文档
中早期“尚未确认私钥方案/完全没有签名能力”的表述已修正为当前事实：独立 key MySQL 与离线测试
签名已完成，但真实私钥导入、主网签名和广播仍未授权且没有入口。

新增 `docs/GOAL_ACCEPTANCE.md`，逐项把 Goal 要求映射到代码/服务器证据，并将测试链真实生命
周期、广播瞬间边界、操作员验收和用户最终授权保持为部分验证或待外部确认。该矩阵明确禁止用
模拟回执、空 execution 账本或单纯测试总数宣称上线就绪。

服务器只读检查未发现 anvil/geth/ganache/hardhat 二进制或相关 Docker 镜像，因此没有自动下载、
安装或启动测试节点。为后续外部测试广播补上 `execution-track` CLI：验证 RPC chain ID 后调用
既有只读 tracker，可跟踪原始 hash 或显式 replacement，输出固定 `broadcast_performed=false`、
`copy_eligible=false`。CLI 参数与 pending→confirmed、replacement 回归共 3 项定向测试通过。

完整验证为 `pip check`、compileall、133/133 项 unittest 和 `git diff --check` 通过。CLI 总帮助已
列出 `execution-track`；源码检索只有两个 `broadcast_performed=false` 证据字段，没有
`eth_sendRawTransaction` 或发送函数。

补充 `execution-track` 进程路径回归：从磁盘 SQLite 的 signed plan 开始，模拟只读 RPC 后输出
`observed_pending,broadcast_performed=false,copy_eligible=false`，重开数据库确认状态持久；另验证
错误 chain ID 与缺失 signed plan 均失败关闭，输出不含 raw/private 字段。3 项定向测试通过；仍
明确属于 CLI/RPC 模拟，不是实际 EVM 广播。

2026-09-12 07:58 UTC 按锁定环境重新执行规定命令：`pip check` 返回
`No broken requirements found`，compileall 通过，完整 unittest 为 135/135 通过（0.212 秒）。
新增计数包含 `execution-track` 的磁盘持久化进程路径、错误 chain ID 和缺失 signed plan
失败关闭回归。`git diff --check` 通过；源码检索仍未发现可调用的
`eth_sendRawTransaction`，该字符串只存在于禁止广播的回归测试。所有运行时输出继续固定
`copy_eligible=false`，没有读取 key MySQL 私钥、签名或广播主网交易。

用户随后选择使用主网进行测试。受 AGENTS.md 当前里程碑和未完成风险清单约束，本轮先执行真实
主网 RPC 的无状态只读预检；没有把该选择扩张解释为允许花费资金或广播。沙箱内首次
`eth_chainId` 在 20 秒超时；获准使用服务器外部网络后，同一付费 RPC 返回 chain ID `4663`
（hex `0x1237`）。继续只读读取到最新区块 `60950904`、区块哈希
`0x8edaac5b0eebc23274348a088617c84b5be47ec0aa146638d5f5ee70a596058d` 和 Gas price
`96164000` wei。输出明确 `rpc_read_only=true`、`broadcast_performed=false`、
`copy_eligible=false`，未访问 key MySQL 或任何发送方法。该证据证明已配置付费 RPC 可访问正确
主网及基础状态，不证明真实签名、广播、成交、revert、replacement 或重组链路已经验收。

用户确认将运行状态和收益分析迁到业务 MySQL 后，新增
`docker/mysql/init/003_runtime_ledger.sql`。该 schema 在本机 `smart-money-mysql`（MySQL 8.4）
实际执行成功；使用 `smart_money_runtime` 查询 information_schema 得到目标表数 `20`。
`SHOW GRANTS` 实测该账号对 `copy_relationships` 仍只有 SELECT，对 20 张运行账本表逐表只有
SELECT/INSERT/UPDATE/DELETE，没有 schema 管理权限，也没有 key 数据库权限。新增静态回归检查
所有 20 张表、对应权限、十进制字符串金额列、一周期唯一 active guard，并确认 schema 不含
`wallet_keys`/`private_key`；定向 1 项测试通过。应用 Store 尚未切换到 MySQL，未迁移任何 SQLite
数据，也未删除或修改原 SQLite 文件。

新增 SQLite→业务 MySQL 的单向 `ledger-migrate`：要求操作员提供完整源文件 SHA-256、拒绝非空
WAL，使用 SQLite read-only 一致性事务和 MySQL 单事务，按 20 张表的外键顺序复制。目标主键若
存在则逐列核对，相同计入幂等结果，冲突则全量回滚且不会覆盖。回归用真实临时 SQLite Store
与内存 MySQL 协议替身验证首次 1 行插入、第二次 0 插入/1 行 identical、篡改目标 payload 后
冲突回滚；私钥数据固定不在迁移清单。服务器 `var/` 发现多个带具体测试名称的历史 SQLite，
没有唯一正式 `observer.sqlite3`，故本轮没有猜测迁移源，也没有改动这些文件或向业务 MySQL
写入历史账本数据。

新增 `MySqlStore` 运行后端，账本相关 CLI 均可显式使用 `--ledger-mysql`，默认 SQLite 保持兼容且
没有双写分支。方言层仅转换 Store 已知的占位符、INSERT IGNORE/upsert、JSON event ID，并把
SQLite `BEGIN IMMEDIATE` 映射为 MySQL 显式事务；事务内 SELECT 使用 `FOR UPDATE`。真实空库
`execution-audit --ledger-mysql` 返回 healthy=true、plans/attempts=0、end_to_end_evidenced=false。

随后以不可预测 UUID 在实际 MySQL 演练 signal/candidate 幂等、候选领取、100/1000 额度预留、
BUY fill、持仓与收益归因导出。第一次 `paper_trades` 暴露 JSON_UNQUOTE 返回 utf8mb4 与
ASCII event_id 的 collation 冲突并失败；finally 完成精确清理。方言层增加显式 ASCII/`ascii_bin`
转换后原样重跑通过，输出 signal_idempotent、candidate_claimed_once、reservation_atomic、
buy_fill_attributed、earnings_export_readable 均为 true，且 private_key_accessed=false、
copy_eligible=false。再次按测试 UUID 查询周期/提案/订单/成交/持仓残留总数为 0。

扩展同一安全演练后，真实 MySQL SELL 路径从归因 lot 的 250 token 中卖出 100，按整数比例释放
40 原始本金，budget invested 由 100 恢复到 60，保存/读取 `realized_pnl_raw="19"`。规范链测试
不再使用固定高度：脚本从 UUID 派生随机高位高度，并在游标为空且两个高度均不存在后才写入，
验证 safe_head_confirmed 信号在 rewind 后变为 orphaned、candidate 重置后再次领取。公开 execution
状态验证 nonce 7 reservation、prepared plan、signed hash、observed_pending、confirmed 和
`execution_audit.healthy/end_to_end_evidenced=true`；没有生成签名、读取私钥或广播。最后查询 UUID
周期/提案/订单/成交/持仓/nonce/plan/信号以及 canonical cursor 的综合残留为 0。

完整 138 项 unittest、pip check、compileall 和 diff check 通过后，实际运行
`sm-copy monitor --seconds 60 --ledger-mysql`。进程连接付费主网 RPC/feed，未启用 paper 配置；
最终 counters 为 frames=1100、decoded=5233、stale skipped=4126、backfill blocks=569、
candidates/dispatched/receipts=5/5/5，receipt unavailable/retry/drop/worker/RPC backfill/account-code/
frame/reconnection errors 全为 0。队列最终 pending=queued=retry=failed=0、complete=5。

5 个信号均为 `INCOMING_TRANSFER`，有成功 receipt 和钱包 ERC20 正向 delta，但没有 swap event，
因此全部正确保持 `needs_review`、理由 `recipient_is_not_proof_of_order_ownership`，没有当作买入。
结束后直接查询业务 MySQL 得到 signals=5、copy_eligible false=5、needs_review=5、complete
candidates=5、cursor=60984943、canonical_blocks=570、paper_fills=0、execution_attempts=0。最后两个
为 0 符合本次未启用跟单配置/未广播的边界，不能解释为收益链路测试失败；BUY/SELL/PnL 和公开
execution lifecycle 已由此前隔离 UUID 演练覆盖。旧具名 SQLite 文件保持未修改，作为历史验证
归档，不与本次新的业务 MySQL 运行起点混合。

## Anvil 本地双钱包买卖闭环

2026-09-12 UTC 使用 Foundry 官方镜像（锁定 digest `sha256:0c00...f9a17`），Anvil 仅绑定
`127.0.0.1:8545`，chain ID 31337。首次编译发现 artifact bytecode 自带 `0x` 前缀，部署脚本
重复添加导致本地 RPC 拒绝；修正后重新编译和执行成功。

- 聪明钱临时地址执行 `0.1 ETH → 100 USDG`，跟单临时地址按 50% 执行
  `0.05 ETH → 50 USDG`。
- 聪明钱卖出 `50 USDG → 0.05 ETH`，跟单地址按对应持仓比例卖出
  `25 USDG → 0.025 ETH`。
- 四笔交易均取得 status=1 回执；脚本要求每笔恰有一个测试池 Swap，且 indexed trader 必须
  等于对应聪明钱或跟单地址。最终 token 余额分别为 50 USDG 与 25 USDG。
- 项目未处理私钥，未读取 `.env`/key MySQL，未连接 Robinhood RPC，未签名或广播主网交易；
  输出固定 `mainnet_rpc_used=false`、`private_keys_used_by_project=false`、
  `copy_eligible=false`、`live_trading=false`。

这证明隔离链上的双钱包买卖和 50% 数量关系能够执行，不证明主网 Feed→signal→业务 MySQL→
执行器端到端通过。

随后将本地回执接入现有状态机并重跑：两个源回执保存为 chain 31337 的 `swap_evidenced` 信号；
BUY 经 50% 比例策略预留并投入 `50000000000000000` wei 本金，形成带 smart wallet、follower、
relationship/source tx 的 order/fill/position 归因。SELL 只预留同一关系 lot 的
`25000000000000000000` token，成交后恢复 `25000000000000000` wei 本金；预算最终
reserved=0、invested=`25000000000000000`、available=`975000000000000000`，realized PnL=0。
导出顺序为 BUY/SELL 两条 paper trade。测试账本位于被忽略的
`var/local-copytrade-9d87fddfffb8388e.sqlite3`；该轮未写业务 MySQL。

## 精简版 Goal 1：Relay + 0x/Kyber 信号闭环（完成）

新增 Relay cleanup、Depository full-allowance deposit、0x AllowanceHolder `exec` 和 Kyber
MetaAggregationRouterV2 `swap` 解码。聚合器调用只有在同一真实钱包、同一 UserOperation、
同一 Relay 根调用下唯一关联 USDG deposit/orderId 时才判 SELL；回执还必须证明 UserOperation
成功、钱包 Token 精确扣款、唯一 DepositRecorded 金额/orderId 及同范围 Swap，才能进入
`relay_sell_evidenced`。不满足条件保持 UNKNOWN/needs_review。

被动买入要求已有 `EXTERNAL_DELIVERY_CANDIDATE`/`INCOMING_TRANSFER` 与保存的 Relay 响应中
的 request/order 身份、源付款人、目标 recipient/payment、唯一成功源交易、目标 outTx、
settlement fill 及本地唯一 Token 净入账完全相符，才生成 `relay_buy_evidenced`；单纯收币不升级。
离线入口为：

```bash
.venv/bin/sm-copy relay-associate --db var/observer.sqlite3 \
  --event-id '<完整 event_id>' --document '<保存的 Relay requests/v2 JSON>'
```

该命令只读取本地文件和现有 SQLite/MySQL 信号，不自动访问 Relay/RPC，不进入 paper 或实盘。
所有新增阶段仍强制 `copy_eligible=false`。

实测：`pip check` 通过；完整 unittest 在修复一次回归发现的重复接口定义并增加真实样本断言后，
最终 153/153 通过；`git diff --check` 通过。13 笔离线 replay 通过，行为分布为 TRANSFER=1、
CLAIM=1、APPROVAL=12、UNKNOWN=2、SELL=3、BUY=1、LIQUIDITY=1、
EXTERNAL_DELIVERY_CANDIDATE=1、INTENT_DEPOSIT=4、BULK_DISTRIBUTION=1。旧 `0x23419e…`
真实 Relay + 0x 样本闭合，旧直连 Kyber 样本因没有 Relay 订单关联保持 UNKNOWN。

主网只读监听首次在沙箱内阻塞于初始网络连接且没有创建账本，不计为监听。获准联网后运行至少
60 秒并以 Ctrl-C 触发现有优雅收尾：feed healthy，connections=1、frames=3043、decoded=7896；
候选/派发/回执=4/4/4，complete=4，pending/queued/retry/failed=0；重连、frame、worker、
account-code、backfill、RPC、retry、drop 错误均为 0。receipt_signals=13、UNKNOWN=0、
needs_review=1、relay_sell_evidenced=3；现场同时捕获 0x 与 Kyber Relay SELL。一笔无本地 Swap
的入账先保持 INCOMING_TRANSFER/needs_review，随后通过 Relay 官方按目标 tx hash 查询得到
唯一订单，进入下述独立 BUY 归属验证。

现场账本：`/tmp/smart-money-goal1-live-escalated.sqlite3`。紧凑正负证据：
`data/relay_signal_evidence_2026-09-12.json`。本轮没有读取私钥、签名、广播、paper 或真实跟单。
随后保存独立紧凑 Relay 响应 `data/relay_passive_buy_evidence_2026-09-12.json`。真实结构显示
Base 付款人不是 Robinhood 收币钱包，因此规则分别要求 `request.user == origin.depositor`，
以及目标钱包匹配 recipient、orderData payment、成功 outTx/stateChange、destination fill 和
本地回执唯一正向 Token delta。真实样本闭合为 `relay_buy_evidenced`；篡改 payment recipient
负例被拒绝。另补 CLI→Store 重复关联幂等和重组 orphan 回归，以及同钱包跨 UserOperation
不得拼接 aggregator/deposit 的负例。

最终再次执行 `pip check`、153 项 unittest、13 笔 replay 和 `git diff --check`，全部通过。Goal 1
至此完成；没有自动进入 Goal 2、paper 或实盘。非阻塞后续风险：紧凑 Kyber 样本不是完整原始
tx/receipt 归档；Relay requests/v2 官方将在 2026-11-24 退役，而 v3 需要 API key。当前程序不
自动调用 v2，只消费操作者保存的证据 JSON；Goal 2 前应单独设计 v3 凭据和响应兼容方案。
