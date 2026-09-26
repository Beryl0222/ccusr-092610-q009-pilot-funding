# 领域约定

第三批30个城市作为一刻钟便民生活圈全域推进先行区，将利用约两年实现试点城市主城区及有条件县城社区覆盖；建设强调因地制宜和补齐服务功能。

聚合对象包括 `funding_round`（资金批次）、`community_target`（社区分服务类别的覆盖目标）、`service_contract`（服务商合同）、`milestone_proof`（网点证据、复审、营业与资格）、`disbursement_case`（分段拨付案件）。一个网点以 `project_no` 贯穿合同、证据、营业观察、居民反馈与全部拨付案件。

事件信封必含 `event_id`、`event_type`、`aggregate_type`、`aggregate_id`、`occurred_at`、`version`、`actor`、`payload`。所有发生时间都必须携带时区，版本号按聚合从 1 开始递增，基础校验不会改写调用方输入。`actor.role` 取值：`operator`（项目执行人）、`finance_reviewer`（财政复核者）、`community_acceptor`（社区验收人）、`approver`（批准人）、`system`。

## 事件与载荷

| 事件 | 聚合 | 关键字段（除公共字段外） |
| --- | --- | --- |
| `ROUND_OPENED` | funding_round | `total_amount` |
| `TARGET_PUBLISHED` / `TARGET_REVISED` | community_target | `community_id`, `service_category`, `planned_outlets`；调整另含 `supersedes_version` |
| `CONTRACT_REGISTERED` / `CONTRACT_REVISED` | service_contract | `contract_id`, `operator_id`, `service_category`, `round_id`, `segments[]`；修订另含 `revision` |
| `PROJECT_LINKED` | service_contract | `project_no`, `contract_id`, `community_id`, `service_category` |
| `MILESTONE_SUBMITTED` | milestone_proof | `contract_revision`, `proof_ref`, `evidence_kind`, `project_no` |
| `DUPLICATE_FLAG_RAISED` | milestone_proof | `project_no`, `flag_type`（`duplicate_claim` / `evidence_conflict` / `proof_reused_across_projects`）, `detail` |
| `REVIEW_OPENED` / `REVIEW_RESOLVED` | milestone_proof | `review_id`, `project_no`, `evidence_kind`, `original_ref`, `contested_ref`；裁决含 `outcome`（`uphold_original` / `replace_evidence`） |
| `EVIDENCE_ACCEPTED` / `EVIDENCE_REJECTED` | milestone_proof | `proof_ref`, `evidence_kind`, `project_no`；驳回含 `reason` |
| `SERVICE_VERIFIED` | milestone_proof | `project_no`, `open_for_service_at` |
| `SERVICE_OBSERVED` | milestone_proof | `project_no`, `observed_at`, `status`（`open` / `closed`）, `seq` |
| `FEEDBACK_RECORDED` | milestone_proof | `project_no`, `score`（1–5）, `content_excerpt` |
| `PAYMENT_RESERVED` | disbursement_case | `amount`, `milestone_ref`, `segment_seq`, `project_no` |
| `PAYMENT_APPROVED` / `PAYMENT_RELEASED` | disbursement_case | `case_id`, `amount` |
| `PAYMENT_REJECTED` | disbursement_case | `case_id`, `reason` |
| `BUDGET_UNCOMMITTED` | funding_round | `case_ids[]`, `amount` |
| `ELIGIBILITY_REVOKED` | milestone_proof | `project_no`, `effective_at`, `reason` |
| `UNPAID_FROZEN` | milestone_proof | `project_no`, `case_ids[]` |
| `PAID_AUDIT_OPENED` | milestone_proof | `project_no`, `paid_total`, `case_ids[]` |

`evidence_kind` 分两类：`engineering_acceptance`（工程验收材料，资金依据）与 `opening_certificate`（开业凭证）。合同分段 `segments[]` 的 `gate` 为 `engineering` / `opening` / `sustained`；持续营业门可声明 `observations_due`（到期抽查次数）、`min_feedback`（居民反馈条数）、`observation_interval_days`（抽查间隔天数）。

## 业务规则（由上层服务 `pilot_funding.service` 执行）

1. **角色分权**：执行人可关联本方合同网点、提交材料，但不得批准本方拨付（批准人账号与运营方相同同样拒绝）；财政复核者只确认工程资金依据、占用/放款预算；社区验收人采信开业凭证、确认服务实际可用、记录到期抽查与居民反馈；复审裁决与资格撤销属于批准人。
2. **分段拨付**：前段未放款，后段不得占用预算；案件状态为 `reserved → approved → released`，驳回或撤销时经 `BUDGET_UNCOMMITTED` 退回占用。
3. **重复申报**：同一网点同一分段只有一个拨付案件；同一证据再次提交标记 `duplicate_claim`，不产生新案件、不重复占用预算；同一凭证在别的网点使用标记 `proof_reused_across_projects`。
4. **同项目异证据复审**：相同项目编号提交与已采信证据不同的凭证，标记 `evidence_conflict` 并自动立案复审；复审未结清前证据不能采信、资金不能推进。裁决 `replace_evidence` 更换资金依据，`uphold_original` 后争议证据不得再被采信或提交。
5. **批次余额**：多社区并行申领时，总承诺 = 已占用（reserved/approved）+ 已放款（released），不得超过 `ROUND_OPENED.total_amount`。
6. **资格撤销**：只冻结未付分段（案件置 `frozen` 并释放其预算占用），已付金额另立 `PAID_AUDIT_OPENED` 核查；撤销后网点不得再提交材料或记录观察，且不计入覆盖。
7. **目标口径**：`TARGET_REVISED` 只追加新版本，原统计口径全部保留；覆盖率同时给出对原口径与现行口径两个分母。计划网点本身不算覆盖，只有实际在营网点计入 `actually_open`，满足持续营业门（重启后重新累计的到期抽查数与反馈数达标、当前在营）才计入 `sustained_service`；撤销网点单列 `revoked`。
8. **持续营业**：在营抽查须按合同间隔到期进行，提前抽查不计账；记录 `closed` 后持续营业计数清零并挂起，重启当次在营观察作为新起点，之后继续按间隔到期抽查。
9. **资金卡点**：`money_blocked(project_no)` 逐分段返回状态与阻塞节点（缺工程依据、缺开业凭证、待确认实际可用、复审未决、前段未付、抽查/反馈不足、停业、待批准、待放款、已冻结等）及应处理角色。

相同事件标识的业务幂等、并发冲突隔离和跨实例状态推进仍由上层服务负责；本仓库的事件日志负责信封契约校验与按聚合递增版本。
