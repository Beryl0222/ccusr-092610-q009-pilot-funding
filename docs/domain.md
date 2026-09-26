# 领域约定

第三批30个城市作为一刻钟便民生活圈全域推进先行区，将利用约两年实现试点城市主城区及有条件县城社区覆盖；建设强调因地制宜和补齐服务功能。

聚合对象包括 `funding_round`（资金批次）、`community_target`（社区目标）、`service_contract`（服务商合同）、`service_outlet`（服务网点）、`milestone_proof`（节点材料）、`disbursement_case`（拨付案件）。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件类型

- 资金与目标：`ROUND_OPENED`、`TARGET_SET`、`TARGET_REBASED`（目标调整须另起改口径事件，原统计口径保留在历史版本中）。
- 合同与网点：`CONTRACT_SIGNED`、`OUTLET_REGISTERED`。
- 履约证据：`MILESTONE_SUBMITTED`、`SERVICE_VERIFIED`。
- 重复申报：`DUPLICATE_FLAGGED`（相同项目编号而验收证据不同，自动进入复审并冻结该案件）、`DUPLICATE_RESOLVED`。
- 拨付决定：`PAYMENT_RESERVED`（按合同节点分段占用预算）、`PAYMENT_RELEASED`（释放占用）、`PAYMENT_REJECTED`、`PAYMENT_PAID`。
- 资格与核查：`ELIGIBILITY_REVOKED`（只冻结未付部分，已付部分转入核查）、`PAID_REVIEW_CLOSED`。
- 运营事实：`OBSERVATION_RECORDED`（持续营业观察与到期抽查）、`FEEDBACK_RECORDED`（居民反馈）。

## 事件载荷

- `ROUND_OPENED`：`total_amount`。
- `TARGET_SET` / `TARGET_REBASED`：`community_id`, `period`, `metric`, `planned_count`；改口径另需 `reason`。
- `CONTRACT_SIGNED`：`round_id`, `project_code`, `operator_id`, `segments`（节点分段，含节点编码与金额）。
- `OUTLET_REGISTERED`：`contract_id`, `community_id`, `service_category`。
- `MILESTONE_SUBMITTED`：`contract_revision`, `proof_ref`, `project_code`, `milestone_code`, `verification_fingerprint`。
- `SERVICE_VERIFIED`：`case_id`, `verification_fingerprint`。
- `DUPLICATE_FLAGGED`：`project_code`, `case_id`, `prior_fingerprint`, `incoming_fingerprint`。
- `DUPLICATE_RESOLVED`：`project_code`, `case_id`, `resolution`。
- `PAYMENT_RESERVED`：`amount`, `milestone_ref`。
- `PAYMENT_RELEASED` / `PAYMENT_REJECTED`：`amount`, `milestone_ref`, `reason`。
- `PAYMENT_PAID`：`amount`, `milestone_ref`。
- `ELIGIBILITY_REVOKED`：`effective_at`, `reason`。
- `PAID_REVIEW_CLOSED`：`outcome`, `clawback_amount`。
- `OBSERVATION_RECORDED`：`observed_at`, `is_open`, `next_check_due`。
- `FEEDBACK_RECORDED`：`channel`, `summary`, `sentiment`。

## 角色分权

- 项目执行人（executor）：提交节点材料与开业凭证，登记居民反馈；不得批准本方拨付。
- 财政复核者（fiscal_reviewer）：只确认资金依据（预算占用、释放、驳回、支付登记）。
- 社区验收人（community_acceptor）：确认服务实际可用（SERVICE_VERIFIED）。
- 三类角色分离，同一案件的提交人不得出现在其验收或拨付决定环节。

## 核心不变量

- 重复申报不得重复占用预算：相同项目编号且验收证据一致的提交按幂等处理；证据不同进入复审，复审期间该案件冻结。
- 多个社区并行申领时，批次内承诺（占用中）加已付不得超过批次总额。
- 撤销网点资格只冻结未付部分；已支付金额转入核查，核查关闭时记录追回金额。
- 覆盖统计按计划、在建、营业中、停业、关闭的实际状态区分；计划网点不得计入持续服务。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本层只定义可稳定交换的基础事实。
