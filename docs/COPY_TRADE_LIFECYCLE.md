# 一笔聪明钱交易，如何走到我们的跟单成交

和 [copy_trade_flow.md](copy_trade_flow.md) 的区别：那份是**架构与执行顺序**（代码怎么跑），
这份是**问题驱动的生命周期**（一笔交易要闯过哪些判断），仿 `fomo_wallet_research/README.md` 的写法。

图中数字为 2026-09-13 ~ 09-16 实测，口径见文末。

```mermaid
flowchart TD
    A{这笔链上交易是谁发的？}
    A1["Feed：已排序交易流<br/>observation_source=feed<br/>占已成交 147/147，延迟 3~12 秒"]
    A2["Backfill：按地址过滤日志范围扫描<br/>补进程中断的历史<br/>曾因逐块串行 RPC 落后 5.6 小时"]
    A1 --> A
    A2 --> A

    A --> I["身份归属<br/>67 个观察名单：只采信号<br/>6 条 enabled 关系：才会真跟"]
    I1["copy_relationships（MySQL 3308）<br/>smart_wallet、run_mode、trigger_mode<br/>allowed_assets、仓位规则、风控阈值"] --> I
    I2["5 个真实聪明钱全部取自研究层 51 候选<br/>sebdegen #8 / skullNick3 #30 / Blizzardkicks_ #32<br/>BertLuvv #39 / aim_z_sol #43，另 1 个测试钱包"] --> I

    I --> B{这是不是一笔真买卖？}
    B1["dRPC debug_traceTransaction prestateTracer<br/>取真实余额变动 actual_input_debit / output_credit<br/>不是 feed 里的意向金额"] --> B
    B2["Relay 订单关联：跨链买入的 USDC 来源<br/>卖出款存入 Relay 的 deposit 闭环"] --> B

    B --> B3["relay_buy_evidenced / relay_sell_evidenced<br/>swap_evidenced（本链直接 swap，历史仅 2 次）<br/>→ 可跟"]
    B --> B4["needs_review / execution_observed / intent<br/>failed（链上 revert，56 次）<br/>BULK_DISTRIBUTION 空投分发<br/>→ 一律不跟"]

    B3 --> C{该不该跟？六道闸}
    C1["① 触发模式：evidenced 必须有链上证据<br/>feed_intent / receipt_success 只做影子记录"] --> C
    C2["② 资产白名单 allowed_assets<br/>③ 持仓归属 attributed_position（卖出必须有货）"] --> C
    C3["④ 价格偏离 adverse_price_deviation<br/>⑤ 价格冲击 price_impact<br/>⑥ 报价可得 quote_unavailable"] --> C

    C --> C4["拒绝原因落库，不下单<br/>卖出最大拦点：attributed_position_insufficient<br/>买入最大拦点：价格类风控"]
    C --> D{怎么把单发出去？}

    D1["路由：local V2/V3/V4 优先<br/>找不到路由落 Kyber<br/>0x AllowanceHolder（09-16 新增）"] --> D
    D2["报价复用：提前取好，移出关键路径<br/>签名前重报价 + 模拟 + nonce 复核"] --> D

    D --> D3["放弃（abandoned）<br/>quote_missing_or_expired 占 44%<br/>aggregator simulation reverted 占 26%<br/>feed_intent_expired、快照不匹配"]
    D --> E["签名 → 独立广播器发送 → 释放钱包锁"]

    E --> F["tracker：回执 + 规范区块<br/>fill / lot / 已实现 PnL / 额度归还<br/>revert 则释放预留"]

    F --> G{事后复盘}
    G1["延迟拆解：发现+决策 / 报价 / 建计划<br/>签名广播 / 入块，链上仅占 0.5~0.8 秒"] --> G
    G2["按 Birdeye 实时价对未平仓市值重估<br/>不能用成本价充当市值"] --> G
    G --> G3["跟单覆盖率：买 52% / 卖 19%"]
    G --> G4["源钱包整体 −2.0%（2 赚 3 亏）<br/>我们 −26.5%（不含 gas）"]
    G --> G5["单笔 gas ≈ 仓位的 50~100%<br/>仓位 $0.10 是结构性亏损来源"]
```

## 口径与关键结论

**为什么必须等链上证据**：147/147 已成交订单的金额都来自 prestate trace 的真实余额变动。
feed 只负责**发现**，RPC 负责**确认与计量**。5,696 条信号里「从未上链」**0 条**、
「上链但 revert」1.14% —— 所以这道等待买的不是「防未上链」，而是**分类与计量能力本身**：
82% 的信号是空投、授权、转账、待复核，没有 trace 就分不出哪些是真买卖。

**延迟不是主要矛盾**：09-13 的 13 秒 → 09-16 的 3 秒，而告警到见顶的中位是 44 分钟。
真正决定盈亏的是下面三条。

**三个未解决的结构性问题**：
1. **只买不卖** —— 买入覆盖 52% 导致一半仓位我们没有，他们卖时 `attributed_position_insufficient`。
   源钱包靠卖出回收买入的 62%，我们只回收 44%，这是 24 个百分点收益差的主因。
2. **跟单权重与研究评分脱节** —— 主仓压在 BertLuvv（−18.7%），而 +20% 的 sebdegen（研究 #8）跟得最少。
3. **仓位 $0.10 扛不住 gas** —— gas 每笔恒定约 0.0000232 原生币，与仓位大小无关。

**易错点**：Kyber / 0x AllowanceHolder 是**路由合约不是人**；
观察名单 67 ≠ enabled 关系 6；`relay/1` 路径的 `smart_wallet_label` 为 NULL 但归属没丢。
