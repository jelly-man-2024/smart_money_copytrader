# Mac mini 运行环境准备交付

日期：2026-09-13 UTC。适用于把本仓库交给 Mac mini 上的 Claude Code，仅完成运行环境与空数据库
准备。钱包、策略、额度、私钥、实盘关系启用、授权和 monitor 启动由使用者在下一阶段亲自完成。

## 可直接交给 Claude Code 的提示词

```text
你正在 Mac mini 上接手 smart_money_copytrader，仓库为：
git@github.com:jelly-man-2024/smart_money_copytrader.git

请完整阅读并严格执行仓库中的 docs/MAC_MINI_ENV_HANDOFF.md，只执行“Claude Code 本轮允许执行”
范围：安全拉取 main 最新代码、创建全新的本机 Python 虚拟环境、按 requirements.lock 安装依赖、
运行 pip check/全部 unittest/13笔离线回放、创建并验收 mysql 与 key_mysql 两个 Docker 容器。

先检查 Git 状态并保留任何已有改动；不要 reset、checkout 覆盖、stash 或删除文件/volume。
当前代码至少应包含提交 d8c4f86，但以 origin/main 实际后续提交为准，不要重置到这个哈希。

严格停在空环境交付点：
- 不索取、读取、生成、复制或插入任何私钥；
- 不插入或启用 copy_relationships，不导入聪明钱 CSV；
- 不启用 mainnet relationship 或进程开关，不发送 approve，不签名或广播交易；
- 不启动 monitor、常驻脚本、LaunchAgent 或其他服务；
- 不复制旧服务器的 .env、数据库、var 或运行状态；
- 不执行 docker compose down -v，不删除既有 volume；
- 不修改依赖锁、安全门、RPC allowlist或业务代码来通过测试；
- 不提交或推送 Git。

如果端口、旧容器/volume、脏工作树、依赖架构兼容或 Docker 权限存在冲突，停止对应步骤并报告，
不要自行覆盖。结束时按文档模板汇报实际 HEAD、架构/Python、测试结果、容器健康、schema与空表
行数、急停文件状态、未完成事项；不得输出任何密码、连接密钥或完整付费 RPC URL。
```

## 1. 本轮边界

Claude Code 本轮允许执行：

1. 检查 Mac 架构、macOS、Python、Docker Desktop 和 Compose。
2. 克隆仓库，或在已有仓库没有未处理冲突的前提下 `git pull --ff-only`。
3. 新建 Mac 本机 `.venv`，按锁定版本安装并运行离线验证。
4. 启动 `mysql` 和 `key_mysql`；只允许它们绑定现有 Compose 定义的
   `127.0.0.1:3308/3309`。
5. 验证 schema、权限账号和表为空；创建本地执行急停文件。
6. 汇报结果与明确交给使用者的下一步，不运行下一步。

以下全部不在本轮授权内：钱包/私钥写入、relationship 插入或启用、付费 RPC 配置、实盘启用、
USDG/Token 授权、主网签名/广播、monitor 启动、旧账本迁移、Git 提交/推送、系统常驻项和防火墙修改。

特别注意：旧服务器当前可能仍运行同一聪明钱与跟单钱包的主网 monitor。Mac mini 绝不能同时启动
第二个实例，否则可能重复跟单、争抢同一钱包 nonce，并产生两个互不一致的额度/持仓账本。

## 2. 拉取与环境检查

先采集状态，不安装系统级软件：

```bash
uname -sm
sw_vers
python3 --version
docker version
docker compose version
```

要求 Python 3.10+。锁文件此前主要在 Linux x86_64/Python 3.10 验证；Apple Silicon 必须以本机
完整测试结果为准。若缺少 Docker Desktop、Python 3.10+、Xcode Command Line Tools 或 Git SSH
权限，报告缺项并等待使用者处理；不要静默安装 Homebrew、Docker Desktop 或改系统安全设置。

仓库不存在时：

```bash
git clone git@github.com:jelly-man-2024/smart_money_copytrader.git
cd smart_money_copytrader
```

仓库已经存在时，先进入实际根目录：

```bash
git status --short --branch
git rev-parse HEAD
git remote
```

这里只确认存在 `origin`，不得输出可能带凭据的 remote URL。工作树干净且当前分支为 `main` 时才执行：

```bash
git fetch origin
git pull --ff-only origin main
```

如果有改动、分支不对或无法 fast-forward，保留现场并报告，不 reset/stash/强制 pull。拉取后完整
阅读 `AGENTS.md`、`README.md`、`docs/OPERATOR_RUNBOOK.md`、`docs/HANDOFF.md`、
`docs/LIVE_RISK_CHECKLIST.md` 和本文。记录实际 HEAD；它应包含 `d8c4f86` 或更晚提交。

不要复制其他机器的 `.venv`、`.env`、`var/`、SQLite、Docker volume、数据库 dump 或
任何密钥。先检查以下端口和既有 Docker 状态：

```bash
lsof -nP -iTCP:3308 -sTCP:LISTEN
lsof -nP -iTCP:3309 -sTCP:LISTEN
docker compose ps -a
docker volume ls
```

端口已被非本项目进程占用，或发现名称/用途不明的旧容器、旧 volume 时，不停止、不删除，先报告。
不要用 `docker compose config` 输出插值后的配置，因为它可能暴露本机提供的密码。

## 3. Python 依赖与离线基线

只创建 Mac 本机虚拟环境。若 `.venv` 已存在，先核对其中 Python 架构和版本；无法证明属于当前
Mac/当前仓库时不要删除它，报告后使用者决定是否更名或重建。

新环境命令：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sm-copy replay --extra-fixture data/bulk_distribution.json --db :memory:
```

当前参考值是177项 unittest全部通过、13笔历史回放，所有信号保持 `copy_eligible=false`。后续提交
可能增加测试，以实际 discover 数量为准。回放必须继续保留 UNKNOWN、被动入账和其他负例；不能
放宽断言或改锁定依赖来制造通过。Apple Silicon 如遇某个 pin 没有 wheel/无法构建，记录 Python
版本、`uname -m`、包名和错误类型，不自动换版本、不修改 `requirements.lock`。

测试阶段不要设置主网进程开关，也不要连接私钥库。单元测试成功后再创建急停文件：

```bash
mkdir -p var
touch var/EXECUTION_STOP
chmod 600 var/EXECUTION_STOP
ls -l var/EXECUTION_STOP
```

该文件存在时，真实密钥读取、签名和广播门禁会失败关闭。`var/` 已被 Git 忽略。

## 4. 创建两个空 MySQL 容器

只启动以下两个服务，不启动 `local_chain`，也不启动项目 monitor：

```bash
docker compose up -d mysql key_mysql
docker compose ps
```

首次启动允许 Docker 下载 Compose 已指定的 `mysql:8.4` 镜像。等待两个容器都变为 healthy；可只
查看健康状态，不打印容器完整环境变量：

```bash
docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  smart-money-mysql smart-money-key-mysql
```

Compose 的持久卷不能删除。初始化 SQL 只在空 volume 第一次启动时执行；如果 volume 不是新的，
必须验证所有 schema，不能假定后续 `docker/mysql/init/*.sql` 已应用，更不能用 `down -v` 重建。

本地结构应为：

- 业务库：`127.0.0.1:3308`，数据库 `smart_money`；包含 `copy_relationships` 和20张运行/收益账本表。
- 私钥库：`127.0.0.1:3309`，数据库 `smart_money_keys`；包含 `wallet_keys`。
- 两个端口只绑定 loopback，不得改成 `0.0.0.0`。

用容器内 root 环境变量验证结构，命令不会显示密码：

```bash
docker compose exec -T mysql sh -lc \
  'mysql -N -B -uroot -p"$MYSQL_ROOT_PASSWORD" smart_money -e "
   SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"smart_money\";
   SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema=\"smart_money\" AND table_name=\"copy_relationships\"
       AND column_name=\"live_risk_accepted_at\";
   SELECT COUNT(*) FROM copy_relationships;
   SELECT COUNT(*) FROM signals;
   SHOW GRANTS FOR \"smart_money_runtime\"@\"%\";"'

docker compose exec -T key_mysql sh -lc \
  'mysql -N -B -uroot -p"$MYSQL_ROOT_PASSWORD" smart_money_keys -e "
   SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"smart_money_keys\";
   SELECT COUNT(*) FROM wallet_keys;
   SHOW GRANTS FOR \"smart_money_key_runtime\"@\"%\";"'
```

全新交付环境中，`copy_relationships`、`signals`、`wallet_keys` 都应为0行。MySQL 客户端可能显示
“password on command line”警告，因为密码通过容器环境变量展开；不要把实际密码复制到汇报。
上面的实盘确认字段数量应为1。全新 volume 会自动执行 `006_database_live_acceptance.sql`；如果
volume 在拉取新代码前已经初始化，Docker 不会重放 init 文件，应报告“需要数据库管理员执行006
一次”，本轮不要删除 volume 来触发重建。

再验证应用能使用业务运行账号访问空账本：

```bash
.venv/bin/sm-copy execution-audit --ledger-mysql
```

空账本应显示 `plans=0`、`attempts=0`，且不能声称已经有端到端交易证据。若 audit 将空账本标为
未覆盖，这是正确的安全语义，不是数据库初始化失败。

不得运行 `relationships-import`：它会批量写入关系配置。不得运行 `key-status`：本轮既没有密钥行，
急停文件也应保持存在。

## 5. Claude Code 的停止点与交付报告

确认以下条件后立即停止，不继续配置或启动：

- 实际 `origin/main` 已拉取，工作树原有修改均被保留。
- `.venv` 是本机创建；`pip check`、完整 unittest、13笔离线回放结果已记录。
- `smart-money-mysql`、`smart-money-key-mysql` 均 healthy。
- 3308/3309 仅监听 `127.0.0.1`。
- 业务和私钥 schema 完整，三张关键表均为空。
- `var/EXECUTION_STOP` 存在且权限为600。
- 没有 monitor/sm-copy 常驻进程，没有 relationship 或私钥，没有链上交易。
- Git 暂存区为空；不提交、不推送。

按以下格式回复使用者，值来自实测但不要包含秘密：

```text
Mac mini 环境准备结果
- 仓库绝对路径：
- 分支 / HEAD：
- 原有未提交改动：
- macOS / 架构：
- Python / pip：
- pip check：
- unittest：通过数 / 失败数
- 离线回放：交易数 / 信号数 / copy_eligible=true数量
- Docker / Compose：
- mysql健康状态 / 端口：
- key_mysql健康状态 / 端口：
- smart_money表数量，copy_relationships/signals行数：
- smart_money_keys表数量，wallet_keys行数：
- var/EXECUTION_STOP：存在 / 权限
- monitor或主网操作：未运行
- Git最终状态：
- 阻塞或架构差异：
- 下一步由使用者完成：钱包、relationship、额度、RPC、实盘启用、单实例切换和启动
```

## 6. 后续由使用者亲自完成（本轮不要执行）

使用者后续需要单独完成并验收：

1. 在独立 `smart_money_keys.wallet_keys` 中写入跟单钱包地址和对应私钥。地址与私钥均要求小写
   `0x` 格式，先保持 `enabled=FALSE`；私钥不得进入聊天、Git、`.env`、shell history或业务库。
2. 在 `smart_money.copy_relationships` 写入一条“跟单钱包 × 聪明钱”的唯一关系，先保持
   `enabled=FALSE`，明确 fixed/proportional规则、USDG/ETH额度、卖出比例、协议、可信本金/中间资产
   和报价风险参数。
3. 确保聪明钱地址出现在启动命令指定的 watchlist 中。若使用自定义列表，启动时必须显式传
   `--watchlist /实际路径.csv`；遗漏会在签名之前失败退出。
4. 配置 RPC/Feed。项目 `.env` 只自动读取 `ROBINHOOD_RPC_URL`、`ROBINHOOD_FEED_URL`；不要把完整
   付费 URL 写进文档或日志。数据库凭据必须通过进程环境或使用者自己的 secret 管理方式提供。
5. 轮换 Compose 初始化默认凭据，并使运行账号、进程环境和权限最小化配置一致。不得开放3308/
   3309公网访问。
6. 如沿用旧服务器同一 follower，不得建立空的新账本直接接管。应先停止旧 monitor、恢复旧急停、
   禁用旧 relationship、确认无 pending nonce，再决定是否迁移完整业务 MySQL账本；否则既可能
   双重跟买，也可能因缺失归因 lot而无法跟卖。
7. 配置完成后先做只读 relationship/status、余额、allowance、nonce、执行审计和有限监听；核对
   follower、relationship 与 config snapshot 后，由使用者在同一条 SQL 中将对应 MySQL 行设为
   `enabled=TRUE/run_mode='mainnet_live'`，并设置
   `live_risk_accepted_at=CURRENT_TIMESTAMP(6)`。不再创建仓库外风险确认 JSON。
8. `mainnet-approve-usdg` 与 `sm-copy run` 都是会产生真实资产风险的下一阶段操作，
   只有使用者再次明确确认后才能执行。

完整配置字段与运行门禁以 `docs/OPERATOR_RUNBOOK.md`、`docs/COPYTRADING_FLOW.md` 和
`docs/LIVE_RISK_CHECKLIST.md` 为准。本文件只交付空环境准备，不代表 Mac mini 已可安全上线。
