# Arc 接入交接（面向后续模型，2026-09-17）

本文是 Arc（Circle L1，chain **5042**）跟单接入的短版交接，可独立阅读。较早的文档保留了历史过程，
其中的 PID、测试数、地址与「尚未支持」的结论**可能已经过时**；冲突时以当前代码、只读账本审计和
最新运行日志为准。

快照时间：**2026-09-17 SGT（UTC+8）**。接手后必须先跑第 2 节的检查。

## 1. 一页结论

- 仓库 `/Users/claw/smart_money_copytrader`，分支 `docs/copy-trade-lifecycle`。
- Robinhood（chain 4663）实盘**正在运行且健康**：PID `26036`，命令
  `.venv/bin/sm-copy run --early-trial-id feed-early-20260917 --backfill-interval 15`，
  日志 `var/log/sm-copy-early-20260917-restart2.log`。
- **Arc 目前没有任何进程在跑**，也**没有任何 Arc 实盘能力**：`execution_prep` 和 `broadcast`
  仍然硬拒 `chain_id != 4663`，这是**有意的 fail-closed 边界**，不要顺手拆掉。
- 旧的 `sm-copy arc-monitor` 已**按用户决定退役**，不要重启它（原因见第 5 节）。
- 全量测试 **581 通过**（12 skipped）。
- 未完成的收尾工作在第 6 节，按顺序做。

## 2. 接手后先做的只读检查

```bash
cd /Users/claw/smart_money_copytrader
pgrep -fl '.venv/bin/sm-copy'                      # 应只有一个 run 进程
.venv/bin/sm-copy execution-audit --ledger-mysql   # 期望 signed=0, issues=[]
ls var/EXECUTION_STOP                              # 不存在才是正常
tail -1 var/log/sm-copy-early-20260917-restart2.log | python3 -m json.tool | head -20
.venv/bin/python -m unittest discover -s tests
```

**单实例规则**：任何时候只允许一个 Feed/跟单进程。停机流程是「touch 急停文件 → 轮询
`execution-audit` 直到 `signed == 0` → 再 SIGTERM」。固定等几秒就 kill 会遗留已签名 nonce
（本会话发生过两次，都需要人工和解）。macOS **没有 `setsid`**，detach 用
`subprocess.Popen(..., start_new_session=True)`。

## 3. Arc 已完成的工作（本会话提交）

| commit | 内容 |
|---|---|
| `99fb733` | `arc_pool_safety.py`：round-trip（买入 + 卖回）模拟安全门，税率/损耗阈值可配，只读 |
| `7133658` | 报价场馆按 `signal.chain_id` 解析；缺场馆**报错而非借用另一条链的地址** |
| `d188381` | 预算桶与执行路由按链隔离；新增 USDC 桶；跨链资产返回 None（fail-closed） |
| `13e2952` | Arc 的 0x / Relay 实测地址登记、0x 按链传参、原生双刻度声明、Relay 归属按链解析 |
| 本次提交 | Arc 摄取触发改为**按钱包过滤的 Transfer 订阅** |

## 4. Arc 方案（记录版，按此推进）

聪明钱范围：**v3 + v4**。内盘（FOMO bonding curve）**明确暂缓**。

```
1. WSS 两条订阅，服务端按 watchlist 过滤 Transfer 的 indexed topic：
      topics: [TRANSFER, [wallets], null]   # 钱包转出
      topics: [TRANSFER, null, [wallets]]   # 钱包转入
2. 命中后取【那一笔】交易 + receipt，并用 canonical 区块哈希复核（WSS 只是提示）
3. 算【该钱包自己的】ERC-20 净变动
4. 归属判定：
     有进有出                  → BUY / SELL（receipt 里的 swap 事件做零成本佐证）
     只有进，来源是 Relay 合约 → 查 Relay 订单，用源链付款归因（见 4.1）
     只有进，存疑（疑似原生付款）→ debug_traceTransaction 裁决
     只有进，确认无流出        → 收币，不跟
5. 【不验它的池子】——我们走 0x，它选路由，聪明钱的池子约束不了我们的成交
6. 向 0x 要正向 + 反向（卖出）报价，报不出 → 拒（这是"能不能卖出去"的真正检验）
7. 跟单决策（金额/预算/偏离）→ 下单
```

**为什么按钱包而不是按场馆订阅**（实测 1500 个 Arc 区块）：v4 全量 11288 条、v3 全量 2464 条，
而按钱包过滤的 Transfer 只有 **18 条**（0.13s）——约 **764 倍**差距。而且它一次覆盖 v2/v3/v4
（v3 在 Arc 上有**至少两个不同 fork**，没有单例合约可按地址过滤），也能看见 Relay 的跨链交付。

### 4.1 Relay 归属（**不要省略**）

Robinhood 当前的信号构成是 `relay_buy_evidenced 25 / relay_sell_evidenced 6 / swap_evidenced **0**`
——**实盘跟到的单几乎全是 Relay 的,不是 DEX swap**。Arc 上同样成立：

- `relay_proxy` / `relay_router` 在 Arc 上**字节码与 Robinhood 逐字节相同**（已登记进 registry）；
- Relay 官方 API `/chains` 列出 chain 5042，`depositEnabled: true`；
- 用真实 Arc 交易哈希调 `lookup_by_destination_hash`，**3/3 返回 `status: success`**，
  且 `user` 是 **Solana** 地址、`recipient` 是 Arc 钱包——**在别的链出钱、Arc 收币**。

这种交易在 Arc 上**只有进、没有出**，如果只用净变动规则会被判成收币而漏掉，trace 也救不了
（Arc 上确实没有流出）。`solver.py::relay_passive_buy` 已经改成按链解析资金资产，
**但"只有进且来自 Relay 合约 → 去查订单"这条分支还没接进 Arc 的摄取流程**（第 6 节第 2 项）。

`relay_delivery_evidence` / `relay_confirmed_sell` 描述的是**我们自己**往 Relay 存款的流程，
只有 Robinhood 有经过核实的 depository，它们现在**显式按 chain_id 拒绝**其他链。
Arc 的 `depository` 字段**故意留空**：那个地址在 Arc 上是另一个合约（长度相同、哈希不同）。

## 5. 必须知道的坑

1. **同址不同合约（CREATE2）**。RH 与 Arc 的 v4 Quoter 是**同一个地址**；但 RH 的 v3
   factory/quoter/router 在 Arc 上**有代码却是完全不同的合约**，`depository` 也是。
   **任何跨链复用地址之前必须比 `keccak(eth_getCode)`**，"地址上有代码"不作数。
   `quotes.py::_venue()` 就是为此存在：缺场馆时报错，绝不借用。
2. **10¹² 刻度坑（Arc 专属，上 paper 前必须修）**。Arc 原生 USDC 是 **18 位**，内置 ERC-20
   `0x3600…0000` 是 **6 位**，**同一份余额**（已链上验证：某钱包 `eth_getBalance`
   752486736963102522762 而 `balanceOf` 752486736）。`budget_bucket` 把两种形态都归到 USDC 桶，
   而代码里**没有任何原生↔ERC-20 的换算**（`receipts.py:196-205` 直接 `!=` 比较，在 RH 上正确，
   因为 ETH/WETH 都是 18 位）。registry 已声明 `native_decimals` / `native_erc20_decimals`
   和 `native_to_erc20_divisor`（Arc = 10¹²），**但还没有任何调用点使用它**。
3. **原生付款不产生日志**。用 `msg.value` 付的 USDC 在日志里看不见，只能靠 prestateTracer
   状态差。Arc 的 RPC **支持** prestateTracer（实测 194–334 ms、49–97 KB，约为 receipt 的 10 倍）。
   只在"有进无出"的存疑件上调用，**不要放宽到每笔**。gas 已被正确扣除
   （`native_flows.py`：`asset_delta = delta + gasUsed * effectiveGasPrice`）。
4. **arc-monitor 的 backfill 死锁（已从结构上消除，但要理解）**。旧实现用固定 500 块批次扫
   v4 全量 Swap 日志，**500 块的响应超过连接池 16MB 上限**（实测：500 块超限、200 块 9005 条）。
   一旦游标落后 ≥500 块，每批必然超限 → 连续 30 次失败 → 退出 → 重启后游标不变 → **永久卡死**。
   改成按钱包过滤后响应只有几条，这个失效模式不再存在。
5. **进程不会自动重启**。本会话两个进程都被一次基础设施抖动打挂且无人拉起。要无人值守需要
   launchd/supervisor，目前没有。
6. **账本里有 2 笔遗留 reserved 提案**（`c7917e00…`、`dc9835ec…`，USDG 桶，崩溃前创建），
   重启时被标为 `live_recovery_requires_operator_review`，即 health 里的 `live_errors: 2`。
   **不阻塞跟单**（之后正常成交了 10+ 笔），但**占着预算额度**。用户已明确表示**先放着**。
7. **arc-observer 写入的 chain_id 仍是 4663**（迁移 011 的默认值）。它自己的 SQLite 里无害，
   但 **Arc 往共享账本写任何一行之前必须修**，并且要先上迁移 `012_arc_chain_keys.sql`
   （PK 收紧 + bucket 加宽，需要维护窗口，**未应用**）。

## 6. 下一步（按顺序）

1. **收尾本次的观测改造**：`arc_observer.py` 的触发已改完并测试通过，但
   `cli.py` 的 `arc-monitor` 子命令仍按旧语义描述（只打 `arc_signal_observed` 日志）。
   如果要重新常驻观测，需要重新评估它的定位。
2. **接 Relay 归属分支**：在 Arc 摄取流程里加「只有进 + 来源是 Relay 合约 → 调
   `lookup_by_destination_hash` → `relay_passive_buy`」，参考 `cli.py` 中 RH 的
   `relay_lookup_pending` / `relay_buy_associated` 阶段。
3. **修 10¹² 刻度坑**：在信号入口把原生腿归一到 6 位刻度，或让所有比较带刻度。
4. **接 0x 正向 + 反向报价门**（替代在聪明钱池子上做 round-trip）。
5. 迁移 012 + Arc 的 relationship / USDC 预算（**需要用户授权，涉及线上库**）。
6. paper 先跑。
7. 之后才谈实盘（需要用户明确授权，并且要动 `execution_prep` / `broadcast` 的链门）。

## 7. 用户的长期约束（必须遵守）

- 普通 Transfer、赠送、空投或单纯收币**不能**认定为聪明钱买入。
- receipt 中出现 Swap，**不代表** bundled transaction 中每个钱包都执行了 Swap。
- 未知路径保持 unknown，**不得**为提高覆盖率放宽归属条件。
- SELL 只能使用同一关系中**可归因的持仓 lot**，不能因为链上有余额就直接出售。
- 不输出 `.env`、API Key、私钥、签名原文、raw transaction 或带凭据的 RPC URL。
- 不启动第二个 Feed/跟单进程；不自动重试签名或广播结果不确定的交易。
- 不覆盖或提交当前未跟踪的图片及 Relay 探针文件
  （`docs/feedback/*.png`、`scripts/validate_relay_prefetch.py`、`tests/test_relay_prefetch_probe.py`）。
- 金额继续使用整数，并序列化为十进制字符串。
- **先调查并给出证据和根因；只有用户明确要求实施后才修改代码。**
- 编辑后必须运行：`.venv/bin/python -m unittest discover -s tests -v`、
  `python -m compileall -q src tests`、`pip check`、`git diff --check`。
- **未经用户明确授权，不得修改在线配置、trial、预算、授权额度、急停状态，不得重启或执行实盘交易。**
