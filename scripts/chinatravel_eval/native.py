"""现状直测基线：现有 `plan_agent.run_plan`（实时高德/秘塔/wiki）出 final_plan，
再把它的选点映射回 sandbox 实体，交给同一个确定性排程器。

这不是主评测口径：实时数据与 sandbox 实体/价格本来就不一致，FPR 会反映“映射覆盖率”，
而不是规划能力。主评测请用 `--mode rule|llm`。
"""

from __future__ import annotations

import re

from .rule_plan import DayPlan, schedule_plan, select_intercity, select_hotel
from .sandbox import Sandbox

PRODUCT_NEEDS = (
    "origin", "origin_city", "destination_city", "intercity", "day_trip", "days",
    "budget_yuan", "themes", "must_visit", "need_intercity_ticket", "date",
)


def _product_needs(needs: dict) -> dict:
    return {k: needs[k] for k in PRODUCT_NEEDS if k in needs}


def _match(name: str, rows: list[dict]) -> dict | None:
    """精确优先；唯一子串才认（与仓库 ledger_lookup 同口径）。"""
    exact = [r for r in rows if r["name"] == name]
    if exact:
        return exact[0]
    fuzzy = [r for r in rows if name and (name in r["name"] or r["name"] in name)]
    return fuzzy[0] if len(fuzzy) == 1 else None


def _meal_kind(start: str | None) -> str | None:
    if not start or not re.match(r"^\d{2}:\d{2}$", start):
        return None
    h = int(start[:2])
    if 10 <= h <= 14:
        return "lunch"
    if 17 <= h <= 20:
        return "dinner"
    return None


def _convert(final_plan: dict, task, sandbox: Sandbox) -> tuple[list[DayPlan], dict | None, dict]:
    city = task.target_city
    attractions = sandbox.attractions(city)
    restaurants = sandbox.restaurants(city)
    hotels = sandbox.hotels(city)
    stats = {"items": 0, "attractions_mapped": 0, "restaurants_mapped": 0,
             "hotels_mapped": 0, "dropped": []}
    days: list[DayPlan] = []
    hotel_pick = None
    seen_a, seen_r = set(), set()
    for day in final_plan.get("days") or []:
        dp = DayPlan(day=int(day.get("day") or len(days) + 1))
        for item in day.get("timeline") or []:
            stats["items"] += 1
            kind = item.get("type")
            name = str(item.get("name") or "")
            if kind == "attraction":
                row = _match(name, attractions)
                if row and row["name"] not in seen_a:
                    dp.attractions.append(row)
                    seen_a.add(row["name"])
                    stats["attractions_mapped"] += 1
                else:
                    stats["dropped"].append(f"attraction:{name}")
            elif kind == "meal":
                slot = _meal_kind(item.get("start_time"))
                if not slot:
                    stats["dropped"].append(f"meal_time:{name}")
                    continue
                row = _match(name, restaurants)
                if row and row["name"] not in seen_r and getattr(dp, slot) is None:
                    setattr(dp, slot, row)
                    seen_r.add(row["name"])
                    stats["restaurants_mapped"] += 1
                else:
                    stats["dropped"].append(f"meal:{name}")
            elif kind == "hotel":
                row = _match(name, hotels)
                if row:
                    hotel_pick = hotel_pick or row
                    stats["hotels_mapped"] += 1
                else:
                    stats["dropped"].append(f"hotel:{name}")
        days.append(dp)
    if hotel_pick is None and task.days > 1:
        hotel_pick = select_hotel(task, {}, sandbox, days[0] if days else DayPlan(day=1))
    return days, hotel_pick, stats


def build_native_plan(task, needs, sandbox: Sandbox) -> tuple[dict, dict]:
    import plan_agent as pa

    res = pa.run_plan(
        task.nature_language,
        date=needs.get("date"),
        needs=_product_needs(needs),
        json_mode=True,
        max_extra=0,
        max_external=32,
        quiet=True,
    )
    if res.get("stage") == "clarify" or "plan" not in res:
        raise RuntimeError(f"native_plan_unavailable: {res.get('stage') or 'no plan'}")
    final_plan = res["plan"]
    if final_plan.get("degraded"):
        raise RuntimeError("native_plan_degraded")
    days, hotel, stats = _convert(final_plan, task, sandbox)
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
            "mapping": stats,
        },
    }
    return plan, meta
