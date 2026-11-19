"""v2 受控专家编排。复用旧管线的工具与地理内核，不启动额外线程或 MCP。"""

from __future__ import annotations

import json
import time
from typing import Any


def run_curated_plan(pa, needs: dict[str, Any], question: str, ext: dict[str, Any],
                     pages: list[str], wiki_text: str, start_date: str, thinking: str,
                     quiet: bool, max_external: int, mark) -> dict[str, Any]:
    """一次小策展 LLM + 确定性餐宿/排程；最终仍走统一 final_plan 适配器。"""
    from .experts import (CURATOR_PROMPT, curator_view, deterministic_day_items,
                          hospitality_for_day, parse_curation)
    from .quality import correct_item_types, repair_opening, validate_groups
    from .routing import order_selected

    started = time.perf_counter()
    view = curator_view(needs, ext["pois"])
    llm_result = pa.llm([{"role": "system", "content": CURATOR_PROMPT},
                         {"role": "user", "content": view}],
                        max_tokens=1000, thinking=thinking)
    selected, warnings = parse_curation(pa.extract_json(llm_result["text"]), ext["pois"], needs,
                                        max_count=max(3 * int(needs.get("days") or 1), 1))
    mark(f"C 景点策展    LLM {llm_result['seconds']:.1f}s，有效候选 {len(selected)} 个")
    if not selected:
        raise ValueError("景点策展没有返回任何有效候选；不使用虚构地点兜底")

    suggestion = order_selected(pa, selected, ext["pois"], int(needs.get("days") or 1),
                                ext.get("adcode"))
    groups = []
    extra_calls = []
    for index, day in enumerate(suggestion["days"]):
        names = day["names"]
        hospitality = hospitality_for_day(names, ext["pois"], pa.km_between)
        # 全程只有全城候选时，围绕当日末站补一次有限周边搜；不重复全城扫店。
        if names and (not hospitality["lunch"] or not hospitality["dinner"]
                      or (index < len(suggestion["days"]) - 1 and not hospitality["hotel"])):
            anchor = ext["pois"][names[-1]]
            extra_calls.append(("get_amap_poi_search", {
                "types": "050000|100000", "location": anchor["coord"], "radius": 2000,
                "offset": 8, "show_fields": pa.POI_SHOW_FIELDS}))
    remaining = max(0, max_external - ext["external_count"]) if max_external else len(extra_calls)
    if extra_calls and remaining:
        results = pa.run_calls(extra_calls[:remaining], quiet)
        ext["results"] += results
        ext["external_count"] += len(results)
        pa.collect_pois(results, ext["pois"], needs["destination_city"])
        mark(f"E 动线餐宿    周边补查 {len(results)} 次")
    used_hospitality: set[str] = set()
    for index, day in enumerate(suggestion["days"]):
        names = day["names"]
        hospitality = hospitality_for_day(names, ext["pois"], pa.km_between)
        for slot in ("lunch", "dinner"):
            name = hospitality[slot]
            if name:
                brand = name.split("(")[0].split("（")[0]
                if brand in used_hospitality:
                    alternatives = [(pa.km_between(ext["pois"][names[-1]], p), n)
                                    for n, p in ext["pois"].items()
                                    if str(p.get("type") or "").startswith("餐饮服务")
                                    and n.split("(")[0].split("（")[0] not in used_hospitality]
                    hospitality[slot] = min(alternatives)[1] if alternatives else None
                if hospitality[slot]:
                    used_hospitality.add(hospitality[slot].split("(")[0].split("（")[0])
        groups.append({"day": day["day"], "theme": "、".join(needs.get("themes") or []) or "城市游览",
                       "items": deterministic_day_items(day, names, ext["pois"], hospitality,
                                                       bool(needs.get("intercity")), index == 0,
                                                       index == len(suggestion["days"]) - 1,
                                                       needs.get("origin_city") or needs.get("origin") or "",
                                                       needs["destination_city"])})
    warnings += correct_item_types(groups, ext["pois"])
    repairs = repair_opening(groups, ext["pois"])
    # 已选点才有核价必要：景点票价与酒店房价不能用 POI 人均消费推断。
    pricing = []
    for name in selected:
        pricing.append(("metaso_web_search", {"q": f"{needs['destination_city']} {name} 门票 官方 价格", "size": 3}))
    for day in groups:
        for item in day["items"]:
            if item["type"] == "hotel":
                pricing.append(("metaso_web_search", {"q": f"{item['name']} {needs['destination_city']} 每晚 房价", "size": 3}))
    remaining = max(0, max_external - ext["external_count"]) if max_external else len(pricing)
    if pricing and remaining:
        results = pa.run_calls(pricing[:remaining], quiet)
        ext["results"] += results
        ext["external_count"] += len(results)
        ext["gap_asks"] = [{"tool": t, "args": a} for t, a in pricing[:remaining]]
    from .evidence import extract_price
    for day in groups:
        for item in day["items"]:
            if item["type"] in ("attraction", "hotel") and item.get("cost") is None:
                fact = extract_price(item["name"], item["type"], ext["results"])
                if fact:
                    item["cost"] = fact["value"]
                    item["source"] = "metaso"
                    item["tip"] = fact["text"]
    from .schedule import adjacent_legs, fill_transit, schedule_day
    topup = adjacent_legs(pa, groups, ext["pois"], ext.get("adcode"), needs["destination_city"])
    if topup:
        results = pa.run_calls(topup, quiet)
        ext["results"] += results
        ext["external_count"] += len(topup)
    legs = pa.leg_index(ext)
    fill_transit(groups, legs)
    for day in groups:
        schedule_day(day, legs)
    repairs += repair_opening(groups, ext["pois"])
    final = json.dumps({"days": groups}, ensure_ascii=False)
    mark(f"F 路线与校验  补腿 {len(topup)} 条，时间修复 {len(repairs)} 处")
    plan = pa.build_plan_json(needs, question, final, ext, pages, start_date,
                              1, time.perf_counter() - started,
                              thinking, quiet)
    plan["sources"]["agent_runs"] = [{"agent": "AttractionCurator", "selected": len(selected),
                                      "seconds": llm_result["seconds"]}]
    plan["sources"]["repairs"] = repairs
    plan["sources"]["curation_warnings"] = warnings
    plan["edit_candidates"] = [{"candidate_id": name, "name": name,
        "type": ("meal" if str(p.get("type") or "").startswith("餐饮服务") else
                 "hotel" if str(p.get("type") or "").startswith("住宿服务") else "attraction"),
        "location": {"lng": float(p["lon"]), "lat": float(p["lat"])},
        "rating": p.get("rating"), "open_time": p.get("opentime"),
        "photo": p.get("photo"), "cost": p.get("price"), "source": "amap"}
        for name, p in ext["pois"].items()
        if str(p.get("type") or "").split(";")[0] in ("餐饮服务", "住宿服务", "风景名胜", "科教文化")]
    issues = validate_groups(groups, ext["pois"], needs)
    if plan.get("quality") is not None:
        known = {(i["code"], i.get("day"), i.get("item_id")) for i in plan["quality"]["issues"]}
        plan["quality"]["issues"] += [i for i in issues if (i["code"], i.get("day"), i.get("item_id")) not in known]
    return plan
