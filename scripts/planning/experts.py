"""窄职责策展：LLM 只选择真实候选，代码负责餐宿与时间。"""

from __future__ import annotations

import re
from typing import Any, Callable


CURATOR_PROMPT = """你是景点策展专家，不是行程写手。只从给出的候选 ID 中选择地点。
输出 JSON：{"selected":[{"candidate_id":"台账原ID","reason":"不超过20字","preferences":["对应偏好"]}],"excluded":[{"candidate_id":"ID","reason":"不超过15字"}]}
规则：
1. 必须保留用户必去点；不要创造 ID、坐标、费用、营业时间或完整日程。
2. 按偏好选择，普通公园/广场不能仅因评分高挤掉主题地点；无偏好才优先代表性。
3. 同一天可游的邻近点无需你分组，程序会计算。输出不超过 1000 字，只输出 JSON。
"""


def curator_view(needs: dict[str, Any], pois: dict[str, dict], limit: int = 48) -> str:
    rows = []
    for name, poi in pois.items():
        kind = str(poi.get("type") or "").split(";")[0]
        if kind in ("餐饮服务", "住宿服务", "交通设施服务"):
            continue
        rows.append({"candidate_id": name, "type": kind,
                     "rating": poi.get("rating"), "query": poi.get("q"),
                     "open_time": poi.get("opentime")})
        if len(rows) >= limit:
            break
    import json
    return json.dumps({"needs": needs, "candidates": rows}, ensure_ascii=False)


def parse_curation(raw: Any, pois: dict[str, dict], needs: dict[str, Any],
                   max_count: int) -> tuple[list[str], list[str]]:
    """未注册 ID、餐宿和交通枢纽不进入景点集合；必去点由代码兜底。"""
    selected: list[str] = []
    warnings: list[str] = []
    rows = raw.get("selected", []) if isinstance(raw, dict) else []
    for row in rows:
        name = row.get("candidate_id") if isinstance(row, dict) else None
        if name not in pois:
            warnings.append(f"策展引用未注册候选：{name}")
            continue
        kind = str(pois[name].get("type") or "")
        if kind.startswith(("餐饮服务", "住宿服务", "交通设施服务")):
            warnings.append(f"策展类别不符，已排除：{name}")
            continue
        if name not in selected:
            selected.append(name)
    for wanted in needs.get("must_visit") or []:
        match = next((n for n in pois if wanted == n or wanted in n or n in wanted), None)
        if match and match not in selected:
            selected.insert(0, match)
        elif not match:
            warnings.append(f"必去点没有匹配到台账：{wanted}")
    if len(selected) > max_count:
        selected = selected[:max_count]
    return selected, warnings


def kind_of(poi: dict[str, Any]) -> str:
    return str(poi.get("type") or "").split(";")[0]


def hospitality_for_day(day_names: list[str], pois: dict[str, dict],
                        distance: Callable[[dict, dict], float]) -> dict[str, str | None]:
    """按当天真实动线选餐宿，不凭名称把街区当餐厅。"""
    if not day_names:
        return {"lunch": None, "dinner": None, "hotel": None}
    end = pois[day_names[-1]]
    lunch_anchor = pois[day_names[min(1, len(day_names) - 1)]]

    def nearest(anchor: dict, category: str, used: set[str]) -> str | None:
        candidates = [(distance(anchor, p), n) for n, p in pois.items()
                      if kind_of(p) == category and n not in used]
        return min(candidates)[1] if candidates else None

    used: set[str] = set()
    lunch = nearest(lunch_anchor, "餐饮服务", used)
    if lunch:
        used.add(lunch)
    dinner = nearest(end, "餐饮服务", used)
    hotel = nearest(end, "住宿服务", used)
    return {"lunch": lunch, "dinner": dinner, "hotel": hotel}


def deterministic_day_items(day: dict[str, Any], names: list[str], pois: dict[str, dict],
                            hospitality: dict[str, str | None], intercity: bool,
                            first: bool, last: bool, origin: str = "", city: str = "") -> list[dict[str, Any]]:
    """生成可被校验的草稿；时间为排程初值，未知费用不填。"""
    items: list[dict[str, Any]] = []
    clock = 9 * 60

    def add(kind: str, name: str, duration: int, tip: str = "") -> None:
        nonlocal clock
        if name not in pois:
            return
        poi = pois[name]
        start = clock
        end = start + duration
        items.append({"name": name, "type": kind, "start_time": f"{start // 60:02d}:{start % 60:02d}",
                      "end_time": f"{end // 60:02d}:{end % 60:02d}", "transport": "",
                      "cost": poi.get("price") if kind in ("meal", "hotel") else None,
                      "tip": tip, "source": "amap"})
        clock = end

    if first and intercity:
        items.insert(0, {"name": f"{origin or '出发地'}→{city or '目的地'}", "type": "transit", "start_time": "07:30",
                         "end_time": "09:00", "transport": "城际交通，班次与票价待核实",
                         "cost": None, "tip": "", "source": "none"})
    for idx, name in enumerate(names):
        add("attraction", name, 90)
        if idx == 0 and hospitality.get("lunch"):
            add("meal", hospitality["lunch"], 60, "按当天动线选择")
    if hospitality.get("dinner"):
        add("meal", hospitality["dinner"], 60)
    if not last and hospitality.get("hotel"):
        add("hotel", hospitality["hotel"], 0)
    return items
