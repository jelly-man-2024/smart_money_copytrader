# Smart Money Copytrader

Robinhood Chain 聪明钱跟单工程，独立于同级 `fomo_sniper`。
当前 v0.1.0 是 **只读观察器 + 回放验证**：不读取私钥，不签名、不发送交易，不实现真实成交。

- [完整跟单方案](docs/COPYTRADING_PLAN.md)
- [继续开发交接](docs/HANDOFF.md)
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

离线回放包含 11 笔历史样本、1 笔真实 feed 存款样本和可选的 230 地址批量分发。
不需要网络、付费 RPC 或私钥。信号输出到 stdout（JSONL），统计输出到 stderr。
SQLite 默认在 `var/replay.sqlite3`；重复回放不会重复插入相同信号。
目前单元测试共 46 项。服务器首次验证顺序为安装、单元测试、离线回放，
再执行下面的 60 秒实时只读监听；完整历史验证记录见 [VALIDATION](docs/VALIDATION.md)。

## 实时只读监听

```bash
.venv/bin/sm-copy monitor --seconds 60
.venv/bin/sm-copy export --db var/observer.sqlite3
```

`--seconds 0` 持续运行直到 Ctrl-C。默认仅监听 60 秒，另有启动 RPC 和最多 15 秒排空时间。
支持环境变量 `ROBINHOOD_RPC_URL`、`ROBINHOOD_FEED_URL`；`.env.example` 不会自动加载。
不要把含 API key 的完整 URL 提交 Git 或贴到日志中。公开端点可能限流。

程序会恢复真实发送者、解析已知智能账户/EntryPoint 包装，再按支持的 ABI 识别行为。
目标只是收款人的大规模等额分发汇总为一条 BULK_DISTRIBUTION，不生成多个买入信号。
部分交易仍为 UNKNOWN/needs_review，这是明确的支持边界，不是已证明没有兑换。

## 输出如何理解

重要字段：`mode`（执行路径）、`behavior`（业务行为）、`stage`（证据状态）、
`userop_index`、`path`、输入输出资产、原始整数金额字符串、`reasons` 和 `evidence`。
`execution_success` 只表示外层或对应 UserOperation 成功，不保证可失败子调用成功。
`swap_evidenced` 是有限的回执级对应证据，不是最终性或实盘许可。
所有信号的 `copy_eligible` 都是 false。

## 当前覆盖与未完成项

已实现 legacy/type 1/2/4 交易解析，两类已知 7702 账户、4337 handleOps、Relay 外包装与
存款、部分 V2/V3 方法、Universal Router 的 V2/V3 与新版 V4 单跳、领奖和转账识别。
包含消息新鲜度、重连、序列缺口告警、容量限制、回执核对和 SQLite 事件去重。

**尚未实现**：完整聚合器覆盖、V4 多跳/完整结算接收人、V2/V3 factory 归属校验、
ETH 净流与 trace、规范链重组处理、断线补洞、完整 Solver 订单关联、模拟报价/PnL、
实盘风控与订单/仓位执行器。非零 hooks 和未知资产需要单独验证。

不要将观察器部署后直接当作自动交易机器人。后续顺序见完整方案的 M2/M3/M4。
