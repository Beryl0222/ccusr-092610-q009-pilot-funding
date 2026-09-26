import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pilot_funding.domain import (
    CASE_OPEN,
    CASE_PAID,
    CASE_READY,
    CASE_REJECTED,
    CASE_RESERVED,
    CASE_REVIEW_FROZEN,
    CASE_REVOKED_FROZEN,
    OUTLET_OPEN,
    OUTLET_PLANNED,
    OUTLET_SUSPENDED,
    DomainError,
    Ledger,
)
from pilot_funding.reports import coverage_report, due_checks, fund_position
from pilot_funding.service import (
    ROLE_ACCEPTOR,
    ROLE_ADMIN,
    ROLE_EXECUTOR,
    ROLE_FISCAL,
    DisbursementService,
)

T0 = "2026-09-01T09:00:00+08:00"
T1 = "2026-09-05T09:00:00+08:00"
T2 = "2026-09-10T09:00:00+08:00"
T3 = "2026-09-15T09:00:00+08:00"
T4 = "2026-09-20T09:00:00+08:00"

SEGMENTS = [
    {"code": "renovation", "amount": "300000"},
    {"code": "opening", "amount": "200000"},
]


def make_service(round_total="1000000") -> DisbursementService:
    svc = DisbursementService()
    svc.open_round("round-1", round_total, role=ROLE_ADMIN, at=T0)
    svc.set_target("community-a", "2026-2028", "coverage", 10, role=ROLE_ADMIN, at=T0)
    svc.sign_contract(
        "contract-1", "round-1", "PRJ-001", "op-1", SEGMENTS, role=ROLE_ADMIN, at=T0
    )
    svc.register_outlet(
        "outlet-1", "contract-1", "community-a", "便民早餐", role=ROLE_ADMIN, at=T0
    )
    return svc


def submit_and_verify(svc: DisbursementService, milestone: str, fingerprint: str) -> str:
    case_id = svc.submit_milestone(
        "contract-1", milestone, f"proof/{milestone}", fingerprint,
        actor="exec-1", role=ROLE_EXECUTOR, at=T1,
    )
    svc.verify_service(case_id, actor="acceptor-1", role=ROLE_ACCEPTOR, at=T2)
    return case_id


class FlowTests(unittest.TestCase):
    def test_segmented_disbursement_flow(self) -> None:
        svc = make_service()
        case_id = submit_and_verify(svc, "renovation", "fp-reno-1")
        svc.reserve_payment(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        svc.mark_paid(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T4)

        round_state = svc.ledger.rounds["round-1"]
        self.assertEqual("0", str(round_state.reserved))
        self.assertEqual("300000", str(round_state.paid))
        self.assertEqual(CASE_PAID, svc.ledger.cases[case_id].status)

        # 第二段（开业补贴）独立走同一流程，互不影响
        case2 = submit_and_verify(svc, "opening", "fp-open-1")
        svc.reserve_payment(case2, actor="fiscal-1", role=ROLE_FISCAL, at=T4)
        self.assertEqual("200000", str(svc.ledger.rounds["round-1"].reserved))

    def test_fund_position_names_evidence_node(self) -> None:
        svc = make_service()
        case_id = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno", "fp-reno-1",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        self.assertEqual(
            "awaiting_service_verification", fund_position(svc.ledger, case_id)["node"]
        )
        svc.verify_service(case_id, actor="acceptor-1", role=ROLE_ACCEPTOR, at=T2)
        self.assertEqual(
            "awaiting_funding_basis", fund_position(svc.ledger, case_id)["node"]
        )
        svc.reserve_payment(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        self.assertEqual("awaiting_payment", fund_position(svc.ledger, case_id)["node"])
        svc.mark_paid(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T4)
        self.assertEqual("settled", fund_position(svc.ledger, case_id)["node"])


class RoleTests(unittest.TestCase):
    def test_executor_cannot_approve_own_disbursement(self) -> None:
        svc = make_service()
        case_id = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno", "fp-reno-1",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        with self.assertRaises(DomainError) as ctx:
            svc.verify_service(case_id, actor="exec-1", role=ROLE_ACCEPTOR, at=T2)
        self.assertEqual("self_approval", ctx.exception.code)

        svc.verify_service(case_id, actor="acceptor-1", role=ROLE_ACCEPTOR, at=T2)
        with self.assertRaises(DomainError) as ctx:
            svc.reserve_payment(case_id, actor="exec-1", role=ROLE_FISCAL, at=T3)
        self.assertEqual("self_approval", ctx.exception.code)

    def test_roles_are_separated(self) -> None:
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            svc.submit_milestone(
                "contract-1", "renovation", "p", "fp",
                actor="fiscal-1", role=ROLE_FISCAL, at=T1,
            )
        self.assertEqual("role_not_allowed", ctx.exception.code)

        case_id = svc.submit_milestone(
            "contract-1", "renovation", "p", "fp",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        with self.assertRaises(DomainError) as ctx:
            svc.verify_service(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T2)
        self.assertEqual("role_not_allowed", ctx.exception.code)

        svc.verify_service(case_id, actor="acceptor-1", role=ROLE_ACCEPTOR, at=T2)
        with self.assertRaises(DomainError) as ctx:
            svc.reserve_payment(case_id, actor="acceptor-1", role=ROLE_ACCEPTOR, at=T3)
        self.assertEqual("role_not_allowed", ctx.exception.code)


class DuplicateClaimTests(unittest.TestCase):
    def test_same_evidence_is_idempotent_and_holds_no_extra_budget(self) -> None:
        svc = make_service()
        first = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno", "fp-reno-1",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        second = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno", "fp-reno-1",
            actor="exec-1", role=ROLE_EXECUTOR, at=T2,
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(svc.ledger.cases))
        self.assertEqual("0", str(svc.ledger.rounds["round-1"].committed))

    def test_different_evidence_enters_review_and_freezes(self) -> None:
        svc = make_service()
        case_id = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno-a", "fp-reno-A",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        with self.assertRaises(DomainError) as ctx:
            svc.submit_milestone(
                "contract-1", "renovation", "proof/reno-b", "fp-reno-B",
                actor="exec-1", role=ROLE_EXECUTOR, at=T2,
            )
        self.assertEqual("duplicate_claim", ctx.exception.code)
        self.assertEqual(CASE_REVIEW_FROZEN, svc.ledger.cases[case_id].status)
        self.assertTrue(svc.ledger.duplicate_reviews[case_id].open)

        svc.resolve_duplicate(case_id, "cleared", actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        self.assertEqual(CASE_OPEN, svc.ledger.cases[case_id].status)
        self.assertFalse(svc.ledger.duplicate_reviews[case_id].open)

    def test_confirmed_duplicate_is_rejected(self) -> None:
        svc = make_service()
        case_id = svc.submit_milestone(
            "contract-1", "renovation", "proof/reno-a", "fp-reno-A",
            actor="exec-1", role=ROLE_EXECUTOR, at=T1,
        )
        with self.assertRaises(DomainError):
            svc.submit_milestone(
                "contract-1", "renovation", "proof/reno-b", "fp-reno-B",
                actor="exec-1", role=ROLE_EXECUTOR, at=T2,
            )
        svc.resolve_duplicate(
            case_id, "confirmed_duplicate", actor="fiscal-1", role=ROLE_FISCAL, at=T3
        )
        self.assertEqual(CASE_REJECTED, svc.ledger.cases[case_id].status)
        self.assertEqual("0", str(svc.ledger.rounds["round-1"].committed))


class BudgetTests(unittest.TestCase):
    def test_parallel_claims_never_exceed_round_balance(self) -> None:
        svc = DisbursementService()
        svc.open_round("round-1", "500000", role=ROLE_ADMIN, at=T0)
        for contract, project, outlet, community in (
            ("contract-a", "PRJ-A", "outlet-a", "community-a"),
            ("contract-b", "PRJ-B", "outlet-b", "community-b"),
        ):
            svc.sign_contract(
                contract, "round-1", project, "op-x",
                [{"code": "renovation", "amount": "300000"}], role=ROLE_ADMIN, at=T0,
            )
            svc.register_outlet(outlet, contract, community, "家政服务", role=ROLE_ADMIN, at=T0)
            svc.submit_milestone(
                contract, "renovation", f"proof/{contract}", f"fp-{contract}",
                actor="exec-1", role=ROLE_EXECUTOR, at=T1,
            )
            svc.verify_service(
                f"{contract}:renovation", actor="acceptor-1", role=ROLE_ACCEPTOR, at=T2
            )

        svc.reserve_payment("contract-a:renovation", actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        with self.assertRaises(DomainError) as ctx:
            svc.reserve_payment("contract-b:renovation", actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        self.assertEqual("budget_exceeded", ctx.exception.code)

        svc.release_payment(
            "contract-a:renovation", "计划调整", actor="fiscal-1", role=ROLE_FISCAL, at=T4
        )
        svc.reserve_payment("contract-b:renovation", actor="fiscal-1", role=ROLE_FISCAL, at=T4)
        round_state = svc.ledger.rounds["round-1"]
        self.assertEqual("300000", str(round_state.committed))
        self.assertLessEqual(round_state.committed, round_state.total)


class RevocationTests(unittest.TestCase):
    def test_revoke_freezes_unpaid_and_audits_paid(self) -> None:
        svc = make_service()
        paid_case = submit_and_verify(svc, "renovation", "fp-reno-1")
        svc.reserve_payment(paid_case, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        svc.mark_paid(paid_case, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        open_case = submit_and_verify(svc, "opening", "fp-open-1")
        svc.reserve_payment(open_case, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        self.assertEqual("200000", str(svc.ledger.rounds["round-1"].reserved))

        svc.revoke_outlet(
            "outlet-1", "运营方违约", "2026-09-21T00:00:00+08:00", role=ROLE_ADMIN, at=T4
        )

        # 未付部分冻结并释放占用，已付部分转入核查
        self.assertEqual(CASE_REVOKED_FROZEN, svc.ledger.cases[open_case].status)
        self.assertEqual("0", str(svc.ledger.rounds["round-1"].reserved))
        review = svc.ledger.cases[paid_case].paid_review
        self.assertIsNotNone(review)
        self.assertTrue(review.open)
        self.assertEqual("300000", str(review.amount))
        self.assertEqual(
            "eligibility_revoked", fund_position(svc.ledger, open_case)["node"]
        )
        self.assertEqual("paid_under_review", fund_position(svc.ledger, paid_case)["node"])

        svc.close_paid_review(paid_case, "部分追回", "120000", role=ROLE_FISCAL, at=T4)
        review = svc.ledger.cases[paid_case].paid_review
        self.assertFalse(review.open)
        self.assertEqual("120000", str(review.clawback_amount))

        # 撤销后不得再提交新材料
        with self.assertRaises(DomainError) as ctx:
            svc.submit_milestone(
                "contract-1", "opening", "proof/open-2", "fp-open-2",
                actor="exec-1", role=ROLE_EXECUTOR, at=T4,
            )
        self.assertEqual("contract_ineligible", ctx.exception.code)


class TargetAndCoverageTests(unittest.TestCase):
    def test_rebase_keeps_baseline_and_planned_is_not_sustained(self) -> None:
        svc = make_service()
        svc.rebase_target(
            "community-a", "2026-2028", "coverage", 6, "建设条件变化",
            role=ROLE_ADMIN, at=T1,
        )
        report = coverage_report(svc.ledger, as_of=T4)
        target = report["communities"]["community-a"]["targets"][0]
        self.assertEqual(10, target["baseline_count"])  # 原统计口径保留
        self.assertEqual(6, target["current_count"])
        # 计划网点不得算作持续服务
        self.assertEqual(0, report["communities"]["community-a"]["sustained_service"])
        self.assertEqual(1, report["communities"]["community-a"]["planned_not_sustained"])

    def test_observation_makes_outlet_sustained(self) -> None:
        svc = make_service()
        svc.record_observation(
            "outlet-1", True, T2, "2026-12-01T00:00:00+08:00",
            role=ROLE_ACCEPTOR, at=T2,
        )
        report = coverage_report(svc.ledger, as_of=T4)
        community = report["communities"]["community-a"]
        self.assertEqual(1, community["sustained_service"])
        self.assertEqual(1, community["status_counts"][OUTLET_OPEN])
        self.assertEqual(0, community["status_counts"][OUTLET_PLANNED])

    def test_restart_continues_due_checks(self) -> None:
        svc = make_service()
        svc.record_observation(
            "outlet-1", True, T1, "2026-10-01T00:00:00+08:00", role=ROLE_ACCEPTOR, at=T1
        )
        self.assertEqual([], due_checks(svc.ledger, "2026-09-30T00:00:00+08:00"))
        self.assertEqual(1, len(due_checks(svc.ledger, "2026-10-02T00:00:00+08:00")))

        # 停业观察后按新的到期日计算
        svc.record_observation(
            "outlet-1", False, T2, "2026-11-01T00:00:00+08:00", role=ROLE_ACCEPTOR, at=T2
        )
        self.assertEqual(OUTLET_SUSPENDED, svc.ledger.outlets["outlet-1"].status)
        self.assertEqual([], due_checks(svc.ledger, "2026-10-15T00:00:00+08:00"))

        # 服务重启后继续到期抽查，不会漏查
        svc.record_observation(
            "outlet-1", True, T3, "2026-12-01T00:00:00+08:00", role=ROLE_ACCEPTOR, at=T3
        )
        self.assertEqual(OUTLET_OPEN, svc.ledger.outlets["outlet-1"].status)
        due = due_checks(svc.ledger, "2026-12-02T00:00:00+08:00")
        self.assertEqual(["outlet-1"], [row["outlet_id"] for row in due])

    def test_coverage_groups_by_community_category_and_status(self) -> None:
        svc = make_service()
        svc.sign_contract(
            "contract-2", "round-1", "PRJ-002", "op-2",
            [{"code": "renovation", "amount": "100000"}], role=ROLE_ADMIN, at=T0,
        )
        svc.register_outlet(
            "outlet-2", "contract-2", "community-b", "家政服务", role=ROLE_ADMIN, at=T0
        )
        svc.record_observation(
            "outlet-1", True, T2, "2026-12-01T00:00:00+08:00",
            role=ROLE_ACCEPTOR, at=T2,
        )
        svc.record_feedback(
            "outlet-1", "热线", "早餐供应稳定", "positive", role=ROLE_EXECUTOR, at=T3
        )
        svc.record_feedback(
            "outlet-1", "走访", "希望延长营业", "neutral", role=ROLE_ACCEPTOR, at=T3
        )

        report = coverage_report(svc.ledger, as_of=T4)
        rows = {(r["community_id"], r["service_category"], r["status"]): r["count"] for r in report["rows"]}
        self.assertEqual(1, rows[("community-a", "便民早餐", OUTLET_OPEN)])
        self.assertEqual(1, rows[("community-b", "家政服务", OUTLET_PLANNED)])
        feedback = report["feedback_by_outlet"]["outlet-1"]
        self.assertEqual(2, feedback["total"])
        self.assertEqual({"positive": 1, "neutral": 1}, feedback["by_sentiment"])


class StoreTests(unittest.TestCase):
    def test_event_id_is_idempotent(self) -> None:
        svc = make_service()
        event = svc.ledger.store.events()[0].to_dict()
        ledger = Ledger()
        ledger.append(event)
        ledger.append(dict(event, payload=dict(event["payload"])))
        self.assertEqual(1, len(ledger.store.events()))

    def test_version_conflict_is_rejected(self) -> None:
        svc = make_service()
        event = svc.ledger.store.events()[0].to_dict()
        with self.assertRaises(DomainError) as ctx:
            svc.ledger.append(dict(event, event_id="ev-x", version=3))
        self.assertEqual("version_conflict", ctx.exception.code)

    def test_replay_restores_state(self) -> None:
        svc = make_service()
        case_id = submit_and_verify(svc, "renovation", "fp-reno-1")
        svc.reserve_payment(case_id, actor="fiscal-1", role=ROLE_FISCAL, at=T3)
        svc.revoke_outlet(
            "outlet-1", "运营方违约", "2026-09-21T00:00:00+08:00", role=ROLE_ADMIN, at=T4
        )
        replayed = Ledger.replay(e.to_dict() for e in svc.ledger.store.events())
        self.assertEqual(
            svc.ledger.rounds["round-1"].reserved, replayed.rounds["round-1"].reserved
        )
        self.assertEqual(
            svc.ledger.cases[case_id].status, replayed.cases[case_id].status
        )
        self.assertEqual(
            coverage_report(svc.ledger, as_of=T4)["rows"],
            coverage_report(replayed, as_of=T4)["rows"],
        )


if __name__ == "__main__":
    unittest.main()
