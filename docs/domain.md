# 领域约定

第三批30个城市作为一刻钟便民生活圈全域推进先行区，将利用约两年实现试点城市主城区及有条件县城社区覆盖；建设强调因地制宜和补齐服务功能。

聚合对象包括`funding_round`、`service_contract`、`milestone_proof`、`disbursement_case`。事件类型包括`ROUND_OPENED`、`MILESTONE_SUBMITTED`、`SERVICE_VERIFIED`、`PAYMENT_RESERVED`、`ELIGIBILITY_REVOKED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `MILESTONE_SUBMITTED`：载荷还需包含 `contract_revision`, `proof_ref`。
- `PAYMENT_RESERVED`：载荷还需包含 `amount`, `milestone_ref`。
- `ELIGIBILITY_REVOKED`：载荷还需包含 `effective_at`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
