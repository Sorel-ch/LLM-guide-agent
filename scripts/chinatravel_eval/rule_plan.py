"""rule_v0：不调 LLM 的确定性选点 + 排程 + 官方 schema 组装。

目标不是刷分，而是先证明“任意 easy 题都能产出一个能过 schema/commonsense 的合法方案”。
LLM 选点后续只替换 `select_day_items()`；排程与金额仍由本模块负责。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sandbox import Sandbox, from_min, open_at, to_min

SKIP_ATTRACTION_TYPES = {"其它"}
SKIP_ATTRACTION_WORDS = ("地铁站", "火车站", "机场", "停车场", "售票处", "服务中心")
TYPE_SCORE = {
    "博物馆/纪念馆": 4,
    "人文景观": 4,
    "历史古迹": 4,
    "文化旅游区": 3,
    "美术馆/艺术馆": 3,
    "公园": 2,
    "商业街区": 2,
    "自然风光": 3,
    "游乐园/体育娱乐": 1,
}
MEAL_MINUTES = 60
ATTRACTION_MIN = 45
ATTRACTION_MAX = 120
HOTEL_START = "20:00"
LUNCH_WINDOW = ("11:00", "14:00")
DINNER_WINDOW = ("17:00", "20:00")


class PlanError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class DayPlan:
    day: int
    attractions: list[dict] = field(default_factory=list)
    lunch: dict | None = None
    dinner: dict | None = None
    hotel: dict | None = None


def _km(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _center(rows: list[dict]) -> dict:
    if not rows:
        return {"lat": 0.0, "lon": 0.0}
    return {
        "lat": sum(r["lat"] for r in rows) / len(rows),
        "lon": sum(r["lon"] for r in rows) / len(rows),
    }


def _nearest(rows: list[dict], point: dict, key=lambda r: r) -> list[dict]:
    return sorted(rows, key=lambda r: _km(key(r), point))


def _attraction_ok(row: dict) -> bool:
    if row.get("price") is None or not row.get("opentime") or not row.get("endtime"):
        return False
    if row.get("type") in SKIP_ATTRACTION_TYPES:
        return False
    if any(w in row["name"] for w in SKIP_ATTRACTION_WORDS):
        return False
    return True


def _restaurant_ok(row: dict) -> bool:
    return bool(row.get("price") is not None and row.get("opentime") and row.get("endtime"))


def _hotel_ok(row: dict) -> bool:
    return bool(row.get("price") is not None and row["price"] > 0 and row.get("numbed", 1) >= 1)


def _open_during(row: dict, lo: str, hi: str) -> bool:
    """餐次窗口内任意 30 分钟点营业即可（酒吧 18:00 开门也算能排晚餐）。"""
    start, end = to_min(lo) or 0, to_min(hi) or 0
    for t in range(start, max(start, end), 30):
        if open_at(row.get("opentime"), row.get("endtime"), from_min(t)):
            return True
    return False


def _duration_min(row: dict) -> int:
    lo = row.get("recommendmin")
    hi = row.get("recommendmax")
    if lo is None and hi is None:
        return 90
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    return max(ATTRACTION_MIN, min(ATTRACTION_MAX, int(((lo + hi) / 2) * 60)))


def _score(row: dict, needs: dict) -> float:
    score = float(TYPE_SCORE.get(row.get("type", ""), 1))
    pref = needs.get("spot_pref")
    if pref and pref in row.get("type", ""):
        score += 5
    if any(m and (m in row["name"] or row["name"] in m) for m in needs.get("must_visit") or []):
        score += 10
    if row.get("price") == 0:
        score += 0.5
    return score


def select_days(task, needs, sandbox: Sandbox, outbound: dict | None) -> list[DayPlan]:
    city = task.target_city
    days = max(1, int(task.days))
    attractions = [r for r in sandbox.attractions(city) if _attraction_ok(r)]
    if not attractions:
        raise PlanError("no_attractions", city)
    center = _center(attractions)
    arrival = center
    if outbound:
        arrival = sandbox.resolve(city, outbound["to"]) or center
    ranked = sorted(attractions, key=lambda r: (-_score(r, needs), _km(r, center)))
    wanted = 3 if days == 1 else 2
    pool = ranked[: max(wanted * days * 2, wanted * days)]
    used: set[str] = set()
    result: list[DayPlan] = []
    for day in range(1, days + 1):
        # 每一天取“离城市中心/前一天最后一点最近”的一簇；rule_v0 用简单的最近邻切片。
        remaining = [r for r in pool if r["name"] not in used]
        if not remaining:
            remaining = [r for r in ranked if r["name"] not in used]
        anchor = arrival if day == 1 else result[-1].attractions[-1] if result[-1].attractions else center
        picked = _nearest(remaining, anchor)[:wanted]
        for r in picked:
            used.add(r["name"])
        result.append(DayPlan(day=day, attractions=picked))
    return result


def select_meals(task, needs, sandbox: Sandbox, days: list[DayPlan], hotel: dict | None) -> None:
    city = task.target_city
    restaurants = [r for r in sandbox.restaurants(city) if _restaurant_ok(r)]
    preferred = needs.get("cuisine_pref")
    used: set[str] = set()
    for day in days:
        anchor = _center(day.attractions) if day.attractions else _center(restaurants)
        for kind, window, start in (
            ("lunch", LUNCH_WINDOW, "11:30"),
            ("dinner", DINNER_WINDOW, "17:30"),
        ):
            if kind == "dinner" and day.day == len(days) and task.days > 1:
                # 最后一天晚餐可选；先留着，排程时如果赶不上返程会跳过。
                pass
            cands = [r for r in restaurants if r["name"] not in used and _open_during(r, *window)]
            if preferred:
                pref_cands = [r for r in cands if r["cuisine"] == preferred]
                if pref_cands:
                    cands = pref_cands
            if not cands:
                continue
            pick = _nearest(cands, anchor)[0]
            used.add(pick["name"])
            setattr(day, kind, pick)


def select_hotel(task, needs, sandbox: Sandbox, day1: DayPlan) -> dict | None:
    city = task.target_city
    hotels = [r for r in sandbox.hotels(city) if _hotel_ok(r)]
    if not hotels:
        return None
    anchor = day1.attractions[-1] if day1.attractions else _center(hotels)
    pref = needs.get("hotel_pref")
    if pref == "经济型":
        cheap = [h for h in hotels if h["price"] <= 600]
        hotels = cheap or hotels
    elif pref == "高档":
        high = [h for h in hotels if h["price"] >= 800]
        hotels = high or hotels
    # 房价别把预算吃光：优先 300–900，再退到最近。
    mid = [h for h in hotels if 300 <= h["price"] <= 900]
    pool = mid or hotels
    return _nearest(pool, anchor)[0]


def _arrival_min(option: dict) -> int:
    """到达分钟数；跨夜班次按次日加 24h。"""
    begin = to_min(option["begin"]) or 0
    end = to_min(option["end"]) or 0
    return end if end > begin else end + 24 * 60


def select_intercity(task, sandbox: Sandbox, pref: str | None = None) -> tuple[dict | None, dict | None]:
    if task.start_city == task.target_city:
        return None, None
    out_opts = sandbox.intercity_options(task.start_city, task.target_city, pref)
    back_opts = sandbox.intercity_options(task.target_city, task.start_city, pref)
    # 去程：优先当天 12:30 前到达，选到达最早的一班（早班机通常比长途动车更省白天）。
    out = None
    same_day = [o for o in out_opts if _arrival_min(o) <= 24 * 60]
    morning = [o for o in same_day if _arrival_min(o) <= to_min("12:30")]
    pool = morning or same_day or out_opts
    if pool:
        out = min(pool, key=_arrival_min)
    # 返程：优先 17:00 后出发、当天 23:30 前到达，选出发最晚的一班。
    back = None
    same_day_back = [o for o in back_opts if _arrival_min(o) <= 24 * 60]
    evening = [o for o in same_day_back
               if to_min(o["begin"]) >= to_min("17:00") and _arrival_min(o) <= to_min("23:30")]
    pool = evening or same_day_back or back_opts
    if pool:
        back = max(pool, key=lambda o: to_min(o["begin"]) or 0)
    return out, back


def _transport_payload(segments: list[dict], people: int) -> list[dict]:
    out = []
    for seg in segments:
        tool_cost = seg.get("cost", 0) or 0
        mode = seg.get("mode")
        if mode == "walk":
            price, cost, tickets = 0, 0, people
        elif mode == "metro":
            price, tickets = tool_cost, people
            cost = price * tickets
        else:
            price, tickets, cars = tool_cost, None, 1
            cost = price * cars
        item = {
            "start": seg["start"],
            "end": seg["end"],
            "mode": mode,
            "start_time": seg["start_time"],
            "end_time": seg["end_time"],
            "price": price,
            "cost": cost,
            "distance": seg.get("distance", 0),
        }
        if tickets is not None:
            item["tickets"] = tickets
        if mode == "taxi":
            item["cars"] = 1
        out.append(item)
    return out


def _mode_for(a: dict, b: dict, needs: dict) -> str:
    pref = needs.get("transport_pref")
    if pref in ("walk", "metro", "taxi"):
        return pref
    return "walk" if _km(a, b) <= 1.2 else "metro"


def _goto_with_fallback(sandbox: Sandbox, city: str, a: dict, b: dict, t: str, needs: dict):
    first = _mode_for(a, b, needs)
    order = [first] + [m for m in ("metro", "taxi", "walk") if m != first]
    for mode in order:
        segs = sandbox.goto(city, a["name"], b["name"], t, mode)
        if segs:
            return segs
    return None


def _attraction_activity(row: dict, transports: list[dict], start: str, end: str, people: int) -> dict:
    price = float(row["price"])
    return {
        "position": row["name"],
        "type": "attraction",
        "start_time": start,
        "end_time": end,
        "price": price,
        "tickets": people,
        "cost": price * people,
        "transports": transports,
    }


def _meal_activity(kind: str, row: dict, transports: list[dict], start: str, end: str, people: int) -> dict:
    price = float(row["price"])
    return {
        "position": row["name"],
        "type": kind,
        "start_time": start,
        "end_time": end,
        "price": price,
        "cost": price * people,
        "transports": transports,
    }


def _hotel_activity(row: dict, transports: list[dict], start: str, end: str, people: int) -> dict:
    rooms = math.ceil(people / max(1, int(row["numbed"])))
    price = float(row["price"])
    return {
        "position": row["name"],
        "type": "accommodation",
        "start_time": start,
        "end_time": end,
        "price": price,
        "room_type": int(row["numbed"]),
        "rooms": rooms,
        "cost": price * rooms,
        "transports": transports,
    }


def _intercity_activity(train: dict, transports: list[dict], people: int) -> dict:
    price = float(train["cost"])
    act = {
        "type": train["type"],
        "start": train["from"],
        "end": train["to"],
        "start_time": train["begin"],
        "end_time": train["end"],
        "price": price,
        "tickets": people,
        "cost": price * people,
        "transports": transports,
    }
    act["TrainID" if train["type"] == "train" else "FlightID"] = train["id"]
    return act


def _fit_visit(row: dict, arrival: str, hard_end: str | None = None) -> tuple[str, str] | None:
    """在开放时间内安排一次参观；排不下返回 None。"""
    open_min = to_min(row["opentime"])
    close_min = to_min(row["endtime"])
    if open_min is None or close_min is None:
        return None
    if close_min <= open_min:
        close_min += 24 * 60
    arrive = to_min(arrival) or 0
    if arrive < open_min:
        arrive = open_min
    dur = _duration_min(row)
    end = arrive + dur
    limit = close_min
    if hard_end:
        limit = min(limit, to_min(hard_end) or limit)
    if arrive + 30 > limit:
        return None
    if end > limit:
        end = limit
    if end - arrive < 30:
        return None
    return from_min(arrive), from_min(end)


def _meal_slot(kind: str, row: dict, arrival: str, hard_end: str | None = None) -> tuple[str, str] | None:
    lo, hi = LUNCH_WINDOW if kind == "lunch" else DINNER_WINDOW
    arrive = to_min(arrival) or 0
    open_min = to_min(row.get("opentime"))
    close_min = to_min(row.get("endtime"))
    if open_min is None or close_min is None:
        return None
    if close_min <= open_min:
        close_min += 24 * 60
    start = max(arrive, to_min(lo) or 0, open_min)
    end = start + MEAL_MINUTES
    limit = min(to_min(hi) or end, close_min)
    if hard_end:
        limit = min(limit, to_min(hard_end) or limit)
    if start >= limit:
        return None
    if end > limit:
        end = limit
    if end - start < 30:
        return None
    return from_min(start), from_min(end)


def schedule_plan(task, needs, sandbox: Sandbox, days: list[DayPlan], hotel: dict | None,
                  outbound: dict | None, back: dict | None) -> dict:
    city = task.target_city
    people = int(task.people)
    activities_by_day: list[list[dict]] = []
    prev = None
    current = None
    for idx, day in enumerate(days, 1):
        acts: list[dict] = []
        if idx == 1 and outbound:
            acts.append(_intercity_activity(outbound, [], people))
            prev = sandbox.resolve(city, outbound["to"]) or {"name": outbound["to"], "lat": 0.0, "lon": 0.0}
            current = outbound["end"]
        else:
            if idx > 1 and hotel:
                prev = sandbox.resolve(city, hotel["name"]) or hotel
            elif prev is None:
                prev = sandbox.resolve(city, hotel["name"]) if hotel else _center(days[0].attractions)
            current = "08:30"
        # 最后一天给返程留时间
        hard_end = None
        if idx == len(days) and back:
            # 返程前要留出到车站的实际车程；45 分钟对远郊最后一站不够（深圳实测 85 分钟）。
            hard_end = from_min((to_min(back["begin"]) or 0) - 120)
        items = []
        for a in day.attractions:
            items.append(("attraction", a))
        # 午餐插在第一站之后，晚餐在最后一站之后
        if day.lunch:
            items.insert(1, ("lunch", day.lunch))
        if day.dinner:
            items.append(("dinner", day.dinner))
        for kind, row in items:
            t = current or "08:30"
            segs = _goto_with_fallback(sandbox, city, prev, row, t, needs)
            if not segs:
                continue
            arrive = segs[-1]["end_time"]
            if kind == "attraction":
                slot = _fit_visit(row, arrive, hard_end)
                if not slot:
                    continue
                start, end = slot
                acts.append(_attraction_activity(row, _transport_payload(segs, people), start, end, people))
            else:
                slot = _meal_slot(kind, row, arrive, hard_end)
                if not slot:
                    continue
                start, end = slot
                acts.append(_meal_activity(kind, row, _transport_payload(segs, people), start, end, people))
            prev = row
            current = end
        if idx < len(days) and hotel:
            t = current or HOTEL_START
            segs = _goto_with_fallback(sandbox, city, prev, hotel, t, needs)
            if segs:
                arrive = segs[-1]["end_time"]
                start = max(arrive, HOTEL_START)
                if to_min(start) < to_min("24:00"):
                    acts.append(_hotel_activity(hotel, _transport_payload(segs, people), start, "24:00", people))
                    prev = hotel
                    current = "24:00"
        if idx == len(days) and back:
            dest = sandbox.resolve(city, back["from"]) or {"name": back["from"], "lat": 0.0, "lon": 0.0}
            segs = None
            # 返程前用实际车程回查：远郊最后一站到机场/车站可能超过固定预留，逐条摘掉。
            for _ in range(5):
                segs = _goto_with_fallback(sandbox, city, prev, dest, current or "17:00", needs)
                if segs and segs[-1]["end_time"] <= back["begin"]:
                    break
                if not acts or acts[-1].get("type") in ("train", "airplane"):
                    break
                acts.pop()
                if acts:
                    last = acts[-1]
                    name = last.get("position") or last.get("start") or last.get("end")
                    prev = sandbox.resolve(city, name) or prev
                    current = last["end_time"]
                elif idx == 1 and outbound:
                    prev = sandbox.resolve(city, outbound["to"]) or prev
                    current = outbound["end"]
                elif hotel:
                    prev = sandbox.resolve(city, hotel["name"]) or prev
                    current = "08:30"
            if not segs or segs[-1]["end_time"] > back["begin"]:
                raise PlanError("intercity_infeasible", f"cannot reach {back['from']} before {back['begin']}")
            acts.append(_intercity_activity(back, _transport_payload(segs, people), people))
        activities_by_day.append(acts)
    return {
        "people_number": people,
        "start_city": task.start_city,
        "target_city": city,
        "itinerary": [
            {"day": i, "activities": acts} for i, acts in enumerate(activities_by_day, 1)
        ],
    }


def build_rule_plan(task, needs, sandbox: Sandbox) -> tuple[dict, dict]:
    outbound, back = select_intercity(task, sandbox, needs.get("intercity_pref"))
    days = select_days(task, needs, sandbox, outbound)
    select_meals(task, needs, sandbox, days, None)
    hotel = select_hotel(task, needs, sandbox, days[0]) if task.days > 1 else None
    plan = schedule_plan(task, needs, sandbox, days, hotel, outbound, back)
    meta = {
        "outbound": outbound,
        "back": back,
        "hotel": hotel,
        "days": [
            {
                "day": d.day,
                "attractions": [a["name"] for a in d.attractions],
                "lunch": d.lunch["name"] if d.lunch else None,
                "dinner": d.dinner["name"] if d.dinner else None,
            }
            for d in days
        ],
        "cuisine_pref": needs.get("cuisine_pref"),
    }
    return plan, meta
