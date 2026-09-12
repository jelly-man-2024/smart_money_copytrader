# 来源与证据

本项目由用户要求在原项目同级新建；不是原项目子目录或克隆远端交易机器人配置。

- 原工程：git@github.com:dvzhang/Fomo_sniper.git；本机目录 `/home/jelly/applet/fomo_sniper`。
- 初始研究基于原 `sniper/feed.py` 的 Nitro 拆包思路，新的模块独立实现，补充签名恢复、
  type 4、尺寸/递归限制和持续新鲜度检查。未复制旧私钥、钱包、.env 或运行数据库。
- `data/fomo_watchlist.csv`：用户在原工程提供的清单，原样复制，67 个地址。
- `data/transaction_examples.json` 和 `data/summary.json`：2026-09-08 只读研究产物，原样复制。
- `data/account_codes.json`：从同日 watchlist_snapshot.csv 的 account_code_at_snapshot 提取。
  仅用于历史回放；不能作为未来账户实现的永久保证。
- `data/bulk_distribution.json`：同日 RPC 查询已保存的 tx/receipt 中提取交易
  0xb81520b047688d7f40db42073fcc3343e87d3815cb909a6a3fef702116d52658。
  230 个等额收款人，其中 32 个在清单。用于批量分发负例测试，不用于验证空投价值。
- `data/relay_order_evidence_3ccc6f52.json`：2026-09-11 通过 Relay 公开只读
  `GET /requests/v2` 按链上 orderId 查询后人工裁剪的字段保留样本。保留 requestId、源交易、
  protocol deposit、destination fill/outTx 和接收者 FT 正向 stateChange；未保存无关费用明细、
  签名或大段原始 calldata。API key 未使用。该样本证明 Relay API 报告的订单关联，不证明
  Solana 目标交易已由本项目独立 RPC 复核。
- `data/relay_signal_evidence_2026-09-12.json`：2026-09-12 使用项目 `.env` 中已配置的
  dRPC 与 Robinhood 公开 feed 做只读监听时捕获的紧凑证据摘要。包含 Relay + 0x、
  Relay + Kyber 各一笔正例和一笔关联前的被动入账候选；原始整数金额保持字符串，
  不含 RPC URL、凭据或私钥。完整运行账本位于服务器临时路径
  `/tmp/smart-money-goal1-live-escalated.sqlite3`，该临时文件不作为长期来源保证。
- `data/relay_passive_buy_evidence_2026-09-12.json`：2026-09-12 使用 Relay 官方公开只读
  `GET /requests/v2?hash=...` 按上述被动入账的目标交易哈希查询并人工裁剪。保留唯一 request/
  order ID、源链付款人和入金、Robinhood outTx/stateChange、destination fill 与 orderData payment。
  Relay 响应声明 v2 已弃用、应迁移 v3；该文件不含签名、大段 calldata 或接口凭据。
- `data/watchlist_route_analysis_2026-09-12.json`：2026-09-12 使用项目已配置 dRPC 只读端点，
  对原有 67 地址从区块 51,551,461 至 61,167,966 做 Transfer 起点的窗口扫描后生成的聚合摘要。
  另取候选交易、回执和受限 prestate state-diff，按目标 UserOperation 隔离；未保存 RPC 凭据、
  完整原始扫描或私钥。旧样本均未改写。详细口径和限制见
  `docs/WATCHLIST_ROUTE_ANALYSIS_2026-09-12.md`。
- 2026-09-12 同时只读核对 Fomo 官方条款、钱包架构文章、Fomo Web 说明及当时公开 Web
  静态模块。条款明确 Robinhood Token 入口包含 Relay API；公开模块的 `/swaps/v2` 返回结构
  包含 `relaySwapId`/`relayTransaction`。静态模块未复制进仓库，也没有登录、签名或提交交易；
  聚合器归属仍以来自 dRPC 的目标 UserOperation 链上证据为准。
- 测试中的随机本地账户只用于离线编码/验签，没有资金、不会广播。

纸面报价使用的 V3 Quoter `0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7` 和 V4 Quoter
`0x8dc178efb8111bb0973dd9d722ebeff267c98f94` 来自 Uniswap 官方 SDK 地址注册表，并在
2026-09-12 与官方 v4 deployments 页面交叉核对：

- https://github.com/Uniswap/sdks/blob/main/sdks/sdk-core/src/addresses.ts
- https://developers.uniswap.org/docs/protocols/v4/deployments
- https://github.com/Uniswap/v4-periphery/blob/main/src/interfaces/IV4Quoter.sol
- https://github.com/Uniswap/v4-periphery/blob/main/src/libraries/PathKey.sol

后两项是 V4 多跳 `QuoteExactParams` 与 `PathKey[]` ABI 的官方来源；没有依据第三方聚合器猜测
tuple 布局。

服务器随后通过已配置 RPC 做固定区块的只读 `eth_call` 验证。实时报价不会保存成可在未来
复用的价格样本，也不能作为盈利证明。

这些不是完整交易历史、随机样本或收益证明。原生币、失败交易、日志之外的状态变化
不一定被最初 Transfer 抽样覆盖。所有统计必须保留抽样口径。

原项目授权/许可证未独立确认；本工程暂不擅自声明上游代码或清单的开源许可。
如果之后发布或商用分发，应先核对所有来源和依赖的授权条款。
