# 跟单流程文档入口

更新时间：2026-09-14。

完整流程统一维护在 [copy_trade_flow.md](copy_trade_flow.md)。本页替代原 v2.0 的重复说明，
避免旧版“只有本地执行路径”“固定64块重组窗口”等描述与当前代码混用。

新版包含：

- Feed、Transfer范围补洞、候选持久化与重试、UserOperation归属和历史状态取证。
- 直接兑换、Relay被动BUY、Relay SELL和订单补证的具体条件。
- 转账、分发、领奖、授权、LP、未知路径以及策略拒绝的行为矩阵。
- 按relationship隔离的金额、预算、lot与跟卖比例。
- 本地路径与Kyber执行提供方、四轮报价、授权、签名、广播和结算。
- 真实交易元数据、数据库和日志快照、可复算时间线，以及分阶段延时优化建议。

原始金额以整数计算、十进制字符串保存。观察层的
`copy_eligible=false` 不授予交易权限；已有live能力仍由独立关系配置、证据和执行门禁控制。

初次流程核对只读完成。后续提前执行及报价优化已实现，部署状态与操作步骤以
[Feed 提前执行交接](EARLY_FEED_LIVE_HANDOFF.md) 为准；实现完成不代表实盘已启用或延时已改善。

相关资料：

- [运行数据与时间线摘录](feedback/flow_audit_2026-09-14.json)
- [操作手册](OPERATOR_RUNBOOK.md)
- [风险检查清单](LIVE_RISK_CHECKLIST.md)
- [开发交接与历史记录](HANDOFF.md)
- [历史方案](COPYTRADING_PLAN.md)
