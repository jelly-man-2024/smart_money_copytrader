# 多数据源与实盘准备 Goal 验收矩阵

更新时间：2026-09-12。本文按当前工作树和服务器实测记录逐项核验，不是主网上线授权。

状态含义：`已验证` 表示存在代码与对应测试/运行证据；`部分验证` 表示实现存在但缺目标环境
证据；`待外部确认` 表示必须由操作员或用户提供授权，不能由项目自行完成。

| 要求 | 状态 | 权威证据 | 未覆盖边界 |
|---|---|---|---|
| 配置 MySQL 可由使用者服务器托管 | 已验证 | `mysql_config.py`；localhost Docker；远程六项显式配置/TLS/脱敏测试 | 尚未连接使用者另一台远程服务器 |
| `copy_relationships` 单表保存 follower×smart 唯一策略/额度 | 已验证 | schema 唯一键；严格 loader；真实两 follower 隔离脚本 | 当前 `run_mode` 仅 paper |
| CSV 批量导入聪明钱 | 已验证 | 67 条历史来源、导入 CLI、默认 disabled、零地址写入口拒绝 | 新真实 follower 批量导入需操作员执行 |
| 独立 key MySQL 简单签名数据源 | 已验证 | 独立 schema/容器/列级 SELECT；`OfflineDatabaseSigner`；`key-status` | key 表保持 0 行，未使用真实密钥 |
| 最小权限与连接加密 | 已验证 | 实际 `SHOW GRANTS`；远程 CA/完整显式配置门禁 | TLS 证书轮换由部署方负责 |
| 日志脱敏、私钥/raw 不进账本 | 已验证 | 固定异常、secret/raw 字段拒绝、slots 签名结果、SQLite 审计 | 调用方仍必须避免显式打印 raw 属性 |
| 配置快照与签名前新鲜关系复核 | 已验证 | relationship snapshot；`MySqlRelationshipGate`；真实 disable 演练 | 签名检查与未来广播之间仍有时间窗口 |
| 严格信号与路由门禁 | 已验证 | 仅 swap_evidenced/success/exact-in/allowlist；UNKNOWN 等拒绝 | V4 token-input、V4 execution 多跳不支持 |
| nonce、余额、allowance、Gas、报价、滑点、额度预检 | 已验证 | `ExecutionPreparer`、`OfflineExecutionSigner`、pre-broadcast reviewer 回归 | 尚无实际广播瞬间的原子检查 |
| proposal/plan/nonce 幂等与重启恢复 | 已验证 | SQLite 唯一键/事务；prepared 复用；signed bytes 确定性恢复 | 外部广播方崩溃窗口仍需集成演练 |
| 公开执行生命周期与重组处理 | 部分验证 | pending/confirmed/reverted/replaced/orphaned 模拟；attempt audit；`execution-track` 磁盘重启/错误链 CLI 回归 | 服务器无现成本地 EVM 节点，缺真实广播、重组和 replacement 演练 |
| 广播前最终复核 | 部分验证 | 内存 raw hash/sender/字段、关系、额度、报价、RPC 全量重查 | reviewer 与真实发送尚未形成原子边界 |
| `copy_eligible=false` 且禁止主网签名/广播 | 已验证 | 所有 replay；RPC allowlist；mainnet gate 无条件拒绝 | 无 |
| 操作员验收 | 待外部确认 | `OPERATOR_RUNBOOK.md` 已提供步骤 | 需确认钱包、额度、Gas、滑点、停止/恢复流程 |
| 用户最终主网授权 | 待外部确认 | 当前没有授权，门禁保持关闭 | 必须在审阅证据后再次明确授权 |

## 当前结论

简单双 MySQL 架构与离线实盘准备路径已经形成，但 Goal 仍不能标记完成：测试链真实生命周期、
广播瞬间边界、操作员验收和用户最终授权均缺少权威证据。项目不得用模拟回执、空账本或
`healthy=true` 替代这些证据。

`OPERATOR_RUNBOOK.md` 末尾提供默认“未通过”的验收记录模板。只有操作员填写具体地址、配置
snapshot、额度/风控值、停止与测试链证据并明确给出结论，才可更新本矩阵；模板存在不等于验收。

在上述门槛补齐前继续保持：

- `copy_eligible=false`；
- 主网 gate 无条件拒绝；
- RPC allowlist 不包含 `eth_sendRawTransaction`；
- key MySQL 不导入真实密钥；
- 不提交或推送 Git，除非用户另行明确要求。
