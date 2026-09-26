"""履约拨付应用服务：角色分权、预算不变量与流程推进。

所有写操作都经由本服务产生事件并追加到账本（`Ledger`），
保证：执行人不得批准本方拨付、财政复核只管资金依据、社区验收
确认服务可用、重复申报不重复占用预算、并行申领不超出批次余额。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .domain import (
    CASE_OPEN,
    CASE_READY,
    CASE_RESERVED,
    DomainError,
    Ledger,
    money,
)

# 角色
ROLE_EXECUTOR = "executor"  # 项目执行人：提交材料，不得批准本方拨付
ROLE_FISCAL = "fiscal_reviewer"  # 财政复核者：只确认资金依据
ROLE_ACCEPTOR = "community_acceptor"  # 社区验收人：确认服务实际可用
ROLE_ADMIN = "program_admin"  # 项目管理方：批次、目标、合同、资格

# 重复申报复审结论
RESOLUTION_CLEARED = "cleared"  # 证据澄清，解除冻结
RESOLUTION_CONFIRMED = "confirmed_duplicate"  # 确认重复，驳回案件


def _require_role(role: str, allowed: Sequence[str], action: str) -> None:
    if role not in allowed:
        raise DomainError(
            "role_not_allowed",
            f"{action} 需要角色 {'/'.join(allowed)}，实际为 {role}",
        )


class DisbursementService:
    """试点履约拨付系统的应用服务入口。"""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger or Ledger()
        self._seq = 0

    # ---- 事件构造 ----

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> str:
        self._seq += 1
        stream = self.ledger.store.stream(aggregate_type, aggregate_id)
        event = {
            "event_id": f"ev-{self._seq:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": len(stream) + 1,
            "payload": dict(payload),
        }
        return self.ledger.append(event).event_id

    @staticmethod
    def _target_key(community_id: str, period: str, metric: str) -> str:
        return f"{community_id}:{period}:{metric}"

    @staticmethod
    def case_key(contract_id: str, milestone_code: str) -> str:
        """拨付案件标识：同一合同的同一节点只会有一个案件。"""
        return f"{contract_id}:{milestone_code}"

    # ---- 资金批次与社区目标 ----

    def open_round(self, round_id: str, total_amount: Any, *, role: str, at: str) -> str:
        _require_role(role, [ROLE_ADMIN], "开设资金批次")
        if round_id in self.ledger.rounds:
            raise DomainError("duplicate_round", f"资金批次 {round_id} 已存在")
        return self._emit(
            "ROUND_OPENED", "funding_round", round_id, at,
            {"total_amount": str(money(total_amount))},
        )

    def set_target(
        self,
        community_id: str,
        period: str,
        metric: str,
        planned_count: int,
        *,
        role: str,
        at: str,
    ) -> str:
        _require_role(role, [ROLE_ADMIN], "设定社区目标")
        key = self._target_key(community_id, period, metric)
        if key in self.ledger.targets:
            raise DomainError("duplicate_target", f"目标 {key} 已存在，请使用改口径调整")
        return self._emit(
            "TARGET_SET", "community_target", key, at,
            {
                "community_id": community_id,
                "period": period,
                "metric": metric,
                "planned_count": planned_count,
            },
        )

    def rebase_target(
        self,
        community_id: str,
        period: str,
        metric: str,
        planned_count: int,
        reason: str,
        *,
        role: str,
        at: str,
    ) -> str:
        """调整试点目标：原统计口径保留在历史版本中，只追加不改写。"""
        _require_role(role, [ROLE_ADMIN], "调整社区目标")
        key = self._target_key(community_id, period, metric)
        if key not in self.ledger.targets:
            raise DomainError("unknown_target", f"目标 {key} 不存在，无法改口径")
        return self._emit(
            "TARGET_REBASED", "community_target", key, at,
            {
                "community_id": community_id,
                "period": period,
                "metric": metric,
                "planned_count": planned_count,
                "reason": reason,
            },
        )

    # ---- 合同与网点 ----

    def sign_contract(
        self,
        contract_id: str,
        round_id: str,
        project_code: str,
        operator_id: str,
        segments: Sequence[Mapping[str, Any]],
        *,
        role: str,
        at: str,
    ) -> str:
        _require_role(role, [ROLE_ADMIN], "签订服务商合同")
        if round_id not in self.ledger.rounds:
            raise DomainError("unknown_round", f"资金批次 {round_id} 不存在")
        if contract_id in self.ledger.contracts:
            raise DomainError("duplicate_contract", f"合同 {contract_id} 已存在")
        if any(c.project_code == project_code for c in self.ledger.contracts.values()):
            raise DomainError("duplicate_project", f"项目编号 {project_code} 已被其他合同使用")
        if not segments:
            raise DomainError("segments_required", "合同至少需要一个拨付节点")
        payload_segments = [
            {"code": str(s["code"]), "amount": str(money(s["amount"]))} for s in segments
        ]
        return self._emit(
            "CONTRACT_SIGNED", "service_contract", contract_id, at,
            {
                "round_id": round_id,
                "project_code": project_code,
                "operator_id": operator_id,
                "segments": payload_segments,
            },
        )

    def register_outlet(
        self,
        outlet_id: str,
        contract_id: str,
        community_id: str,
        service_category: str,
        *,
        role: str,
        at: str,
    ) -> str:
        _require_role(role, [ROLE_ADMIN], "登记服务网点")
        if contract_id not in self.ledger.contracts:
            raise DomainError("unknown_contract", f"合同 {contract_id} 不存在")
        if outlet_id in self.ledger.outlets:
            raise DomainError("duplicate_outlet", f"网点 {outlet_id} 已存在")
        contract = self.ledger.contracts[contract_id]
        if contract.outlet_ids:
            raise DomainError(
                "outlet_already_bound",
                f"合同 {contract_id} 已绑定网点 {contract.outlet_ids[0]}，一合同一网点",
            )
        return self._emit(
            "OUTLET_REGISTERED", "service_outlet", outlet_id, at,
            {
                "contract_id": contract_id,
                "community_id": community_id,
                "service_category": service_category,
            },
        )

    # ---- 材料提交与验收 ----

    def submit_milestone(
        self,
        contract_id: str,
        milestone_code: str,
        proof_ref: str,
        verification_fingerprint: str,
        *,
        actor: str,
        role: str,
        at: str,
    ) -> str:
        """提交节点材料。重复申报（同项目同节点）：

        - 验收证据一致：幂等返回已有案件，不重复占用预算；
        - 验收证据不同：冻结已有案件并进入复审，抛出 duplicate_claim。
        """
        _require_role(role, [ROLE_EXECUTOR], "提交节点材料")
        contract = self.ledger.contracts.get(contract_id)
        if contract is None:
            raise DomainError("unknown_contract", f"合同 {contract_id} 不存在")
        for outlet_id in contract.outlet_ids:
            if not self.ledger.outlets[outlet_id].eligible:
                raise DomainError("contract_ineligible", f"合同 {contract_id} 下网点资格已被撤销")
        seq = next(
            (i for i, s in enumerate(contract.segments) if s.code == milestone_code), None
        )
        if seq is None:
            raise DomainError("unknown_milestone", f"合同 {contract_id} 没有节点 {milestone_code}")
        segment = contract.segments[seq]
        case_id = self.case_key(contract_id, milestone_code)
        existing = self.ledger.cases.get(case_id)
        if existing is not None:
            if existing.verification_fingerprint == verification_fingerprint:
                return existing.case_id  # 幂等：同一证据的重复提交不重复占用预算
            self._emit(
                "DUPLICATE_FLAGGED", "disbursement_case", existing.case_id, at,
                {
                    "project_code": existing.project_code,
                    "case_id": existing.case_id,
                    "prior_fingerprint": existing.verification_fingerprint,
                    "incoming_fingerprint": verification_fingerprint,
                },
            )
            raise DomainError(
                "duplicate_claim",
                f"项目 {existing.project_code} 节点 {milestone_code} 已申报，"
                "验收证据不一致，案件已冻结进入复审",
            )
        self._emit(
            "MILESTONE_SUBMITTED", "milestone_proof", case_id, at,
            {
                "contract_revision": contract.revision,
                "proof_ref": proof_ref,
                "project_code": contract.project_code,
                "milestone_code": milestone_code,
                "verification_fingerprint": verification_fingerprint,
                "contract_id": contract_id,
                "segment_seq": seq,
                "amount": str(segment.amount),
                "submitted_by": actor,
            },
        )
        return case_id

    def verify_service(self, case_id: str, *, actor: str, role: str, at: str) -> str:
        """社区验收人确认服务实际可用；提交人不得验收本方案件。"""
        _require_role(role, [ROLE_ACCEPTOR], "确认服务可用")
        case = self.ledger.cases.get(case_id)
        if case is None:
            raise DomainError("unknown_case", f"拨付案件 {case_id} 不存在")
        if case.status != CASE_OPEN:
            raise DomainError("bad_case_state", f"案件 {case_id} 当前状态 {case.status}，不能验收")
        if actor == case.submitted_by:
            raise DomainError("self_approval", "提交人不得验收本方提交的案件")
        contract = self.ledger.contracts[case.contract_id]
        if not contract.outlet_ids:
            raise DomainError("outlet_required", f"合同 {case.contract_id} 尚未登记网点")
        outlet_id = contract.outlet_ids[0]
        return self._emit(
            "SERVICE_VERIFIED", "service_outlet", outlet_id, at,
            {
                "case_id": case_id,
                "verification_fingerprint": case.verification_fingerprint,
                "verified_by": actor,
            },
        )

    # ---- 拨付决定（财政复核） ----

    def _check_fiscal_case(self, case_id: str, actor: str, allowed: Sequence[str]):
        case = self.ledger.cases.get(case_id)
        if case is None:
            raise DomainError("unknown_case", f"拨付案件 {case_id} 不存在")
        if actor == case.submitted_by:
            raise DomainError("self_approval", "提交人不得批准本方提交的拨付")
        if case.status not in allowed:
            raise DomainError("bad_case_state", f"案件 {case_id} 当前状态 {case.status}")
        return case

    def reserve_payment(self, case_id: str, *, actor: str, role: str, at: str) -> str:
        """批准拨付并占用预算；并行申领时承诺加已付不得超过批次总额。"""
        _require_role(role, [ROLE_FISCAL], "批准拨付")
        case = self._check_fiscal_case(case_id, actor, [CASE_READY])
        round_state = self.ledger.rounds[case.round_id]
        if round_state.available < case.amount:
            raise DomainError(
                "budget_exceeded",
                f"批次 {case.round_id} 余额 {round_state.available} 不足，"
                f"案件需占用 {case.amount}",
            )
        return self._emit(
            "PAYMENT_RESERVED", "disbursement_case", case_id, at,
            {
                "amount": str(case.amount),
                "milestone_ref": case.milestone_code,
                "round_id": case.round_id,
                "contract_id": case.contract_id,
                "project_code": case.project_code,
                "decided_by": actor,
            },
        )

    def release_payment(
        self, case_id: str, reason: str, *, actor: str, role: str, at: str
    ) -> str:
        """释放已占用但未支付的预算。"""
        _require_role(role, [ROLE_FISCAL], "释放预算占用")
        case = self._check_fiscal_case(case_id, actor, [CASE_RESERVED])
        return self._emit(
            "PAYMENT_RELEASED", "disbursement_case", case_id, at,
            {
                "amount": str(case.amount),
                "milestone_ref": case.milestone_code,
                "reason": reason,
                "decided_by": actor,
            },
        )

    def reject_payment(
        self, case_id: str, reason: str, *, actor: str, role: str, at: str
    ) -> str:
        """驳回拨付申请（资金依据不成立或复审确认重复）。"""
        _require_role(role, [ROLE_FISCAL], "驳回拨付")
        case = self._check_fiscal_case(case_id, actor, [CASE_OPEN, CASE_READY])
        return self._emit(
            "PAYMENT_REJECTED", "disbursement_case", case_id, at,
            {
                "amount": str(case.amount),
                "milestone_ref": case.milestone_code,
                "reason": reason,
                "decided_by": actor,
            },
        )

    def mark_paid(self, case_id: str, *, actor: str, role: str, at: str) -> str:
        """登记支付：占用转为已付。"""
        _require_role(role, [ROLE_FISCAL], "登记支付")
        case = self._check_fiscal_case(case_id, actor, [CASE_RESERVED])
        return self._emit(
            "PAYMENT_PAID", "disbursement_case", case_id, at,
            {
                "amount": str(case.amount),
                "milestone_ref": case.milestone_code,
                "round_id": case.round_id,
                "decided_by": actor,
            },
        )

    # ---- 重复申报复审 ----

    def resolve_duplicate(
        self, case_id: str, resolution: str, *, actor: str, role: str, at: str
    ) -> str:
        """复审结论：cleared 解除冻结；confirmed_duplicate 驳回案件。"""
        _require_role(role, [ROLE_FISCAL], "复审重复申报")
        review = self.ledger.duplicate_reviews.get(case_id)
        if review is None or not review.open:
            raise DomainError("unknown_review", f"案件 {case_id} 没有待处理的复审")
        if resolution not in (RESOLUTION_CLEARED, RESOLUTION_CONFIRMED):
            raise DomainError("bad_resolution", f"未知的复审结论 {resolution}")
        event_id = self._emit(
            "DUPLICATE_RESOLVED", "disbursement_case", case_id, at,
            {
                "project_code": review.project_code,
                "case_id": case_id,
                "resolution": resolution,
                "resolved_by": actor,
            },
        )
        if resolution == RESOLUTION_CONFIRMED:
            self.reject_payment(case_id, "复审确认重复申报", actor=actor, role=role, at=at)
        return event_id

    # ---- 资格撤销与已付核查 ----

    def revoke_outlet(
        self, outlet_id: str, reason: str, effective_at: str, *, role: str, at: str
    ) -> str:
        """撤销网点资格：只冻结未付部分，已支付金额自动转入核查。"""
        _require_role(role, [ROLE_ADMIN], "撤销网点资格")
        outlet = self.ledger.outlets.get(outlet_id)
        if outlet is None:
            raise DomainError("unknown_outlet", f"网点 {outlet_id} 不存在")
        if not outlet.eligible:
            raise DomainError("already_revoked", f"网点 {outlet_id} 资格已被撤销")
        return self._emit(
            "ELIGIBILITY_REVOKED", "service_outlet", outlet_id, at,
            {"effective_at": effective_at, "reason": reason},
        )

    def close_paid_review(
        self,
        case_id: str,
        outcome: str,
        clawback_amount: Any,
        *,
        role: str,
        at: str,
    ) -> str:
        """关闭已支付金额核查，记录结论与追回金额。"""
        _require_role(role, [ROLE_FISCAL], "关闭已付核查")
        case = self.ledger.cases.get(case_id)
        if case is None or case.paid_review is None or not case.paid_review.open:
            raise DomainError("unknown_paid_review", f"案件 {case_id} 没有进行中的已付核查")
        clawback = money(clawback_amount)
        if clawback < 0 or clawback > case.paid_review.amount:
            raise DomainError("bad_clawback", "追回金额必须介于 0 与已付金额之间")
        return self._emit(
            "PAID_REVIEW_CLOSED", "disbursement_case", case_id, at,
            {"outcome": outcome, "clawback_amount": str(clawback)},
        )

    # ---- 运营观察与居民反馈 ----

    def record_observation(
        self,
        outlet_id: str,
        is_open: bool,
        observed_at: str,
        next_check_due: str,
        *,
        role: str,
        at: str,
    ) -> str:
        """记录持续营业观察；服务重启（is_open 恢复）后按新的到期日继续抽查。"""
        _require_role(role, [ROLE_ACCEPTOR, ROLE_ADMIN], "记录营业观察")
        outlet = self.ledger.outlets.get(outlet_id)
        if outlet is None:
            raise DomainError("unknown_outlet", f"网点 {outlet_id} 不存在")
        if not outlet.eligible:
            raise DomainError("outlet_revoked", f"网点 {outlet_id} 资格已撤销")
        return self._emit(
            "OBSERVATION_RECORDED", "service_outlet", outlet_id, at,
            {
                "observed_at": observed_at,
                "is_open": is_open,
                "next_check_due": next_check_due,
            },
        )

    def record_feedback(
        self,
        outlet_id: str,
        channel: str,
        summary: str,
        sentiment: str,
        *,
        role: str,
        at: str,
    ) -> str:
        _require_role(role, [ROLE_EXECUTOR, ROLE_ACCEPTOR, ROLE_ADMIN], "登记居民反馈")
        if outlet_id not in self.ledger.outlets:
            raise DomainError("unknown_outlet", f"网点 {outlet_id} 不存在")
        return self._emit(
            "FEEDBACK_RECORDED", "service_outlet", outlet_id, at,
            {"channel": channel, "summary": summary, "sentiment": sentiment},
        )
