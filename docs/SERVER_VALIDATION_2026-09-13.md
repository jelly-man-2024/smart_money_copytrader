# 服务器验证记录（2026-09-13 UTC）

## 动态目标资产修正

本轮修正 `copy_relationships.allowed_assets` 的错误语义。该字段现在只约束可信本金、结算币和
中间路由币，不再要求把每个未来可能买入的 meme token 预先加入配置。动态放行只适用于执行成功
且已形成 `swap_evidenced`、`relay_buy_evidenced` 或 `relay_sell_evidenced` 的 BUY 输出 token / SELL
输入 token。未确认 feed、UNKNOWN、needs_review、失败交易及未知中间币继续拒绝；SELL 仍需要
同 relationship 的归因持仓 lot。

同时允许 `allowed_routes=[]`，避免配置加载阶段强迫预先枚举未知目标 token。已有预配置路径和
安全检查均保留。当前聚合器/Relay 源若没有本地执行路径，在 OKX 动态交易构建完全接入前仍会在
报价阶段拒绝，不会降级复用聪明钱 calldata。

## 实测结果

- `.venv/bin/python -m pip check`：通过，`No broken requirements found`。
- `.venv/bin/python -m unittest discover -s tests -v`：本阶段最终 169/169 通过；新增完整
  `PaperEngine` 动态 meme 目标→报价→额度预留回归通过。
- `.venv/bin/sm-copy replay --extra-fixture data/bulk_distribution.json --db :memory:`：13/13 笔
  通过，输出 27 条信号，全部继续为 `copy_eligible=false`；保留 UNKNOWN、被动入账候选和批量
  分发负例。
- `git diff --check`：最终修改后通过。
- 未读取私钥、未签名、未广播、未发送主网交易。

## 当前边界

OKX v6 swap-data 客户端已经实现严格响应校验的第一步，但不在本次最小闭环内。实际 Fomo 样本使用
Relay 跨链订单和 Robinhood V3 目标池；本轮只为这条已有链上证据的路径补齐自动关联、动态本地
V3 路由发现、成交结算和有界 USDG 授权工具。`copy_eligible` 继续固定为 `false`；主网 live 关系
保持禁用，未签名、未广播。

## 专用主网测试钱包预配置

使用者于 2026-09-13 提供以下公开测试地址；私钥未提供给项目，也未由本轮读取：

- smart wallet：`0x89909912c58e2182d92b1a8638d6ff8d965e173b`
- follower wallet：`0x3004ab92565deeea0a2eaa27e40e297bb457e1a6`

业务 MySQL 新增 relationship `78`，保持 `enabled=0`，模式为 `mainnet_live`，主触发点已改为
统一的 `evidenced`。USDG 固定单笔为 `2000000` raw（2 USDG），手动周期累计上限为
`10000000` raw（10 USDG）。只把 USDG 配置为可信本金/结算资产；未来 meme 输出资产仍由已确认
Swap 动态识别。ETH/WETH 桶设为不可达的最小占位值，不允许 ETH/WETH 出资跟买。严格配置加载后
snapshot 为 `aa11cd2486b97f3064cd5f7c744f519f3dc6c079d4dbc4e909e25fe488c2f647`。

付费只读 RPC 实测 chain ID `4663`、USDG decimals `6`。查询时 follower 余额为
`4944071749814556` wei 与 `19792295` USDG raw。使用者自行插入 key 后，仅通过不选择私钥列的
元数据查询确认该 follower 记录存在且 enabled；项目未读取或输出私钥。

smart wallet 的现有 Fomo 测试活动不是直接池买入。区块 `61564933`（2026-09-13
01:26:36 UTC）由 Relay 向钱包被动交付 `55201742408563043470` raw MEME；它只能先标为
`EXTERNAL_DELIVERY_CANDIDATE/needs_review`。区块 `61565781`（01:28:01 UTC）则由 EntryPoint
UserOperation 执行 `MEME -> 0x -> USDG -> Relay Depository`，回执可严格归因为
`SELL/relay_sell_evidenced`，实际卖出 `55201742408563043470` raw MEME、存入 `2244353` raw
USDG。中间区块 `61565110` 的 PREPRERICH 被动入账没有 Swap/订单归属，继续保持
`INCOMING_TRANSFER/needs_review`，不能当成买入。

Relay 公共 `requests/v2` 将该 BUY 唯一关联到源链 Solana USDC `2500000` raw，并将目标钱包、
目标交易和输出币逐项闭合；目标 receipt 中又唯一发现经 V3 factory 验证的 USDG/MEME fee=3000
池。源 USDC 与 Robinhood USDG 的等价只接受这个明确的 chain/currency 二元组，其他来源保持
UNKNOWN/needs_review。该历史交易通过 monitor 实际回补：2 个候选、2 个 receipt 均 complete，
`relay_buy_associated=1`、`local_v3_routes_verified=1`，另一个普通被动入账仍为 needs_review；队列、
retry、failed、worker/RPC/reorg error 均为 0。日志位于
`/tmp/mainnet-test-relay-backfill.health.log`，信号位于
`/tmp/mainnet-test-relay-backfill.signals.jsonl`。

2 USDG 实时报价和完整配置/预算探针已经运行：BUY proposal 预留 `2000000` raw，10 USDG 周期剩余
`8000000` raw；签名前检查最终按真实链上状态拒绝为 `insufficient token allowance`，全过程未读取
私钥且未广播。按操作员后续明确选择，`mainnet-approve-usdg` 固定授权 relationship 当前 USDG
总预算的200倍；relationship 78 对应 `2000000000` raw（2000 USDG）。它仍禁止无限授权、已有部分
授权和存在 pending nonce 的钱包；软件单笔2U、净累计10U限制不变。该路径尚未在主网执行。

confirmed live receipt 现会重新获取规范块，按 follower 的 ERC-20 Transfer 净差额严格核对实际
输入/输出，并复用归因 lot/PnL 账本结算；失败、revert 或非规范链不结算。169 项单测、pip check、
compileall、13 笔/27 信号 replay 与 diff check 通过。由于项目规则要求完成风险清单后才可使用真实
密钥和广播，本记录不是实盘授权；relationship `78` 仍为 disabled。

最终另跑 60 秒当前时点只读监听：feed 全程 healthy，connections=1、frames=1458、decoded=4503、
backfill_blocks=582；reconnection/frame/RPC/backfill/worker/reorg error 均为 0，最终队列和所有
candidate 状态为 0，游标推进至 61598481。该窗口没有目标钱包候选，因此只证明当前 feed、RPC、
游标和队列健康，不证明交易闭环；闭环证据仍以此前固定历史区间实际产生的 2 个 candidate、2 个
receipt、1 个 Relay BUY 自动关联及1个负例为准。可复核运行账本/日志为
`/tmp/mainnet-test-relay-backfill.sqlite3`、`/tmp/mainnet-test-relay-backfill.health.log`、
`/tmp/mainnet-test-relay-backfill.signals.jsonl`；最终13笔回放为
`/tmp/mainnet-mvp-final-replay-13.jsonl`。

## 人工看守 mainnet_live 测试启动

使用者随后明确将本轮切换为人工看守的小额主网测试，并确认 relationship 78、私钥库读取、真实
签名/广播、2 USDG 单笔和10 USDG净累计风险；另明确选择 USDG allowance 为预算上限的200倍。
代码固定倍率为200，不使用 uint256 无限授权；专项及全量回归仍为169/169通过。

启动前反查 relationship 78 的 follower/smart wallet、`mainnet_live/evidenced`、额度与 snapshot
全部一致；latest/pending nonce 均为0，钱包余额为 `4944071749814556` wei、`19792295` raw USDG，
原 allowance 为0。真实授权交易
`0xd7efd877042113a2125fc3f2e974ab6bdcf6254a81d5f04dcc08302c26990f6e` 已在区块61606628成功，
规范块 hash 核对通过，gasUsed=57976、effectiveGasPrice=92752000 wei，最终 allowance 为
`2000000000` raw（2000 USDG）。日志及账本不保存私钥或 raw signed transaction。

一小时 live monitor 已以周期 `mainnet-live-78-20260913-a` 启动，配置为 MySQL 关系/MySQL账本、
`--enable-mainnet-live --relay-auto-associate`；运行日志为
`var/mainnet_live_78_20260913.log`。启动健康检查 feed connected、队列0、错误0。此时尚未发生新的
目标钱包候选或跟单交易；授权成功本身不等于跟单闭环通过。

## 第一笔真实 BUY 闭环

smart wallet 在 Fomo 买入后，monitor 捕获目标交易
`0x385a3c52ed7fbd4976f10cf73d21b625a3d9cb751108f2b5c0956fe53fb3b26b`：先保持
`EXTERNAL_DELIVERY_CANDIDATE/needs_review`，Relay API 暂未返回时持久重试2次；随后唯一关联
order `0xadc36ef07aedbbcf9095fcfcd6b43afcabb7656b6b3f32c75c605f9c3d48bccf`，证明源链
Solana USDC 输入 `3000000` raw、目标钱包收到
`69405773920665976786` raw token。目标 receipt 又唯一验证 V3 fee=3000 池
`0x5d37b1d887b502594414a82d2cf7d4ef774a8027`，信号升级为 `relay_buy_evidenced`。

relationship 78 接受固定2 USDG proposal，完成重新报价、余额/allowance/Gas/nonce、配置快照、
签名发送者与广播 hash 复核。真实跟单交易
`0xdc5fda26b7d1a5646f258faac6af481e76018b93a081c48ea3f51fc53f751d78` 在区块61608440成功并位于
规范块；实际支出 `2000000` raw USDG，收到 `50303912913447597330` raw token，Gas 为
`14908762260000` wei。

首次自动结算暴露状态模型错误：tracker 将 confirmed 写入 execution attempt，而 plan 按设计保持
signed；结算器错误要求 plan=confirmed，因此报 `live_settlement_error/ValueError`。立即停止
monitor 并禁用 relationship 78 后，修正为要求 signed plan 加同 tx hash 的唯一 confirmed attempt，
同时核对 attempt 与 receipt block number/hash。专项测试及169项全量回归通过；用同一规范回执
幂等补结算成功。MySQL 最终 proposal=filled、budget reserved=0、invested=`2000000`、open lot=
`50303912913447597330` token，完整保存 follower、smart wallet、relationship、source event/tx、
strategy version 和 snapshot 归因；execution audit healthy=true、issues=[]、
end_to_end_evidenced=true。链上复查 follower 为 `17792295` raw USDG、同 token
`50303912913447597330` raw、USDG allowance=`1998000000`、latest/pending nonce均为2。

诊断完成后 relationship 78 继续保持 disabled，monitor 已停止；在跟卖授权与金额语义确认前不会
自动恢复实盘。

## 跟卖自动授权修复（恢复前）

操作员明确要求：relationship 已启用时，聪明钱卖出应自动触发跟卖，不应逐笔等待人工确认。
实现已改为先按聪明钱本次卖出占其已归因来源持仓的比例，映射到 follower 自己的归因 lot；不得
直接复制两边不同成交价下的 Token raw 数量。本次真实 BUY 的来源数量为
`69405773920665976786`，follower lot 为 `50303912913447597330`，只读演算全卖结果精确为
`50303912913447597330`。

SELL 使用 ERC-20 且 allowance 不足时，monitor 自动执行受 relationship/snapshot/live gates
约束的 bounded approve。上限是该 relationship ledger scope 的当前同 Token open lot 总量，
不包含钱包里无归因余额，也不是无限授权。approve 先做合约代码、pending nonce、Gas、ETH 余额和
`eth_call` 模拟检查；广播后必须取得成功的规范 receipt 并复查最终 allowance，随后才重新报价、
签名和发送 SELL。首笔 SELL 可能因此多一笔授权确认延迟。

修复后 `pip check` 通过；171/171 unittest 通过。测试期间临时移开故障后建立的
`var/EXECUTION_STOP`，shell trap 在退出时恢复，生产侧全程保持停机。恢复前只读复查：MySQL cycle
仍为 `mainnet-live-78-20260913-a`，budget limit/invested/reserved/available 分别为
`10000000/2000000/0/8000000`；open lot 与链上 follower 余额均为
`50303912913447597330`，smart wallet 链上余额仍为 `69405773920665976786`，目标 Token 的 V3
allowance 为0。relationship 78 此时仍 disabled，尚未广播 Token approve 或 SELL。

随后在确认无 reserved proposal 后恢复 relationship 78；snapshot 仍为
`aa11cd2486b97f3064cd5f7c744f519f3dc6c079d4dbc4e909e25fe488c2f647`。主网 monitor 以 PID
139612、`--paper-cycle-action reuse --relay-auto-associate` 运行，日志为
`var/mainnet_live_78_20260913_sell.log`。最新健康记录 feed healthy、queued=0、live_errors=0，尚无
新候选、approve 或 SELL 广播。`var/EXECUTION_STOP` 已改名为
`var/EXECUTION_STOP.paused-after-buy-20260913` 留档；如需紧急停机，应立即恢复为原文件名并将
relationship 78 设为 disabled。

## 第一笔真实 SELL 闭环

smart wallet 随后通过 0x + Relay 卖出，源交易为
`0xcd205e6f70058ed1eb8de85b5f5dc89a82db6cd2311462bc300db8b57e025827`，区块61622277；严格证据
确认 smart wallet 卖出 `69405773920665976786` raw Token，并收到 `2792655` raw USDG。第一次
决策安全拒绝为 `quote_unavailable`：SELL receipt 没有可用于本地执行的 V3 Swap 日志，而动态
路由发现当时只检查当前 receipt，不能把 0x/Relay 源 calldata 当作 follower 的执行路径。

修复后，SELL 先按 smart wallet 的来源持仓比例映射 follower 的归因 lot，再从该 lot 对应的原始
BUY signal 恢复已验证的本地执行路径。本例恢复 V3 fee=3000、pool
`0x5d37b1d887b502594414a82d2cf7d4ef774a8027`，并反向报价 Token→USDG；如果涉及的 lot 没有路径、
路径不一致或不是 V2/V3/V4，仍保持拒绝。`allowed_protocols` 仅是准入集合，不表示运行时扫描所有
协议和池子，也不会把 0x/Relay 来源自动变成可复用执行 calldata。

重试后 proposal `212a8a5eef826acc37ca48e25d4a5ac34a9e8d3f65df3d0e5d5bcf7598d01a23`
被接受。系统按 follower 的归因持仓精确授权 `50303912913447597330` raw Token；approve tx
`0x627ed57065f48da545093e1ef20ab5578e327dbbe16bec63ba5a845c1ac1db8c` 在规范区块61626855成功。
真实跟卖 tx `0xbea680b6a15bc2632531bea29aa89e97a91a770e4d994d9defeb3a77d5c38120` 在规范区块61626875成功，
实际卖出 follower 全部 `50303912913447597330` raw Token，收到 `1993796` raw USDG，SELL Gas
为 `12143022084000` wei。fill id 为
`5b7aa16926ca7b84283b504f63ed213c5c45d7cf10532edb9e7468b946249ac6`。

MySQL 结算后 open lot=0，budget invested/reserved/available 为 `0/0/10000000`；本轮按本金
`2000000` raw USDG 计算的 realized PnL 为 `-6204` raw USDG（即 -0.006204 USDG，不含 Gas）。
只读链上复核 follower 的 USDG/目标 Token/V3 Token allowance 分别为
`19786091/0/0` raw，ETH 余额 `4907112447998556` wei，latest/pending nonce 均为4。
日志健康记录显示 proposal、approve、broadcast、confirmed、settled 各1，live_errors=0、queue=0、
candidate pending/retry/failed 均为0。relationship 78 已重新禁用，`var/EXECUTION_STOP` 已恢复，
测试 monitor 已退出。本轮证据日志为 `var/mainnet_live_78_20260913_sell_retry.log`。

## Allowance 最低需求与授权目标分离

第二轮测试前修正了自动授权阈值。此前 USDG 每次买入都会将 allowance 从低于“预算×200”的状态
重新补满，即使剩余 allowance 已足够覆盖本次交易；这不会直接发送必然失败的 Swap，但会造成不必要
的 approve、Gas 和延迟。现在授权接口分别接收 `minimum_required_raw` 与有界 `amount_raw`：现有
allowance 达到本次交易输入量就直接执行，只有不足时才授权到目标上限。

USDG 的授权目标仍为 relationship USDG 预算×200；meme Token 的授权目标仍为该 relationship
当前归因 open position 总量。两者均只用本次 proposal 输入量作为最低需求。专项测试覆盖“USDG
allowance 已消费但足够本次买入不补授权”和“Token allowance 小于总持仓但足够本次部分卖出不补
授权”；非法的最低需求大于授权目标保持拒绝。全量回归为172/172，`pip check` 与 `git diff --check`
通过。验证期间仅临时移开急停文件并由 shell trap 恢复；relationship 78 保持 disabled，未启动
monitor 或广播交易。

## 第二轮 mainnet_live 启动

第二轮启动前，执行账本审计 `healthy=true/issues=[]`，无 signed/pending attempt 或 active
reservation；USDG budget limit/reserved/invested/available 为 `10000000/0/0/10000000`。只读链上
复核 follower 的 USDG balance/allowance、ETH balance、latest/pending nonce 分别为
`19786091/1998000000/4907112447998556/4/4`。密钥库只查询 wallet address 与 enabled 元数据，未
查询私钥列。relationship snapshot 仍为
`aa11cd2486b97f3064cd5f7c744f519f3dc6c079d4dbc4e909e25fe488c2f647`，与0600权限风险文件一致。

初次后台检查发现上一轮及一次新启动的 screen monitor 在受限 shell 中被误报为 Dead，系统层实际
仍存在两个进程，并发 scanner 触发 `canonical block rewind requires explicit reorg handling`。
relationship 当时已禁用且急停存在，没有 proposal、签名或广播。已终止四个精确 PID，系统层确认
无残留；Feed 短暂 HTTP 429 在清除重复连接后恢复，单连接探针成功收到帧。

随后仅启动一个 monitor：screen `smart_money_mainnet_live_78_round2`，monitor PID `208123`，日志
`var/mainnet_live_78_20260913_round2.log`。新实例日志从最后一个 `monitor_started` 起计算：Feed
connected，连续 health healthy，connections=1、reconnections/frame/worker/live errors=0、queue=0，
候选 pending/retry/failed=0；独立 RPC 游标单实例继续补历史缺口。relationship 78 已启用，急停文件
暂存为 `var/EXECUTION_STOP.round2-20260913`。本状态仅表示可以开始人工看守的小额第二轮，不保证
路由命中、成交、收益或完整覆盖。

## 第二轮 Relay 被动买入路由修复与实盘闭环

smart wallet 在 Fomo 下单后，目标链交易为
`0x6e77122df63ce50d36c1a861bdf45010213f446507ae3cee89c36b9214bb3067`。本地 receipt 确认 wallet 仅收到
`12457560493773349` raw Token
`0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec`，receipt 本身没有 Swap event，因此初始解码正确保持
`INCOMING_TRANSFER/needs_review`，没有仅凭收币发起跟单。Relay 公共订单严格匹配出唯一 order
`0xa343f7410dc670e4fa578c831bfd3868c510156ee5c2cf15a186574b7208b7e3`，源为 Solana USDC
`3000000` raw，目标 recipient、目标 tx、token 和 amount 均与本地 receipt 一致，因而可提升为
`BUY/relay_buy_evidenced`。

首次 monitor 仍要求从该被动入账 receipt 的 Swap log 恢复本地路径，导致严格归因成功但没有 proposal。
已先禁用 relationship、恢复急停并终止 monitor，再修正为：被动 receipt 没有可用 Swap 时，由
`LiveQuoter` 仅查询配置的 V3 Factory 四个标准 fee tier `100/500/3000/10000`；在同一固定区块核对
pool code、token0、token1 和 fee，并以该 relationship 的实际计划输入量比较直接池报价。它不是任意
多跳/多协议寻路，现有报价年龄、源价格偏差、price impact、slippage、Gas、余额、allowance、nonce
与配置快照门禁仍全部执行。专项和全量回归均通过，最终为174/174；`pip check`、`git diff --check`
通过。真实只读探针在区块61649276、2 USDG 输入下选择 fee=500 的验证池
`0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3`，报价输出
`9160999567144901` raw Token。

确认账本 `healthy=true/issues=[]`、预算完整、无既有 proposal 后，只将上述已完成候选重置为 pending，
并启动唯一 monitor `smart_money_mainnet_live_78_round2_retry`，证据日志为
`var/mainnet_live_78_20260913_round2_retry.log`。重放后 Relay 归因、主动受限 V3 路由发现、2 USDG
proposal 和全部签名前检查通过；已有 USDG allowance 足够本次输入，没有重复 approve。follower BUY
交易为 `0x3ba0aefd147cd67d1ef5c42fe7080311da809c0614ad9fe4e248c5d53812d29b`，在区块61649821
确认，实际支付 `2000000` raw USDG、收到 `9161024053399471` raw Token，Gas 为
`15116542004000` wei。

smart wallet 随后卖出，源交易为
`0xf9f3f04034246f6baafb51aa30e3ee2e171e482df653dc8b3f508583d7d70790`；receipt 严格闭合 Token debit、
V3 fee=3000 pool Swap 和 Relay USDG deposit，形成 `relay_sell_evidenced`。系统按来源卖出比例映射到
该 relationship 的唯一归因 lot，proposal 输入为 follower 全部 `9161024053399471` raw Token。
Token approve `0x2dee0fb952b9e6fdb22f482998bb947fdd984ce07e848fac37e875f608631c7d`
仅授权该归因持仓，在区块61650351成功，Gas `5773530462000` wei。follower SELL
`0x71529d0ac8a5e2d4a2bec56d9fbb762351449ce07ca67131c95a99808d484fb7`
在区块61650372确认，实际卖出全部归因 Token，收到 `1998190` raw USDG，Gas
`12888172656000` wei。

最终两个 proposal/fill 均为 filled，lot 为 closed 且 principal/token remaining 都为0；USDG budget
limit/reserved/invested/available 为 `10000000/0/0/10000000`。本轮 realized PnL 为 `-1810` raw
USDG（-0.001810 USDG，不含 ETH Gas）。链上 follower 的 USDG/Token 余额为
`19784281/0` raw，USDG/Token 对 V3 Router allowance 为 `1996000000/0` raw，ETH 余额
`4873334202876556` wei，latest/pending nonce 均为7。执行审计共4个 plan/attempt，全部 confirmed，
`healthy=true/issues=[]`；两个源候选 complete，pending/retry/failed=0，日志最后 health 的 queue、
worker/frame/reconnection/live errors 均为0。日志中的两次 `paper_rejected` 都来自不执行交易的
shadow：feed intent 因动态目标尚未证实而 `asset_not_allowed`，receipt_success 在 evidenced 主路径
已预留全部持仓后因 `attributed_position_insufficient` 拒绝；两者均未生成 proposal 或广播，不是 live
执行失败。结束后 relationship 78 已禁用、`var/EXECUTION_STOP` 已
恢复、monitor 已退出。代码补充了主动路线验证健康计数，下一次同类事件会记录
`local_v3_route_discovered`；本次既有日志中的 counter=0 是修复前的观测缺口，不表示路线未验证。
所有 signal 继续保持 `copy_eligible=false`，本次结果不代表盈利或无人值守上线验收。

## 第三轮常驻测试：Kyber 来源跟卖

在提交并推送 `00c1536` 后，常驻 monitor 捕获源 BUY
`0xfc8877462d4ff5f92b39e511c15d12ab36c79fc9abc2300283f86286bdbad517`。Relay 唯一订单关联完成后，
系统按实际2 USDG输入发现并验证 V3 fee=10000 直接池
`0xe547c18f46db55ab788343bcc503f9cf0bd7d564`；follower BUY
`0x9712426141fd285fc2dfd62a47b43d86fa3deb794b46c2e40f2e292885a17d94`
支付 `2000000` raw USDG，收到 `6806140178764240342` raw Token
`0x2e8c31162b855a2ffa90f6f8634643ad6f111e18`。BUY 后预算 invested/available 为
`2000000/8000000` raw USDG。

smart wallet 随后的源 SELL
`0xcf1d3ed1d46d674f809c9eed02ae37b33439650054322effc4c9b4ea458a2aab`
严格闭合了 wallet Token debit `9547457307005586297` raw 与 Relay USDG deposit
`2722464` raw，但来源聚合器被识别为 `kyber`。relationship 78 当时的 `allowed_protocols` 遗漏
Kyber，主执行路径以 `protocol_not_allowed` 拒绝，没有 proposal、签名或广播；follower lot 保持
完整。发现后先禁用 relationship、恢复急停并停止 monitor。

全局协议解析本来已支持 Kyber，本次只把 relationship 78 的准入集合改为
`[v2,v3,v4,0x,kyber,relay_solver]`。同时修正重放幂等键：decision、proposal 和账本 source key
都绑定 `config_snapshot_hash`，因此同一源事件在配置变更后可形成新决策，又不会覆盖旧的安全拒绝
证据。新 snapshot 为
`f51aa692e2535866c6c6eae8b448a479b556e88ccf2800f223e220765a22ed58`，仓库外0600风险文件同步绑定；
修复提交 `24faa62` 已推送。`pip check` 通过；停机文件存在时全量测试有7项按设计被急停拦截，随后在
relationship disabled 状态下由 shell trap 临时挪开并恢复急停，最终175/175通过。

重放前执行审计 `healthy=true/issues=[]`，链上 Token余额/allowance、latest/pending nonce 为
`6806140178764240342/0/8/8`，与唯一 open lot 完全一致。第一次启动因遗漏专用
`data/mainnet_test_watchlist.csv` 立即退出，急停自动恢复，审计确认未产生 plan、签名或广播。第二次
使用正确 watchlist 后接受 proposal
`99742296cfb0b06900966c3bbfefd7d690f1d5e00a4ccd409c234b2041ed1299`，从该 lot 恢复并反转此前验证的
V3 fee=10000路径，没有复用 Kyber 源 calldata。

Token 有界 approve
`0x3baa61de842a5a65d391f57557ae6c62def0962b7a9ff964389de77fe9a6a611`
授权精确归因持仓 `6806140178764240342` raw，在规范区块61660652成功；gasUsed=48653、
effectiveGasPrice=`89492000` wei。follower SELL
`0x4bc67f6160424387e5d2c6d03a6102ce69abf7853c6e3113f1127c2a250a4ea8`
在规范区块61660679成功，实际卖出全部归因 Token，收到 `1918286` raw USDG；gasUsed=129106、
effectiveGasPrice=`90424000` wei，Gas=`11674280944000` wei。fill
`ef17ca913ae03b6f016c04e8bd94be542251bcbee1cb9c004a14fe4b9ee33b00`
已结算，lot closed，budget reserved/invested/available 为 `0/0/10000000`；realized PnL 为
`-81714` raw USDG（-0.081714 USDG，不含 approve/SELL 的 ETH Gas）。

链上最终复核 follower 的 USDG/Token余额为 `19702567/0` raw，USDG/Token V3 allowance 为
`1994000000/0` raw，ETH余额 `4842894815770556` wei，latest/pending nonce 均为10。执行审计共6个
plan/attempt，全部 confirmed，`healthy=true/issues=[]`；候选 complete，队列与 pending/retry/failed
均为0。monitor 继续运行于 screen `smart_money_mainnet_live_78_kyber_retry2`，证据日志为
`var/mainnet_live_78_20260913_kyber_retry2.log`；relationship 78 保持 enabled，急停文件暂存为
`var/EXECUTION_STOP.continuous-kyber-20260913`。这是使用者要求的人工看守常驻状态，不代表无人值守
上线验收或盈利保证。

## 第四轮常驻测试：连续 Relay BUY/SELL

常驻 monitor 捕获源 BUY
`0xf1b298eedcb00ba7bf82261ecea23e9ad8669f915025c6627808589d29693306`。Relay 公共订单两次暂未就绪后
经持久候选重试成功唯一关联，源目标入账为 `53440441097412968275` raw Token
`0x385f4f8ae47651ce5f58f5265395a669f8281e18`。系统按跟单实际输入验证 V3 fee=3000 池
`0x5d37b1d887b502594414a82d2cf7d4ef774a8027`，follower BUY
`0xf1db6b9611e2b6d2843449b9d0b1d6ea9e93817a7901732c0d0f373d95dc81a6`
在规范区块61663171成功；实际支付 `2000000` raw USDG，收到
`38533793488645720533` raw Token，Gas=`13899377920000` wei。candidate attempts=3后 complete，
proposal/fill 均已结算，唯一 open lot 与链上余额精确一致；预算 invested/available 为
`2000000/8000000` raw USDG，执行审计7/7 confirmed且无 issue。

源 SELL
`0x57d0a6cf52b70ddcc08ddb35d6fa5747adffa0d31d96bc4d003fd8ab462b66fc`
由 Kyber + Relay 完成，严格证据记录 smart wallet Token debit `53440441097412968275` raw 与 USDG
deposit `2787250` raw。系统按来源全卖比例映射 follower 的唯一归因 lot，并反转该 lot 保存的 V3
fee=3000路径。Token有界 approve
`0x5ac0c870c3ba982f0cb2adae9256e81887fbe9bea6f18356f5f356f127d085e3`
在规范区块61664128成功，授权精确归因数量 `38533793488645720533` raw；follower SELL
`0x05ef728f046f4269ba534eb107a2e62af664e3cf530994617b48c5efa8e30c7f`
在规范区块61664148成功，实际卖出全部归因 Token并收到 `1988313` raw USDG，SELL
Gas=`11936486600000` wei。

结算后 lot closed，principal/token remaining均为0，预算 reserved/invested/available为
`0/0/10000000` raw USDG，realized PnL=`-11687` raw USDG（-0.011687 USDG，不含 BUY、approve和
SELL的ETH Gas）。链上复核 USDG/Token余额为 `19690880/0` raw，Token allowance为0，ETH余额
`4812658077970556` wei，latest/pending nonce均为13。执行审计8/8 attempt全部confirmed，
`healthy=true/issues=[]`；候选complete，queue与pending/retry/failed均为0。原 screen、relationship
和急停暂存状态不变，monitor继续常驻。

## 第五轮常驻测试：第二组连续 Relay BUY/SELL

源 BUY `0x5bdf5097e6d54abf7d7533ee6a6aa2f3a0d005ff6d088b308f19330eb2d03163`
经两次 RelayNotReady 后自动重试并唯一关联；目标 Token 为
`0x39dbed3a2bd333467115de45665cc57f813c4571`。系统验证 V3 fee=3000 池
`0x0652d61511f3a96b8721be6825680f5954d9baf3`，follower BUY
`0x867e5806e98d470a905b67509060f009e9cc4071892e26a5ed2434fad0bd2b0c`
在规范区块61665773成功，实际支付 `2000000` raw USDG、收到
`3396809340185972048` raw Token，Gas=`13508420220000` wei。candidate attempts=3后complete，
open lot、链上余额与预算 invested=`2000000` raw精确一致。

源 SELL `0xb1c690fffde9ff31d79b69acb326bdfd6b45d5352e5ae8029b3c6ee9e9ead324`
由 Kyber + Relay 完成并严格闭合 Token debit与USDG deposit。系统按全卖比例映射 follower lot，
从 BUY lot反转已验证的 V3 fee=3000路径。Token approve
`0x902a911cb0789f6854a56b35e61d064c30ca7966a5009e53937690c6601a056f`
在规范区块61666802成功；follower SELL
`0x61051d5f1bd056db1018237c711bf81c725c208b07bf5ae513e079225934afc5`
在规范区块61666829成功，卖出全部 `3396809340185972048` raw Token并收到
`1988018` raw USDG，SELL Gas=`10873767584000` wei。

结算后 realized PnL=`-11982` raw USDG（-0.011982 USDG，不含 Gas），lot closed，预算
reserved/invested/available=`0/0/10000000` raw。链上 USDG/Token余额为 `19678898/0` raw，Token
allowance为0，ETH余额 `4784170529528556` wei，latest/pending nonce均为16。执行审计10/10 attempt
全部confirmed，`healthy=true/issues=[]`；monitor、relationship和急停暂存状态保持不变。
