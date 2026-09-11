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
- 样本中 CRIBS 买/卖可识别为意图；原生 ETH 净流尚未核实，所以回执状态仍需 review。
- 实时只读 smoke 已接通 feed 和 RPC，成功获取一笔外部交付候选的回执；没有发送交易。
- 2026-09-11 新服务器基线和首个 P0 小步见
  `SERVER_VALIDATION_2026-09-11.md`：候选已改为先持久化再进入有界队列，支持重启恢复和
  最多 8 次持久回执退避；关键零值 counters、候选状态与阶段延迟分位数已进入健康日志。
  当前共 51 项测试。RPC 允许列表、保守分类和 `copy_eligible=false` 均未改变。
- 意向、执行、规范链状态已拆为独立字段，Feed 来源单独标记；第三方入账不归属为目标意向。
  SQLite 已有独立 `canonical_l2` 区块游标，禁止未经过显式重组处理的静默倒退。RPC 区块
  扫描器、safe head 推进和重组回滚仍是下一小步，不能把“已有游标表”误报为补洞完成。

## 明确未完成，不能误报为已上线跟单

M1 不读取私钥，不签名、不广播，copy_eligible 恒为 false。
完整聚合器、V4 多跳/结算收款人、V2/V3 factory 校验、原生币 trace/净流、
历史执行位置的委托代码验证、规范链重查/重组、断线补洞、Relay 完整订单关联尚未实现。
也没有报价模拟、PnL、订单/仓位/预算预留系统或生产交易执行器。
未知输入不会被猜成成交。真实样本 UNKNOWN 是支持边界，不是没有交易。

## 下次开发建议：先把 M2 的确认链路做完整

1. 阅读 COPYTRADING_PLAN.md，运行 tests 和 replay。
2. 增加 V2/V3 factory/pool 验证、V4 结算 recipient 和完整资金流对应。
3. 为 ETH 净流接只读 trace 或可验证状态差分，分开 Gas、退款与实际成交。
4. 给第三方入账添加区块/Transfer 兜底和订单关联，保守处理未证明的购买关系。
5. 候选持久化、回压不静默丢失和有界回执重试已完成首版；下一步补独立区块游标、
   断线补洞、规范链重查与重组撤销。Feed sequence 不能冒充区块号。
6. 补完以上后再做实时可得报价和纸面跟单，不能用目标历史成交价代替自己的报价。

用户尚未选择实盘预算、跟卖比例、止损参数、私钥方案或付费 RPC。需要到实盘阶段再确认。
只读开发可以继续，不要提前索要私钥。

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
依据 [官方 CLI 参考](https://developers.openai.com/codex/cli/reference/) 和本机帮助。
