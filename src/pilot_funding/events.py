"""只追加的领域事件日志。

事件信封必须通过 contracts/domain.schema.json 校验；版本号按聚合从 1 递增。
相同事件标识的业务幂等由调用方在上层保证，本类只做信封与版本约束。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class EventLog:
    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self.schema = dict(schema) if schema is not None else load_schema()
        self._events: list[dict[str, Any]] = []
        self._versions: dict[tuple[str, str], int] = {}

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def append(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        actor_id: str,
        actor_role: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> dict[str, Any]:
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        event = {
            "event_id": event_id or f"{aggregate_id}-{event_type}-v{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": version,
            "actor": {"id": actor_id, "role": actor_role},
            "payload": dict(payload),
        }
        issues = validate_event(event, self.schema)
        if issues:
            details = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise ValueError(f"事件未通过契约校验: {details}")
        self._events.append(event)
        self._versions[key] = version
        return event

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self._events
            if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id
        ]
