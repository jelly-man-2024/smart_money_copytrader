# Smart Money Copytrader

Robinhood Chain 聪明钱跟单工程，独立于同级 `fomo_sniper`。
项目由只读观察器和回放验证起步，当前还包含纸面跟单及受独立门禁控制的 `mainnet_live` 执行链路。
普通只读观察/回放不签名；`sm-copy run` 会按 enabled relationship 的运行模式进入相应分支。
当前实现、历史文档差异与运行限制以[流程及延时分析](docs/copy_trade_flow.md)为准。

- [完整跟单方案](docs/COPYTRADING_PLAN.md)
- [当前跟单流程、行为矩阵与延时分析](docs/copy_trade_flow.md)
- [Feed 提前资格规则与接口（离线）](docs/EARLY_FEED_REFERENCE.md)
- [Feed 提前执行接线与操作员切换交接（尚未部署）](docs/EARLY_FEED_LIVE_HANDOFF.md)
- [历史 Feed 覆盖回放操作](docs/HOWTO_REPLAY_EARLY_FEED.md)
- [观察名单历史交易路径分析](docs/WATCHLIST_ROUTE_ANALYSIS_2026-09-12.md)
- [继续开发交接](docs/HANDOFF.md)
- [新服务器测试与开发交付](docs/SERVER_HANDOFF.md)
- [Mac mini 空运行环境交付](docs/MAC_MINI_ENV_HANDOFF.md)
- [聚合器执行路径接入设计（提案）](docs/AGGREGATOR_ROUTE_DESIGN.md)
- [数据与代码来源](docs/PROVENANCE.md)

## 快速开始

要求 Python 3.10+，当前已在 Linux/Python 3.10 验证。从新服务器首次拉取：

```bash
git clone git@github.com:jelly-man-2024/smart_money_copytrader.git
cd smart_money_copytrader
```

SSH 拉取需要服务器具有该仓库的读取权限。在本项目目录安装并测试：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e .
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sm-copy replay --extra-fixture data/bulk_distribution.json
```

对 monitor 已保存的被动入账候选，可用操作者另行保存的 Relay 响应做严格离线订单关联：

```bash
.venv/bin/sm-copy relay-associate --db var/observer.sqlite3 \
  --event-id '<完整 event_id>' --document '<Relay requests JSON>'
```

只有源付款身份、目标 recipient/payment、outTx/fill 和本地回执 Token 入账全部唯一匹配才升级为
`relay_buy_evidenced`；命令不联网、不签名、不广播，输出仍为 `copy_eligible=false`。

离线回放包含 11 笔历史样本、1 笔真实 feed 存款样本和可选的 230 地址批量分发。
不需要网络、付费 RPC 或私钥。信号输出到 stdout（JSONL），统计输出到 stderr。
新增独立的 `scripts/replay_early_feed.py` 可检查提前候选、UserOp 签名、订单归属和决策快照；
它未接入 monitor，`--evaluate` 只启用离线检查，不修改实盘触发点。历史缺少当时证据会明确
标为无法验证，不用事后成交补成提前成功。操作和已保存的覆盖报告见上方链接。
`scripts/observe_early_feed.py` 另提供默认关闭的独立实时影子采集入口；首版只采集识别/归属和业务
快照，不报价、构建或下单。用法及未完成边界见 [影子采集说明](docs/HOWTO_REPLAY_EARLY_FEED.md#独立实时影子采集首版识别与归属)。
v2 增加固定 race 字节码适配和独立部署快照门禁；历史 BUY 语义覆盖为 116/119，仍不是提前下单通过数。
`--reconstruct-context --mysql --log ...` 可进一步恢复已有历史报价并审计订单、决策及预检来源；
避免把旧导入器的空 snapshots 误解为数据库无数据。解析加新鲜度覆盖为 133/145，完整提前资格另计。
SQLite 默认在 `var/replay.sqlite3`；重复回放不会重复插入相同信号。
当前测试共 361 项，包括 12 项需 `requirements-race-tests.txt` 的可选 EVM 测试；未安装可选依赖时会跳过。
服务器首次验证顺序为安装、单元测试、离线回放，
再执行下面的 60 秒实时只读监听；完整历史验证记录见 [VALIDATION](docs/VALIDATION.md)。

## 实时只读监听

```bash
.venv/bin/sm-copy monitor --seconds 60
.venv/bin/sm-copy export --db var/observer.sqlite3
```

`--seconds 0` 持续运行直到 Ctrl-C。默认仅监听 60 秒，另有启动 RPC 和最多 15 秒排空时间。
支持环境变量 `ROBINHOOD_RPC_URL`、`ROBINHOOD_FEED_URL`，也会从当前目录 `.env` 加载且
只接受这两个键；钱包和私钥变量会被忽略。
不要把含 API key 的完整 URL 提交 Git 或贴到日志中。公开端点可能限流。

只读纸面模式使用独立、严格校验且不接受私钥字段的 JSON 配置。示例中的钱包和额度必须先
替换，再在每次启动时明确选择沿用或重置手动额度周期：

```bash
.venv/bin/sm-copy monitor --seconds 60 --paper-config config/paper.example.json \
  --paper-cycle-action reset --paper-cycle-id manual-2026-09-12 --paper-cycle-reason operator_reset
.venv/bin/sm-copy monitor --seconds 60 --paper-config config/paper.example.json \
  --paper-cycle-action reuse
.venv/bin/sm-copy paper-mark --config config/paper.example.json --db var/observer.sqlite3
.venv/bin/sm-copy paper-export --db var/observer.sqlite3
.venv/bin/python scripts/validate_paper_readonly.py
```

默认主触发为 `swap_evidenced`；也可选择统一的 `evidenced`，接受严格的 direct swap、Relay BUY
或 Relay SELL 证据。Feed 和 receipt 只作影子比较且不占额度。主触发通过后会再次
取得固定区块的实时报价；只有第二次报价仍通过原始 `minOut`、时效、偏离、价格冲击和 Gas
门控，才写入本地 paper fill。`run_mode=paper` 全过程没有签名、广播或真实订单。显式的
`mainnet_live` 测试模式默认只执行最终能严格验证为本地 V2/V3/V4 的路径；关系的
`execution_providers=["local","kyber"]` 时，本地路径不可用后改由 KyberSwap 聚合器为 follower 询价；
显式设为 `["kyber"]` 则跳过本地寻路，直接由聚合器询价
并构建交易，经 Router 白名单、顶层 calldata 反解、滑点下限和签名前 `eth_call` 模拟四道门禁后
执行（见 `docs/OPERATOR_RUNBOOK.md`）。`mainnet_live` 还要求 MySQL
配置/账本、逐条 enabled live relationship、全局 stop file 和签名/广播前的 MySQL 配置快照复核；
普通启动仍然不会签名或广播。Relay 自动关联需显式传 `--relay-auto-associate`，它只使用订单
做归因，再从同一回执发现并验证本地池，不复用源 calldata。confirmed live 回执会按规范块与
follower ERC-20 净差额结算收益 lot；当前仍不能用于无人值守运行。具体见
`docs/OPERATOR_RUNBOOK.md`。
`allowed_assets` 是可信本金、结算币和中间路由币集合，不是可以买入的 token 白名单。
对于回执已经形成严格兑换证据的 BUY，目标 token 动态放行；SELL 的来源 token 只有存在该
relationship 的归因持仓 lot 才能卖。路径中的其他端点和所有中间资产仍必须出现在
`allowed_assets`，旧 `feed_intent` 影子分支不会动态放行未确认目标币。新提前入口则仅在
`run --early-trial-id` 显式选择试运行、原始意向及归属重新验证后动态接受目标币，本金/结算币
仍须受信，预算/持仓/授权/时效检查仍保留。主 `trigger_mode=evidenced` 保留为严格后备；
仅将它改成旧 `feed_intent` 不会启用新提前入口。详见上方操作员交接文档。
`allowed_routes` 可为空；其中列出的
V2/V3/V4 路径只作为预配置本地路由（含 fee/tickSpacing/hook/hookData）。严格证据中的动态目标
直连池或仅经过可信中间币的池不要求事先枚举 meme token 合约。对 0x、Kyber 或 Relay Solver 源信号，
同一配置必须能得到唯一的本地 V2/V3/V4 报价路径；它既可来自预配置，也可来自同一 receipt
经 factory 验证的唯一方向匹配池。程序保留源协议归因，但不会复用聪明钱的聚合器 calldata。路由方向对称，但换 fee、hook 或中间池
都会得到不同 key 并被拒绝。

跟卖只处置该 relationship 归因形成的 position lot。退出资产取该 lot 的原始本金资产：例如
ETH 买入后，即使聪明钱经 Relay 卖成 USDG，纸面跟单仍以允许列表中的 Token→ETH 路径重新报价，
按 ETH 原始整数计算收益并恢复 ETH_WETH 额度。多个本金资产均可满足同一卖出而无法唯一选择时，
以 `attributed_principal_asset_ambiguous` 拒绝，不做跨币种原始整数相减。

程序会恢复真实发送者、解析已知智能账户/EntryPoint 包装，再按支持的 ABI 识别行为。
目标只是收款人的大规模等额分发汇总为一条 BULK_DISTRIBUTION，不生成多个买入信号。
部分交易仍为 UNKNOWN/needs_review，这是明确的支持边界，不是已证明没有兑换。

## 输出如何理解

重要字段：`mode`（执行路径）、`behavior`（业务行为）、`stage`（证据状态）、
`userop_index`、`path`、输入输出资产、原始整数金额字符串、`reasons` 和 `evidence`。
`execution_success` 只表示外层或对应 UserOperation 成功，不保证可失败子调用成功。
`swap_evidenced` 是有限的回执级对应证据，不是最终性或实盘许可。
所有信号的 `copy_eligible` 都是 false。
`intent_status`、`execution_status` 和 `canonical_status` 分开保存；重组只能纠正执行/规范链
证据，不能删除已观察到的 Feed 意向。第三方入账的 intent_status 为 not_attributed。

## 当前覆盖与未完成项

已实现 legacy/type 1/2/4 交易解析，两类已知 7702 账户、4337 handleOps、Relay 外包装与
存款、部分 V2/V3 方法、Universal Router 的 V2/V3 与新版 V4 单跳、领奖和转账识别。
包含消息新鲜度、重连、序列缺口告警、容量限制、回执核对、SQLite 事件去重，以及
先落盘后入队的持久候选和有界回执重试。健康日志会报告候选状态及进程内阶段延迟分位数。
独立规范区块游标按默认 2 个确认后的 safe head 推进，每轮最多补 20 个完整区块；补抓候选
标记为 `backfill/fresh=false`。父哈希不连续时停止推进，等待显式重组处理。
扫描器保存最近的规范区块链条；发生不连续时最多回查 64 块寻找共同祖先，将孤块信号标为
`canonical_status=orphaned` 并重新核对候选，但永久保留原 Feed 意向。超过自动深度时会停扫，
由操作员显式运行 `sm-copy reconcile-reorg --db ... --max-depth N`；该命令只用 RPC 逐块核对已
保存的哈希，找到共同祖先后才回退，不接受未经链上验证的人工哈希。
补洞还使用单区块 `eth_getLogs`，只查询 ERC-20 Transfer 和观察地址 recipient topic；命中
仅扩大第三方入账候选，仍须回执核对且不能直接分类为 BUY。

**尚未实现**：完整聚合器覆盖、V3/V4 多跳真实样本验证、bundled/UserOp Gas 付款归属、
目标链 Solver 交付的独立链上复核、V4 多跳报价的真实链样本、组合 USD 换算、
实盘风控与订单/仓位执行器。非零 hooks 和未知资产需要单独验证。

不要将观察器部署后直接当作自动交易机器人。后续顺序见完整方案的 M2/M3/M4。

## MySQL 跟单关系配置

可用 MySQL 单表 `copy_relationships` 维护“跟单钱包公开地址 × 聪明钱 × 唯一策略”。本机
Docker 初始化仅绑定 `127.0.0.1:3308`：

```bash
docker compose up -d mysql
.venv/bin/sm-copy relationships-import \
  --follower-wallet 0x你的公开地址 \
  --follower-label my-paper-wallet
```

导入命令读取 `data/fomo_watchlist.csv`，为 67 个聪明钱建立关系，默认全部 `enabled=0`。
历史数据中的零地址仅作为禁用导入占位；任何启用配置中的零 smart wallet 或零 follower wallet
都会在加载阶段拒绝。现有旧占位数据继续保留，但新的 `relationships-import` 要求非零 follower 和
非零 smart wallet。native asset 使用的零地址不受此限制。
远程数据库连接由进程环境提供：

```bash
export SMART_MONEY_MYSQL_HOST=db.example.internal
export SMART_MONEY_MYSQL_PORT=3306
export SMART_MONEY_MYSQL_USER=smart_money_runtime
export SMART_MONEY_MYSQL_PASSWORD='由使用者自行维护'
export SMART_MONEY_MYSQL_DATABASE=smart_money
export SMART_MONEY_MYSQL_SSL_CA=/etc/ssl/certs/数据库服务端CA.pem
```

远程 MySQL 必须显式配置 HOST、PORT、USER、PASSWORD、DATABASE、SSL_CA 并校验 TLS；不会把
本机 Docker 的默认端口或账号密码用于远程连接。只有 `127.0.0.1`/`localhost` 允许使用容器
自签名证书。
监听进程账号只需 `SELECT copy_relationships`。批量导入另用
`SMART_MONEY_MYSQL_ADMIN_USER`/`SMART_MONEY_MYSQL_ADMIN_PASSWORD` 可写账号。

使用 MySQL 配置启动纸面监听：

```bash
.venv/bin/sm-copy monitor --seconds 60 --paper-mysql \
  --paper-cycle-action reset --paper-cycle-id manual-2026-09 \
  --paper-cycle-reason operator_reset --db var/observer.sqlite3
```

一个 monitor 进程可加载多个跟单钱包，也允许同一个聪明钱分别配置给多个跟单钱包；额度、
提案、仓位和跟卖额度恢复按 relationship 隔离。实盘启动会逐条校验关系与对应 key 元数据；
同一 follower 的授权、nonce、签名和广播串行，不同 follower 可并行。私钥不在
`copy_relationships` 中，只在通过逐关系门禁后从独立 key MySQL 按 follower 精确读取。

业务 MySQL 还包含 `003_runtime_ledger.sql` 定义的运行与收益账本。将既有 SQLite 搬入 MySQL 时，
必须先停止该 SQLite 的写入进程，确认没有非空 `-wal` 文件，再由操作员核对源文件 SHA-256：

```bash
sha256sum var/observer.sqlite3
.venv/bin/sm-copy ledger-migrate \
  --sqlite var/observer.sqlite3 \
  --confirm-source-sha256 64位小写SHA256
```

迁移器只接受完整匹配的源哈希，在一个 MySQL 事务中按外键顺序复制 20 张表。目标主键已存在时
逐列核对：完全相同则视为幂等重跑，任何差异都会回滚整次迁移，不覆盖目标数据。输出逐表
`source_rows/inserted_rows/existing_identical_rows`；不会迁移私钥。迁移核对完成后，运行命令可加
`--ledger-mysql`，例如：

```bash
.venv/bin/sm-copy monitor --seconds 60 --paper-mysql --ledger-mysql \
  --paper-cycle-action reuse
.venv/bin/sm-copy paper-export --ledger-mysql
.venv/bin/sm-copy execution-audit --ledger-mysql
```

不加该参数仍使用 SQLite；一个进程只选择一个后端，不双写。正式切换前不要删除原 SQLite，
也不要让 SQLite 和 MySQL 两个 monitor 同时处理同一组钱包。

每条启用关系独立使用该行的策略版本、主/影子触发点、报价风控、协议、资产和精确路由；不同
关系无需配置成相同值。每行都有自己的配置快照哈希并写入归因记录。

可用以下临时数据演练核对多 follower 隔离。脚本只复制一条现有公开配置，使用两个保留测试
地址，验证后按本次自增 ID 删除并查询确认剩余为零；不连接私钥库或 RPC：

```bash
.venv/bin/python scripts/validate_mysql_relationship_isolation.py
```

## 独立私钥数据库（仅离线准备）

用户要求的简单私钥数据源使用另一个 MySQL 实例，与关系配置库分离：

```bash
docker compose up -d key_mysql
```

本机绑定 `127.0.0.1:3309`，数据库/表为 `smart_money_keys.wallet_keys`。初始化后为空；项目不会
自动生成或导入私钥。使用者通过管理账号插入小写公开地址及 `0x` 开头的 32-byte 私钥，并先
保持 `enabled=0`：

```sql
INSERT INTO wallet_keys(wallet_address, private_key_hex, enabled)
VALUES ('0x公开地址', '0x私钥', FALSE);
```

运行账号只拥有三列的 SELECT 权限。远程连接需设置
`SMART_MONEY_KEY_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE/SSL_CA`，非本机连接缺少 CA 会拒绝。
远程 key MySQL 同样要求上述六项全部显式提供，不会回退到本机默认凭据。离线检查需要同时
显式设置 `SMART_MONEY_EXECUTION_MODE=offline_test`、
`SMART_MONEY_SIGNING_MODE=offline_test` 和 `SMART_MONEY_EMERGENCY_STOP=0` 才能访问，并且每次
签名前检查 `SMART_MONEY_EMERGENCY_STOP_FILE`（默认 `var/EXECUTION_STOP`）不存在。运行期间创建
该文件即可阻止下一次密钥读取/签名。只允许 chain ID 4663 的 type-2 签名；这些开关不要写进
项目 `.env`。`enabled=TRUE`、`run_mode='mainnet_live'` 且行内 `live_risk_accepted_at` 不早于当前
更新时间的 MySQL 行，才是逐关系实盘
授权源；每次私钥读取和广播前都会重新查询并核对 follower、relationship ID、完整配置快照及确认
是否仍为最新，不再维护单独的风险确认 JSON。详见
[OPERATOR_RUNBOOK](docs/OPERATOR_RUNBOOK.md) 与
[LIVE_RISK_CHECKLIST](docs/LIVE_RISK_CHECKLIST.md)。

部署级 RPC/Feed 和两个 MySQL 连接设置一次后，数据库驱动的固定任务直接运行：

```bash
.venv/bin/sm-copy run
```

该命令固定使用 enabled `copy_relationships`、MySQL 运行账本、Relay 自动关联和自动额度周期复用。
聪明钱地址由 enabled relationship 自动加入监听集合。新增或修改 `copy_relationships` 时，在同一条
SQL 中刷新行内实盘确认；修改 `wallet_keys` 后不需要其他确认。随后重启同一任务即可生效；重启不会
重置累计投入，降低上限至已占用额度以下会拒绝启动。
全局 `var/EXECUTION_STOP` 存在时仍会在读取实盘密钥之前拒绝。

只检查某个公开钱包在 key DB 中是否存在/启用，而不读取私钥列：

```bash
SMART_MONEY_EXECUTION_MODE=offline_test \
SMART_MONEY_SIGNING_MODE=offline_test \
SMART_MONEY_EMERGENCY_STOP=0 \
.venv/bin/sm-copy key-status --wallet 0x公开钱包地址
```

输出只有公开地址、`found`、`enabled`、`read_only` 和固定的 `private_key_read=false`。该命令仍受
stop file、TLS 和远程连接完整显式配置约束。

如果进程在离线签名后、raw bytes 交给调用方前退出，`recover_signed` 可以在重启后重新执行关系、
额度、报价和 RPC 预检，再确定性重建同一交易；仅当账本仍是唯一、尚未被 RPC 观察的 `signed`
attempt 且重建 hash 完全相同时返回。raw bytes 仍不落库，已 pending/confirmed/reverted/replaced/
orphaned 的 attempt 不允许走该恢复路径。

由独立测试广播方或未来获授权的发送方提供公开 hash 后，可运行只读生命周期跟踪：

```bash
.venv/bin/sm-copy execution-track --db var/observer.sqlite3 \
  --proposal-id 公开proposal-id

.venv/bin/sm-copy execution-track --db var/observer.sqlite3 \
  --proposal-id 公开proposal-id --tx-hash 0x替换交易hash \
  --replaces-tx-hash 0x被替换交易hash
```

命令只调用 RPC 查询方法并输出 `broadcast_performed=false`；replacement 仍须通过相同意图、nonce
和逐级提价验证。它不能发送交易。

启动、重启、紧急停止和状态处置步骤见
[OPERATOR_RUNBOOK](docs/OPERATOR_RUNBOOK.md)。
逐项完成度与仍需外部验收的边界见
[GOAL_ACCEPTANCE](docs/GOAL_ACCEPTANCE.md)。

## 本地双钱包买卖闭环

本地集成测试使用 Anvil chain ID `31337`，不会连接 Robinhood 主网，也不会读取项目 `.env` 或
私钥数据库。脚本每次生成两个临时公开地址，由 Anvil 在本地模拟账户能力，部署一次性 USDG 与
固定汇率池；聪明钱买入/卖出后，跟单钱包按 50% 比例执行对应买卖：

```bash
docker compose --profile local-test up -d local_chain
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/work" -w /work \
  ghcr.io/foundry-rs/foundry@sha256:0c00cb0bda1ab1b91c9a6bf60f4c76c09c1a8870824b6d4718afbabacf6f9a17 \
  'forge build'
.venv/bin/python scripts/validate_local_copytrade.py
docker compose --profile local-test stop local_chain
```

输出包含两个临时地址、四笔本地交易 hash、原始整数金额和最终 token 余额，并固定声明
`mainnet_rpc_used=false`、`private_keys_used_by_project=false`、`copy_eligible=false`。
脚本还把两个聪明钱回执保存为 chain 31337 的 `swap_evidenced` 测试信号，通过现有比例策略、
额度预留、BUY/SELL proposal、fill、position lot 和 realized PnL 状态机；跟卖后仅恢复对应本金。
测试账本使用每次独立的 `var/local-copytrade-*.sqlite3`，不写业务 MySQL。该测试不代表主网
Router、Feed、报价、延迟或实盘广播已经通过。
