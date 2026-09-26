# 社区服务圈试点履约拨付账

第三批30个城市作为一刻钟便民生活圈全域推进先行区，将利用约两年实现试点城市主城区及有条件县城社区覆盖；建设强调因地制宜和补齐服务功能。

本仓库在领域事件契约之上提供一套内存态的试点履约拨付领域服务，把**资金批次、社区目标、服务商合同、工程节点、开业凭证、持续营业观察、居民反馈和拨付决定**关联为一条可审计链路。

## 目录

- `contracts/domain.schema.json`：聚合、事件、角色信封与载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/pilot_funding/contracts.py`：基础契约校验（不改写输入）。
- `src/pilot_funding/events.py`：只追加事件日志（契约校验 + 按聚合递增版本）。
- `src/pilot_funding/model.py`：角色、证据门、分段、案件、复审、目标版本等值对象。
- `src/pilot_funding/service.py`：`FundingService` 领域服务与全部业务规则。
- `src/pilot_funding/cli.py`：单事件命令行校验入口。
- `tests/`：契约信封测试与领域规则测试。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 领域规则速览

- **角色分权**：执行人提交材料但不得批准本方拨付；财政复核者只确认工程资金依据并占用/放款；社区验收人确认服务实际可用、记录到期抽查与居民反馈；批准人裁决复审、批准拨付、撤销资格。
- **分段拨付**：按合同节点（工程 → 开业 → 持续营业）分段，前段未放款后段不得占用预算。
- **重复申报不重复占预算**：同一证据重复申报仅记 `duplicate_claim`；同一凭证跨网点使用自动立案。
- **同项目异证据进复审**：复审未结清资金停摆；裁决可维持原证据或更换采信口径。
- **预算硬约束**：多社区并行申领时，占用 + 已付不得超过批次余额。
- **撤销只冻结未付**：未付分段冻结并退回预算占用，已付金额另立核查账。
- **目标调整保留原口径**：计划网点不算持续服务；覆盖率同时按原口径与现行口径输出。
- **停业清零、重启续查**：持续营业抽查在停业后清零，重启后重新累计并继续到期抽查。
- **资金卡点可解释**：`money_blocked()` 逐分段给出当前卡住的证据节点与应处理角色。

## 用法示例

```python
from decimal import Decimal
from pilot_funding import (
    FundingService, EventLog, Actor, Role, Segment, Gate,
    EvidenceKind, ObservationStatus,
)

svc = FundingService(EventLog())
system = Actor("city-finance", Role.SYSTEM)
operator = Actor("op-001", Role.OPERATOR)
finance = Actor("reviewer-01", Role.FINANCE_REVIEWER)
acceptor = Actor("community-01", Role.COMMUNITY_ACCEPTOR)
approver = Actor("approver-01", Role.APPROVER)

svc.open_round(system, "round-2026", Decimal("5000000"), "2026-01-01T09:00:00+08:00")
svc.publish_target(system, "community-07", "早餐店", 3, "2026-01-02T09:00:00+08:00")
svc.register_contract(
    system, "ct-001", "op-001", "早餐店", "round-2026",
    [
        Segment(1, "工程验收", Decimal("300000"), Gate.ENGINEERING),
        Segment(2, "开业补助", Decimal("300000"), Gate.OPENING),
        Segment(3, "持续营业", Decimal("400000"), Gate.SUSTAINED,
                observations_due=2, min_feedback=1, observation_interval_days=30),
    ],
    "2026-01-03T09:00:00+08:00",
)
svc.link_project(operator, "p-2026-0007", "ct-001", "community-07", "2026-01-04T09:00:00+08:00")

# 一笔资金当前卡在哪里
for status in svc.money_blocked("p-2026-0007"):
    print(status.segment.seq, status.state, [b.node for b in status.blocks])

# 覆盖结果（按社区 × 服务类别 × 实际营业状态）
for row in svc.coverage_report():
    print(row["community_id"], row["service_category"], row["by_operating_status"])
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m pilot_funding.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
