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
- 测试中的随机本地账户只用于离线编码/验签，没有资金、不会广播。

这些不是完整交易历史、随机样本或收益证明。原生币、失败交易、日志之外的状态变化
不一定被最初 Transfer 抽样覆盖。所有统计必须保留抽样口径。

原项目授权/许可证未独立确认；本工程暂不擅自声明上游代码或清单的开源许可。
如果之后发布或商用分发，应先核对所有来源和依赖的授权条款。
