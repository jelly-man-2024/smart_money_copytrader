# 跟单执行操作手册

本文描述当前受支持的运维流程。普通模式仍不广播，`ReadOnlyRpc` allowlist 继续不包含
`eth_sendRawTransaction`。单独的 `MainnetBroadcaster` 只在 `mainnet_live` 的全部门禁通过后发送；
`copy_eligible` 仍固定为 `false`，不能把观察字段当成实盘授权。

## 数据源边界

- 配置库保存公开数据：`copy_relationships` 一行表示一个“跟单钱包 × 聪明钱”关系以及唯一策略、
  额度、触发点、允许协议/资产/路由和配置快照。
- 私钥库是另一个 MySQL，由使用者在自己的服务器上自行维护。项目运行账号只能按公开地址读取
  `wallet_address, private_key_hex, enabled`；配置库和 SQLite 账本都没有私钥字段。
- 自动化回归不得向私钥库导入真实或测试私钥，只使用进程内随机无资金密钥和模拟查询。
  `mainnet_live` 测试的私钥只能由使用者自行维护，并受本文的逐关系验收和多重门禁限制。

可用 `sm-copy key-status --wallet 0x公开地址` 做元数据连通性检查。它只选择 `wallet_address` 和
`enabled`，不选择 `private_key_hex`，输出固定带 `private_key_read=false`；仍必须显式打开三个
offline_test 门禁，且 stop file 存在时拒绝连接。

## 新增或修改跟单关系

全新 MySQL volume 会自动执行 `docker/mysql/init/006_database_live_acceptance.sql`。已经初始化过的
数据库不会自动重放 Docker init 文件，部署新代码时需由数据库管理员只执行该迁移一次；不要重复
执行，也不要删除 volume。迁移只增加 `live_risk_accepted_at`，既有关系默认保持未确认。

1. 用配置库管理账号插入关系，首先保持 `enabled=0`。CSV 批量导入同样默认禁用。
2. 核对 follower/smart 地址、策略类型、固定金额或比例、币种额度、单聪明钱累计投入上限、
   trigger、quote policy、协议、资产和完整 route。
3. 使用运行账号加载配置并检查 snapshot hash；运行
   `.venv/bin/python scripts/validate_mysql_relationship_isolation.py` 验证多 follower 隔离。
4. 实盘关系还必须在同一条 INSERT/UPDATE 中设置
   `live_risk_accepted_at=CURRENT_TIMESTAMP(6)`。确认时间必须不早于该行 `updated_at`；任何后续字段
   变化都会使旧确认失效。核对新值后在本次 UPDATE 同时刷新确认时间，再重启固定任务。

已有关系确认并启用的 SQL 形态如下；必须同时限定 ID 和双方钱包，避免更新错行：

```sql
UPDATE copy_relationships
SET run_mode = 'mainnet_live',
    enabled = TRUE,
    live_risk_accepted_at = CURRENT_TIMESTAMP(6)
WHERE id = ?
  AND follower_wallet = '0x跟单钱包'
  AND smart_wallet = '0x聪明钱钱包';
```

执行后用 `relationship-status` 确认 `live_risk_acceptance_current=true`，再重启任务。配置字段发生任何
变化时，把确认时间放在同一条 UPDATE 中刷新；只修改 `wallet_keys` 不会改变关系确认。

### 执行路径提供方 `execution_providers`

`docker/mysql/init/007_execution_providers.sql` 为 `copy_relationships` 增加 JSON 列
`execution_providers`，默认 `["local"]`，已初始化的库同样只执行该迁移一次。含义：

- `local`：只执行能在本地逐字段验证的 V2/V3/V4 路径（回执提取、预配置 `allowed_routes`、
  V3 工厂直连池发现），与此前行为完全一致。必须是列表第一项。
- `kyber`：本地路径不可用时，向 KyberSwap 官方聚合器 API（`aggregator-api.kyberswap.com`，
  链名 `robinhood`，无需密钥）为 follower 自己的输入量询价并构建交易。只接受 Router 为
  `0x6131b5fae19ea4f9d964eac0408e4408b66337b5`、顶层 `swap` 描述里 dstReceiver 等于 follower、
  金额等于计划输入、无手续费、value 为 0 的 calldata；链上 minReturn 必须不低于按
  `max_slippage_bps` 从构建输出算出的下限；签名前和广播前都会以 follower 身份 `eth_call`
  模拟整笔交易并要求返回量不低于该最小值。USDG 对 Kyber Router 的授权沿用"周期预算 × 200"
  的有界规则，卖出授权等于归因持仓。聚合器买入的 lot 卖出时同样走聚合器重新询价。

该字段进入配置快照，修改时必须在同一条 UPDATE 中刷新 `live_risk_accepted_at`：

```sql
UPDATE copy_relationships
SET execution_providers = JSON_ARRAY('local', 'kyber'),
    live_risk_accepted_at = CURRENT_TIMESTAMP(6)
WHERE id = ? AND follower_wallet = '0x跟单钱包' AND smart_wallet = '0x聪明钱钱包';
```

聚合器 calldata 的内部执行数据是黑盒，这是相对本地路径的安全模型让步；启用前应确认接受
`docs/AGGREGATOR_ROUTE_DESIGN.md` 第 6 节列出的替代门禁。任何提供方失败都回落到
`quote_unavailable`，决策记录的 `quote_error` 字段保存各步失败原因。

CSV 占位关系可能使用零 follower，但必须保持 disabled；启用策略中的零 follower 或零 smart
wallet 会在配置加载阶段失败关闭。旧占位数据保留用于来源追踪，但新的 `relationships-import`
拒绝零 follower/smart wallet。资产路由中的零地址仍表示 native asset，不应替换。

监听账号只需配置库的 `SELECT copy_relationships`。导入和修改使用单独管理账号。远程 MySQL
必须显式设置 HOST、PORT、USER、PASSWORD、DATABASE、SSL_CA 并验证 TLS；缺少任一项都会在建立
连接前拒绝，不会回退到本机 Docker 默认凭据。数据库密码仅通过进程环境或部署平台 secret
注入，不写入仓库 `.env`。

## 启动与重启检查

启动前运行：

```bash
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sm-copy execution-audit --db var/observer.sqlite3
```

`execution-audit` 只汇总 execution plan、nonce reservation 和公开 attempt 状态，不接受 raw
transaction，也不读取私钥。`healthy=false` 时不得继续签名准备，应先根据 `issues` 检查损坏、
缺失 reservation、身份/状态错配或重复 active attempt。不要删除账本或手工释放 nonce 来消除告警。
`healthy=true` 只表示已有记录内部一致；必须另看 `coverage` 和 `end_to_end_evidenced`。空账本会明确
返回 `has_plans=false`、`end_to_end_evidenced=false`，不能作为执行链路通过的证据。只有只读 tracker
看到规范链成功 receipt 后，`has_successful_confirmation` 和 `end_to_end_evidenced` 才为 true；这也
仍然只是该笔样本的证据，不代表所有路由、异常或延迟均已覆盖。
一致性检查也逐条验证 attempt 的 hash/nonce/from/to/chain/type/gas/value/calldata、原始费用或逐级
replacement 提价、replacement parent 状态以及最终 block number/hash；任何一项损坏都会让
`healthy=false`。

纸面跟单手工命令可以明确选择周期：

- `--paper-cycle-action reuse`：沿用上次累计投入和剩余额度。
- `--paper-cycle-action reset --paper-cycle-id ... --paper-cycle-reason ...`：人工开启新周期。
- 跟卖成交会按该 relationship 的归因持仓释放累计投入额度；失败、revert 或 UNKNOWN 不释放。

### 规范链回补的范围扫描

回补不再逐块拉取完整区块。每一轮用 `eth_getLogs` 在一个区块范围（`run` 默认 2000 块，
`--backfill-batch` 上限 5000）内查询"发送方或接收方是监听钱包"的 ERC-20 Transfer 日志，只对命中的
交易再取交易体，RPC 被拒绝或日志超过上限时自动对半拆分范围。任何可跟的买入或卖出都必然移动聪明钱
的 ERC-20 余额，因此不会漏掉交易；单纯授权等不移动代币的调用不再进入回补候选。回补的监听集合只包含
enabled 关系里的聪明钱，CSV 观察地址继续只走实时 feed。父哈希连续性在范围起点、每个命中区块和范围
终点核对，`canonical_blocks` 只在这些高度落库，因此重组检测从逐块降为逐段，自动回退深度放宽到
`max(64, backfill_batch + 1)`；健康日志的 `chain_cursor` 应在几分钟内追平安全链头。

数据库驱动的 `sm-copy run` 会自动沿用当前活动周期，绝不因重启重置已投入额度。第一次运行没有
活动周期时才自动创建一个；新增 relationship 会在同一周期初始化自己的额度。修改额度会保留已有
invested/reserved 数值，若新上限低于已占用额度则启动失败，不会偷偷清零。

### Relay 跨链卖出的订单确认

Fomo 聪明钱的卖出经 Relay 编排：本链用 0x/Kyber 把代币换成 USDG 后存入 Relay Depository，
USDG 再桥到聪明钱的 Solana 地址。回执规则只有在"代币支出、唯一存款订单、范围内至少一个已识别
Swap 事件"同时成立时才给出 `relay_sell_evidenced`；若卖出发生在事件签名未知的池子（例如 Pons V2
池，其 Swap topic `0x8113d738…59df` 已加入识别表），信号会停在 `needs_review` 并带
`relay_sell_evidence_not_uniquely_closed`。

对这类信号，receipt 工作线程会用交易哈希查询 Relay 公共接口 `requests/v2`（同一笔打包交易可能返回
多个用户的订单，因此按订单号筛选而不是假设唯一），只在下列全部成立时把信号升级为
`relay_sell_evidenced`：订单 `status=success`；`user` 与 `data.metadata.sender` 都是该聪明钱；
`protocol.orderId` 等于本地存款事件的订单号；`protocol.deposit.origin` 的链、币种（USDG）、存款人、
Depository、交易哈希与本地回执一致，金额等于本地存款事件金额；`data.metadata.currencyIn` 的代币和数量
等于钱包的代币支出；`inTxs` 只有这一笔交易且成功。升级后 `amount_out_raw` 取存款金额，证据新增
`relay_request_id`、`relay_destination_chain_id/currency/amount_raw/recipient`，reasons 携带
`relay_order_confirms_sell_without_recognized_swap_event`。Relay 尚未返回订单时事件为
`relay_lookup_pending` 并按候选重试；不一致时事件为 `relay_sell_confirmation_rejected`，信号保持
`needs_review`。心跳计数器 `relay_sell_confirmed` 统计成功升级次数。该路径与本地路由发现一致：
升级后仍需 follower 侧有可用的本地或聚合器卖出路径才会真正跟卖。

## 停止控制

任一层都应 fail closed：

1. 将目标 `copy_relationships.enabled` 设为 `0`，签名前的新连接复核会拒绝旧配置快照。
2. 创建 `SMART_MONEY_EMERGENCY_STOP_FILE` 指向的文件；默认是 `var/EXECUTION_STOP`。每次实盘
   密钥访问和广播前都会复查。`SMART_MONEY_EMERGENCY_STOP` 只保留给离线签名测试，不再是实盘
   部署配置。

停止进程时先创建 stop file，再 `kill -INT <pid>`。若进程是在非交互 shell 中以 `nohup ... &`
方式启动的，它会继承对 SIGINT 的忽略而不会退出，此时等待 30 秒后改用 `kill -TERM <pid>`；两种
信号下账本写入都是事务性的，stop file 已保证不会再有密钥读取或广播。

出现异常时先停用关系并创建 stop file，再运行 `execution-audit` 和只读 RPC 核对。不要清库、
重置 nonce 或盲目重发交易。mainnet_live 广播 hash 和外部广播 hash 都交给只读 tracker 跟踪。
CLI 为 `sm-copy execution-track --db ... --proposal-id ...`；原始签名 hash 可省略 `--tx-hash`，
replacement 必须同时给出新 hash 和 `--replaces-tx-hash`。输出固定
`read_only=true,broadcast_performed=false,copy_eligible=false`。

## 状态处置

- `prepared`：有持久 nonce reservation，尚未签名；重启应复用同一 proposal/plan。若签名前的
  二次报价、门禁或模拟在同一进程内被拒（例如 `adverse_price_deviation_exceeded`），monitor 会
  立即把该计划改为 `cancelled`、把 nonce reservation 置为 `released`（nonce 值改为高位哨兵，
  原 nonce 记录在 `final_review.released_nonce`）并取消 proposal 释放额度，日志事件为
  `live_execution_abandoned`。否则被占用的 nonce 会让后续计划比网络 pending nonce 多 1 而全部
  在广播前被拒。已签名的计划不会被自动取消，仍走操作员复核。
- `signed`：只保存公开 tx hash 和最终复核证据；raw signed bytes 不落库。
- `observed_pending`：RPC 已发现外部广播交易，nonce 标记为 broadcast。
- `confirmed`：receipt 位于同高度规范块且成功。
- `reverted`：链上失败，不得按成功成交记账。
- `orphaned`：receipt 的 block hash 非规范链；继续只读重查，不能视为成交。
- `replaced`：同 nonce 的新 hash 必须保持原交易意图且提高费用；旧 attempt 不再 active。

若进程在 `signed` 后、raw bytes 尚未交付给调用方时退出，只能使用 signer 的显式
`recover_signed` 路径：它会在重启后重做关系、额度、报价、余额/Gas/nonce 预检，重签相同的
immutable transaction，并要求 hash 与账本一致。只允许唯一 attempt 仍为 `signed`；RPC 已观察
或 nonce 状态已推进后必须拒绝，不能用恢复操作生成 replacement。

签名返回对象的默认 repr 只显示公开 plan/proposal/hash/preflight，并禁止 `vars()`/dataclass 通用
序列化，避免普通对象日志自动带出 raw bytes。调用方仍必须把 `raw_transaction` 当作敏感、短生命周期
数据：不得打印、结构化记录、写文件或放入异常文本。

任何 UNKNOWN、needs_review、失败执行、非规范链或归属不明信号都不得构建交易。被动收币不算买入，
bundle 中他人的 Swap 不归给目标钱包，claim + swap 仍分别保留。

## 小额 mainnet_live 测试入口

当前一个进程会加载全部 enabled `mainnet_live` relationship，且每一条都必须满足：

- 配置来自 `--paper-mysql`，运行账本使用 `--ledger-mysql`；
- `run_mode='mainnet_live'`、`trigger_mode='evidenced'`；
- 来源必须最终得到严格验证的本地 V2/V3/V4 exact-input 路径；Relay 订单只用于归因，绝不降级
  复用聪明钱或聚合器的源 calldata。Relay 被动交付 receipt 没有 Swap 时，只允许查询配置 V3
  Factory 的 `100/500/3000/10000` 四个标准直接池，在同一固定区块验证 code、token0/token1/fee，
  并按该 relationship 本次实际计划输入量择优；这不是任意多跳或多协议寻路；
- token 输入必须对实际执行 router 有足额 allowance。`mainnet-approve-usdg` 固定按该关系 USDG
  周期总预算的200倍预授权；SELL 若 allowance 不足，monitor 会在每次签名/广播前重新验证关系，
  并仅按该 relationship 账本中的同 Token 当前归因持仓总量自动授权。授权确认位于规范块且链上
  allowance 足额后才重新报价并卖出；不会授权钱包中无归因的同币余额。该 allowance 不放大软件
  预算，测试结束仍应单独撤销为0。monitor 以本次 proposal 输入量判断现有 allowance 是否足够；
  已足够时不补授权，只有不足时才写入上述有界授权目标；
- 先用 `relationship-status --relationship-id ID` 核对 follower、smart wallet、额度、协议和精确
  `config_snapshot_hash`。`enabled=TRUE`、`run_mode='mainnet_live'` 且确认时间不早于行更新时间，
  三者共同构成逐关系实盘授权；不再创建
  单独的风险确认 JSON；
- 每次读取私钥及每次广播前，程序都会用运行时只读账号重新加载该行并核对 follower、relationship
  ID 与完整 snapshot。运行中禁用或修改该行会让旧进程立即拒绝后续签名；确认新配置后重启才会
  加载新 snapshot；
- 私钥仍只由使用者写入独立 key MySQL，不得写到业务 MySQL、仓库 `.env`、命令参数或聊天。

RPC、Feed、业务 MySQL 和私钥 MySQL 连接信息只需在部署平台配置一次。日常启动不再设置
execution/signing/broadcast/chain ID 环境开关，也不再准备风险 JSON或手工传额度周期参数。

确认全局急停文件不存在后，固定启动命令只有：

```bash
.venv/bin/sm-copy run
```

该命令固定使用 MySQL 配置、MySQL 账本、自动复用额度周期和 Relay 关联；enabled relationship 中的
smart wallet 会自动加入监听集合，不再维护额外 watchlist。启动时任何一条 enabled 实盘关系的
确认、快照或 key 元数据不合格都会整体失败关闭，避免静默漏跟。运行时同一 follower 的授权、
nonce、签名和广播按钱包串行，不同 follower 可并行；单条关系的执行错误会记录该 relationship
与 follower，不会阻断同一信号对应的其他关系。修改 `copy_relationships` 时在同一条
SQL 刷新 `live_risk_accepted_at`，或修改 `wallet_keys` 后，重启同一命令即可生效。

MySQL 配置 snapshot、关系 enabled/run_mode 状态、密钥 enabled 状态、余额、allowance、Gas、nonce、报价或
签名发送者不匹配都会在广播前拒绝。RPC 返回 hash 必须等于本地签名 hash；日志只保存公开 hash，
不保存 raw signed transaction。收到 `live_recovery_requires_operator_review` 时先运行
`execution-audit --ledger-mysql`，不要直接重发。

当前已知限制：confirmed 会按规范 receipt 的 follower ERC-20 净差额写入 position lot/PnL；但只
覆盖当前 ERC-20 exact-input MVP，native/复杂手续费币仍需扩展。首笔 SELL 在没有 allowance 时会
多等待一笔 approve 的规范回执，之后只要剩余 allowance 覆盖归因持仓便不会重复授权；OKX 仅完成
严格客户端，尚未接 monitor；Relay 公共 requests/v2 还需在退役前迁移 v3。
因此此入口只适合人工看守、可承受全部损失的极小额验证，不是正式无人值守上线。

## 正式主网上线前仍需人工验收

- 在独立测试链完成真实广播的失败、超时、nonce 冲突、replacement、revert、重启和重组演练。
- 确认广播瞬间的最终报价、余额、Gas、额度和 nonce 原子边界。
- 操作员书面确认钱包、逐关系/逐币种额度、周期规则、Gas/滑点上限、停止和恢复流程。
- 用户审阅证据后明确授权具体主网 follower、relationship、snapshot 和测试窗口。

这些条件未全部满足前，不得把小额测试入口升级为无人值守实盘；`copy_eligible=false` 不改变。

## 操作员验收记录模板（默认未通过）

复制以下内容到一份带日期的新记录中填写；不得直接勾选本模板，也不得由自动化替操作员确认。

```text
验收日期/时区：<未填写>
操作员：<未填写>
代码 commit：<未填写；当前工作树尚未提交>
配置 snapshot hash：<未填写>
follower 公钥地址：<未填写>
relationship ID / smart wallet：<未填写>
策略与触发点：<未填写>
USDG 单笔规则 / 周期累计投入上限：<未填写>
ETH_WETH 单笔规则 / 周期累计投入上限：<未填写>
Gas 上限 / 滑点 / 价格偏离 / 报价年龄：<未填写>
允许协议、资产、逐跳路由：<未填写>
周期选择（reuse/reset）及理由：<未填写>
数据库 enabled 停止演练：未通过
进程 emergency stop 演练：未通过
stop file 演练：未通过
重启 execution-audit 结果：未填写
测试链成功/revert/timeout/nonce/replacement/reorg 证据：未填写
回滚与事件响应负责人：<未填写>
操作员结论：未通过
用户最终主网授权：未提供
```
