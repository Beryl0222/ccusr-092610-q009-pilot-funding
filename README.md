# 社区服务圈试点履约拨付账

第三批30个城市作为一刻钟便民生活圈全域推进先行区，将利用约两年实现试点城市主城区及有条件县城社区覆盖；建设强调因地制宜和补齐服务功能。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/pilot_funding/contracts.py`：事件契约校验（不修改输入）。
- `src/pilot_funding/domain.py`：事件存储与状态投影（资金批次、目标、合同、网点、案件、复审）。
- `src/pilot_funding/service.py`：应用服务（角色分权、分段拨付、重复申报复审、预算不变量、资格撤销与已付核查）。
- `src/pilot_funding/reports.py`：资金卡点查询、到期抽查与覆盖结果。
- `src/pilot_funding/cli.py`：命令行契约校验入口。
- `examples/walkthrough.py`：端到端走查（重复申报、并行申领、撤销冻结、重启抽查）。
- `tests/`：契约、流程、角色、预算、复审、撤销、覆盖与存储测试。
- `docs/domain.md`：领域对象、事件语义与核心不变量。

## 角色分权

- 项目执行人：提交节点材料与居民反馈，不得批准本方拨付。
- 财政复核者：只确认资金依据（占用、释放、驳回、支付、核查关闭）。
- 社区验收人：确认服务实际可用。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 样例校验

```bash
PYTHONPATH=src python3 -m pilot_funding.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 走查示例

```bash
PYTHONPATH=src python3 examples/walkthrough.py
```

演示同一笔资金被重复申报时的复审冻结、多社区并行申领的预算上限、
撤销网点后未付冻结与已付核查、服务重启后的到期抽查，以及按社区、
服务类别和实际营业状态区分的覆盖结果。
