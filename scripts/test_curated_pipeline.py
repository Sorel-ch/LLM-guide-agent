"""受控策展管线的零网络回归：模型只选 ID，代码负责可执行性。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_agent as pa
from planning.experts import hospitality_for_day, parse_curation
from planning.quality import correct_item_types, repair_opening
from planning.orchestrator import run_curated_plan
from planning.schedule import schedule_day


def poi(kind, lon, lat, price=None, hours=None):
    return {"type": kind, "typecode": "", "lon": lon, "lat": lat,
            "coord": f"{lon},{lat}", "price": price, "opentime": hours}


POIS = {
    "动漫馆": poi("科教文化", 121.0, 31.0),
    "老街": poi("风景名胜", 121.01, 31.01),
    "普通公园": poi("风景名胜", 121.02, 31.02),
    "晚间餐厅": poi("餐饮服务", 121.011, 31.011, 60, "16:00-24:00"),
    "午间餐厅": poi("餐饮服务", 121.012, 31.012, 50, "09:00-21:00"),
    "酒店": poi("住宿服务", 121.013, 31.013, 200),
}


def test_selection():
    selected, warnings = parse_curation({"selected": [{"candidate_id": "动漫馆"},
        {"candidate_id": "虚构点"}, {"candidate_id": "午间餐厅"}]}, POIS, {"must_visit": ["老街"]}, 4)
    assert selected == ["老街", "动漫馆"] and len(warnings) == 2
    selected = hospitality_for_day(["动漫馆", "老街"], POIS, pa.km_between)
    assert selected["hotel"] == "酒店" and selected["lunch"] != selected["dinner"]
    groups = [{"day": 1, "items": [{"name": "老街", "type": "meal", "start_time": "10:00", "end_time": "11:00"},
        {"name": "晚间餐厅", "type": "attraction", "start_time": "11:40", "end_time": "12:30"}]}]
    assert correct_item_types(groups, POIS)
    assert repair_opening(groups, POIS)
    assert groups[0]["items"][0]["type"] == "meal"  # 特色街区可以是合理餐点，不能一刀切。
    assert groups[0]["items"][1]["name"] == "午间餐厅"


def test_orchestrator():
    ext = {"pois": POIS, "adcode": "310100", "results": [], "legs": [], "external_count": 0}
    pa.llm = lambda *a, **k: {"text": json.dumps({"selected": [{"candidate_id": "动漫馆"},
        {"candidate_id": "老街"}]}), "seconds": 0.1}
    pa.plan_leg_topup = lambda *a, **k: []
    marks = []
    plan = run_curated_plan(pa, {"destination_city": "上海", "days": 1, "themes": ["动漫"],
                                 "budget_yuan": 500}, "测试", ext, [], "", "2026-11-20", "off", True, 32, marks.append)
    assert not plan["degraded"] and plan["days_count"] == 1
    assert plan["sources"]["llm_calls"] == 1
    assert all(i["name"] != "虚构点" for i in plan["days"][0]["timeline"])
    assert plan["budget_summary"]["known_total"] > 0


if __name__ == "__main__":
    test_selection()
    test_orchestrator()
    day = {"day": 1, "items": [{"name": "杭州→南京", "type": "transit"},
           {"name": "景点", "type": "attraction"}, {"name": "午餐", "type": "meal"},
           {"name": "晚餐", "type": "meal"}]}
    schedule_day(day, [])
    assert day["items"][0]["start_time"] == "07:30" and day["items"][0]["end_time"] == "09:00"
    assert day["items"][2]["start_time"] == "11:30" and day["items"][3]["start_time"] == "17:00"
    print("3 组策展编排测试通过")
