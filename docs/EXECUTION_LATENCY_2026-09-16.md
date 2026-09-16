# 执行阶段三项延时优化（2026-09-16）

状态：已实现，完成离线回归及公开接口只读对照。随后经用户授权，关系 1/2/3/4/5/9
已切换为 0x 首选、Kyber 备选，旧进程已停止并完成启动前核销/人工续期；急停仍保留，
等待操作员启动。**本次未读取真实私钥或广播交易**。运维记录见 [HANDOFF](HANDOFF.md)。
不改变 Feed 解析、Relay 归属证明、普通转账排除及持仓归因规则。

## 1. 报价与构建合并

支持 `execution_providers=["zeroex","kyber"]`，0x 首选、Kyber 备选。
已有 `["kyber"]` 配置不自动切换。`zeroex` 是 follower 执行 provider，来源协议的 `0x` 标识保持不变。

```text
已验证来源 → 预算/归因/防重 → 聚合器选择
  0x：完整金额 /quote（含 calldata） ∥ 小金额 /price（参考价格）
  Kyber：完整金额 routes ∥ 小金额 routes → route/build
→ 并行预检 → 模拟准确的待签交易 → 短期检查凭据 → 签名/复核/发送
```

0x `/swap/allowance-holder/quote` 返回报价及 `transaction.to/data/value/gas`，不单独 build。
保留小金额参考价格风控，故正常路径为**两次聚合器请求，不是总共一次**。
完整报价限于已进入执行决策的操作；不对未经归属验证的 Feed 候选批量预取。
缓存绑定钱包、配置、来源信号、金额和滑点，跨操作不复用完整报价。

当前支持 ERC-20 → ERC-20 的 AllowanceHolder `exec` → Settler `execute` 封装。
校验链、资产、金额、接收者、Router、spender、最低输出及 API issues；
通过官方 registry 的 current/previous Settler 校验目标，暂停或未知目标拒绝。
未知 selector、Permit2、不能验证最低输出的 ABI 不放行。
模拟按 Settler 的成功返回 ABI 解码，最低输出由已验证 calldata 强制执行，不伪造模拟输出量。
本地 deadline 限制发送时间，不宣称 0x calldata 内含链上 deadline。

0x 报价不可用/不支持，或准备阶段发生可识别的路由模拟失败、授权不足时，按配置最多切换一次 Kyber。
切换仍在同一预算/防重操作内；保护原报价最低输出及已有交易 deadline。
RPC 故障、来源失效、配置/预算/持仓问题不能通过换 provider 绕过；已有 execution plan 不切换。
签名/广播后也不自动改发第二笔。Kyber 原有受限增 Gas、排除 V4 重报价机制保留。

0x 使用既有 AllowanceHolder allowance，不自动扩容。SELL 输入 token 也需要授权；
额度不足可尝试已配置备选，备选仍不足则拒绝。本次没有增加真实授权。
Kyber 白名单 Beta 单接口本次未接入，保留原 routes/build。

官方依据：[Quote API](https://docs.0x.org/api-reference/evm-ap-is/swap/allowanceholder-getquote)、
[合约说明](https://docs.0x.org/docs/core-concepts/contracts)、
[Settler 源码与注册机制](https://github.com/0xProject/0x-settler)。

## 2. 一轮并行预检与短期复用

构建完成后并发读取 pending nonce、原生币余额、Gas 价格、输入 token 余额及 allowance。
0x 另并发验证两项 registry 字段；并发受 RPC 池上限约束。随后以实际钱包、calldata、value、Gas 模拟一次。
正常 sign/review 不再重复报价、上述 RPC 和模拟，复用进程内 `PreflightTicket`：

- 绑定 proposal、关系、钱包、配置、完整交易（含 nonce/Gas）、plan、来源信号、报价策略。
- **从该轮预检开始起最多 2 秒**，且交易 deadline 未到；不放宽 Feed/报价期限。
- 保留最新配置、预算/持仓、来源、试运行额度、急停及发送 fence，不缓存这些许可。
- 签名前过期：同一未签名 plan 最多重检/模拟一次；不刷新原报价时间，不改变交易参数。
- 签名后过期：拒绝发送，不重签改价。恢复记录不能恢复内存凭据，走保守复核。
- 取得发送连接后、写请求前，再查急停、凭据、报价及提前执行期限；凭据只能消费一次。
- 增 Gas 或重建路径必须重新预检和模拟，不复用旧交易结果。

Feed 仍为 **7 秒**，报价按现有策略 **6 秒**，不会续期。
检查后链状态仍可能变化，模拟成功不保证上链成功。发送状态不确定时保留记录供恢复核对，不隐藏重试。

## 3. HTTPS 持久连接复用

RPC、0x、Kyber 各自持有有界连接池及复用的 TLS context，广播使用独立单连接池；不新增 Feed 订阅。
保留 TLS 校验、超时、响应大小、路径及 RPC 方法限制；不跟随重定向、不自动重发请求或交易。
连接在完整读取/解析前独占；协程取消后，后台线程仍持有连接租约，不能被下一请求并用。
坏连接淘汰，退出关闭。修复并回归验证了 Python 3.10 `read1()` 读完 Content-Length 后
未释放响应、导致下一请求 `ResponseNotReady` 的实际连接复用问题。

诊断提供池等待、建连、响应、解析及 RPC 排队耗时。
执行事件增加 `preflight_ms`、各 RPC 耗时、`simulation_ms`、`signing_ms`。
`signing_ms` 包含 signer 内部许可检查及取钥边界，不是纯签名 CPU 时间。
日志不输出 API Key、端点凭据或签名原文。

## 4. 只读验证及限制

脚本 `scripts/validate_execution_latency.py` 仅调用公开报价与 allowlist RPC，不连接账本/私钥库、不签名广播。
金额限制为 100–100000 raw USDG，轮数 1–5。两家同钱包/金额/滑点，每轮报价后固定同一区块模拟，交替请求顺序。
这是当前状态验证，不是历史回放或实际跟单；脚本不执行 Kyber 增 Gas/换路重试。

```bash
.venv/bin/python scripts/validate_execution_latency.py \
  --follower 0x3004ab92565deeea0a2eaa27e40e297bb457e1a6 \
  --token 0x88ad8ddf1e3898412146a534538d418c6f8a9062 --rounds 3
```

2026-09-16 实测，STANDARD，输入 `100000` raw USDG，滑点 300 bps，单位毫秒：

| 轮次 | 0x 报价+构建 | Kyber 报价+构建 | 0x 预检/模拟 | Kyber 预检/模拟 | 结果 |
|---|---:|---:|---:|---:|---|
| 1（冷连接） | 904.052 | 1162.959 | 83.552 / 23.748 | 49.039 / 57.593 | 两家通过 |
| 2（热连接） | 372.917 | 331.831 | 66.246 / 25.258 | 33.448 / 23.576 | 两家通过 |
| 3（热连接） | 374.499 | 386.548 | 130.663 / 21.840 | 43.965 / 21.848 | 两家通过 |

区块来源（Robinhood）：

- 第 1 轮 `0x3d298f8` / `0x4f0bcc861a72e1f8796ce94e8701b0a2bb013fd084c289acb46dc390dfbe2c9c`。
- 第 2 轮 `0x3d29902` / `0x9e12e076ecf2e5d771005d09f3ec2b49196ef23267379b81674f9dcf5608eacc`。
- 第 3 轮 `0x3d2990b` / `0x5f3553bb4ffc69e7beaf8566eaf49698c8fbdf6a445f84f242dcbe98fdcfa5cf`。

另测 token `0x648cf99e79a8e799cdd096364985e7e20d261e18`：
0x 报价/构建 1114.151 ms、预检 79.122 ms、模拟 19.774 ms，通过；
Kyber 报价/构建 1207.205 ms，首次模拟 RPC code 3 回滚。
区块 `0x3d29d03` / `0xee6a5858e702d91bf880cc1b48149087de3513efcfbd11f09b640db2079394c8`。
两家输出分别 `3990610684214139828812` / `3993866128255451398143`，
最低输出分别 `3870892363687715633948` / `3874050144407787856198`，
模拟 Gas 分别 1734638 / 1135666。未执行增 Gas 重试，不能断言 Kyber 路径永远不可执行，亦不能断言 0x 不会回滚。

热连接下本批两家报价/构建约 0.33–0.39 秒，0x 并非每轮更快。
小样本不能估计稳定 P95，真实 Feed→广播→上链收益尚待部署后验证。
应按 provider、BUY/SELL、首次/重试分别统计，不能将冷启动、失败样本混成“平均提速”。

## 5. 配置、回退与回归

经操作员批准，MySQL 关系 1/2/3/4/5/9 已切换为 `["zeroex","kyber"]`，
并核对 `.env` 的 `0X_API_KEY` 是否配置、输入币授权、余额及试运行许可。
本次人工续期的证据与时间见 HANDOFF；程序不会自动延期或重置试运行。
保留 `["kyber"]` 可仅使用连接池和单轮预检优化。
`sm-copy run --legacy-preflight` 可关闭内存凭据复用、恢复独立 sign/review 网络检查，
不关闭聚合器/连接池、不放宽许可。完全回退连接池/适配器需要回退代码版本。

回归涵盖 BUY/SELL calldata、请求次数、ABI/registry 拒绝、报价回退及 RPC 故障不回退、
单轮预检、Gas/路径重试后重检、凭据过期/篡改/跨进程/消费、恢复不信任旧凭据、
发送前急停及连接取消/断连/坏响应不重试。
命令：`.venv/bin/python -m unittest discover -s tests -v`。
本次提交范围运行 484 项：472 通过，12 项因可选 EVM 依赖缺失而跳过；另通过 compileall 和 diff 空白检查。
包含原有未跟踪 Relay 采样模块的完整工作区另运行 507 项：495 通过、12 项跳过，该模块不纳入本次提交。
包含本地路由与 0x 混合配置时的操作级报价上下文回归。
完整 monitor 的 0x 准备失败→Kyber 成功组合仍缺专门集成测试；真实 SELL 对照尚未进行。
跳过项及离线测试均不计作成功实盘验证。
