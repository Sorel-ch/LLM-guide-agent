"""行程修订与局部重算。保存编辑不调用 LLM，不隐式联网。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .budget import calculate_budget
from .models import Location, PlanDocument, adapt_legacy_plan


def ensure_item_ids(plan: PlanDocument) -> None:
    for day in plan.days:
        for idx, item in enumerate(day.timeline):
            item.item_id = item.item_id or f"d{day.day}-i{idx}"


def revise_plan(raw: dict[str, Any], base_revision: int,
                operations: list[dict[str, Any]]) -> dict[str, Any]:
    plan = adapt_legacy_plan(deepcopy(raw))
    ensure_item_ids(plan)
    if plan.revision != base_revision:
        raise RevisionConflict(f"当前版本为 {plan.revision}，请求基于 {base_revision}")
    by_id = {item.item_id: (day, item) for day in plan.days for item in day.timeline}

    for op in operations:
        kind = op.get("op")
        if kind == "move_item":
            item_id = op["item_id"]
            day, item = by_id[item_id]
            direction = -1 if op["direction"] == "up" else 1
            idx = day.timeline.index(item)
            target = idx + direction
            if 0 <= target < len(day.timeline):
                day.timeline[idx], day.timeline[target] = day.timeline[target], day.timeline[idx]
                for leg in day.timeline:
                    if leg.type == "transit" and leg.path_status == "measured":
                        leg.path = None
                        leg.path_status = "missing"
                        leg.transport = "顺序已调整，路线待实测"
        elif kind == "remove_item":
            day, item = by_id[op["item_id"]]
            day.timeline.remove(item)
        elif kind == "set_time":
            _day, item = by_id[op["item_id"]]
            item.start_time = op.get("start_time", item.start_time)
            item.end_time = op.get("end_time", item.end_time)
        elif kind == "replace_item":
            _day, item = by_id[op["item_id"]]
            replacement = op["candidate"]
            if not isinstance(replacement, dict) or not replacement.get("name"):
                raise ValueError("替换必须提供已核实的候选快照")
            if not replacement.get("location") or replacement.get("type") not in ("attraction", "meal", "hotel"):
                raise ValueError("候选必须包含坐标和合法类别")
            item.name = replacement["name"]
            item.type = replacement["type"]
            item.location = Location.model_validate(replacement["location"])
            item.photo = replacement.get("photo")
            item.rating = replacement.get("rating")
            item.open_time = replacement.get("open_time")
            item.cost = replacement.get("cost")
            item.source = replacement.get("source", "amap")
            item.verified = True
            item.candidate_id = replacement.get("candidate_id")
            for leg in plan.days[plan.days.index(_day)].timeline:
                if leg.type == "transit" and leg.path_status == "measured":
                    leg.path = None
                    leg.path_status = "missing"
                    leg.transport = "地点已替换，路线待实测"
        elif kind == "set_note":
            _day, item = by_id[op["item_id"]]
            item.tip = op.get("tip", "")
        else:
            raise ValueError(f"不支持的编辑操作：{kind}")

    for day in plan.days:
        day.sights = sum(i.type == "attraction" for i in day.timeline)
    plan.revision += 1
    plan.budget_summary = calculate_budget(plan)
    # 修改相邻顺序后旧腿可能不再对应，未重测时只能标 missing。
    plan.sources.setdefault("edit_history", []).append(
        {"revision": plan.revision, "operations": operations, "llm_calls": 0, "external_calls": 0})
    return plan.model_dump(exclude_none=True)


class RevisionConflict(Exception):
    pass
