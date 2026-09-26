"""履约拨付账的事件存储与状态投影。

本模块只负责"事实如何落地为状态"：事件先经契约校验，再按聚合版本
顺序追加，最后由投影还原为资金批次、社区目标、合同、网点、拨付案件
等内部状态。业务规则（角色分权、预算不变量、复审流程）在
`service.py` 中执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import ContractIssue, validate_event

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

# 拨付案件状态
CASE_OPEN = "open"  # 材料已提交，待验收
CASE_READY = "ready"  # 验收通过，待拨付决定
CASE_RESERVED = "reserved"  # 已批准并占用预算
CASE_PAID = "paid"  # 已支付
CASE_REJECTED = "rejected"  # 拨付被驳回
CASE_REVIEW_FROZEN = "review_frozen"  # 重复申报复审中，冻结
CASE_REVOKED_FROZEN = "revoked_frozen"  # 资格撤销，未付部分冻结
CASE_RELEASED = "released"  # 占用已释放（未支付）

# 网点营业状态
OUTLET_PLANNED = "planned"  # 计划网点，不计入持续服务
OUTLET_BUILDING = "building"  # 在建（已有工程节点材料）
OUTLET_OPEN = "open"  # 营业中
OUTLET_SUSPENDED = "suspended"  # 停业（观察发现未在营业）
OUTLET_CLOSED = "closed"  # 关闭（资格撤销）

CASE_STATUS_LABELS = {
    CASE_OPEN: "材料待验收",
    CASE_READY: "待拨付决定",
    CASE_RESERVED: "已占用待支付",
    CASE_PAID: "已支付",
    CASE_REJECTED: "已驳回",
    CASE_REVIEW_FROZEN: "重复申报复审中",
    CASE_REVOKED_FROZEN: "资格撤销冻结",
    CASE_RELEASED: "占用已释放",
}

OUTLET_STATUS_LABELS = {
    OUTLET_PLANNED: "计划",
    OUTLET_BUILDING: "在建",
    OUTLET_OPEN: "营业中",
    OUTLET_SUSPENDED: "停业",
    OUTLET_CLOSED: "关闭",
}


class DomainError(Exception):
    """领域错误，code 为稳定的机器可读码。"""

    def __init__(self, code: str, message: str, issues: Iterable[ContractIssue] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.issues = list(issues or [])


def money(value: Any) -> Decimal:
    """把输入统一为 Decimal，避免浮点误差。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def default_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": dict(self.payload),
        }


class EventStore:
    """追加式事件存储：契约校验、版本递增、event_id 幂等。"""

    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self._schema = schema or default_schema()
        self._events: list[StoredEvent] = []
        self._by_id: dict[str, StoredEvent] = {}

    def append(self, event: Mapping[str, Any]) -> tuple[StoredEvent, bool]:
        """追加事件；返回 (事件, 是否真正追加)。event_id 重复时幂等返回。"""
        issues = validate_event(event, self._schema)
        if issues:
            summary = "；".join(f"{i.field}:{i.code}" for i in issues)
            raise DomainError("contract_violation", f"事件未通过契约校验：{summary}", issues)
        event_id = str(event["event_id"])
        if event_id in self._by_id:
            return self._by_id[event_id], False
        aggregate_type = str(event["aggregate_type"])
        aggregate_id = str(event["aggregate_id"])
        expected = 1 + sum(
            1
            for e in self._events
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        )
        version = event["version"]
        if version != expected:
            raise DomainError(
                "version_conflict",
                f"聚合 {aggregate_type}/{aggregate_id} 期望版本 {expected}，收到 {version}",
            )
        stored = StoredEvent(
            event_id=event_id,
            event_type=str(event["event_type"]),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=str(event["occurred_at"]),
            version=int(version),
            payload=dict(event["payload"]),
        )
        self._events.append(stored)
        self._by_id[event_id] = stored
        return stored, True

    def events(self) -> list[StoredEvent]:
        return list(self._events)

    def stream(self, aggregate_type: str, aggregate_id: str) -> list[StoredEvent]:
        return [
            e
            for e in self._events
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]


@dataclass
class RoundState:
    round_id: str
    total: Decimal
    reserved: Decimal = Decimal(0)  # 占用中（已批准未支付）
    paid: Decimal = Decimal(0)  # 已支付（总额，追回在 clawed_back 中单列）
    clawed_back: Decimal = Decimal(0)  # 已核查追回

    @property
    def committed(self) -> Decimal:
        return self.reserved + self.paid

    @property
    def available(self) -> Decimal:
        return self.total - self.committed


@dataclass
class TargetRevision:
    planned_count: int
    reason: str
    recorded_at: str


@dataclass
class TargetState:
    """社区目标：revisions[0] 为原统计口径，改口径只追加不覆盖。"""

    community_id: str
    period: str
    metric: str
    revisions: list[TargetRevision] = field(default_factory=list)

    @property
    def baseline_count(self) -> int:
        return self.revisions[0].planned_count

    @property
    def current_count(self) -> int:
        return self.revisions[-1].planned_count


@dataclass
class Segment:
    code: str
    amount: Decimal


@dataclass
class ContractState:
    contract_id: str
    round_id: str
    project_code: str
    operator_id: str
    revision: int
    segments: list[Segment]
    outlet_ids: list[str] = field(default_factory=list)


@dataclass
class Observation:
    observed_at: str
    is_open: bool
    next_check_due: str


@dataclass
class OutletState:
    outlet_id: str
    contract_id: str
    community_id: str
    service_category: str
    status: str = OUTLET_PLANNED
    eligible: bool = True
    revoked_reason: str | None = None
    observations: list[Observation] = field(default_factory=list)
    feedback: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def latest_observation(self) -> Observation | None:
        return self.observations[-1] if self.observations else None


@dataclass
class PaidReview:
    """资格撤销后对已支付金额的核查。"""

    amount: Decimal
    reason: str
    opened_at: str
    open: bool = True
    outcome: str | None = None
    clawback_amount: Decimal = Decimal(0)


@dataclass
class CaseState:
    case_id: str
    contract_id: str
    round_id: str
    project_code: str
    milestone_code: str
    segment_seq: int
    amount: Decimal
    status: str = CASE_OPEN
    proof_ref: str | None = None
    verification_fingerprint: str | None = None
    submitted_by: str | None = None
    verified_by: str | None = None
    decided_by: str | None = None
    paid_review: PaidReview | None = None
    history: list[str] = field(default_factory=list)  # 相关 event_id，按发生顺序


@dataclass
class DuplicateReview:
    case_id: str
    project_code: str
    prior_fingerprint: str
    incoming_fingerprint: str
    open: bool = True
    resolution: str | None = None


class Ledger:
    """事件投影：从事件流还原全部内部状态。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()
        self.rounds: dict[str, RoundState] = {}
        self.targets: dict[str, TargetState] = {}
        self.contracts: dict[str, ContractState] = {}
        self.outlets: dict[str, OutletState] = {}
        self.cases: dict[str, CaseState] = {}
        self.duplicate_reviews: dict[str, DuplicateReview] = {}
        self.cases_by_project: dict[str, list[str]] = {}

    @classmethod
    def replay(cls, events: Iterable[Mapping[str, Any]]) -> "Ledger":
        ledger = cls()
        for event in events:
            ledger.append(event)
        return ledger

    def append(self, event: Mapping[str, Any]) -> StoredEvent:
        """追加事件并推进投影；event_id 重复时为幂等空操作。"""
        stored, applied = self.store.append(event)
        if applied:
            self._apply(stored)
        return stored

    # ---- 投影分派 ----

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type.lower()}", None)
        if handler is None:
            raise DomainError("unknown_event", f"未登记的事件类型 {event.event_type}")
        handler(event)

    def _on_round_opened(self, event: StoredEvent) -> None:
        self.rounds[event.aggregate_id] = RoundState(
            round_id=event.aggregate_id,
            total=money(event.payload["total_amount"]),
        )

    def _on_target_set(self, event: StoredEvent) -> None:
        p = event.payload
        key = event.aggregate_id
        self.targets[key] = TargetState(
            community_id=str(p["community_id"]),
            period=str(p["period"]),
            metric=str(p["metric"]),
            revisions=[TargetRevision(int(p["planned_count"]), "initial", event.occurred_at)],
        )

    def _on_target_rebased(self, event: StoredEvent) -> None:
        target = self.targets[event.aggregate_id]
        p = event.payload
        target.revisions.append(
            TargetRevision(int(p["planned_count"]), str(p["reason"]), event.occurred_at)
        )

    def _on_contract_signed(self, event: StoredEvent) -> None:
        p = event.payload
        segments = [
            Segment(code=str(s["code"]), amount=money(s["amount"])) for s in p["segments"]
        ]
        self.contracts[event.aggregate_id] = ContractState(
            contract_id=event.aggregate_id,
            round_id=str(p["round_id"]),
            project_code=str(p["project_code"]),
            operator_id=str(p["operator_id"]),
            revision=event.version,
            segments=segments,
        )

    def _on_outlet_registered(self, event: StoredEvent) -> None:
        p = event.payload
        outlet = OutletState(
            outlet_id=event.aggregate_id,
            contract_id=str(p["contract_id"]),
            community_id=str(p["community_id"]),
            service_category=str(p["service_category"]),
        )
        self.outlets[event.aggregate_id] = outlet
        contract = self.contracts.get(outlet.contract_id)
        if contract is not None:
            contract.outlet_ids.append(outlet.outlet_id)

    def _on_milestone_submitted(self, event: StoredEvent) -> None:
        p = event.payload
        contract = self.contracts[str(p["contract_id"])]
        contract.revision = int(p["contract_revision"])
        case = CaseState(
            case_id=event.aggregate_id,
            contract_id=contract.contract_id,
            round_id=contract.round_id,
            project_code=str(p["project_code"]),
            milestone_code=str(p["milestone_code"]),
            segment_seq=int(p["segment_seq"]),
            amount=money(p["amount"]),
            proof_ref=str(p["proof_ref"]),
            verification_fingerprint=str(p["verification_fingerprint"]),
            submitted_by=str(p["submitted_by"]),
            history=[event.event_id],
        )
        self.cases[case.case_id] = case
        self.cases_by_project.setdefault(case.project_code, []).append(case.case_id)
        # 工程节点材料到达即视为在建，但计划网点不得直接算作持续服务
        for outlet_id in contract.outlet_ids:
            outlet = self.outlets[outlet_id]
            if outlet.status == OUTLET_PLANNED:
                outlet.status = OUTLET_BUILDING

    def _on_service_verified(self, event: StoredEvent) -> None:
        case = self.cases[str(event.payload["case_id"])]
        case.status = CASE_READY
        case.verified_by = str(event.payload["verified_by"])
        case.history.append(event.event_id)

    def _on_duplicate_flagged(self, event: StoredEvent) -> None:
        p = event.payload
        case = self.cases[str(p["case_id"])]
        # 已支付案件不回退状态，仅保留复审记录；未付案件冻结
        if case.status != CASE_PAID:
            case.status = CASE_REVIEW_FROZEN
        case.history.append(event.event_id)
        self.duplicate_reviews[case.case_id] = DuplicateReview(
            case_id=case.case_id,
            project_code=str(p["project_code"]),
            prior_fingerprint=str(p["prior_fingerprint"]),
            incoming_fingerprint=str(p["incoming_fingerprint"]),
        )

    def _on_duplicate_resolved(self, event: StoredEvent) -> None:
        p = event.payload
        case = self.cases[str(p["case_id"])]
        review = self.duplicate_reviews[case.case_id]
        review.open = False
        review.resolution = str(p["resolution"])
        # 复审澄清后回到待验收；确认重复由服务层另行驳回。
        # 若期间资格已被撤销，则保持撤销冻结不回退。
        if case.status == CASE_REVIEW_FROZEN:
            case.status = CASE_OPEN
        case.history.append(event.event_id)

    def _on_payment_reserved(self, event: StoredEvent) -> None:
        p = event.payload
        case = self.cases[event.aggregate_id]
        case.status = CASE_RESERVED
        case.decided_by = str(p["decided_by"])
        case.history.append(event.event_id)
        round_state = self.rounds[case.round_id]
        round_state.reserved += case.amount

    def _on_payment_released(self, event: StoredEvent) -> None:
        case = self.cases[event.aggregate_id]
        case.status = CASE_RELEASED
        case.history.append(event.event_id)
        round_state = self.rounds[case.round_id]
        round_state.reserved -= case.amount

    def _on_payment_rejected(self, event: StoredEvent) -> None:
        case = self.cases[event.aggregate_id]
        case.status = CASE_REJECTED
        case.decided_by = str(event.payload["decided_by"])
        case.history.append(event.event_id)

    def _on_payment_paid(self, event: StoredEvent) -> None:
        case = self.cases[event.aggregate_id]
        case.status = CASE_PAID
        case.history.append(event.event_id)
        round_state = self.rounds[case.round_id]
        round_state.reserved -= case.amount
        round_state.paid += case.amount

    def _on_eligibility_revoked(self, event: StoredEvent) -> None:
        outlet = self.outlets[event.aggregate_id]
        outlet.eligible = False
        outlet.status = OUTLET_CLOSED
        outlet.revoked_reason = str(event.payload["reason"])
        # 级联：该网点合同下的未付案件一律冻结（占用中的先释放预算），
        # 已支付案件转入核查。级联只依赖既有事件，重放结果确定。
        for case in self.cases.values():
            if case.contract_id != outlet.contract_id:
                continue
            if case.status in (CASE_OPEN, CASE_READY, CASE_REVIEW_FROZEN):
                case.status = CASE_REVOKED_FROZEN
                case.history.append(event.event_id)
            elif case.status == CASE_RESERVED:
                self.rounds[case.round_id].reserved -= case.amount
                case.status = CASE_REVOKED_FROZEN
                case.history.append(event.event_id)
            elif case.status == CASE_PAID and case.paid_review is None:
                case.paid_review = PaidReview(
                    amount=case.amount,
                    reason=outlet.revoked_reason,
                    opened_at=str(event.payload["effective_at"]),
                )
                case.history.append(event.event_id)

    def _on_paid_review_closed(self, event: StoredEvent) -> None:
        case = self.cases[event.aggregate_id]
        if case.paid_review is not None:
            case.paid_review.open = False
            case.paid_review.outcome = str(event.payload["outcome"])
            case.paid_review.clawback_amount = money(event.payload["clawback_amount"])
            self.rounds[case.round_id].clawed_back += case.paid_review.clawback_amount
        case.history.append(event.event_id)

    def _on_observation_recorded(self, event: StoredEvent) -> None:
        outlet = self.outlets[event.aggregate_id]
        p = event.payload
        observation = Observation(
            observed_at=str(p["observed_at"]),
            is_open=bool(p["is_open"]),
            next_check_due=str(p["next_check_due"]),
        )
        outlet.observations.append(observation)
        if outlet.status in (OUTLET_PLANNED, OUTLET_BUILDING, OUTLET_OPEN, OUTLET_SUSPENDED):
            outlet.status = OUTLET_OPEN if observation.is_open else OUTLET_SUSPENDED

    def _on_feedback_recorded(self, event: StoredEvent) -> None:
        outlet = self.outlets[event.aggregate_id]
        outlet.feedback.append(dict(event.payload))
