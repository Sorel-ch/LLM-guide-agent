"""已选地点之间的实际相邻路段与保守时间排程。"""

from __future__ import annotations

from typing import Any


def adjacent_legs(pa, groups: list[dict[str, Any]], pois: dict[str, dict],
                  adcode: str | None, city: str) -> list[tuple[str, dict]]:
    calls = []
    for day in groups:
        previous = None
        for item in day["items"]:
            if item["type"] == "transit":
                continue
            if previous and previous["name"] in pois and item["name"] in pois:
                a, b = pois[previous["name"]], pois[item["name"]]
                distance = pa.km_between(a, b)
                calls.append(("get_amap_direction", {
                    "origin": a["coord"], "destination": b["coord"],
                    "mode": "walking" if distance <= 2.5 else "transit",
                    "city1": a.get("adcode") or adcode or city,
                    "city2": b.get("adcode") or adcode or city,
                }))
            previous = item
    # 同一坐标对只请求一次；终稿补腿独立额度，不占主取数软上限。
    unique = []
    seen = set()
    for tool, args in calls:
        key = (args["origin"], args["destination"], args["mode"])
        if key not in seen:
            seen.add(key)
            unique.append((tool, args))
    return unique[:pa.LEG_TOPUP_CAP]


def fill_transit(groups: list[dict[str, Any]], legs: list[dict[str, Any]]) -> None:
    """只引用已测坐标对；缺路线时不插入伪造交通段。"""
    for day in groups:
        expanded = []
        previous = None
        for item in day["items"]:
            if previous and item["type"] != "transit" and previous["type"] != "hotel":
                leg = next((l for l in legs if l["a"] == previous["name"] and l["b"] == item["name"]), None)
                if leg:
                    expanded.append({"name": f"{previous['name']}→{item['name']}", "type": "transit",
                                     "start_time": previous.get("end_time"), "end_time": item.get("start_time"),
                                     "transport": f"{leg['mode']} {leg['minutes']}分", "cost": leg.get("fee"),
                                     "tip": "", "source": "amap_direction"})
            expanded.append(item)
            previous = item
        day["items"] = expanded


def schedule_day(day: dict[str, Any], legs: list[dict[str, Any]], start: int = 540) -> None:
    clock = start
    lunch_done = False
    for item in day["items"]:
        if item.get("path_status") == "cross_city" or (item["type"] == "transit" and "→" in item["name"]
                                                      and not any(l["a"] + "→" + l["b"] == item["name"] for l in legs)):
            item["start_time"] = "07:30"
            item["end_time"] = "09:00"
            item["transport"] = "城际交通，班次与票价待核实"
            continue
        if item["type"] == "transit":
            leg = next((l for l in legs if l["a"] + "→" + l["b"] == item["name"]), None)
            duration = int(leg["minutes"] or 0) if leg else 0
            item["start_time"] = f"{clock // 60:02d}:{clock % 60:02d}"
            clock += duration
            item["end_time"] = f"{clock // 60:02d}:{clock % 60:02d}"
            continue
        if item["type"] == "hotel":
            item["start_time"] = f"{clock // 60:02d}:{clock % 60:02d}"
            item["end_time"] = None
            continue
        duration = 60 if item["type"] == "meal" else 90
        if item["type"] == "meal":
            clock = max(clock, 690 if not lunch_done else 1020)
            lunch_done = True
        item["start_time"] = f"{clock // 60:02d}:{clock % 60:02d}"
        clock += duration
        item["end_time"] = f"{clock // 60:02d}:{clock % 60:02d}"
