# 跟单执行操作手册（当前为离线准备）

本文描述当前受支持的运维流程。它不是主网上线授权。项目当前没有广播入口，RPC allowlist
不包含 `eth_sendRawTransaction`，`copy_eligible` 固定为 `false`。

## 数据源边界

- 配置库保存公开数据：`copy_relationships` 一行表示一个“跟单钱包 × 聪明钱”关系以及唯一策略、
  额度、触发点、允许协议/资产/路由和配置快照。
- 私钥库是另一个 MySQL，由使用者在自己的服务器上自行维护。项目运行账号只能按公开地址读取
  `wallet_address, private_key_hex, enabled`；配置库和 SQLite 账本都没有私钥字段。
- 当前里程碑不得向私钥库导入真实或测试私钥。离线回归只使用进程内随机无资金密钥和模拟查询。
  将来只有完成风险验收并得到用户再次明确授权后，才可单独启用主网密钥读取、签名和广播。

可用 `sm-copy key-status --wallet 0x公开地址` 做元数据连通性检查。它只选择 `wallet_address` 和
`enabled`，不选择 `private_key_hex`，输出固定带 `private_key_read=false`；仍必须显式打开三个
offline_test 门禁，且 stop file 存在时拒绝连接。

## 新增或修改跟单关系

1. 用配置库管理账号插入关系，首先保持 `enabled=0`。CSV 批量导入同样默认禁用。
2. 核对 follower/smart 地址、策略类型、固定金额或比例、币种额度、单聪明钱累计投入上限、
   trigger、quote policy、协议、资产和完整 route。
3. 使用运行账号加载配置并检查 snapshot hash；运行
   `.venv/bin/python scripts/validate_mysql_relationship_isolation.py` 验证多 follower 隔离。
4. 当前只允许用于只读监听和纸面跟单。不得因配置 `enabled=1` 推断系统获准实盘。

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

纸面跟单每次启动必须明确选择周期：

- `--paper-cycle-action reuse`：沿用上次累计投入和剩余额度。
- `--paper-cycle-action reset --paper-cycle-id ... --paper-cycle-reason ...`：人工开启新周期。
- 跟卖成交会按该 relationship 的归因持仓释放累计投入额度；失败、revert 或 UNKNOWN 不释放。

## 三层停止控制

任一层都应 fail closed：

1. 将目标 `copy_relationships.enabled` 设为 `0`，签名前的新连接复核会拒绝旧配置快照。
2. 保持 `SMART_MONEY_EMERGENCY_STOP` 非 `0` 或未设置。离线测试只有显式设置为 `0` 才放行。
3. 创建 `SMART_MONEY_EMERGENCY_STOP_FILE` 指向的文件；默认是 `var/EXECUTION_STOP`。每次密钥访问
   和离线签名前都会复查。

出现异常时先停用关系并创建 stop file，再运行 `execution-audit` 和只读 RPC 核对。不要清库、
重置 nonce 或重发交易。当前没有项目内广播，因此外部广播产生的 hash 只能交给只读 tracker 跟踪。
CLI 为 `sm-copy execution-track --db ... --proposal-id ...`；原始签名 hash 可省略 `--tx-hash`，
replacement 必须同时给出新 hash 和 `--replaces-tx-hash`。输出固定
`read_only=true,broadcast_performed=false,copy_eligible=false`。

## 状态处置

- `prepared`：有持久 nonce reservation，尚未签名；重启应复用同一 proposal/plan。
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

## 主网上线前仍需人工验收

- 在独立测试链完成真实广播的失败、超时、nonce 冲突、replacement、revert、重启和重组演练。
- 确认广播瞬间的最终报价、余额、Gas、额度和 nonce 原子边界。
- 操作员书面确认钱包、逐关系/逐币种额度、周期规则、Gas/滑点上限、停止和恢复流程。
- 用户审阅证据后再次明确授权主网读取私钥、签名与广播。

这些条件未全部满足前，不增加主网开关，不改变 `copy_eligible=false`。

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
