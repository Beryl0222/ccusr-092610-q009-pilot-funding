"""端到端走查：同一笔改造资金与开业补贴的重复申报、分段拨付与覆盖统计。

运行：PYTHONPATH=src python3 examples/walkthrough.py
"""

from __future__ import annotations

import json

from pilot_funding.domain import DomainError
from pilot_funding.reports import coverage_report, due_checks, fund_position
from pilot_funding.service import (
    ROLE_ACCEPTOR,
    ROLE_ADMIN,
    ROLE_EXECUTOR,
    ROLE_FISCAL,
    DisbursementService,
)

EXEC = ("exec-1", ROLE_EXECUTOR)
ACCEPTOR = ("acceptor-1", ROLE_ACCEPTOR)
FISCAL = ("fiscal-1", ROLE_FISCAL)


def call(svc, action, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        print(f"  [OK] {action}")
    except DomainError as exc:
        print(f"  [拒绝:{exc.code}] {action} —— {exc}")


def main() -> None:
    svc = DisbursementService()
    at = lambda d: f"2026-{d}T09:00:00+08:00"

    print("== 1. 开设批次、设定目标、签订分段合同 ==")
    svc.open_round("round-3", "500000", role=ROLE_ADMIN, at=at("09-01"))
    svc.set_target("community-a", "2026-2028", "coverage", 8, role=ROLE_ADMIN, at=at("09-01"))
    svc.sign_contract(
        "c1", "round-3", "PRJ-100", "op-1",
        [{"code": "renovation", "amount": "300000"}, {"code": "opening", "amount": "200000"}],
        role=ROLE_ADMIN, at=at("09-02"),
    )
    svc.register_outlet("o1", "c1", "community-a", "便民早餐", role=ROLE_ADMIN, at=at("09-02"))

    print("== 2. 执行人提交改造节点材料，验收人确认服务可用 ==")
    case1 = svc.submit_milestone("c1", "renovation", "proof/reno-v1", "fp-A",
                                 actor=EXEC[0], role=ROLE_EXECUTOR, at=at("09-05"))
    call(svc, "执行人试图自行验收（应拒绝）",
         svc.verify_service, case1, actor=EXEC[0], role=ROLE_ACCEPTOR, at=at("09-06"))
    svc.verify_service(case1, actor=ACCEPTOR[0], role=ROLE_ACCEPTOR, at=at("09-06"))

    print("== 3. 财政占用预算并支付改造节点 ==")
    svc.reserve_payment(case1, actor=FISCAL[0], role=ROLE_FISCAL, at=at("09-08"))
    svc.mark_paid(case1, actor=FISCAL[0], role=ROLE_FISCAL, at=at("09-09"))
    print(f"  批次已付/总额：{svc.ledger.rounds['round-3'].paid} / 500000")

    print("== 4. 运营方将同一笔改造资金包装成开业补贴重复申报 ==")
    case2 = svc.submit_milestone("c1", "opening", "proof/open-v1", "fp-B",
                                 actor=EXEC[0], role=ROLE_EXECUTOR, at=at("09-10"))
    call(svc, "相同证据再次提交（幂等，不重复占用）",
         svc.submit_milestone, "c1", "opening", "proof/open-v1", "fp-B",
         actor=EXEC[0], role=ROLE_EXECUTOR, at=at("09-11"))
    call(svc, "不同验收证据重复提交（进复审冻结）",
         svc.submit_milestone, "c1", "opening", "proof/open-v2", "fp-C",
         actor=EXEC[0], role=ROLE_EXECUTOR, at=at("09-12"))
    print(f"  卡点：{fund_position(svc.ledger, case2)['node_label']}")

    print("== 5. 复审澄清后验收、占用，并行申领触发预算上限 ==")
    svc.resolve_duplicate(case2, "cleared", actor=FISCAL[0], role=ROLE_FISCAL, at=at("09-13"))
    svc.verify_service(case2, actor=ACCEPTOR[0], role=ROLE_ACCEPTOR, at=at("09-14"))
    call(svc, "占用开业补贴 20 万（余额仅 20 万，临界通过）",
         svc.reserve_payment, case2, actor=FISCAL[0], role=ROLE_FISCAL, at=at("09-15"))
    svc.sign_contract(
        "c2", "round-3", "PRJ-200", "op-2",
        [{"code": "renovation", "amount": "100000"}], role=ROLE_ADMIN, at=at("09-15"),
    )
    svc.register_outlet("o2", "c2", "community-b", "家政服务", role=ROLE_ADMIN, at=at("09-15"))
    svc.submit_milestone("c2", "renovation", "proof/r2", "fp-D",
                         actor="exec-2", role=ROLE_EXECUTOR, at=at("09-16"))
    svc.verify_service("c2:renovation", actor=ACCEPTOR[0], role=ROLE_ACCEPTOR, at=at("09-17"))
    call(svc, "第二个社区再申领 10 万（超批次余额，应拒绝）",
         svc.reserve_payment, "c2:renovation", actor=FISCAL[0], role=ROLE_FISCAL, at=at("09-18"))

    print("== 6. 撤销网点：冻结未付、已付核查 ==")
    svc.revoke_outlet("o1", "现场发现转包", at("09-20"), role=ROLE_ADMIN, at=at("09-20"))
    print(f"  开业补贴案件：{fund_position(svc.ledger, case2)['node_label']}")
    print(f"  改造资金案件：{fund_position(svc.ledger, case1)['node_label']}")
    svc.close_paid_review(case1, "部分追回", "180000", role=ROLE_FISCAL, at=at("09-25"))

    print("== 7. 目标调整保留原口径；观察、重启与居民反馈 ==")
    svc.rebase_target("community-a", "2026-2028", "coverage", 6, "老旧小区条件限制",
                      role=ROLE_ADMIN, at=at("09-21"))
    svc.record_observation("o2", True, at("10-01"), at("12-01"),
                           role=ROLE_ACCEPTOR, at=at("10-01"))
    svc.record_observation("o2", False, at("11-05"), at("12-15"),
                           role=ROLE_ACCEPTOR, at=at("11-05"))
    svc.record_observation("o2", True, "2026-11-20T09:00:00+08:00",
                           "2027-02-01T09:00:00+08:00",
                           role=ROLE_ACCEPTOR, at=at("11-20"))
    svc.record_feedback("o2", "走访", "重开后服务正常", "positive",
                        role=ROLE_EXECUTOR, at=at("11-21"))

    report = coverage_report(svc.ledger, as_of=at("11-25"))
    print("\n== 覆盖结果（按社区/类别/实际营业状态） ==")
    for row in report["rows"]:
        print(f"  {row['community_id']} | {row['service_category']} | "
              f"{row['status_label']} | {row['count']} 个：{row['outlet_ids']}")
    for cid, data in report["communities"].items():
        targets = ", ".join(
            f"原口径{t['baseline_count']}→现行{t['current_count']}" for t in data["targets"]
        )
        print(f"  {cid}: 持续营业 {data['sustained_service']}，计划未营业 "
              f"{data['planned_not_sustained']}，目标[{targets}]")

    print("\n== 截至 2027-02-02 的到期抽查（服务重启后继续） ==")
    due = due_checks(svc.ledger, "2027-02-02T09:00:00+08:00")
    print("  " + json.dumps(due, ensure_ascii=False))


if __name__ == "__main__":
    main()
