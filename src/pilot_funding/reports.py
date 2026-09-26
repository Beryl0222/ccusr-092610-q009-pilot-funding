"""查询接口：资金卡点说明、到期抽查与覆盖结果。

覆盖结果按社区、服务类别和实际营业状态区分；计划网点只计入
计划口径，不计入持续服务。目标调整保留原统计口径（baseline），
与现行口径（current）并列展示。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import (
    CASE_OPEN,
    CASE_PAID,
    CASE_READY,
    CASE_REJECTED,
    CASE_RELEASED,
    CASE_RESERVED,
    CASE_REVIEW_FROZEN,
    CASE_REVOKED_FROZEN,
    CASE_STATUS_LABELS,
    OUTLET_OPEN,
    OUTLET_PLANNED,
    OUTLET_STATUS_LABELS,
    OUTLET_SUSPENDED,
    DomainError,
    Ledger,
)

# 资金卡点节点编码与说明
NODE_LABELS = {
    "awaiting_service_verification": "材料已提交，待社区验收人确认服务实际可用",
    "awaiting_funding_basis": "服务已验收，待财政复核确认资金依据",
    "awaiting_payment": "拨付已批准并占用预算，待支付执行",
    "duplicate_review": "相同项目编号验收证据不一致，复审中",
    "eligibility_revoked": "网点资格已撤销，未付部分冻结",
    "paid_under_review": "已支付金额核查中",
    "settled": "已支付，流程完结",
    "rejected": "拨付已驳回",
    "released": "预算占用已释放",
}

_CASE_NODE = {
    CASE_OPEN: "awaiting_service_verification",
    CASE_READY: "awaiting_funding_basis",
    CASE_RESERVED: "awaiting_payment",
    CASE_REVIEW_FROZEN: "duplicate_review",
    CASE_REVOKED_FROZEN: "eligibility_revoked",
    CASE_REJECTED: "rejected",
    CASE_RELEASED: "released",
}


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError("timezone_required", f"时间 {value} 必须包含时区")
    return parsed


def fund_position(ledger: Ledger, case_id: str) -> dict[str, Any]:
    """说明一笔资金当前卡在哪个证据/决定节点。"""
    case = ledger.cases.get(case_id)
    if case is None:
        raise DomainError("unknown_case", f"拨付案件 {case_id} 不存在")
    if case.status == CASE_PAID:
        node = "paid_under_review" if (case.paid_review and case.paid_review.open) else "settled"
    else:
        node = _CASE_NODE[case.status]
    position: dict[str, Any] = {
        "case_id": case.case_id,
        "project_code": case.project_code,
        "contract_id": case.contract_id,
        "round_id": case.round_id,
        "milestone_code": case.milestone_code,
        "amount": str(case.amount),
        "status": case.status,
        "status_label": CASE_STATUS_LABELS[case.status],
        "node": node,
        "node_label": NODE_LABELS[node],
        "evidence": {
            "proof_ref": case.proof_ref,
            "verification_fingerprint": case.verification_fingerprint,
            "submitted_by": case.submitted_by,
            "verified_by": case.verified_by,
            "decided_by": case.decided_by,
        },
        "history": list(case.history),
    }
    review = ledger.duplicate_reviews.get(case_id)
    if review is not None:
        position["duplicate_review"] = {
            "open": review.open,
            "prior_fingerprint": review.prior_fingerprint,
            "incoming_fingerprint": review.incoming_fingerprint,
            "resolution": review.resolution,
        }
    if case.paid_review is not None:
        position["paid_review"] = {
            "open": case.paid_review.open,
            "amount": str(case.paid_review.amount),
            "reason": case.paid_review.reason,
            "outcome": case.paid_review.outcome,
            "clawback_amount": str(case.paid_review.clawback_amount),
        }
    return position


def due_checks(ledger: Ledger, as_of: str) -> list[dict[str, Any]]:
    """到期抽查清单：营业中或停业的网点，最近观察的到期日不晚于 as_of。

    服务重启（新的在营观察）会以新的到期日重新进入清单，
    停业期间到期的网点仍保留在清单中，不因重启而漏查。
    """
    moment = _parse_instant(as_of)
    due: list[dict[str, Any]] = []
    for outlet in ledger.outlets.values():
        latest = outlet.latest_observation
        if latest is None or outlet.status not in (OUTLET_OPEN, OUTLET_SUSPENDED):
            continue
        if _parse_instant(latest.next_check_due) <= moment:
            due.append(
                {
                    "outlet_id": outlet.outlet_id,
                    "community_id": outlet.community_id,
                    "service_category": outlet.service_category,
                    "status": outlet.status,
                    "last_observed_at": latest.observed_at,
                    "next_check_due": latest.next_check_due,
                }
            )
    return sorted(due, key=lambda row: (row["next_check_due"], row["outlet_id"]))


def coverage_report(ledger: Ledger, as_of: str | None = None) -> dict[str, Any]:
    """按社区、服务类别和实际营业状态区分的覆盖结果。"""
    if as_of is None:
        events = ledger.store.events()
        as_of = max((e.occurred_at for e in events), default=None)

    rows: dict[tuple[str, str, str], list[str]] = {}
    communities: dict[str, dict[str, Any]] = {}
    for outlet in ledger.outlets.values():
        key = (outlet.community_id, outlet.service_category, outlet.status)
        rows.setdefault(key, []).append(outlet.outlet_id)
        community = communities.setdefault(
            outlet.community_id,
            {
                "status_counts": {},
                "sustained_service": 0,
                "planned_not_sustained": 0,
                "targets": [],
            },
        )
        community["status_counts"][outlet.status] = (
            community["status_counts"].get(outlet.status, 0) + 1
        )
        latest = outlet.latest_observation
        if outlet.status == OUTLET_OPEN and latest is not None and latest.is_open:
            community["sustained_service"] += 1
        if outlet.status == OUTLET_PLANNED:
            community["planned_not_sustained"] += 1

    for target in ledger.targets.values():
        community = communities.setdefault(
            target.community_id,
            {
                "status_counts": {},
                "sustained_service": 0,
                "planned_not_sustained": 0,
                "targets": [],
            },
        )
        community["targets"].append(
            {
                "period": target.period,
                "metric": target.metric,
                "baseline_count": target.baseline_count,  # 原统计口径
                "current_count": target.current_count,  # 现行口径
                "revisions": len(target.revisions),
            }
        )

    feedback_by_outlet = {}
    for outlet in ledger.outlets.values():
        by_sentiment: dict[str, int] = {}
        for item in outlet.feedback:
            sentiment = str(item.get("sentiment", "unknown"))
            by_sentiment[sentiment] = by_sentiment.get(sentiment, 0) + 1
        feedback_by_outlet[outlet.outlet_id] = {
            "total": len(outlet.feedback),
            "by_sentiment": by_sentiment,
        }

    return {
        "as_of": as_of,
        "rows": [
            {
                "community_id": community_id,
                "service_category": category,
                "status": status,
                "status_label": OUTLET_STATUS_LABELS[status],
                "count": len(outlet_ids),
                "outlet_ids": sorted(outlet_ids),
            }
            for (community_id, category, status), outlet_ids in sorted(rows.items())
        ],
        "communities": {
            community_id: {
                **data,
                "status_counts": {
                    status: data["status_counts"].get(status, 0)
                    for status in sorted(OUTLET_STATUS_LABELS)
                },
            }
            for community_id, data in sorted(communities.items())
        },
        "due_checks": due_checks(ledger, as_of) if as_of is not None else [],
        "feedback_by_outlet": feedback_by_outlet,
    }
