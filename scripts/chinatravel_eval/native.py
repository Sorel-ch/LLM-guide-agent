"""现状直测基线：现有 `plan_agent.run_plan`（实时高德/秘塔/wiki）出 final_plan，
再把它的选点映射回 sandbox 实体，交给同一个确定性排程器。

映射口径（2026-11-15 起）：先按名字精确命中，再按唯一子串命中；都对不上时用
`final_plan` 条目自带的 `location:{lng,lat}` 找最近的 sandbox 同城实体。
这样测的是 agent 的“选点/顺序/分天”，而不是“名字能不能逐字对上”。
时间、交通、金额仍由 sandbox 重算，所以它不测 agent 自己的算账。

这不是主评测口径：实时数据与 sandbox 实体/价格本来就不一致。主评测请用
`--mode rule|llm`；native 结果要与闭环结果分开列。
"""

from __future__ import annotations

import re

from .rule_plan import DayPlan, _km, schedule_plan, select_intercity, select_hotel
from .sandbox import Sandbox

# 坐标兜底的最大距离（km）：超出就当作“sandbox 里没有这个点”，不硬映射。
ATTRACTION_NEAR_KM = 3.0
RESTAURANT_NEAR_KM = 1.5
HOTEL_NEAR_KM = 3.0


def _item_loc(item: dict) -> dict | None:
    """final_plan 条目用 `location:{lng,lat}`；兼容旧的 `coord` 字段。"""
    loc = item.get("location") or item.get("coord") or {}
    if not isinstance(loc, dict):
        return None
    try:
        lat, lon = float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"lat": lat, "lon": lon}


def _resolve(name: str, loc: dict | None, rows: list[dict], used: set[str],
             max_km: float, prefer=None) -> tuple[dict | None, str | None, float | None]:
    """名字精确 → 唯一子串 → 坐标最近。返回 (row, 方式, 距离 km)。

    `prefer` 只作用于坐标兜底：先找满足偏好的最近点，找不到再退回全体最近。
    名字命中不套偏好——那是 agent 自己写的名字，应当优先尊重。
    """
    exact = [r for r in rows if r["name"] == name and r["name"] not in used]
    if exact:
        return exact[0], "exact", 0.0
    fuzzy = [r for r in rows
             if name and r["name"] not in used
             and (name in r["name"] or r["name"] in name)]
    if len(fuzzy) == 1:
        return fuzzy[0], "fuzzy", 0.0
    if loc:
        for pool in ([r for r in rows if prefer and prefer(r)], rows) if prefer else (rows,):
            best: tuple[float, dict] | None = None
            for r in pool:
                if r["name"] in used:
                    continue
                dist = _km(loc, r)
                if dist <= max_km and (best is None or dist < best[0]):
                    best = (dist, r)
            if best:
                return best[1], "coord", round(best[0], 2)
    return None, None, None


def _meal_kind(start: str | None) -> str | None:
    """按时间就近归到午/晚两档，而不是卡死 11–14 / 17–20。

    产品排的是“午饭/晚饭”，时间常落在 10:30 或 20:30；卡死窗口会把它们整条丢掉，
    DDR 就变成 0。早餐没有对应槽位，仍不接。
    """
    if not start or not re.match(r"^\d{2}:\d{2}$", start):
        return None
    h = int(start[:2])
    if 6 <= h < 10:
        return None
    if h < 16:
        return "lunch"
    if h <= 23:
        return "dinner"
    return None


def _convert(final_plan: dict, task, sandbox: Sandbox,
             needs: dict | None = None) -> tuple[list[DayPlan], dict | None, dict]:
    city = task.target_city
    pref_cuisine = (needs or {}).get("cuisine_pref")
    attractions = sandbox.attractions(city)
    restaurants = sandbox.restaurants(city)
    hotels = sandbox.hotels(city)
    stats = {
        "items": 0,
        "attractions_mapped": 0, "attractions_exact": 0, "attractions_fuzzy": 0,
        "attractions_coord": 0,
        "restaurants_mapped": 0, "restaurants_exact": 0, "restaurants_fuzzy": 0,
        "restaurants_coord": 0,
        "hotels_mapped": 0, "hotels_exact": 0, "hotels_fuzzy": 0, "hotels_coord": 0,
        "dropped": [],
        "substitutions": [],
    }
    days: list[DayPlan] = []
    hotel_pick = None
    seen_a, seen_r = set(), set()

    def note(how: str | None, kind: str, src: str, row: dict, dist: float | None) -> None:
        if how == "coord":
            stats["substitutions"].append(
                {"kind": kind, "from": src, "to": row["name"], "km": dist})

    for day in final_plan.get("days") or []:
        dp = DayPlan(day=int(day.get("day") or len(days) + 1))
        for item in day.get("timeline") or []:
            stats["items"] += 1
            kind = item.get("type")
            name = str(item.get("name") or "")
            loc = _item_loc(item)
            if kind == "attraction":
                row, how, dist = _resolve(name, loc, attractions, seen_a, ATTRACTION_NEAR_KM)
                if row:
                    dp.attractions.append(row)
                    seen_a.add(row["name"])
                    stats["attractions_mapped"] += 1
                    stats[f"attractions_{how}"] += 1
                    note(how, "attraction", name, row, dist)
                else:
                    stats["dropped"].append(f"attraction:{name}")
            elif kind == "meal":
                slot = _meal_kind(item.get("start_time"))
                if not slot:
                    stats["dropped"].append(f"meal_time:{name}")
                    continue
                if getattr(dp, slot) is not None:
                    stats["dropped"].append(f"meal_dup_slot:{name}")
                    continue
                prefer = (lambda r: r.get("cuisine") == pref_cuisine) if pref_cuisine else None
                row, how, dist = _resolve(name, loc, restaurants, seen_r,
                                          RESTAURANT_NEAR_KM, prefer=prefer)
                if row:
                    setattr(dp, slot, row)
                    seen_r.add(row["name"])
                    stats["restaurants_mapped"] += 1
                    stats[f"restaurants_{how}"] += 1
                    note(how, "restaurant", name, row, dist)
                else:
                    stats["dropped"].append(f"meal:{name}")
            elif kind == "hotel":
                if hotel_pick is not None:
                    continue
                row, how, dist = _resolve(name, loc, hotels, set(), HOTEL_NEAR_KM)
                if row:
                    hotel_pick = row
                    stats["hotels_mapped"] += 1
                    stats[f"hotels_{how}"] += 1
                    note(how, "hotel", name, row, dist)
                else:
                    stats["dropped"].append(f"hotel:{name}")
        days.append(dp)
    if hotel_pick is None and task.days > 1:
        hotel_pick = select_hotel(task, {}, sandbox, days[0] if days else DayPlan(day=1))
        stats["hotel_fallback"] = "rule"
    return days, hotel_pick, stats


def build_native_plan(task, needs, sandbox: Sandbox) -> tuple[dict, dict]:
    import plan_agent as pa

    # 不传 needs、也不用澄清闸门：让 agent 自己从原始题目抽取需求。
    # 基准题大多没写“偏好”，走闸门会全部停在 clarify，测不到后面的规划。
    res = pa.run_plan(
        task.nature_language,
        date=needs.get("date"),
        needs=None,
        clarify_first=False,
        json_mode=True,
        # 打开补查轮：跑产品完整的“初稿 → 补查 → 终稿”，最多 4 次 LLM；
        # 补查条数由剩余外部额度限制（max_external=32）。
        max_extra=-1,
        max_external=32,
        quiet=True,
    )
    if "plan" not in res:
        raise RuntimeError(f"native_plan_unavailable: {res.get('stage') or 'no plan'}")
    final_plan = res["plan"]
    if final_plan.get("degraded"):
        raise RuntimeError("native_plan_degraded")
    days, hotel, stats = _convert(final_plan, task, sandbox, needs)
    outbound, back = select_intercity(task, sandbox, needs.get("intercity_pref"))
    plan = schedule_plan(task, needs, sandbox, days, hotel, outbound, back)
    meta = {
        "days": [
            {"day": d.day, "attractions": [a["name"] for a in d.attractions],
             "lunch": d.lunch["name"] if d.lunch else None,
             "dinner": d.dinner["name"] if d.dinner else None}
            for d in days
        ],
        "hotel": hotel["name"] if hotel else None,
        "outbound": (outbound or {}).get("id"),
        "back": (back or {}).get("id"),
        "native": {
            "final_plan_id": final_plan.get("plan_id"),
            "llm_calls": res.get("llm_calls"),
            "external_calls": res.get("external_calls"),
            "seconds": res.get("seconds"),
            "agent_needs": res.get("needs"),
            "agent_timeline": [
                {"day": d.get("day"), "type": it.get("type"), "name": it.get("name"),
                 "start": it.get("start_time"), "end": it.get("end_time")}
                for d in final_plan.get("days") or []
                for it in d.get("timeline") or []
            ],
            "mapping": stats,
        },
    }
    return plan, meta
