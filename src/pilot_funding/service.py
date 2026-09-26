"""试点履约拨付领域服务。

角色分权：
- operator（项目执行人）：提交材料，不得批准本方拨付；
- finance_reviewer（财政复核者）：只能确认资金依据、占用/释放预算；
- community_acceptor（社区验收人）：确认服务实际可用、记录到期抽查与居民反馈；
- approver：批准/驳回拨付、裁决复审、撤销网点资格。

核心不变量：
- 拨付按合同分段推进，前段未放款后段不占用预算；
- 同一网点同一分段只能产生一个拨付案件，重复申报不重复占用预算；
- 同项目编号而证据不同进入复审，复审未结清前资金不得继续；
- 总承诺（占用+已付）不得超过财政批次余额；
- 撤销网点资格只冻结未付部分（释放其预算占用），并对已付金额立账核查；
- 目标调整保留全部历史口径，计划网点永不计入持续服务；
- 停业清零持续营业抽查进度，重启后重新累计、继续到期抽查。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from .events import EventLog
from .model import (
    Actor,
    Block,
    CaseState,
    ClosurePeriod,
    DisbursementCase,
    DomainError,
    EvidenceKind,
    Gate,
    Observation,
    ObservationStatus,
    Project,
    ProjectStatus,
    ProofSubmission,
    Review,
    ReviewOutcome,
    Role,
    Segment,
    SegmentStatus,
    TargetVersion,
)

_EVIDENCE_ROLE = {
    EvidenceKind.ENGINEERING_ACCEPTANCE: Role.FINANCE_REVIEWER,
    EvidenceKind.OPENING_CERTIFICATE: Role.COMMUNITY_ACCEPTOR,
}


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DomainError("timezone_required", "时间必须包含时区")
    return parsed


def _money(value: Decimal | str | int) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount < 0:
        raise DomainError("negative_amount", "金额不能为负")
    return amount


class FundingService:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.log = event_log or EventLog()
        self.rounds: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, Project] = {}
        self.targets: dict[tuple[str, str], list[TargetVersion]] = defaultdict(list)
        self._proof_owner: dict[str, str] = {}

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _require(actor: Actor, role: Role) -> None:
        if actor.role is not role:
            raise DomainError(
                "forbidden_role",
                f"该操作只允许 {role.value} 执行，当前角色为 {actor.role.value}",
            )

    def _project(self, project_no: str) -> Project:
        project = self.projects.get(project_no)
        if project is None:
            raise DomainError("project_not_found", f"网点 {project_no} 尚未关联合同")
        return project

    def _contract(self, contract_id: str) -> dict[str, Any]:
        contract = self.contracts.get(contract_id)
        if contract is None:
            raise DomainError("contract_not_found", f"合同 {contract_id} 尚未登记")
        return contract

    def _segment(self, contract: dict[str, Any], seq: int) -> Segment:
        for segment in contract["segments"]:
            if segment.seq == seq:
                return segment
        raise DomainError("segment_not_found", f"合同不存在第 {seq} 分段")

    def _round_snapshot(self, round_id: str) -> dict[str, Any]:
        round_ = self.rounds.get(round_id)
        if round_ is None:
            raise DomainError("round_not_found", f"资金批次 {round_id} 尚未开立")
        return round_

    def _available(self, round_id: str) -> Decimal:
        round_ = self._round_snapshot(round_id)
        return round_["total"] - round_["committed"] - round_["released"]

    @staticmethod
    def _dt(value: str) -> datetime:
        return _dt(value)

    def _append(self, **kwargs: Any) -> dict[str, Any]:
        return self.log.append(**kwargs)

    # ------------------------------------------------------------ 资金批次

    def open_round(self, actor: Actor, round_id: str, total_amount: Decimal | str, at: str) -> None:
        self._require(actor, Role.SYSTEM)
        if round_id in self.rounds:
            raise DomainError("round_exists", "资金批次已存在")
        total = _money(total_amount)
        self._dt(at)
        self._append(
            event_type="ROUND_OPENED",
            aggregate_type="funding_round",
            aggregate_id=round_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"total_amount": str(total)},
        )
        self.rounds[round_id] = {"total": total, "committed": Decimal(0), "released": Decimal(0)}

    def round_balance(self, round_id: str) -> dict[str, Decimal]:
        round_ = self._round_snapshot(round_id)
        return {
            "total": round_["total"],
            "committed": round_["committed"],
            "released": round_["released"],
            "available": round_["total"] - round_["committed"] - round_["released"],
        }

    # ------------------------------------------------------------ 社区目标

    def publish_target(
        self,
        actor: Actor,
        community_id: str,
        service_category: str,
        planned_outlets: int,
        at: str,
    ) -> None:
        self._require(actor, Role.SYSTEM)
        key = (community_id, service_category)
        if self.targets[key]:
            raise DomainError("target_exists", "目标已发布，调整应使用 revise_target")
        if planned_outlets < 0:
            raise DomainError("negative_outlets", "计划网点数不能为负")
        self._dt(at)
        self._append(
            event_type="TARGET_PUBLISHED",
            aggregate_type="community_target",
            aggregate_id=f"{community_id}:{service_category}",
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "community_id": community_id,
                "service_category": service_category,
                "planned_outlets": planned_outlets,
            },
        )
        self.targets[key].append(TargetVersion(1, planned_outlets, at))

    def revise_target(
        self,
        actor: Actor,
        community_id: str,
        service_category: str,
        planned_outlets: int,
        at: str,
        reason: str | None = None,
    ) -> None:
        """调整目标；原统计口径（全部历史版本）保留，不覆盖、不删除。"""
        self._require(actor, Role.SYSTEM)
        key = (community_id, service_category)
        versions = self.targets[key]
        if not versions:
            raise DomainError("target_not_found", "目标尚未发布，不能调整")
        if planned_outlets < 0:
            raise DomainError("negative_outlets", "计划网点数不能为负")
        supersedes = versions[-1].version
        self._dt(at)
        self._append(
            event_type="TARGET_REVISED",
            aggregate_type="community_target",
            aggregate_id=f"{community_id}:{service_category}",
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "community_id": community_id,
                "service_category": service_category,
                "planned_outlets": planned_outlets,
                "supersedes_version": supersedes,
                **({"reason": reason} if reason else {}),
            },
        )
        versions.append(TargetVersion(supersedes + 1, planned_outlets, at, reason))

    # ---------------------------------------------------------------- 合同

    def register_contract(
        self,
        actor: Actor,
        contract_id: str,
        operator_id: str,
        service_category: str,
        round_id: str,
        segments: list[Segment],
        at: str,
    ) -> None:
        self._require(actor, Role.SYSTEM)
        if contract_id in self.contracts:
            raise DomainError("contract_exists", "合同已登记")
        self._round_snapshot(round_id)
        segs = sorted(segments, key=lambda s: s.seq)
        if [s.seq for s in segs] != list(range(1, len(segs) + 1)):
            raise DomainError("segment_seq_invalid", "分段序号必须从 1 连续编号")
        if any(s.amount < 0 for s in segs):
            raise DomainError("negative_amount", "分段金额不能为负")
        self._dt(at)
        self._append(
            event_type="CONTRACT_REGISTERED",
            aggregate_type="service_contract",
            aggregate_id=contract_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "contract_id": contract_id,
                "operator_id": operator_id,
                "service_category": service_category,
                "round_id": round_id,
                "segments": [self._segment_payload(s) for s in segs],
            },
        )
        self.contracts[contract_id] = {
            "operator_id": operator_id,
            "service_category": service_category,
            "round_id": round_id,
            "revision": 1,
            "segments": segs,
        }

    def revise_contract(
        self, actor: Actor, contract_id: str, segments: list[Segment], at: str
    ) -> None:
        self._require(actor, Role.SYSTEM)
        contract = self._contract(contract_id)
        outstanding = [
            case
            for p in self.projects.values()
            if p.contract_id == contract_id
            for case in p.cases.values()
            if case.state in (CaseState.RESERVED, CaseState.APPROVED)
        ]
        if outstanding:
            raise DomainError(
                "contract_has_open_cases",
                "存在占用中或已批准未放款的分段，不能修订合同",
            )
        segs = sorted(segments, key=lambda s: s.seq)
        if [s.seq for s in segs] != list(range(1, len(segs) + 1)):
            raise DomainError("segment_seq_invalid", "分段序号必须从 1 连续编号")
        new_revision = contract["revision"] + 1
        self._append(
            event_type="CONTRACT_REVISED",
            aggregate_type="service_contract",
            aggregate_id=contract_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "contract_id": contract_id,
                "revision": new_revision,
                "segments": [self._segment_payload(s) for s in segs],
            },
        )
        contract["revision"] = new_revision
        contract["segments"] = segs

    @staticmethod
    def _segment_payload(segment: Segment) -> dict[str, Any]:
        return {
            "seq": segment.seq,
            "name": segment.name,
            "amount": str(segment.amount),
            "gate": segment.gate.value,
            "observations_due": segment.observations_due,
            "min_feedback": segment.min_feedback,
            "observation_interval_days": segment.observation_interval_days,
        }

    # ------------------------------------------------------------ 网点关联

    def link_project(
        self, actor: Actor, project_no: str, contract_id: str, community_id: str, at: str
    ) -> None:
        self._require(actor, Role.OPERATOR)
        contract = self._contract(contract_id)
        if actor.id != contract["operator_id"]:
            raise DomainError("forbidden_operator", "执行人只能关联本方合同下的网点")
        if project_no in self.projects:
            raise DomainError("project_exists", "该项目编号已关联")
        self._dt(at)
        self._append(
            event_type="PROJECT_LINKED",
            aggregate_type="service_contract",
            aggregate_id=contract_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "project_no": project_no,
                "contract_id": contract_id,
                "community_id": community_id,
                "service_category": contract["service_category"],
            },
        )
        self.projects[project_no] = Project(
            project_no=project_no,
            contract_id=contract_id,
            community_id=community_id,
            service_category=contract["service_category"],
            linked_at=at,
        )

    # ------------------------------------------------------------ 材料提交

    def submit_milestone(
        self,
        actor: Actor,
        project_no: str,
        kind: EvidenceKind,
        proof_ref: str,
        contract_revision: int,
        at: str,
    ) -> dict[str, Any]:
        """执行人提交证据；返回是否触发重复标记/复审。"""
        self._require(actor, Role.OPERATOR)
        project = self._project(project_no)
        contract = self._contract(project.contract_id)
        if actor.id != contract["operator_id"]:
            raise DomainError("forbidden_operator", "执行人只能为本方网点提交材料")
        if project.revoked:
            raise DomainError("project_revoked", "网点资格已撤销，不能再提交材料")
        if contract_revision != contract["revision"]:
            raise DomainError(
                "stale_contract_revision",
                f"材料依据的合同修订号 {contract_revision} 已失效，当前为 {contract['revision']}",
            )
        if project.open_reviews(kind):
            raise DomainError("review_open", "该类证据存在未结清复审，暂不能再次提交")
        rejected_by_review = any(
            not r.is_open
            and r.evidence_kind is kind
            and r.outcome is ReviewOutcome.UPHOLD_ORIGINAL
            and r.contested_ref == proof_ref
            for r in project.reviews
        )
        if rejected_by_review:
            raise DomainError(
                "evidence_rejected_by_review",
                f"证据 {proof_ref} 已经复审裁定不予采信，不得重复提交",
            )
        self._dt(at)

        note: dict[str, Any] = {"flag": None, "review_id": None}
        flag_type: str | None = None
        detail = ""
        accepted_ref = project.accepted_ref.get(kind)
        other_owner = self._proof_owner.get(proof_ref)

        if other_owner and other_owner != project_no:
            flag_type = "proof_reused_across_projects"
            detail = f"凭证 {proof_ref} 已用于网点 {other_owner}"
        elif accepted_ref is not None and proof_ref != accepted_ref:
            flag_type = "evidence_conflict"
            detail = f"同项目新证据 {proof_ref} 与已采信证据 {accepted_ref} 不一致"
        elif accepted_ref is not None and proof_ref == accepted_ref:
            flag_type = "duplicate_claim"
            detail = "同一证据重复申报，不重复占用预算"

        self._append(
            event_type="MILESTONE_SUBMITTED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "contract_revision": contract_revision,
                "proof_ref": proof_ref,
                "evidence_kind": kind.value,
                "project_no": project_no,
            },
        )
        project.submissions.setdefault(kind, []).append(
            ProofSubmission(proof_ref=proof_ref, submitted_at=at)
        )
        self._proof_owner.setdefault(proof_ref, project_no)

        if flag_type is not None:
            self._append(
                event_type="DUPLICATE_FLAG_RAISED",
                aggregate_type="milestone_proof",
                aggregate_id=project_no,
                occurred_at=at,
                actor_id=actor.id,
                actor_role=Role.SYSTEM.value,
                payload={"project_no": project_no, "flag_type": flag_type, "detail": detail},
            )
            note["flag"] = flag_type
            if flag_type != "duplicate_claim":
                review_id = f"rv-{project_no}-{kind.value}-{len(project.reviews) + 1}"
                self._open_review(project, kind, accepted_ref or proof_ref, proof_ref, detail, at, review_id)
                note["review_id"] = review_id
        return note

    def _open_review(
        self,
        project: Project,
        kind: EvidenceKind,
        original_ref: str,
        contested_ref: str,
        reason: str,
        at: str,
        review_id: str,
    ) -> None:
        self._append(
            event_type="REVIEW_OPENED",
            aggregate_type="milestone_proof",
            aggregate_id=project.project_no,
            occurred_at=at,
            actor_id="system",
            actor_role=Role.SYSTEM.value,
            payload={
                "review_id": review_id,
                "project_no": project.project_no,
                "evidence_kind": kind.value,
                "original_ref": original_ref,
                "contested_ref": contested_ref,
                "reason": reason,
            },
        )
        project.reviews.append(
            Review(
                review_id=review_id,
                evidence_kind=kind,
                original_ref=original_ref,
                contested_ref=contested_ref,
                reason=reason,
                opened_at=at,
            )
        )

    def resolve_review(
        self,
        actor: Actor,
        project_no: str,
        review_id: str,
        outcome: ReviewOutcome,
        at: str,
    ) -> None:
        """批准人裁决复审：replace_evidence 以新证据替换采信口径。"""
        self._require(actor, Role.APPROVER)
        project = self._project(project_no)
        review = next((r for r in project.reviews if r.review_id == review_id), None)
        if review is None:
            raise DomainError("review_not_found", "复审不存在")
        if not review.is_open:
            raise DomainError("review_closed", "复审已裁决")
        self._dt(at)
        self._append(
            event_type="REVIEW_RESOLVED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "review_id": review_id,
                "project_no": project_no,
                "outcome": outcome.value,
            },
        )
        review.outcome = outcome
        review.resolved_at = at
        if outcome is ReviewOutcome.REPLACE_EVIDENCE:
            project.accepted_ref[review.evidence_kind] = review.contested_ref

    # ------------------------------------------------------------ 证据采信

    def accept_evidence(
        self, actor: Actor, project_no: str, kind: EvidenceKind, proof_ref: str, at: str
    ) -> None:
        """财政确认工程资金依据；社区验收人确认开业凭证。"""
        required_role = _EVIDENCE_ROLE[kind]
        self._require(actor, required_role)
        project = self._project(project_no)
        if project.revoked:
            raise DomainError("project_revoked", "网点资格已撤销")
        submissions = project.submissions.get(kind, [])
        if not any(s.proof_ref == proof_ref for s in submissions):
            raise DomainError("proof_not_submitted", "该证据尚未由执行人提交")
        if project.open_reviews(kind):
            raise DomainError("review_open", "证据存在未结清复审，不能采信")
        upheld = [
            r for r in project.reviews
            if not r.is_open
            and r.evidence_kind is kind
            and r.outcome is ReviewOutcome.UPHOLD_ORIGINAL
            and r.contested_ref == proof_ref
        ]
        if upheld:
            raise DomainError(
                "evidence_rejected_by_review",
                f"复审已裁决维持原证据，{proof_ref} 不得采信",
            )
        if project.accepted_ref.get(kind) == proof_ref:
            raise DomainError("evidence_already_accepted", "证据已采信，重复采信不产生新拨付")
        self._dt(at)
        self._append(
            event_type="EVIDENCE_ACCEPTED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "proof_ref": proof_ref,
                "evidence_kind": kind.value,
                "project_no": project_no,
            },
        )
        project.accepted_ref[kind] = proof_ref
        for submission in submissions:
            submission.accepted = submission.proof_ref == proof_ref

    def reject_evidence(
        self, actor: Actor, project_no: str, kind: EvidenceKind, proof_ref: str, reason: str, at: str
    ) -> None:
        self._require(actor, _EVIDENCE_ROLE[kind])
        project = self._project(project_no)
        if not any(s.proof_ref == proof_ref for s in project.submissions.get(kind, [])):
            raise DomainError("proof_not_submitted", "该证据尚未由执行人提交")
        self._append(
            event_type="EVIDENCE_REJECTED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"proof_ref": proof_ref, "reason": reason, "project_no": project_no},
        )

    # ------------------------------------------------------- 实际可用确认

    def verify_service_open(
        self, actor: Actor, project_no: str, at: str
    ) -> None:
        """社区验收人确认服务实际可用（开业凭证须先采信）。"""
        self._require(actor, Role.COMMUNITY_ACCEPTOR)
        project = self._project(project_no)
        if project.revoked:
            raise DomainError("project_revoked", "网点资格已撤销")
        if not project.accepted(EvidenceKind.OPENING_CERTIFICATE):
            raise DomainError(
                "opening_proof_missing",
                "开业凭证未经社区验收人采信，不能确认服务实际可用",
            )
        if project.status is not ProjectStatus.NOT_OPENED:
            raise DomainError("already_verified", "首次实际可用只能确认一次；停业后重启请记录抽查")
        self._dt(at)
        self._append(
            event_type="SERVICE_VERIFIED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"project_no": project_no, "open_for_service_at": at},
        )
        project.status = ProjectStatus.OPEN
        project.opened_at = at

    # ------------------------------------------------------- 持续营业观察

    def observe(
        self,
        actor: Actor,
        project_no: str,
        status: ObservationStatus,
        at: str,
    ) -> None:
        """到期抽查。停业则持续营业计数清零并挂起；重启后重新累计、继续到期抽查。

        在营抽查须按合同间隔到期进行（提前抽查不计账）；重启当次在营观察
        作为新一轮计数的起点，不做到期限制。
        """
        if actor.role not in (Role.COMMUNITY_ACCEPTOR, Role.SYSTEM):
            raise DomainError("forbidden_role", "营业抽查只能由社区验收人记录")
        project = self._project(project_no)
        if project.revoked:
            raise DomainError("project_revoked", "网点资格已撤销")

        if status is ObservationStatus.OPEN:
            if project.status is ProjectStatus.NOT_OPENED:
                raise DomainError("not_open_yet", "网点尚未确认开业，不能记录在营抽查")
            if project.status is ProjectStatus.SUSPENDED:
                # 重启：结清最近一段停业期，抽查计数从零重新累计
                project.closures[-1].reopened_at = at
                project.status = ProjectStatus.OPEN
            elif not self._observation_is_due(project, at):
                due_at = self._next_observation_due_for(project)
                raise DomainError(
                    "observation_not_due",
                    f"尚未到下一次抽查时间" + (f"，最早 {due_at}" if due_at else ""),
                )
        else:
            if project.status is ProjectStatus.NOT_OPENED:
                raise DomainError("not_open_yet", "网点尚未开业，不存在停业")
            if project.status is ProjectStatus.SUSPENDED:
                raise DomainError("already_suspended", "网点已处于停业状态，等待重启观察")
            project.closures.append(ClosurePeriod(closed_at=at))
            project.status = ProjectStatus.SUSPENDED

        seq = len(project.observations) + 1
        project.observations.append(Observation(seq=seq, observed_at=at, status=status))
        self._append(
            event_type="SERVICE_OBSERVED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"project_no": project_no, "observed_at": at, "status": status.value, "seq": seq},
        )

    def record_feedback(
        self, actor: Actor, project_no: str, score: int, content_excerpt: str, at: str
    ) -> None:
        self._require(actor, Role.COMMUNITY_ACCEPTOR)
        project = self._project(project_no)
        if not 1 <= score <= 5:
            raise DomainError("score_out_of_range", "反馈评分必须在 1 到 5 之间")
        self._append(
            event_type="FEEDBACK_RECORDED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"project_no": project_no, "score": score, "content_excerpt": content_excerpt},
        )
        project.feedback.append((at, score, content_excerpt))

    # ------------------------------------------------------------ 预算占用

    def reserve_segment(
        self, actor: Actor, project_no: str, segment_seq: int, at: str
    ) -> DisbursementCase:
        """财政复核者在证据门满足时占用预算；总承诺不得超过批次余额。"""
        self._require(actor, Role.FINANCE_REVIEWER)
        project = self._project(project_no)
        existing = project.cases.get(segment_seq)
        if existing is not None:
            if existing.state is CaseState.FROZEN:
                raise DomainError("case_frozen", "该分段已随资格撤销冻结")
            if existing.state in (CaseState.RELEASED, CaseState.RESERVED, CaseState.APPROVED):
                raise DomainError("duplicate_reservation", "该分段已占用或拨付，重复申报不得重复占用预算")
            raise DomainError("case_rejected", "该分段曾被驳回，不能重新占用")
        if project.revoked:
            raise DomainError("project_revoked", "网点资格已撤销")
        contract = self._contract(project.contract_id)
        segment = self._segment(contract, segment_seq)
        round_id = contract["round_id"]
        blocks = self._segment_blocks(project, contract, segment)
        if blocks:
            raise DomainError(
                "gate_not_ready",
                "证据门未满足：" + "；".join(b.detail for b in blocks),
            )
        available = self._available(round_id)
        if segment.amount > available:
            raise DomainError(
                "budget_exceeded",
                f"批次余额不足：需要 {segment.amount}，可用 {available}",
            )

        case_id = f"{project_no}-s{segment_seq}"
        milestone_ref = self._milestone_ref(project, segment)
        self._append(
            event_type="PAYMENT_RESERVED",
            aggregate_type="disbursement_case",
            aggregate_id=case_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "amount": str(segment.amount),
                "milestone_ref": milestone_ref,
                "segment_seq": segment_seq,
                "project_no": project_no,
            },
        )
        case = DisbursementCase(
            case_id=case_id,
            round_id=round_id,
            project_no=project_no,
            segment_seq=segment_seq,
            amount=segment.amount,
            state=CaseState.RESERVED,
            reserved_at=at,
        )
        project.cases[segment_seq] = case
        self.rounds[round_id]["committed"] += segment.amount
        return case

    @staticmethod
    def _milestone_ref(project: Project, segment: Segment) -> str:
        if segment.gate is Gate.ENGINEERING:
            return project.accepted_ref[EvidenceKind.ENGINEERING_ACCEPTANCE]
        if segment.gate is Gate.OPENING:
            return project.accepted_ref[EvidenceKind.OPENING_CERTIFICATE]
        return f"observation:{len(project.observations)}"

    # ------------------------------------------------------------ 批准放款

    def approve_payment(self, actor: Actor, project_no: str, segment_seq: int, at: str) -> None:
        self._require(actor, Role.APPROVER)
        project = self._project(project_no)
        contract = self._contract(project.contract_id)
        if actor.id == contract["operator_id"]:
            raise DomainError("self_approval_forbidden", "执行人不得批准本方拨付")
        case = self._open_case(project, segment_seq)
        if case.state is not CaseState.RESERVED:
            raise DomainError("case_not_reserved", "只有已占用预算的案件可以批准")
        self._append(
            event_type="PAYMENT_APPROVED",
            aggregate_type="disbursement_case",
            aggregate_id=case.case_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"case_id": case.case_id, "amount": str(case.amount)},
        )
        case.state = CaseState.APPROVED
        case.approved_at = at

    def release_payment(self, actor: Actor, project_no: str, segment_seq: int, at: str) -> None:
        self._require(actor, Role.FINANCE_REVIEWER)
        project = self._project(project_no)
        case = self._open_case(project, segment_seq)
        if case.state is not CaseState.APPROVED:
            raise DomainError("case_not_approved", "只有已批准的案件可以放款")
        round_ = self._round_snapshot(case.round_id)
        self._append(
            event_type="PAYMENT_RELEASED",
            aggregate_type="disbursement_case",
            aggregate_id=case.case_id,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"case_id": case.case_id, "amount": str(case.amount)},
        )
        case.state = CaseState.RELEASED
        case.released_at = at
        round_["committed"] -= case.amount
        round_["released"] += case.amount

    def reject_payment(
        self, actor: Actor, project_no: str, segment_seq: int, reason: str, at: str
    ) -> None:
        self._require(actor, Role.APPROVER)
        project = self._project(project_no)
        contract = self._contract(project.contract_id)
        if actor.id == contract["operator_id"]:
            raise DomainError("self_approval_forbidden", "执行人不得批准本方拨付")
        case = self._open_case(project, segment_seq)
        if case.state not in (CaseState.RESERVED, CaseState.APPROVED):
            raise DomainError("case_not_actionable", "当前状态不能驳回")
        self._uncommit(case, at, actor, "PAYMENT_REJECTED", reason=reason)
        case.state = CaseState.REJECTED
        case.reject_reason = reason

    def _open_case(self, project: Project, segment_seq: int) -> DisbursementCase:
        case = project.cases.get(segment_seq)
        if case is None:
            raise DomainError("case_not_found", f"第 {segment_seq} 分段尚未占用预算")
        return case

    def _uncommit(
        self, case: DisbursementCase, at: str, actor: Actor, event_type: str, reason: str | None = None
    ) -> None:
        round_ = self._round_snapshot(case.round_id)
        payload: dict[str, Any] = {"case_ids": [case.case_id], "amount": str(case.amount)}
        self._append(
            event_type="BUDGET_UNCOMMITTED",
            aggregate_type="funding_round",
            aggregate_id=case.round_id,
            occurred_at=at,
            actor_id="system",
            actor_role=Role.SYSTEM.value,
            payload=payload,
        )
        round_["committed"] -= case.amount
        if event_type == "PAYMENT_REJECTED":
            self._append(
                event_type=event_type,
                aggregate_type="disbursement_case",
                aggregate_id=case.case_id,
                occurred_at=at,
                actor_id=actor.id,
                actor_role=actor.role.value,
                payload={"case_id": case.case_id, "reason": reason or ""},
            )

    # ------------------------------------------------------------ 资格撤销

    def revoke_eligibility(
        self, actor: Actor, project_no: str, reason: str, at: str
    ) -> dict[str, Any]:
        """撤销网点资格：冻结全部未付分段（退回预算占用），已付金额立账核查。"""
        self._require(actor, Role.APPROVER)
        project = self._project(project_no)
        if project.revoked:
            raise DomainError("already_revoked", "网点资格已撤销")
        unpaid: list[DisbursementCase] = []
        paid: list[DisbursementCase] = []
        for case in sorted(project.cases.values(), key=lambda c: c.segment_seq):
            if case.state in (CaseState.RESERVED, CaseState.APPROVED):
                unpaid.append(case)
            elif case.state is CaseState.RELEASED:
                paid.append(case)

        self._append(
            event_type="ELIGIBILITY_REVOKED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={"project_no": project_no, "effective_at": at, "reason": reason},
        )
        project.revoked = True
        project.revoked_at = at
        project.revoke_reason = reason

        if unpaid:
            frozen_amount = sum((c.amount for c in unpaid), Decimal(0))
            self._append(
                event_type="BUDGET_UNCOMMITTED",
                aggregate_type="funding_round",
                aggregate_id=unpaid[0].round_id,
                occurred_at=at,
                actor_id="system",
                actor_role=Role.SYSTEM.value,
                payload={
                    "case_ids": [c.case_id for c in unpaid],
                    "amount": str(frozen_amount),
                },
            )
            self.rounds[unpaid[0].round_id]["committed"] -= frozen_amount
            self._append(
                event_type="UNPAID_FROZEN",
                aggregate_type="milestone_proof",
                aggregate_id=project_no,
                occurred_at=at,
                actor_id=actor.id,
                actor_role=actor.role.value,
                payload={"project_no": project_no, "case_ids": [c.case_id for c in unpaid]},
            )
            for case in unpaid:
                case.state = CaseState.FROZEN

        paid_total = sum((c.amount for c in paid), Decimal(0))
        self._append(
            event_type="PAID_AUDIT_OPENED",
            aggregate_type="milestone_proof",
            aggregate_id=project_no,
            occurred_at=at,
            actor_id=actor.id,
            actor_role=actor.role.value,
            payload={
                "project_no": project_no,
                "paid_total": str(paid_total),
                "case_ids": [c.case_id for c in paid],
            },
        )
        return {
            "frozen_case_ids": [c.case_id for c in unpaid],
            "frozen_amount": frozen_amount if unpaid else Decimal(0),
            "audit_case_ids": [c.case_id for c in paid],
            "paid_total": paid_total,
        }

    # ------------------------------------------------------- 证据门与卡点

    def _current_streak(self, project: Project) -> list[Observation]:
        """最近一次开业/重启之后的观察序列；任何停业都会开启新一轮。"""
        anchor = project.closures[-1].reopened_at if project.closures else project.opened_at
        if anchor is None:
            return []
        anchor_dt = _dt(anchor)
        return [obs for obs in project.observations if _dt(obs.observed_at) >= anchor_dt]

    def _observation_interval_days(self, project: Project) -> int | None:
        contract = self.contracts[project.contract_id]
        intervals = [
            s.observation_interval_days
            for s in contract["segments"]
            if s.gate is Gate.SUSTAINED and s.observations_due > 0
        ]
        return min(intervals) if intervals else None

    def _observation_is_due(self, project: Project, at: str) -> bool:
        interval = self._observation_interval_days(project)
        if interval is None:
            return True
        streak = self._current_streak(project)
        opens = [o for o in streak if o.status is ObservationStatus.OPEN]
        anchor = opens[-1].observed_at if opens else project.opened_at
        if anchor is None:
            return True
        from datetime import timedelta

        return _dt(at) >= _dt(anchor) + timedelta(days=interval)

    def _next_observation_due_for(self, project: Project) -> str | None:
        interval = self._observation_interval_days(project)
        if interval is None:
            return None
        from datetime import timedelta

        streak = self._current_streak(project)
        opens = [o for o in streak if o.status is ObservationStatus.OPEN]
        anchor = opens[-1].observed_at if opens else project.opened_at
        if anchor is None:
            return None
        return (_dt(anchor) + timedelta(days=interval)).isoformat()

    def _open_observations_since_last_start(self, project: Project) -> int:
        """最近一次开业/重启之后的在营抽查次数；停业即清零。"""
        if project.status is not ProjectStatus.OPEN:
            return 0
        return sum(1 for obs in self._current_streak(project) if obs.status is ObservationStatus.OPEN)

    def _gate_blocks(self, project: Project, segment: Segment) -> list[Block]:
        blocks: list[Block] = []
        if segment.gate in (Gate.ENGINEERING, Gate.OPENING, Gate.SUSTAINED):
            if not project.accepted(EvidenceKind.ENGINEERING_ACCEPTANCE):
                blocks.append(
                    Block(
                        "engineering_acceptance",
                        "工程验收材料未经财政复核确认资金依据",
                        Role.FINANCE_REVIEWER,
                    )
                )
        if segment.gate in (Gate.OPENING, Gate.SUSTAINED):
            if not project.accepted(EvidenceKind.OPENING_CERTIFICATE):
                blocks.append(
                    Block(
                        "opening_certificate",
                        "开业凭证未经社区验收人采信",
                        Role.COMMUNITY_ACCEPTOR,
                    )
                )
            elif project.status is ProjectStatus.NOT_OPENED:
                blocks.append(
                    Block(
                        "service_verified",
                        "社区验收人尚未确认服务实际可用",
                        Role.COMMUNITY_ACCEPTOR,
                    )
                )
        if segment.gate is Gate.SUSTAINED:
            done = self._open_observations_since_last_start(project)
            if project.status is ProjectStatus.SUSPENDED:
                blocks.append(
                    Block("service_suspended", "网点处于停业状态，持续营业观察已中断", Role.COMMUNITY_ACCEPTOR)
                )
            elif done < segment.observations_due:
                due_at = self._next_observation_due_for(project)
                blocks.append(
                    Block(
                        "sustained_observation",
                        f"持续营业到期抽查不足：已完成 {done}/{segment.observations_due} 次"
                        + (f"，下一次到期 {due_at}" if due_at else ""),
                        Role.COMMUNITY_ACCEPTOR,
                    )
                )
            if len(project.feedback) < segment.min_feedback:
                blocks.append(
                    Block(
                        "resident_feedback",
                        f"居民反馈不足：{len(project.feedback)}/{segment.min_feedback} 条",
                        Role.COMMUNITY_ACCEPTOR,
                    )
                )
        return blocks

    def _segment_blocks(
        self, project: Project, contract: dict[str, Any], segment: Segment
    ) -> list[Block]:
        blocks: list[Block] = []
        if project.open_reviews():
            review = project.open_reviews()[0]
            blocks.append(
                Block(
                    "review",
                    f"复审 {review.review_id} 未裁决（{review.reason}）",
                    Role.APPROVER,
                )
            )
        for earlier in contract["segments"]:
            if earlier.seq < segment.seq:
                case = project.cases.get(earlier.seq)
                if case is None or case.state is not CaseState.RELEASED:
                    blocks.append(
                        Block(
                            f"segment_{earlier.seq}_unpaid",
                            f"第 {earlier.seq} 分段（{earlier.name}）尚未放款，后段不能占用预算",
                        )
                    )
        blocks.extend(self._gate_blocks(project, segment))
        return blocks

    def money_blocked(self, project_no: str) -> list[SegmentStatus]:
        """说明一笔资金当前卡在哪个证据节点（逐分段）。"""
        project = self._project(project_no)
        contract = self._contract(project.contract_id)
        result: list[SegmentStatus] = []
        for segment in contract["segments"]:
            case = project.cases.get(segment.seq)
            blocks: list[Block] = []
            if project.revoked and case is None:
                blocks.append(Block("eligibility_revoked", "网点资格已撤销", None))
                state = "blocked"
            elif case is None:
                blocks = self._segment_blocks(project, contract, segment)
                state = "awaiting_evidence" if blocks else "ready_to_reserve"
            elif case.state is CaseState.RESERVED:
                state = "awaiting_approval"
                blocks.append(Block("approval", "等待批准人批准（执行人不得批准本方拨付）", Role.APPROVER))
            elif case.state is CaseState.APPROVED:
                state = "awaiting_release"
                blocks.append(Block("release", "等待财政放款", Role.FINANCE_REVIEWER))
            elif case.state is CaseState.FROZEN:
                state = "frozen"
                blocks.append(Block("frozen", "未付部分已冻结，等待已支付金额核查结论", Role.APPROVER))
            elif case.state is CaseState.REJECTED:
                state = "rejected"
                blocks.append(Block("rejected", case.reject_reason or "拨付申请被驳回", None))
            else:
                state = "released"
            result.append(SegmentStatus(segment=segment, state=state, case_id=case.case_id if case else None, blocks=blocks))
        return result

    # ------------------------------------------------------------ 覆盖结果

    def coverage_report(self) -> list[dict[str, Any]]:
        """按社区、服务类别、实际营业状态区分覆盖；计划网点不计入持续服务。"""
        grouped: dict[tuple[str, str], list[Project]] = defaultdict(list)
        for project in self.projects.values():
            grouped[(project.community_id, project.service_category)].append(project)
        for key in self.targets:
            grouped.setdefault(key, [])

        report: list[dict[str, Any]] = []
        for (community_id, category), projects in sorted(grouped.items()):
            versions = self.targets[(community_id, category)]
            original_target = versions[0].planned_outlets if versions else None
            current_target = versions[-1].planned_outlets if versions else None
            outlets = []
            open_count = suspended_count = not_opened_count = sustained_count = revoked_count = 0
            for project in sorted(projects, key=lambda p: p.project_no):
                if project.status is ProjectStatus.OPEN:
                    status = "open"
                elif project.status is ProjectStatus.SUSPENDED:
                    status = "suspended"
                else:
                    status = "not_opened"
                is_sustained = self._is_sustained(project)
                if project.revoked:
                    # 已撤销资格的网点物理状态仍可见，但不计入实际覆盖
                    revoked_count += 1
                elif status == "open":
                    open_count += 1
                    if is_sustained:
                        sustained_count += 1
                elif status == "suspended":
                    suspended_count += 1
                else:
                    not_opened_count += 1
                outlets.append(
                    {
                        "project_no": project.project_no,
                        "operating_status": status,
                        "sustained": is_sustained and not project.revoked,
                        "revoked": project.revoked,
                    }
                )

            def rate(served: int, denominator: int | None) -> str | None:
                if denominator is None:
                    return None
                if denominator == 0:
                    return "0%" if served == 0 else None
                return f"{Decimal(served) * 100 / Decimal(denominator):.1f}%"

            report.append(
                {
                    "community_id": community_id,
                    "service_category": category,
                    "planned_outlets_original": original_target,
                    "planned_outlets_current": current_target,
                    "target_versions": [
                        {"version": v.version, "planned_outlets": v.planned_outlets, "published_at": v.published_at}
                        for v in versions
                    ],
                    "outlets": outlets,
                    "by_operating_status": {
                        "open": open_count,
                        "suspended": suspended_count,
                        "not_opened": not_opened_count,
                        "revoked": revoked_count,
                    },
                    "actually_open": open_count,
                    "sustained_service": sustained_count,
                    "open_coverage_vs_original": rate(open_count, original_target),
                    "sustained_coverage_vs_original": rate(sustained_count, original_target),
                    "open_coverage_vs_current": rate(open_count, current_target),
                }
            )
        return report

    def _is_sustained(self, project: Project) -> bool:
        contract = self.contracts[project.contract_id]
        sustained_segments = [s for s in contract["segments"] if s.gate is Gate.SUSTAINED]
        if not sustained_segments:
            return False
        segment = max(sustained_segments, key=lambda s: s.observations_due)
        if project.status is not ProjectStatus.OPEN:
            return False
        if self._open_observations_since_last_start(project) < segment.observations_due:
            return False
        return len(project.feedback) >= segment.min_feedback
