"""LLM 选点：模型只决定“每天去哪几个点、吃哪家、住哪家”，排程/金额仍由 rule_plan 负责。

公平性：prompt 只含 `nature_language` + sandbox 台账 + 从 NL 抽到的偏好；
绝不读取 `hard_logic_py`/`hard_logic_nl`，也不把官方诊断回灌给模型。
"""

from __future__ import annotations

import json
import re

from .rule_plan import (
    DayPlan,
    _attraction_ok,
    _center,
    _hotel_ok,
    _km,
    _restaurant_ok,
    _score,
    build_rule_plan,
    schedule_plan,
    select_hotel,
    select_intercity,
)
from .sandbox import Sandbox

SYSTEM = (
    "你是行程规划器。只输出一个 JSON 对象，不要解释、不要 markdown。\n"
    "结构：{\"days\":[{\"day\":1,\"attractions\":[\"名称\"],\"lunch\":\"名称\",\"dinner\":\"名称\"}],"
    "\"hotel\":\"名称或 null\"}\n"
    "规则：\n"
    "1. 题目说几天就给几天，day 从 1 连续编号；\n"
    "2. 名称必须原样照抄候选台账，不要改写、不要加简称、不要编造；\n"
    "3. 每天 2~3 个景点，整个行程不重复；\n"
    "4. 每天按题目给的偏好选 lunch 与 dinner（从候选餐厅选），全行程不重复；"
    "题目没提菜系就选本地特色；\n"
    "5. 多日题选一家 hotel（从候选酒店选），最后一天不用；一日游 hotel 填 null；\n"
    "6. 只输出 JSON。"
)


def _ledger(task, needs, sandbox: Sandbox) -> dict:
    city = task.target_city
    days = max(1, int(task.days))
    center = _center(sandbox.attractions(city))
    attractions = [r for r in sandbox.attractions(city) if _attraction_ok(r)]
    attractions = sorted(attractions, key=lambda r: (-_score(r, needs), _km(r, center)))[: max(12, 4 * days)]
    restaurants = [r for r in sandbox.restaurants(city) if _restaurant_ok(r)]
    pref = needs.get("cuisine_pref")
    if pref:
        preferred = [r for r in restaurants if r["cuisine"] == pref]
        restaurants = preferred or restaurants
    restaurants = sorted(restaurants, key=lambda r: _km(r, center))[:12]
    hotels = [r for r in sandbox.hotels(city) if _hotel_ok(r)]
    mid = [h for h in hotels if 300 <= h["price"] <= 900]
    hotels = sorted(mid or hotels, key=lambda r: _km(r, center))[:8]
    outbound, back = select_intercity(task, sandbox, needs.get("intercity_pref"))
    return {
        "attractions": attractions,
        "restaurants": restaurants,
        "hotels": hotels,
        "outbound": outbound,
        "back": back,
    }


def _render(task, needs, ledger: dict) -> str:
    lines = [
        f"【题目】{task.nature_language}",
        f"【任务】{task.days} 天，{task.people} 人，目标城市 {task.target_city}。",
    ]
    if needs.get("budget_yuan"):
        lines.append(f"【预算】{needs['budget_yuan']} 元（含交通/门票/餐饮/住宿）")
    if needs.get("cuisine_pref"):
        lines.append(f"【菜系偏好】{needs['cuisine_pref']}")
    if needs.get("spot_pref"):
        lines.append(f"【景点偏好】{needs['spot_pref']}")
    lines.append("【候选景点】名称 | 类型 | 门票 | 开放 | 建议时长(小时)")
    for r in ledger["attractions"]:
        dur = f"{r.get('recommendmin')}-{r.get('recommendmax')}"
        lines.append(
            f"{r['name']} | {r['type']} | {r['price']} | {r['opentime']}-{r['endtime']} | {dur}"
        )
    lines.append("【候选餐厅】名称 | 菜系 | 人均 | 营业")
    for r in ledger["restaurants"]:
        lines.append(f"{r['name']} | {r['cuisine']} | {r['price']} | {r['opentime']}-{r['endtime']}")
    if task.days > 1:
        lines.append("【候选酒店】名称 | 价格 | 床位数")
        for r in ledger["hotels"]:
            lines.append(f"{r['name']} | {r['price']} | {r['numbed']}")
    if ledger["outbound"]:
        o = ledger["outbound"]
        lines.append(f"【去程参考】{o['id']} {o['from']}->{o['to']} {o['begin']}-{o['end']}")
    if ledger["back"]:
        b = ledger["back"]
        lines.append(f"【返程参考】{b['id']} {b['from']}->{b['to']} {b['begin']}-{b['end']}")
    return "\n".join(lines)


def _parse(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            from json_repair import repair_json

            return json.loads(repair_json(m.group(0)))
        except Exception:
            return None


def _apply(task, needs, sandbox: Sandbox, ledger: dict, got: dict) -> tuple[list[DayPlan], dict | None, str | None]:
    """把 LLM 选择映射回实体；未知名称返回错误（不猜）。"""
    names_a = {r["name"]: r for r in ledger["attractions"]}
    names_r = {r["name"]: r for r in ledger["restaurants"]}
    names_h = {r["name"]: r for r in ledger["hotels"]}
    errors = []
    days: list[DayPlan] = []
    seen_a, seen_r = set(), set()
    for i, d in enumerate(got.get("days") or [], 1):
        atts = []
        for name in d.get("attractions") or []:
            name = str(name)
            if name not in names_a:
                errors.append(f"day{i} 景点不在候选: {name}")
            elif name in seen_a:
                errors.append(f"day{i} 景点重复: {name}")
            else:
                atts.append(names_a[name])
                seen_a.add(name)
        if len(atts) < 2:
            errors.append(f"day{i} 景点少于 2 个")
        lunch_name = str(d.get("lunch") or "")
        dinner_name = str(d.get("dinner") or "")
        lunch = names_r.get(lunch_name)
        dinner = names_r.get(dinner_name)
        if lunch_name and lunch is None:
            errors.append(f"day{i} 午餐不在候选: {lunch_name}")
        if dinner_name and dinner is None:
            errors.append(f"day{i} 晚餐不在候选: {dinner_name}")
        if lunch and lunch_name in seen_r:
            errors.append(f"day{i} 午餐重复: {lunch_name}")
        if dinner and dinner_name in seen_r:
            errors.append(f"day{i} 晚餐重复: {dinner_name}")
        if lunch:
            seen_r.add(lunch_name)
        if dinner:
            seen_r.add(dinner_name)
        days.append(DayPlan(day=i, attractions=atts, lunch=lunch, dinner=dinner))
    if len(days) != task.days:
        errors.append(f"天数不对: {len(days)} != {task.days}")
    hotel = None
    hotel_name = got.get("hotel")
    if task.days > 1 and hotel_name:
        hotel = names_h.get(str(hotel_name))
        if hotel is None:
            errors.append(f"酒店不在候选: {hotel_name}")
    if errors:
        return None, None, "\n".join(errors[:12])
    return days, hotel, None


def build_llm_plan(task, needs, sandbox: Sandbox) -> tuple[dict, dict]:
    import plan_agent as pa

    ledger = _ledger(task, needs, sandbox)
    prompt = _render(task, needs, ledger)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    llm_calls = 0
    tokens = {"in": 0, "out": 0}
    raw_text = ""
    days = None
    hotel = None
    error = None
    for attempt in range(2):
        res = pa.llm(messages, max_tokens=1200, temperature=0.0, thinking="off")
        llm_calls += 1
        tokens["in"] += res.get("in_tokens") or 0
        tokens["out"] += res.get("out_tokens") or 0
        raw_text = res.get("text", "")
        got = _parse(raw_text)
        if got is None:
            error = "LLM 输出不是 JSON"
        else:
            days, hotel, error = _apply(task, needs, sandbox, ledger, got)
        if days is not None:
            break
        messages = messages + [
            {"role": "assistant", "content": raw_text},
            {"role": "user", "content": "上一条选择有以下结构问题，请只输出修正后的 JSON：\n" + (error or "")},
        ]
    if days is None:
        # 兜底：LLM 两轮都没给出可用选择时退回 rule_v0，保证每题有产物。
        plan, meta = build_rule_plan(task, needs, sandbox)
        meta.update({"fallback": "rule", "llm_calls": llm_calls, "llm_tokens": tokens,
                     "llm_error": error, "llm_raw": raw_text, "ledger": _brief(ledger), "prompt": prompt})
        return plan, meta
    if task.days > 1 and hotel is None:
        hotel = select_hotel(task, needs, sandbox, days[0])
    outbound, back = select_intercity(task, sandbox, needs.get("intercity_pref"))
    plan = schedule_plan(task, needs, sandbox, days, hotel, outbound, back)
    # 只依据 NL 抽到的菜系做兜底；不读取 hard_logic_py。
    if needs.get("cuisine_pref") and not _plan_has_cuisine(plan, needs["cuisine_pref"], ledger):
        rule_plan, rule_meta = build_rule_plan(task, needs, sandbox)
        rule_meta.update({
            "fallback": "rule_cuisine",
            "llm_calls": llm_calls,
            "llm_tokens": tokens,
            "llm_error": error,
            "llm_raw": raw_text,
            "ledger": _brief(ledger),
            "prompt": prompt,
        })
        return rule_plan, rule_meta
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
        "llm_calls": llm_calls,
        "llm_tokens": tokens,
        "llm_error": error,
        "llm_raw": raw_text,
        "ledger": _brief(ledger),
        "prompt": prompt,
    }
    return plan, meta


def _plan_has_cuisine(plan: dict, cuisine: str, ledger: dict) -> bool:
    names = {r["name"]: r for r in ledger["restaurants"]}
    for day in plan.get("itinerary") or []:
        for act in day.get("activities") or []:
            if act.get("type") in ("breakfast", "lunch", "dinner"):
                row = names.get(act.get("position"))
                if row and row.get("cuisine") == cuisine:
                    return True
    return False


def _brief(ledger: dict) -> dict:
    return {
        "attractions": [r["name"] for r in ledger["attractions"]],
        "restaurants": [r["name"] for r in ledger["restaurants"]],
        "hotels": [r["name"] for r in ledger["hotels"]],
        "outbound": (ledger["outbound"] or {}).get("id"),
        "back": (ledger["back"] or {}).get("id"),
    }
