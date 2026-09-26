"""试点履约拨付领域的枚举、值对象与错误。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Role(str, Enum):
    OPERATOR = "operator"
    FINANCE_REVIEWER = "finance_reviewer"
    COMMUNITY_ACCEPTOR = "community_acceptor"
    APPROVER = "approver"
    SYSTEM = "system"


class EvidenceKind(str, Enum):
    # 工程验收材料：资金依据，由财政复核者确认
    ENGINEERING_ACCEPTANCE = "engineering_acceptance"
    # 开业凭证：由执行人提交，社区验收人据此核验实际可用
    OPENING_CERTIFICATE = "opening_certificate"


class Gate(str, Enum):
    """合同分段的拨付证据门。"""

    ENGINEERING = "engineering"
    OPENING = "opening"
    SUSTAINED = "sustained"


class ObservationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class CaseState(str, Enum):
    RESERVED = "reserved"
    APPROVED = "approved"
    RELEASED = "released"
    REJECTED = "rejected"
    FROZEN = "frozen"


class ProjectStatus(str, Enum):
    NOT_OPENED = "not_opened"
    OPEN = "open"
    SUSPENDED = "suspended"


class ReviewOutcome(str, Enum):
    UPHOLD_ORIGINAL = "uphold_original"
    REPLACE_EVIDENCE = "replace_evidence"


@dataclass(frozen=True)
class Actor:
    id: str
    role: Role


@dataclass(frozen=True)
class Segment:
    """合同拨付分段。

    sustained 分段可要求 observations_due 次到期抽查、min_feedback 条居民反馈，
    observation_interval_days 给出抽查间隔（从上一次开业/重启起算）。
    """

    seq: int
    name: str
    amount: Decimal
    gate: Gate
    observations_due: int = 0
    min_feedback: int = 0
    observation_interval_days: int = 30


@dataclass
class ProofSubmission:
    proof_ref: str
    submitted_at: str
    accepted: bool = False


@dataclass
class ClosurePeriod:
    closed_at: str
    reopened_at: str | None = None


@dataclass
class Observation:
    seq: int
    observed_at: str
    status: ObservationStatus


@dataclass
class Review:
    review_id: str
    evidence_kind: EvidenceKind
    original_ref: str
    contested_ref: str
    reason: str
    opened_at: str
    outcome: ReviewOutcome | None = None
    resolved_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.outcome is None


@dataclass
class TargetVersion:
    """社区目标的一个统计口径版本，旧版本永久保留。"""

    version: int
    planned_outlets: int
    published_at: str
    reason: str | None = None


@dataclass
class DisbursementCase:
    case_id: str
    round_id: str
    project_no: str
    segment_seq: int
    amount: Decimal
    state: CaseState
    reserved_at: str
    approved_at: str | None = None
    released_at: str | None = None
    reject_reason: str | None = None


@dataclass
class Project:
    """一个网点（同一 project_no 贯穿证据、营业与拨付）。"""

    project_no: str
    contract_id: str
    community_id: str
    service_category: str
    linked_at: str
    submissions: dict[EvidenceKind, list[ProofSubmission]] = field(default_factory=dict)
    accepted_ref: dict[EvidenceKind, str] = field(default_factory=dict)
    reviews: list[Review] = field(default_factory=list)
    status: ProjectStatus = ProjectStatus.NOT_OPENED
    opened_at: str | None = None
    closures: list[ClosurePeriod] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    feedback: list[tuple[str, int, str]] = field(default_factory=list)
    revoked: bool = False
    revoked_at: str | None = None
    revoke_reason: str | None = None
    cases: dict[int, DisbursementCase] = field(default_factory=dict)

    def open_reviews(self, kind: EvidenceKind | None = None) -> list[Review]:
        return [
            r
            for r in self.reviews
            if r.is_open and (kind is None or r.evidence_kind == kind)
        ]

    def accepted(self, kind: EvidenceKind) -> bool:
        return kind in self.accepted_ref


@dataclass
class Block:
    """一笔资金当前卡住的证据节点。"""

    node: str
    detail: str
    expected_role: Role | None = None


@dataclass
class SegmentStatus:
    segment: Segment
    state: str
    case_id: str | None
    blocks: list[Block] = field(default_factory=list)


class DomainError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
