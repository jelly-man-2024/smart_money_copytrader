# Robinhood Chain 聪明钱跟单方案

日期：2026-09-09。状态：方案 + M1 只读观察器初版；不是可实盘交易产品。

## 1. 目标和边界

基于原 Fomo_sniper 的研究，在同级目录建立独立工程，监听清单账户的真实兑换行为。
不再以“新币部署/建池”为核心触发条件，不把被动空投、奖励、转账当成买卖。
监听地址来自 `data/fomo_watchlist.csv`，67 个去重地址；地址标签和盈利能力未经独立认证。
链固定为 Robinhood mainnet，chain ID 4663。不会复制原工程的私钥、钱包文件或运行状态。

先完成可审计的只读信号，再做报价与模拟，再开放有限路径实盘；快速跟单最后单独验收。
开发和回放测试不需要私钥，也不需要付费 RPC。公共端点不等于生产可靠性承诺。

## 2. 两个维度，避免分类混淆

### 执行/身份维度

| mode | 身份识别 | 含义 |
| --- | --- | --- |
| direct | 签名恢复出的 tx.from | 账户直接调用合约；不预设它一定在买卖 |
| self_account | tx.from + 自身账户调用 + 委托实现 | 自己调用 execute/executeBatch |
| bundled_account | 已知 EntryPoint 下的 UserOperation.sender | Bundler 代提交；不能把 Bundler 当聪明钱 |
| third_party | 收款人，后续关联订单 | 可能是分发、转账或交付；关联前不能认定 Solver 履约 |

前 3 种是发起方式，第 4 种是入账观察视角，可以属于同一个跨链业务的不同阶段。
EIP-7702 委托是持续账户状态，不意味着后续每笔交易都是 type 4。type 2 也能调用委托账户。

### 业务行为维度

- BUY / SELL / TOKEN_SWAP：明确兑换意图。以 ETH/WETH/USDG 为初始报价资产集合。
- APPROVAL / AUTHORIZATION：授权，不是买卖。
- TRANSFER / INCOMING_TRANSFER：转出或转入，不自动解释为卖出/买入。
- CLAIM：明确的领奖调用；只实现核对过的合约和方法。
- LIQUIDITY / LIQUIDITY_OR_POSITION_CALL：流动性或头寸操作，不当作普通购币。
- INTENT_DEPOSIT：Relay 存款，不能凭存入资产推断目标币。
- BULK_DISTRIBUTION：符合保守分发特征的被动入账汇总，不证明发放方的业务目的。
- EXTERNAL_DELIVERY_CANDIDATE：外部兑换后交付，订单/付款归属未确定。
- UNKNOWN / SETTLEMENT：尚未覆盖或用于结算的调用，不自动下单。

“领取空投 -> 卖掉奖励”必须保留两个动作；不能因出现 claim 就丢弃整个批次。
相反，一次分发命中 32 个清单地址只产生一条分发汇总，不生成 32 个买单。

## 3. 数据来源和解析步骤

1. Feed WebSocket 接收 JSON，Base64 解码 l2Msg，递归拆 Nitro kind 3/4。
2. RLP 解码 legacy/type 1/type 2/type 4，恢复签名发送者、校验 chain ID、计算哈希。
3. 直接发送者命中，或解出已知 EntryPoint 的目标 UserOperation，才建立主动身份。
4. 根据已知账户委托实现解开 execute/executeBatch；合约地址 + ABI 共同决定解码方式。
5. 递归解析支持的 Router、Relay 外包装；得到独立的内部动作和原始整数金额。
6. 回执中逐 UserOperation 限定日志范围，核对失败、资产净变化和池事件。
7. 输出 JSONL 和 SQLite 事件，使用链/交易/钱包/内部路径组成幂等键。

不使用 calldata 地址子串作为身份确认；它只用于扩大低优先级候选集合。
不把 header.sender 当交易发送者，不把 header.blockNumber 当 L2 区块。
M1 不假定 feed sequenceNumber 与所有 Nitro 链的区块号恒等。

## 4. 批量空投过滤

采取两级设计：

- Feed 快速层（后续补充已审计分发合约适配器）：只有验证过部署代码/方法及参数结构，
  确认是纯分发且目标只是被动收款，才在深度解析前短路。不能只拉黑 selector，不能把
  multicall/handleOps 或“收款人超过 100”当作通用排除条件。
- M1 回执层（已实现）：第三方交易无主动目标信号，单一 token、单一资金转出者、
  至少 100 个收款人、金额一致、没有目标 Transfer 支出、没有标准 Swap 事件，输出
  BULK_DISTRIBUTION 汇总；不进入买卖候选。其余外部入账保持候选/未知。

因此 M1 仍可能为批量分发查询一次回执，但不会为每个收款人进行兑换解析或生成买单。
无 Swap 事件不等于绝对不存在经济交换；这项过滤的含义是“没有主动购买证据”，
不是链上空投身份的密码学证明。正规订单交付必须按订单证据重新关联。

真实负例：0xb81520…1652658 向 230 个地址等额发同一种币，其中 32 个在清单中。
原始交易和回执保存在 `data/bulk_distribution.json`。

## 5. 证据状态，而不是非黑即白

| stage | 含义 | 是否能实盘 |
| --- | --- | --- |
| intent | 已解出调用意图，尚未获得执行结果 | 否 |
| execution_observed | 外层或对应 UserOperation 成功；不保证每个可失败子调用成功 | 否 |
| swap_evidenced | 支持的单一 V4 ERC20 兑换中，PoolId、操作日志、钱包输入输出有对应证据 | 否 |
| needs_review | 原生币净流、未知聚合器、多路由、订单归属等证据不完整 | 否 |
| failed | 外层或目标用户操作执行失败 | 否 |

所有状态都不是 L1 最终性、利润承诺或真实操盘者动机的证明。
M1 的 execution_success 表示外层/用户操作成功，不声称每个内部动作成功。
`copy_eligible` 在 M1 永远为 false；程序根本没有签名、私钥读取和广播代码。

确认跟单前的下一步必须包括：

- 已验证 Router/池子部署、V2/V3 factory 归属和 token0/token1，防止伪造事件。
- ABI/代码版本、7702 委托在目标执行位置的有效状态，不仅是 latest 或历史快照。
- 原生 ETH 内部转账、退款、Gas 分开记账；不能把 tx.value 或池级输出等同于钱包净成交。
- V4 hooks、费用、recipient、settlement；完整结算未解析前接收者保持 null，明确标记需验证。
- 同一个 UserOperation 含多个 swap/未知子调用时，保留待核实；不套用整笔净额。
- 回执与规范链区块哈希核对、重组撤销及确认深度。M1 尚未持续重查规范链。
- 代币特殊逻辑、税费和实际可卖性；不能只信任 Transfer 日志或代币名称。

## 6. 跟单策略和资金控制设计（M2/M3，尚未接入）

### 开仓

第一版实盘仅支持显式资产/路由允许列表，采用确认后跟单。每个目标买入事件只触发一次。
金额用独立小额上限或用户配置比例，不复制对方的绝对金额，不默认全仓。
自己的账户、授权、nonce、recipient、deadline、minOut 必须重新生成；绝不重播原始交易。

预交易检查：报价时间、源操作后可得价格、最大价格偏离、价格影响、流动性、Gas、
单笔/单币/单目标/组合风险限额、当日亏损上限、输入币余额和已预留资金。
任何检查超时或缺失都拒绝下单。minOut 不得默认 0。
具体资金上限和滑点由用户在实盘阶段确认；本阶段不假设任何实盘预算。

### 平仓

独立维护跟单仓位 lot，标明目标钱包和来源事件。目标卖出时按其可靠可见的卖出比例
处理本系统对应 lot，而不是卖掉自己的其他资产。目标历史持仓不完整时，不伪造比例。
目标转币另发事件，不当作卖单；关联新地址必须有证据。
目标停止卖出、流动性枯竭、RPC 故障时仍需独立风控退出机制。

### 执行账本

SQLite 初版信号库之后增加 proposals/orders/fills/positions/reservations 表。
预算预留与订单创建必须是同一事务；nonce 采用 pending 状态并持久化。
广播超时不能直接重发另一 nonce，先查原交易；支持重启恢复和替换交易跟踪。
跟单前后核对真实资产净变化。任何账实不符触发全局暂停。

### 两种触发速度

- 确认模式：目标成功且完整证据通过后报价、下单，作为首个实盘版本。
- Feed 快速模式：只向已验证路径开放；接受“目标最终失败、自己已成交”的风险。

排序器 feed 广播的是已排序交易，不是公共待处理交易池。不能仅凭 feed 把自己的
交易插到已排序目标交易之前。原工程“部署 -> 建池”准备窗口不适用于一般钱包 swap。
3–5 块领先是原工程历史观测，不是我们端到端延迟指标。

## 7. Solver / Relay 的独立分支

源账户支出、存款、目标链成交、最终交付分别建事件，以协议订单证据关联。
`orderId` 不直接等于所有 API 的 `requestId`。订单可能在别的链发起，单监听 Robinhood
只能看到最后交付；只有收币不能证明用户主动下单。
Relay 历史订单 API 曾返回需要 x-api-key；是否能提前读取完整资产信息尚未验证。
付费 RPC 不能代替 Relay 的订单访问权限。M1 不访问需要认证的订单接口。

## 8. 当前模块与实现里程碑

| 模块 | M1 初版实现 | 后续 |
| --- | --- | --- |
| feed.py | 有界解包、签名恢复、type 4、新鲜度、重复/缺口处理 | 签名认证、持久游标、RPC 补洞 |
| registry.py | 主网地址、67 地址导入、两种已知委托识别 | 链上代码哈希/版本验证、配置化扩展 |
| decode.py | 账户/4337、Relay 外层、部分 V2/V3、UR V2/V3/V4 单跳 | 未知聚合器、V4 多跳、完整结算接收人 |
| receipts.py | UserOp 日志隔离、回执校验、资金变化、分发汇总 | 池归属验证、trace、规范链重查 |
| store.py | SQLite 幂等事件、禁止意图覆盖更高证据状态 | 订单、资金预留、仓位与重组处理 |
| cli.py | 离线回放、有限时只读实时监听、导出 | 服务部署、告警、连续健康管理 |

M1 本次交付：工程、方案、真实样本回归、只读 observer；不是完整覆盖所有交易。
M2：补齐确认链路和持久化补洞；接历史/实时可得报价做模拟跟单，禁止未来信息泄漏。
M3：独立钱包、有限资产和小额预算，经用户明确确认后实现并验收实盘执行器。
M4：对比确认/快速模式的真实延迟、价格偏离、失败成本；评估是否值得订单前置监听。

## 9. 验收指标

- 必须通过真实领取、转账、LP、存款、230 地址分发等负例；不能产生自动买单。
- claim + swap 同批次保留 swap；其他用户的成功 swap 不能掩盖目标用户失败。
- raw / UserOp / 内部调用的身份归属、哈希、金额原始整数可追溯。
- 统计未知路径占比、支持路径召回率、重复事件、掉队/断线/队列丢弃、RPC 错误。
- 缺少回执、代码验证、规范链证据的信号只能观察；首次实盘前全部未实现项复审。
- 模拟成交价必须来自决策时可得报价，不能拿聪明钱成交价作为自己的成交价。
- 观察到交易不证明目标盈利，也不证明其有意看好代币。

## 10. 已知工程限制

M1 是安全、保守的第一段可运行链路：RPC 使用有界线程池和标准库 HTTP，不声称
低延迟最优；实时钱包委托按候选读取 latest，存在历史位置差异，结果写明状态来源。
外部候选只靠 calldata 出现目标地址扩大集合，无法覆盖收款地址完全隐藏在链上状态
或其他链的订单；需要 M2 增加 Transfer 日志订阅/区块扫描兜底。
Feed 缺口会告警重连，队列溢出会计数，但尚无持久化补洞；回执超时会记录，不自动补抓。
Feed 来源依赖 WSS TLS，尚未验证排序器消息签名。未知/畸形消息有界失败，不开放实盘。

## 11. 依据

- 原项目与 2026-09-08 调查：`../fomo_sniper/docs/smart-money-copytrade-investigation-2026-09-08.md`。
- [Robinhood 连接信息](https://docs.robinhood.com/chain/connecting/)。
- [Nitro Feed 格式](https://docs.arbitrum.io/run-arbitrum-node/sequencer/read-sequencer-feed)。
- [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702) 与 [ERC-4337](https://eips.ethereum.org/EIPS/eip-4337)。
- [Universal Router](https://developers.uniswap.org/docs/protocols/universal-router/concepts/commands)。
- [IV4Router 当前单跳结构](https://github.com/Uniswap/v4-periphery/blob/main/src/interfaces/IV4Router.sol)：
  样本包含 minHopPriceX36 字段，不能照抄旧版结构。
- [Relay 合约地址](https://docs.relay.link/references/api/api_resources/contract-addresses)、
  [Depository](https://docs.relay.link/references/protocol/components/depository)、
  [Settlement](https://docs.relay.link/references/protocol/overview)。
