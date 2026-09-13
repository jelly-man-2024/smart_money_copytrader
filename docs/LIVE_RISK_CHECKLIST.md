# 实盘风险控制清单

本文是实盘功能的硬门槛，不是无人值守上线授权。所有项目未完成并留下可复核证据前，必须保持
`copy_eligible=false`；只有人工看守的单 relationship 小额测试可在逐项门禁匹配后读取对应密钥、
签名和广播。

## 数据源与权限

- [x] `copy_relationships` 与私钥数据源分离；配置表没有私钥字段。
- [x] MySQL 监听账号只授予 `SELECT copy_relationships`；CSV 导入使用独立可写账号。
- [x] 数据库连接错误只报告异常类型，不输出 DSN、账号、密码或服务端响应。
- [x] 远程配置库和私钥库都强制显式提供 HOST/PORT/USER/PASSWORD/DATABASE/SSL_CA；缺项时在
  `pymysql.connect` 前拒绝，不会误用本机 Docker 默认凭据。
- [x] 独立私钥数据库 schema、最小权限账号和连接加密要求完成。
- [x] 私钥查询只能按预期公开地址精确命中一条，地址派生不一致立即拒绝。
- [x] 私钥数据源没有日志接口，连接/查询/格式错误只暴露异常类型或固定原因；私钥不进入
  SQLite、proposal、order 或 fill。仍需在后续执行器集成测试中持续复核。
- [x] 离线签名结果使用不可被 `vars()`/dataclass 通用序列化的 slots 容器；默认 repr 只含公开
  plan/proposal/hash/preflight，不显示字段名或 raw signed bytes，降低结构化日志误收集风险。
- [x] `key-status` 提供不读取 `private_key_hex` 的元数据连通性检查，仅返回公开地址与 found/
  enabled；默认停机、stop file、TLS 和远程显式配置门禁全部复用签名数据源边界。

## 配置与归因

- [x] 每个 `follower_wallet × smart_wallet` 只有一条关系。
- [x] CSV 导入默认禁用，必须人工核对后启用。
- [x] 启用策略在加载阶段拒绝零 smart/follower wallet；只允许零地址继续作为 native asset，
  避免误启用 CSV 零 follower 占位关系并生成错误归因。
- [x] 保留历史 disabled 零 follower 样本，但新的 CSV 导入入口在连接数据库前拒绝零 follower
  或零 smart wallet，不能继续创建不可启用的占位关系。
- [x] 严格验证地址、原始整数、比例、额度、触发档、协议、资产和完整路由。
- [x] 决策/成交归因保存 follower、relationship ID、strategy version 和配置快照哈希。
- [x] SQLite 预算、提案幂等和仓位使用 follower/relationship/smart wallet 派生的稳定 ledger
  scope；同一 smart wallet 对多个 follower 的独立额度与 proposal 已有回归。公开归因仍保存并
  导出原始 follower、relationship、smart wallet 和 source event，不用内部 scope 冒充钱包。
- [x] 签名前通过运行时只读账号按 relationship ID 重新查询 enabled 行，并核对 follower、smart
  wallet 与逐行 snapshot hash；禁用或修改配置会立即拒绝，不只依赖进程启动时缓存。

## 构建交易与广播前复核

- [x] 只为已验证精确路由构建 exact-input：V2/V3 及 V4 单跳 native-input 已完成 ABI/反解回归；
  V4 token-input/多跳在 Permit2 和实际组合验收前显式拒绝，不属于当前受支持路径。
- [x] monitor 不自动生成 approval；独立命令把 USDG allowance 限定为当前 relationship 周期总预算
  的200倍，拒绝无限授权、部分追加授权和存在 pending nonce。软件净累计投入上限不变，测试结束
  必须撤销 allowance。V4 token-input 因 Permit2 语义未验收而拒绝。
- [x] 签名前逐字段核对 chain ID、to、calldata、value、gas、type、EIP-1559 fee 与持久 unsigned
  plan；from 由预期 follower 的数据库密钥派生并在签名后 recovery 复核。deadline/minOut 已固定
  在构建后 ABI 回归核验的 calldata 中，关系配置 snapshot hash 也在签名前匹配当前配置。
- [x] 使用 pending nonce 并持久化 nonce reservation；同一 proposal 幂等，并发事务和重启恢复
  已覆盖。只读生命周期协调已能发现外部广播的原交易和同 nonce replacement，replacement
  必须保持 from/to/calldata/value/gas/chain/type 并逐次提高 EIP-1559 费用。
- [ ] 广播前再次检查余额、Gas、报价年龄、滑点、价格偏离、流动性和额度。当前已完成未签名
  plan 的余额、Router allowance、Gas、报价年龄和目标 allowlist 只读预检；离线签名前已重新
  报价并复查上述状态、pending nonce、BUY budget reservation 或 SELL position reservation。
  最终公开复核证据随 signed 状态原子保存。另有无广播能力的 pre-broadcast reviewer：对调用方
  内存中的 raw bytes 核对 hash/sender/完整交易后再次执行关系、额度、报价和 RPC preflight，且
  要求 pending nonce 与签名 nonce 完全相等。受控 broadcaster 已接在 reviewer 后；confirmed
  ERC-20 路径会核对规范 receipt 和 follower 净差额并结算 lot/PnL。尚无真实主网小额回执证据，
  因此本项仍保持未完成。
- [x] proposal、execution plan 和 nonce reservation 均有持久幂等键；同一 source
  signal/relationship 最多形成一个 prepared plan。受控 live 路径已复用这些幂等键。
- [x] execution plan 的身份字段、follower/relationship/config snapshot、transaction、unsigned
  plan 和初始 preflight 使用持久 SHA-256 完整性哈希；重启读取或签名前不匹配即拒绝。旧 SQLite
  行在首次迁移时按已有内容补建基线哈希，不把该哈希误称为抵御数据库管理员的签名证明。
- [x] raw signed bytes 不落库导致的“签名后、交付前退出”有显式恢复：重启后重做全部签名前检查，
  仅对唯一且未被 RPC 观察的 signed attempt 确定性重签，并要求 hash 与账本一致；状态推进后拒绝。
- [x] 重启 audit 逐条核对 attempt 与 immutable plan 的交易身份、原始费用、逐级 replacement 提价/
  parent 状态和最终区块证据；公开账本局部损坏不能继续显示 healthy。
- [x] 交易构建硬性要求 `swap_evidenced`、execution success、非 orphaned、受支持 BUY/SELL/
  TOKEN_SWAP、exact-input 和完整 allowlist；UNKNOWN、needs_review、失败/未知执行、报价缺失/过期
  或归属不明均拒绝，已有显式回归。

## 验证与上线门禁

- [x] 使用运行时临时生成且无资金的测试密钥完成 type-2 离线签名/发送者恢复测试；密钥不写盘。
  使用者已自行向独立 key MySQL 插入 follower 记录；项目只执行不选择私钥列的元数据检查，确认
  found/enabled，没有读取或输出真实私钥。真实签名仍待风险清单完成后单独执行。
- [x] 离线签名前重新报价/预检；报价恶化或 pending nonce 超过预留值均拒绝。账本只记录公开
  tx hash、signed 状态和最终公开复核证据，不记录 raw signed transaction；复核证据若包含
  private key/raw transaction 字段会拒绝。
- [ ] 在明确测试环境完成失败、超时、nonce 冲突、revert、重启和重组演练。离线回归已覆盖
  RPC 未发现、pending、成功 receipt、revert、replacement 字段/费用拒绝及规范块 hash 不符
  的 orphan；`execution-track` 已提供外部 hash 的只读 CLI 接入。服务器没有现成本地 EVM 节点
  二进制或镜像，仍缺独立测试链的真实广播演练。
- [x] 主网广播 RPC 方法不在默认只读允许列表，并有单独、默认关闭的多重开关。
  `MainnetBroadcaster` 与 `ReadOnlyRpc` 分离，要求 execution/signing/broadcast mode、emergency
  stop、stop file、chain ID、CLI 开关，以及绑定 follower/relationship/config snapshot 的权限
  安全验收文件；发送前独立恢复 sender 并验证 chain/hash。缺任一项在 key SELECT 前或广播前拒绝。
- [ ] 操作员明确确认钱包、单笔/周期额度、Gas 上限、紧急停止和回滚流程。
  数据库 `enabled=0` 的即时签名前停机门禁、默认停止的进程环境门禁及运行期 stop file 已实现；
  启动、重启、三层停止及公开 execution 状态处置已写入 `OPERATOR_RUNBOOK.md`；仍缺操作员验收
  记录和真实广播后的测试链演练，故本项保持未完成。
- [x] 用户已明确授权开发人工看守的小额主网测试入口。
- [ ] 用户在真实小额证据、余额差分结算、approve/重启处置完成后授权无人值守实盘。
