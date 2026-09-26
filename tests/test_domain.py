"""试点履约拨付领域规则测试。"""

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pilot_funding.contracts import validate_event
from pilot_funding.events import EventLog
from pilot_funding.model import (
    Actor,
    CaseState,
    EvidenceKind,
    Gate,
    ObservationStatus,
    ReviewOutcome,
    Role,
    Segment,
)
from pilot_funding.service import FundingService

T = "2026-{:02d}-{:02d}T09:00:00+08:00"

SYS = Actor("sys", Role.SYSTEM)
OP1 = Actor("op-1", Role.OPERATOR)
OP2 = Actor("op-2", Role.OPERATOR)
FIN = Actor("fin-1", Role.FINANCE_REVIEWER)
ACC = Actor("acc-1", Role.COMMUNITY_ACCEPTOR)
APR = Actor("apr-1", Role.APPROVER)
# 批准人账号恰好等于运营方账号：用于验证"不得批准本方拨付"
OPERATOR_AS_APPROVER = Actor("op-1", Role.APPROVER)


def standard_segments(engineering="300", opening="300", sustained="400"):
    return [
        Segment(1, "工程节点", Decimal(engineering), Gate.ENGINEERING),
        Segment(2, "开业节点", Decimal(opening), Gate.OPENING),
        Segment(3, "持续营业节点", Decimal(sustained), Gate.SUSTAINED,
                observations_due=2, min_feedback=1, observation_interval_days=30),
    ]


class FundingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.log = EventLog()
        self.svc = FundingService(self.log)
        self.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        self.svc.open_round(SYS, "round-1", "1000", T.format(1, 1))

    def assert_raises_code(self, code: str, func, *args, **kwargs):  # noqa: ANN001
        with self.assertRaises(Exception) as caught:
            func(*args, **kwargs)
        self.assertEqual(code, getattr(caught.exception, "code", None))
        return caught.exception

    def assert_all_events_valid(self) -> None:
        for event in self.log.events:
            self.assertEqual([], validate_event(event, self.schema), event)

    def register(self, contract_id="ct-1", operator=OP1, category="早餐", segments=None):
        self.svc.register_contract(
            SYS, contract_id, operator.id, category, "round-1",
            segments or standard_segments(), T.format(1, 3),
        )

    def link(self, project_no, contract_id="ct-1", community="c-1", operator=OP1):
        self.svc.link_project(operator, project_no, contract_id, community, T.format(1, 4))

    def engineering_gate(self, project_no, proof="eng-1", operator=OP1):
        self.svc.submit_milestone(operator, project_no, EvidenceKind.ENGINEERING_ACCEPTANCE,
                                  proof, 1, T.format(2, 1))
        self.svc.accept_evidence(FIN, project_no, EvidenceKind.ENGINEERING_ACCEPTANCE,
                                 proof, T.format(2, 2))

    def pay_segment(self, project_no, seq, day_pair=(2, 3)):
        d1, d2 = day_pair
        self.svc.reserve_segment(FIN, project_no, seq, T.format(d1, d2))
        self.svc.approve_payment(APR, project_no, seq, T.format(d1, d2 + 1))
        self.svc.release_payment(FIN, project_no, seq, T.format(d1, d2 + 2))

    def opening_gate(self, project_no, proof="open-1", operator=OP1, month=3):
        self.svc.submit_milestone(operator, project_no, EvidenceKind.OPENING_CERTIFICATE,
                                  proof, 1, T.format(month, 1))
        self.svc.accept_evidence(ACC, project_no, EvidenceKind.OPENING_CERTIFICATE,
                                 proof, T.format(month, 2))
        self.svc.verify_service_open(ACC, project_no, T.format(month, 3))


class RoleSeparationTests(FundingTestBase):
    def test_operator_submits_but_cannot_confirm_or_pay(self):
        self.register()
        self.link("p-1")
        self.svc.submit_milestone(OP1, "p-1", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                  "eng-1", 1, T.format(2, 1))
        # 财政复核者才能确认工程资金依据
        self.assert_raises_code("forbidden_role",
                                self.svc.accept_evidence, OP1, "p-1",
                                EvidenceKind.ENGINEERING_ACCEPTANCE, "eng-1", T.format(2, 2))
        # 财政复核者只能确认资金依据，不能确认开业凭证
        self.svc.submit_milestone(OP1, "p-1", EvidenceKind.OPENING_CERTIFICATE,
                                  "open-1", 1, T.format(2, 3))
        self.assert_raises_code("forbidden_role",
                                self.svc.accept_evidence, FIN, "p-1",
                                EvidenceKind.OPENING_CERTIFICATE, "open-1", T.format(2, 4))
        # 社区验收人不能占用预算
        self.svc.accept_evidence(FIN, "p-1", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                 "eng-1", T.format(2, 5))
        self.assert_raises_code("forbidden_role",
                                self.svc.reserve_segment, ACC, "p-1", 1, T.format(2, 6))

    def test_operator_cannot_approve_own_payment_even_with_approver_role(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.svc.reserve_segment(FIN, "p-1", 1, T.format(2, 3))
        self.assert_raises_code("self_approval_forbidden",
                                self.svc.approve_payment, OPERATOR_AS_APPROVER, "p-1", 1,
                                T.format(2, 4))

    def test_operator_cannot_link_or_submit_for_other_operators_contract(self):
        self.register()
        self.assert_raises_code("forbidden_operator",
                                self.svc.link_project, OP2, "p-9", "ct-1", "c-9", T.format(1, 5))


class SegmentedDisbursementTests(FundingTestBase):
    def test_segments_pay_in_order(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        # 第 2 分段在第 1 分段放款前不能占用
        self.svc.submit_milestone(OP1, "p-1", EvidenceKind.OPENING_CERTIFICATE,
                                  "open-1", 1, T.format(2, 4))
        self.svc.accept_evidence(ACC, "p-1", EvidenceKind.OPENING_CERTIFICATE,
                                 "open-1", T.format(2, 5))
        blocked = {s.segment.seq: s for s in self.svc.money_blocked("p-1")}
        self.assertEqual("awaiting_evidence", blocked[2].state)
        self.assertIn("segment_1_unpaid", [b.node for b in blocked[2].blocks])
        self.pay_segment("p-1", 1)
        # 服务尚未确认实际可用，第 2 分段仍卡在社区验收
        self.assertIn("service_verified", [b.node for b in blocked[2].blocks])

    def test_full_segment_flow_reaches_released(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.pay_segment("p-1", 1)
        self.opening_gate("p-1")
        self.pay_segment("p-1", 2, day_pair=(3, 4))
        blocked = {s.segment.seq: s.state for s in self.svc.money_blocked("p-1")}
        self.assertEqual({1: "released", 2: "released", 3: "awaiting_evidence"}, blocked)
        self.assert_all_events_valid()


class DuplicateAndReviewTests(FundingTestBase):
    def test_same_evidence_resubmitted_is_duplicate_claim_without_new_reservation(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1", "eng-1")
        note2 = self.svc.submit_milestone(OP1, "p-1", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                          "eng-1", 1, T.format(2, 10))
        self.assertEqual("duplicate_claim", note2["flag"])
        self.assertIsNone(note2["review_id"])
        self.svc.reserve_segment(FIN, "p-1", 1, T.format(2, 11))
        # 重复申报不得重复占用预算
        self.assert_raises_code("duplicate_reservation",
                                self.svc.reserve_segment, FIN, "p-1", 1, T.format(2, 12))
        self.assertEqual(Decimal("300"), self.svc.round_balance("round-1")["committed"])

    def engineering_gate_note(self, project_no, proof):
        return self.svc.submit_milestone(OP1, project_no, EvidenceKind.ENGINEERING_ACCEPTANCE,
                                         proof, 1, T.format(2, 1))

    def test_same_project_different_evidence_enters_review_and_blocks_money(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1", "eng-1")
        # 同一项目编号提交了不同的验收证据
        note = self.svc.submit_milestone(OP1, "p-1", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                         "eng-2", 1, T.format(3, 1))
        self.assertEqual("evidence_conflict", note["flag"])
        review_id = note["review_id"]
        self.assertIsNotNone(review_id)
        # 复审期间资金不得推进
        self.assert_raises_code("review_open",
                                self.svc.accept_evidence, FIN, "p-1",
                                EvidenceKind.ENGINEERING_ACCEPTANCE, "eng-2", T.format(3, 2))
        status = self.svc.money_blocked("p-1")[0]
        self.assertEqual("review", status.blocks[0].node)
        # 裁决维持原证据：新证据不得采信，资金按原口径继续
        self.svc.resolve_review(APR, "p-1", review_id, ReviewOutcome.UPHOLD_ORIGINAL,
                                T.format(3, 5))
        self.assert_raises_code("evidence_rejected_by_review",
                                self.svc.accept_evidence, FIN, "p-1",
                                EvidenceKind.ENGINEERING_ACCEPTANCE, "eng-2", T.format(3, 6))
        # 已经复审裁定否决的证据不得再次提交立项
        self.assert_raises_code("evidence_rejected_by_review",
                                self.svc.submit_milestone, OP1, "p-1",
                                EvidenceKind.ENGINEERING_ACCEPTANCE, "eng-2", 1, T.format(3, 7))
        self.pay_segment("p-1", 1)

    def test_review_replace_evidence_swaps_accepted_basis(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1", "eng-1")
        note = self.svc.submit_milestone(OP1, "p-1", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                         "eng-2", 1, T.format(3, 1))
        self.svc.resolve_review(APR, "p-1", note["review_id"],
                                ReviewOutcome.REPLACE_EVIDENCE, T.format(3, 5))
        # 裁决替换：新证据成为采信口径，原证据不再是资金依据
        self.assertEqual("eng-2", self.svc.projects["p-1"].accepted_ref[EvidenceKind.ENGINEERING_ACCEPTANCE])
        self.pay_segment("p-1", 1)
        case = self.svc.projects["p-1"].cases[1]
        self.assertEqual("eng-2", self.log.events_for("disbursement_case", case.case_id)[0]["payload"]["milestone_ref"])

    def test_proof_reused_across_projects_triggers_review(self):
        self.register("ct-1", OP1)
        self.register("ct-2", OP2, category="早餐")
        self.link("p-1", "ct-1", "c-1", OP1)
        self.link("p-2", "ct-2", "c-2", OP2)
        self.engineering_gate("p-1", "eng-shared", OP1)
        note = self.svc.submit_milestone(OP2, "p-2", EvidenceKind.ENGINEERING_ACCEPTANCE,
                                         "eng-shared", 1, T.format(2, 5))
        self.assertEqual("proof_reused_across_projects", note["flag"])
        self.assertIsNotNone(note["review_id"])
        # 同一笔改造资金不能在第二个网点再占预算
        self.assert_raises_code("review_open",
                                self.svc.accept_evidence, FIN, "p-2",
                                EvidenceKind.ENGINEERING_ACCEPTANCE, "eng-shared", T.format(2, 6))


class BudgetTests(FundingTestBase):
    def test_parallel_commitments_cannot_exceed_round_balance(self):
        self.register("ct-1", OP1)
        self.register("ct-2", OP2)
        self.link("p-1", "ct-1", "c-1", OP1)
        self.link("p-2", "ct-2", "c-2", OP2)
        self.engineering_gate("p-1", "eng-1", OP1)
        self.engineering_gate("p-2", "eng-2", OP2)
        self.svc.reserve_segment(FIN, "p-1", 1, T.format(2, 3))  # 300
        self.svc.reserve_segment(FIN, "p-2", 1, T.format(2, 4))  # 300 -> 可用 400
        balance = self.svc.round_balance("round-1")
        self.assertEqual(Decimal("600"), balance["committed"])
        self.assertEqual(Decimal("400"), balance["available"])
        # 两个网点第 1 分段先后放款（占用转为已付）
        self.svc.approve_payment(APR, "p-1", 1, T.format(2, 5))
        self.svc.release_payment(FIN, "p-1", 1, T.format(2, 6))
        self.svc.approve_payment(APR, "p-2", 1, T.format(2, 7))
        self.svc.release_payment(FIN, "p-2", 1, T.format(2, 8))  # released 600，可用 400
        # p-1 开业分段先占 300，可用只剩 100
        self.opening_gate("p-1", "open-1", OP1, month=3)
        self.svc.reserve_segment(FIN, "p-1", 2, T.format(3, 4))
        self.assertEqual(Decimal("100"), self.svc.round_balance("round-1")["available"])
        # p-2 并行申领开业分段 300：总承诺将超过批次余额
        self.opening_gate("p-2", "open-2", OP2, month=3)
        self.assert_raises_code("budget_exceeded",
                                self.svc.reserve_segment, FIN, "p-2", 2, T.format(3, 5))
        self.assertEqual(Decimal("300"), self.svc.round_balance("round-1")["committed"])

    def test_rejected_payment_returns_budget(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.svc.reserve_segment(FIN, "p-1", 1, T.format(2, 3))
        self.assertEqual(Decimal("300"), self.svc.round_balance("round-1")["committed"])
        self.svc.reject_payment(APR, "p-1", 1, "材料存疑", T.format(2, 4))
        self.assertEqual(Decimal("0"), self.svc.round_balance("round-1")["committed"])
        self.assertEqual(Decimal("1000"), self.svc.round_balance("round-1")["available"])


class RevocationTests(FundingTestBase):
    def _paid_two_reserved_third(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.pay_segment("p-1", 1)
        self.opening_gate("p-1")
        self.pay_segment("p-1", 2, day_pair=(3, 4))
        # 持续营业分段只占用、未放款
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(4, 5))
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(5, 6))
        self.svc.record_feedback(ACC, "p-1", 4, "满意", T.format(5, 7))
        self.svc.reserve_segment(FIN, "p-1", 3, T.format(5, 8))

    def test_revocation_freezes_only_unpaid_and_audits_paid(self):
        self._paid_two_reserved_third()
        balance_before = self.svc.round_balance("round-1")
        self.assertEqual(Decimal("400"), balance_before["committed"])
        result = self.svc.revoke_eligibility(APR, "p-1", "查实套取补贴", T.format(5, 20))
        self.assertEqual(["p-1-s3"], result["frozen_case_ids"])
        self.assertEqual(Decimal("400"), result["frozen_amount"])
        self.assertEqual(["p-1-s1", "p-1-s2"], result["audit_case_ids"])
        self.assertEqual(Decimal("600"), result["paid_total"])
        # 未付部分的预算占用被释放回批次
        balance = self.svc.round_balance("round-1")
        self.assertEqual(Decimal("0"), balance["committed"])
        self.assertEqual(Decimal("400"), balance["available"])
        case = self.svc.projects["p-1"].cases[3]
        self.assertEqual(CaseState.FROZEN, case.state)
        # 冻结分段不能再占用或放款
        self.assert_raises_code("case_frozen",
                                self.svc.reserve_segment, FIN, "p-1", 3, T.format(5, 21))
        # 撤销后不能再提交材料或记录抽查
        self.assert_raises_code("project_revoked",
                                self.svc.submit_milestone, OP1, "p-1",
                                EvidenceKind.OPENING_CERTIFICATE, "x", 1, T.format(5, 22))
        statuses = {s.segment.seq: s.state for s in self.svc.money_blocked("p-1")}
        self.assertEqual({1: "released", 2: "released", 3: "frozen"}, statuses)
        # 撤销网点不再计入社区覆盖，只在 revoked 中可见
        row = self.svc.coverage_report()[0]
        self.assertEqual(0, row["actually_open"])
        self.assertEqual(0, row["sustained_service"])
        self.assertEqual(1, row["by_operating_status"]["revoked"])
        self.assert_all_events_valid()


class TargetAndObservationTests(FundingTestBase):
    def test_target_revision_keeps_original_caliber(self):
        self.svc.publish_target(SYS, "c-1", "早餐", 10, T.format(1, 5))
        self.svc.revise_target(SYS, "c-1", "早餐", 8, T.format(6, 1), reason="区划调整")
        versions = self.svc.targets[("c-1", "早餐")]
        self.assertEqual([1, 2], [v.version for v in versions])
        self.assertEqual([10, 8], [v.planned_outlets for v in versions])
        report = {r["service_category"]: r for r in self.svc.coverage_report()}
        row = report["早餐"]
        self.assertEqual(10, row["planned_outlets_original"])
        self.assertEqual(8, row["planned_outlets_current"])
        # 没有实际网点时，计划网点不算任何覆盖
        self.assertEqual(0, row["actually_open"])
        self.assertEqual(0, row["sustained_service"])
        self.assertEqual("0.0%", row["open_coverage_vs_original"])

    def test_observation_due_timing_and_restart_resets_streak(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.pay_segment("p-1", 1)
        self.opening_gate("p-1")
        self.pay_segment("p-1", 2, day_pair=(3, 4))
        # 提前抽查不予计账
        self.assert_raises_code("observation_not_due",
                                self.svc.observe, ACC, "p-1", ObservationStatus.OPEN,
                                T.format(3, 10))
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(4, 5))
        # 停业：持续营业计数中断清零
        self.svc.observe(ACC, "p-1", ObservationStatus.CLOSED, T.format(4, 20))
        blocked = {s.segment.seq: s for s in self.svc.money_blocked("p-1")}[3]
        self.assertIn("service_suspended", [b.node for b in blocked.blocks])
        # 重启当次观察作为新起点，不做到期限制；之后继续按 30 天到期抽查
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(5, 10))
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(6, 10))
        self.svc.record_feedback(ACC, "p-1", 5, "重新开业后稳定", T.format(6, 11))
        # 两次在营抽查均来自重启之后，持续营业门满足
        self.svc.reserve_segment(FIN, "p-1", 3, T.format(6, 12))
        self.assertEqual(T.format(5, 10),
                         self.svc.projects["p-1"].closures[0].reopened_at)

    def test_coverage_distinguishes_community_category_and_status(self):
        self.register("ct-1", OP1, "早餐")
        self.register("ct-2", OP2, "修鞋")
        self.link("p-a", "ct-1", "c-1", OP1)
        self.link("p-b", "ct-2", "c-1", OP2)
        self.link("p-c", "ct-1", "c-2", OP1)
        self.svc.publish_target(SYS, "c-1", "早餐", 2, T.format(1, 5))
        # p-a：开业但未达持续营业；p-b：停业挂起；p-c：从未开业
        self.engineering_gate("p-a", "eng-a", OP1)
        self.pay_segment("p-a", 1)
        self.opening_gate("p-a", "open-a", OP1)
        self.engineering_gate("p-b", "eng-b", OP2)
        self.pay_segment("p-b", 1, day_pair=(2, 13))
        self.svc.submit_milestone(OP2, "p-b", EvidenceKind.OPENING_CERTIFICATE,
                                  "open-b", 1, T.format(3, 1))
        self.svc.accept_evidence(ACC, "p-b", EvidenceKind.OPENING_CERTIFICATE,
                                 "open-b", T.format(3, 2))
        self.svc.verify_service_open(ACC, "p-b", T.format(3, 3))
        self.svc.observe(ACC, "p-b", ObservationStatus.CLOSED, T.format(4, 1))
        rows = {(r["community_id"], r["service_category"]): r for r in self.svc.coverage_report()}
        breakfast_c1 = rows[("c-1", "早餐")]
        self.assertEqual(1, breakfast_c1["actually_open"])
        self.assertEqual(0, breakfast_c1["sustained_service"])  # 计划/在营但未持续
        self.assertEqual({"open": 1, "suspended": 0, "not_opened": 0, "revoked": 0},
                         breakfast_c1["by_operating_status"])
        shoe_c1 = rows[("c-1", "修鞋")]
        self.assertEqual({"open": 0, "suspended": 1, "not_opened": 0, "revoked": 0},
                         shoe_c1["by_operating_status"])
        self.assertEqual(0, shoe_c1["sustained_service"])
        c2 = rows[("c-2", "早餐")]
        self.assertEqual({"open": 0, "suspended": 0, "not_opened": 1, "revoked": 0},
                         c2["by_operating_status"])
        self.assert_all_events_valid()

    def test_sustained_requires_observations_and_feedback(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.pay_segment("p-1", 1)
        self.opening_gate("p-1")
        self.pay_segment("p-1", 2, day_pair=(3, 4))
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(4, 5))
        self.svc.observe(ACC, "p-1", ObservationStatus.OPEN, T.format(5, 6))
        # 仅有抽查、无居民反馈：持续服务不成立
        row = self.svc.coverage_report()[0]
        self.assertEqual(1, row["actually_open"])
        self.assertEqual(0, row["sustained_service"])
        self.svc.record_feedback(ACC, "p-1", 5, "方便", T.format(5, 7))
        row = self.svc.coverage_report()[0]
        self.assertEqual(1, row["sustained_service"])


class EventEnvelopeTests(FundingTestBase):
    def test_versions_increment_per_aggregate(self):
        self.register()
        self.link("p-1")
        contract_events = self.log.events_for("service_contract", "ct-1")
        self.assertEqual([1, 2], [e["version"] for e in contract_events])
        self.assertEqual(1, self.log.events_for("funding_round", "round-1")[0]["version"])

    def test_full_flow_events_all_valid(self):
        self.register()
        self.link("p-1")
        self.engineering_gate("p-1")
        self.pay_segment("p-1", 1)
        self.opening_gate("p-1")
        self.assert_all_events_valid()


if __name__ == "__main__":
    unittest.main()
