"""运行中的语义校验与局部修复，不把问题重新丢给长文生成。"""

from __future__ import annotations

import re
from typing import Any

from .audit import minutes, visit_fits_open
from .experts import kind_of


KIND_BY_CATEGORY = {"餐饮服务": "meal", "住宿服务": "hotel", "交通设施服务": "transit"}


def correct_item_types(groups: list[dict[str, Any]], pois: dict[str, dict]) -> list[str]:
    warnings = []
    for day in groups:
        for item in day["items"]:
            poi = pois.get(item.get("name"))
            if not poi or item.get("type") == "transit":
                continue
            actual = KIND_BY_CATEGORY.get(kind_of(poi))
            if actual == "transit" and item.get("type") != "transit":
                # 不能仅改标签把车站变成移动段：必须替换为真实的同类型候选。
                warnings.append(f"day{day['day']} 交通设施不能作为{item['type']}：{item['name']}")
                continue
            if actual and item["type"] != actual:
                warnings.append(f"day{day['day']} 按台账将 {item['name']} 从 {item['type']} 改为 {actual}")
                item["type"] = actual
    return warnings


def opening_conflicts(groups: list[dict[str, Any]], pois: dict[str, dict]) -> list[dict[str, Any]]:
    conflicts = []
    for day in groups:
        for item in day["items"]:
            poi = pois.get(item.get("name"), {})
            probe = {**item, "open_time": poi.get("opentime")}
            if visit_fits_open(probe) is False:
                conflicts.append({"day": day["day"], "item": item,
                                  "open_time": poi.get("opentime")})
    return conflicts


def repair_opening(groups: list[dict[str, Any]], pois: dict[str, dict],
                   used_names: set[str] | None = None) -> list[str]:
    """先换同类、同日可用的真实候选；换不了就留 warning，不偷偷改营业时间。"""
    repairs = []
    used = used_names if used_names is not None else {i["name"] for d in groups for i in d["items"]}
    for conflict in opening_conflicts(groups, pois):
        item = conflict["item"]
        category = kind_of(pois.get(item["name"], {}))
        candidates = []
        for name, poi in pois.items():
            if name in used or kind_of(poi) != category:
                continue
            probe = {**item, "open_time": poi.get("opentime")}
            if visit_fits_open(probe) is not False:
                candidates.append(name)
        if candidates:
            replacement = sorted(candidates)[0]
            repairs.append(f"day{conflict['day']} {item['name']} → {replacement}（营业时间冲突）")
            used.discard(item["name"])
            used.add(replacement)
            poi = pois[replacement]
            item.update(name=replacement, cost=poi.get("price"), source="amap")
    return repairs


def validate_groups(groups: list[dict[str, Any]], pois: dict[str, dict],
                    needs: dict[str, Any]) -> list[dict[str, Any]]:
    """生成前端可展示的 issue；跨日同酒店不是重复。"""
    from .audit import audit_plan
    raw = {"plan_id": "draft", "days_count": int(needs.get("days") or len(groups)),
           "days": [{**d, "sights": sum(i["type"] == "attraction" for i in d["items"])} for d in groups]}
    return audit_plan(raw)["issues"]


def transport_duration(item: dict[str, Any]) -> int | None:
    match = re.search(r"(\d+)\s*分", item.get("transport") or "")
    return int(match[1]) if match else None
