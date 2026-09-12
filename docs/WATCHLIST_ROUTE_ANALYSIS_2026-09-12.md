# Robinhood 观察名单历史交易路径分析

日期：2026-09-12 UTC。结构化摘要见
[watchlist_route_analysis_2026-09-12.json](../data/watchlist_route_analysis_2026-09-12.json)。

## 结论

当前 67 个观察钱包的主要路径不是直接调用池，也不是 OKX。2026-09-01 07:45:29 至
2026-09-12 14:13:03 UTC 的窗口内，5,248 个目标钱包主动发起、成功且在目标
UserOperation 范围内同时出现 Swap 和钱包资产变化的候选组中：

| 入口或执行器 | 数量 | 占 5,248 的比例 | 说明 |
| --- | ---: | ---: | --- |
| 0x AllowanceHolder | 3,177 | 60.54% | 绝大多数包在 Relay 调用内 |
| KyberSwap MetaAggregationRouterV2 | 1,992 | 37.96% | 绝大多数包在 Relay 调用内 |
| GMGN Router | 30 | 0.57% | 本地资金流闭合交易 |
| Uniswap Universal Router | 13 | 0.25% | 本地资金流闭合交易 |
| 1inch Router | 6 | 0.11% | 本地资金流闭合交易 |
| Uniswap V3 Router | 1 | 0.02% | 本地资金流闭合交易 |
| 其他未知入口 | 28 | 0.53% | 保持 UNKNOWN/needs_review |
| Relay 内层入口未解 | 1 | 0.02% | 保持 UNKNOWN/needs_review |
| 目标钱包直接调用成交池 | 0 | 0% | 本窗口没有观察到 |
| 当前官方 OKX Router | 0 | 0% | 不能据此否定更旧地址或未来使用 |

因此：方案 B 仍可使用 OKX 为**跟单钱包重新报价和构造自己的交易**，但为了正确识别这批
聪明钱的源信号，开发优先级应先是 Relay v3 + 0x + KyberSwap，再补 GMGN 和 1inch。
不能因为跟单执行走 OKX，就跳过源交易归属和 Relay order 的解析。

## Fomo 网站、Relay 与实际成交方的关系

这批清单来自 Fomo 用户资料，但 `handle`、Fomo 站内账户和链上执行地址不能混为一个字段。
Fomo Web 的公开路由使用 `/profile/:handle` 表示用户身份，用
`/tokens/:chain/:tokenAddress` 表示链上资产；项目清单因此保留 `handle`，而实际监听只使用已经
解析出的 `real_evm`。链上归属还必须继续落到该地址对应的 EIP-7702/4337 UserOperation，不能
把 EntryPoint 外层 bundler 地址、同一 bundle 的其他用户，或站内昵称当作目标钱包。

Fomo 官方条款明确说明两点：Fomo 本身不是 DEX/交易所，交易发生在第三方协议、DEX 和链上；
Robinhood Token 的访问基础设施明确包含 Uneven Labs 运营的 Relay API。2026-09-12 获取的公开
Web 模块也显示，买卖报价请求发往 Fomo 后端 `/swaps/v2`；其 v2 返回值包含 `relaySwapId`，以及
类型为 `EVM` 或 `SOLANA` 的 `relayTransaction`。聚合器名称和最终 calldata 并未在网页模块中
固定，而是由后端报价结果带回。

因此这里应分成三层理解：

```text
Fomo 页面/账户：选择币、金额和方向，展示 handle 与交易记录
  -> Fomo /swaps/v2：取得报价与可签名交易
  -> Relay：跨链订单、资金清理、存款及交付编排
  -> 0x / KyberSwap / 其他 DEX 路由：在源链或目标链实际完成兑换
```

“走 Relay”和“走 0x/Kyber”并不冲突。前者描述订单与跨链执行外层，后者描述 Relay 交易内部
实际使用的流动性/成交路由。本窗口的链上结果正好与网站设计相符：5,123 个 Relay 源链存款
尝试中，3,161 个内层走 0x、1,961 个走 Kyber，1 个内层仍未解析。网站前端公开代码只能证明
它请求并执行 Relay 交易；具体某一笔用了哪个 DEX，仍必须按对应 UserOperation 的 calldata、
Swap 日志和钱包资金流逐笔确认。

## 实际操作形态

最常见的 5,123 组不是普通的 `Token -> ETH/USDG -> 原钱包余额`，而是：

```text
观察钱包的 UserOp
  -> Relay ApprovalProxy
  -> 0x AllowanceHolder 或 KyberSwap 聚合换汇
  -> 得到 USDG
  -> Relay Router cleanupErc20sViaCall
  -> Relay Depository.depositErc20(wallet, USDG, orderId)
```

5,123 次调用的 depositor 均精确等于对应观察钱包，目标币均是 Robinhood USDG，orderId 均
唯一。回执中有 5,122 次找到逐字段唯一匹配的 DepositRecorded；另 1 次 calldata 的 orderId
和实际事件 orderId 不同，必须保留复核，不能为了凑统计把它判成功。

这解释了为什么只看钱包余额时只有输入 Token 流出、没有 USDG 或 ETH 回到钱包：USDG 被同一
UserOp 立即存入了 Relay 订单。它可以证明 Robinhood 源链发生了换汇和订单存款，但不能单凭
Robinhood 回执证明目标链收款地址属于同一个聪明钱，也不能证明目标链最终交付。后续需要把
orderId、Relay 请求、目标链交易和目标收款人继续闭环。

除上述订单源链换汇外，有 118 组在 Robinhood 本地闭合了资金流：69 组 ERC-20 -> ETH，
13 组 ETH -> ERC-20，36 组 ERC-20 -> ERC-20。另有 7 组虽有 Swap 和钱包变化，但资金方向仍
无法闭合，继续 UNKNOWN/needs_review。这里的“闭合”是分析口径的目标 UserOp Swap + 钱包
净流闭合；尚未支持的 Router 不会因此自动升级为生产代码中的 `swap_evidenced`。

## Meme 币观察

链上没有“meme 币”标准字段。本报告只用保守的 symbol/name 关键词做可复现的名称启发式，
不代表代币质量、价值或投资分类。该口径命中 172 个代币、1,649 个候选组：

- 1,627 组是 `meme Token -> 0x/Kyber -> USDG -> Relay order`；
- 21 组是 Robinhood 本地资金流闭合兑换；
- 1 组仍无法闭合；
- 1,643 组表现为该 meme Token 从观察钱包扣除，只有 6 组是仅入账方向；
- 路径为 0x 1,095 组、KyberSwap 547 组、Universal Router 2 组、其他未知 5 组、OKX 0 组。

出现最多的清晰 meme 风格名称包括：

| Token | 合约 | 候选组 | 0x | Kyber |
| --- | --- | ---: | ---: | ---: |
| MOO / Memory cow Moo | `0xd9db30bb0d2b8d2eae3826a1372117e058791e18` | 243 | 155 | 88 |
| MEME / A Meme Coin | `0x385f4f8ae47651ce5f58f5265395a669f8281e18` | 159 | 99 | 60 |
| FATCOIN | `0x12d5ee7917ca430073c3a638ee1e6f0648a98a01` | 125 | 99 | 26 |
| AI / Artificial Inu | `0x2e8c31162b855a2ffa90f6f8634643ad6f111e18` | 118 | 80 | 38 |
| SHROOM / MUSHROOM | `0xab093def657f15df31b33922a95e047add645b29` | 86 | 61 | 25 |
| CASHCAT / Cash Cat | `0x020bfc650a365f8bb26819deaabf3e21291018b4` | 77 | 59 | 18 |
| NUDES / Send Nudes | `0xbe98b75361935b18d688409424a869a4c3dc7401` | 72 | 45 | 27 |
| BONER / Boner Coin | `0x98096d17e191b3da1d5f99a6d7b3584351b11e18` | 42 | 27 | 14 |

该结果说明这些钱包在当前窗口里更常见的是处理/卖出已经入账的 meme Token，而不是从
Robinhood 钱包直接大批买入。此前 59,026 条观察名单 Transfer 日志中绝大多数是单向入账；
不能把这些被动收币当成买单。

## OKX bundle 负例

当前官方 OKX Robinhood Router 为 `0x6e2a35a7ad683cf634d91492d73bb7ff774c6919`，Approval 为
`0x42170295f1173c9e5874ea9d00c6d137e1a4f53d`。目标观察钱包的归属范围内没有命中它们。

交易 `0x113cde57e08817cd18306acb1387aba79b3845d8391886c5cecf6cfa341b690b`
的外层 calldata 确实包含 OKX Router，但它位于 UserOp 0；观察钱包
`0xde7c85ed0520221b7d5802753366410fc8eba6d7` 位于 UserOp 2，自己的调用范围内没有 OKX。
把整个 bundle 搜地址会错误地把别人的 OKX Swap 归给目标钱包，因此统计坚持按 UserOp 隔离。

OKX 官方同时说明 Router/Approval 地址可能因升级替换，生产集成应校验 API 返回的地址，而不是
仅硬编码当前地址。本报告的零命中仅适用于当前官方地址和本分析窗口。

## 是否接入其他聚合器

建议分两条独立能力推进：

1. **源信号识别**：优先补 Relay `cleanupErc20sViaCall`、Depository `0x5a1ee3ac`、0x
   AllowanceHolder `0x2213bc0b` 和 Kyber `0xe21fd0e9`。这是当前最大缺口，覆盖 5,123 个
   Relay 订单源链换汇候选。每笔仍需核对 token debit、池 Swap、USDG deposit、depositor、
   orderId 和 DepositRecorded；不能只凭 Router 地址通过。
2. **跟单执行**：继续方案 B，为跟单钱包用 OKX 重新请求报价；不复用聪明钱 calldata，且校验
   chain、token、amount、recipient、Router/Approval、minOut、value、有效期和返回代码。

第二梯队可补 GMGN 和 1inch 的**源信号解码**。它们总量只有 36 组，但在 118 组本地闭合交易中
占 30.51%，对于本地买卖覆盖仍有价值。是否把 GMGN/1inch 也作为跟单报价服务是另一项选择，
当前数据没有证明有必要；先用 OKX 做执行端即可。剩余 28 个未知入口继续样本驱动，不能按
selector 猜测。

## 口径与限制

- 使用 `.env` 已配置的 dRPC 只读端点；报告不保存或打印含凭据 URL。
- 先按 67 地址查询 ERC-20 Transfer topic，再取 37,152 笔相关交易中的主动钱包/UserOp 交易、
  回执和受限 `prestateTracer diffMode`。所有金额仍按原始整数处理。
- 共发现 59,026 条 Transfer、37,152 笔相关交易、60 个有日志的钱包；筛出 7,145 笔主动候选，
  其中 7,133 笔能按当前账户快照解码。
- 对 5,248 个候选逐笔读取交易前一块的账户 code：5,244 个与快照实现一致；3 个差异是后来才
  委托的 EOA 直接交易，不依赖账户批量 ABI；另 1 个是同交易 type-4 authorization，不能只用
  前一块 code 否定其委托执行。
- Transfer 起点会漏掉失败交易、纯 approval、纯原生币和无日志行为；本报告不是链生命周期全史。
- “成功回执 + Swap”仍不自动等于目标钱包交易，统计还要求目标 UserOp 范围与钱包资产变化。
- Relay 源链存款不等于目标链归属或最终性，跟单收益也未在本分析中验证。

合约身份参考：

- [Fomo 官方条款：第三方 DEX、Relay API 与 Robinhood Token](https://fomo.family/terms)
- [Fomo 官方钱包架构：智能钱包、批处理与统一余额](https://fomo.family/blog/learn/fomo-security-wallet-architecture)
- [Fomo Web 官方说明：统一 USD 余额与跨链兑换](https://fomo.family/blog/announcing-fomo-web)
- [Relay 官方合约地址](https://docs.relay.link/references/api/api_resources/contract-addresses)
- [0x 官方 Settler/AllowanceHolder 说明](https://docs.0x.org/docs/core-concepts/contracts)
- [KyberSwap 官方 Router 与执行流程](https://docs.kyberswap.com/kyberswap-solutions/kyberswap-aggregator/developer-guides/execute-a-swap-with-the-aggregator-api)
- [1inch Robinhood Chain 说明](https://help.1inch.com/en/articles/15618946-robinhood-chain)
- [OKX 官方 Robinhood 支持与合约地址](https://web3.okx.com/ro/onchainos/dev-docs/trade/dex-smart-contract)
