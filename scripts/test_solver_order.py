# -*- coding: utf-8 -*-
"""T4 求解器接入的离线回归（0 次外部调用；不装 pyvrp 也应全过）。

锁住的是**开关与回退**，不是求解质量：
1. 未装 pyvrp / TRAVEL_SOLVER=0 / 点不足 / 缺坐标 → 一律 ok=False 并给出原因；
2. ok=False 时 order 必须原样返回输入顺序（调用方据此不动方案）；
3. 这些路径都不得抛异常——求解器是增强项，挂了不能带崩管线。

装不装 pyvrp 都该全过：装了走求解、没装走回退，断言只压公共契约。

跑法：
    python scripts/test_solver_order.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_agent as pa  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = 0, 0


def check(name: str, got, want, note: str = "") -> None:
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{got!r}"
          + ("" if ok else f"（期望 {want!r}）") + (f" | {note}" if note else ""))


def pois_of(*items):
    return {n: {"coord": f"{lon},{lat}", "lon": str(lon), "lat": str(lat)}
            for n, lon, lat in items}


def main() -> int:
    # 宁波四个真实位置
    P = pois_of(
        ("天一阁", "121.5357", "29.8734"),
        ("月湖", "121.5412", "29.8701"),
        ("老外滩", "121.5610", "29.8789"),
        ("南塘老街", "121.5382", "29.8535"),
    )

    print("\n-- 1. 少于 2 个点直接回退 --")
    r = pa.solve_day_order(["天一阁"], P, [])
    check("ok=False", r["ok"], False)
    check("order 原样返回", r["order"], ["天一阁"])
    check("给了原因", bool(r["reason"]), True, r["reason"])

    print("\n-- 2. 缺坐标的点回退，不崩 --")
    bad = dict(P)
    bad["幽灵点"] = {"coord": "", "lon": "", "lat": ""}
    r = pa.solve_day_order(["天一阁", "幽灵点"], bad, [])
    check("ok=False", r["ok"], False)
    check("原因提到坐标", "坐标" in r["reason"], True, r["reason"])
    check("输入顺序原样", r["order"], ["天一阁", "幽灵点"])

    print("\n-- 3. TRAVEL_SOLVER=0 时整体关闭 --")
    old = os.environ.get("TRAVEL_SOLVER")
    os.environ["TRAVEL_SOLVER"] = "0"
    try:
        pa_ok, why = pa._solver_available()
        check("可用性为 False", pa_ok, False)
        check("原因说明已关", "关" in why or "TRAVEL_SOLVER" in why, True, why)
    finally:
        if old is None:
            os.environ.pop("TRAVEL_SOLVER", None)
        else:
            os.environ["TRAVEL_SOLVER"] = old

    print("\n-- 4. 两点正常情形：要么给出顺序、要么明确回退（取决于装没装 pyvrp）--")
    r = pa.solve_day_order(["天一阁", "月湖"], P, [])
    check("返回结构齐全", set(["ok", "order", "reason"]) <= set(r), True)
    if r["ok"]:
        check("点集不变（只换顺序）", sorted(r["order"]), sorted(["天一阁", "月湖"]))
        check("带回了矩阵口径", "basis" in r and len(r["basis"]) == 2, True,
              "两个方向各一条")
    else:
        check("回退时顺序原样", r["order"], ["天一阁", "月湖"])
        print(f"       （本机未接求解器：{r['reason']}）")

    print("\n-- 5. 矩阵与求解共用一套口径 --")
    m = pa.travel_matrix(["天一阁", "月湖", "老外滩"], P, [])
    check("三点两两有向共 6 条", m["pairs"], 6)
    check("全部带 basis", all(v.startswith(("measured", "straight")) for v in m["basis"].values()),
          True)
    check("分钟都是正整数", all(isinstance(v, int) and v > 0 for v in m["minutes"].values()), True)

    print("\n-- 6. 重排只换景点次序：餐宿归位、交通段重写、点集不变 --")
    day = {"day": 1, "items": [
        {"type": "attraction", "name": "天一阁"},
        {"type": "transit", "name": "天一阁→月湖", "cost": 0},
        {"type": "attraction", "name": "月湖"},
        {"type": "meal", "name": "午餐店"},
        {"type": "transit", "name": "月湖→老外滩", "cost": 2},
        {"type": "attraction", "name": "老外滩"},
        {"type": "transit", "name": "老外滩→酒店", "cost": 5},
    ]}
    groups_out, trace = pa.reorder_day_with_solver([day], P, [])
    items = groups_out[0]["items"]
    sights = [i["name"] for i in items if i.get("type") == "attraction"]
    check("景点集合不变", sorted(sights), sorted(["天一阁", "月湖", "老外滩"]))
    meals = [i for i in items if i.get("type") == "meal"]
    check("餐宿条数不变", len(meals), 1, "求解器不决定餐宿插哪")
    kinds = [i for i in items if i.get("type") == "transit"]
    check("交通段条数不变", len(kinds), 3)
    # 首条交通段必须以当天第一个景点为起点
    first_transit = next((i for i in items if i.get("type") == "transit"), None)
    check("交通段起点跟新顺序一致",
          first_transit["name"].split("→")[0], sights[0],
          f"实际 {first_transit['name']}")
    last_transit = [i for i in items if i.get("type") == "transit"][-1]
    check("末段终点仍是酒店", last_transit["name"].endswith("→酒店"), True,
          f"实际 {last_transit['name']}")
    check("末段起点跟到最后一个实体", last_transit["name"].split("→")[0], sights[-1],
          "收尾回宿的腿不随景点次序变，但起点要跟到新末站")
    # 交通段一条都不能丢：早先只把 meal/hotel 收进重建列表，重排后当天一条腿都不剩。
    check("交通段一条都没丢", len(kinds), 3, "重排不能吞腿")

    print("\n-- 7. 重写交通段：首条腿保留原起点（到达枢纽不能丢） --")
    lead = [{"type": "transit", "name": "成都东站→成都大熊猫繁育研究基地"},
            {"type": "attraction", "name": "熊猫塔"},
            {"type": "attraction", "name": "成都大熊猫繁育研究基地"}]
    spliced = pa._resplice_transits(lead)
    check("首条腿起点仍是到达枢纽", spliced[0]["name"], "成都东站→熊猫塔")
    mid = pa._resplice_transits([
        {"type": "attraction", "name": "熊猫塔"},
        {"type": "transit", "name": "熊猫基地→熊猫塔"},
        {"type": "attraction", "name": "熊猫基地"}])
    check("站间腿按新相邻关系重写", mid[1]["name"], "熊猫塔→熊猫基地")
    tail = pa._resplice_transits([
        {"type": "attraction", "name": "熊猫塔"},
        {"type": "transit", "name": "旧点→酒店"}])
    check("末段终点仍是酒店", tail[1]["name"], "熊猫塔→酒店")

    print("\n-- 8. 重排后时间轴回填：时间单调、留白不被压缩 --")
    items = [
        {"type": "transit", "name": "成都东站→熊猫塔", "start_time": "07:30",
         "end_time": "08:45", "_t_gap": 0, "_t_dur": 75},
        {"type": "attraction", "name": "熊猫塔", "start_time": "12:20",
         "end_time": "13:00", "_t_gap": 0, "_t_dur": 40},
        {"type": "attraction", "name": "熊猫基地", "start_time": "08:45",
         "end_time": "11:30", "_t_gap": 0, "_t_dur": 165},
        {"type": "attraction", "name": "熊猫博物馆", "start_time": "11:30",
         "end_time": "12:20", "_t_gap": 0, "_t_dur": 50},
        {"type": "transit", "name": "博物馆→午饭", "start_time": "13:00",
         "end_time": "14:30", "_t_gap": 0, "_t_dur": 90},
        {"type": "meal", "name": "午饭", "start_time": "14:30", "end_time": "15:30",
         "_t_gap": 0, "_t_dur": 60},
        {"type": "transit", "name": "午饭→晚饭", "start_time": None, "end_time": None,
         "_t_gap": 0, "_t_dur": 15},
        {"type": "meal", "name": "晚饭", "start_time": "19:00", "end_time": "20:30",
         "_t_gap": 210, "_t_dur": 90},
        {"type": "transit", "name": "晚饭→东站", "start_time": "20:30",
         "end_time": "21:00", "_t_gap": 0, "_t_dur": 30},
        {"type": "hotel", "name": "酒店", "start_time": None, "end_time": None,
         "_t_gap": 0, "_t_dur": None},
    ]
    pa._retime_items(items, 7 * 60 + 30)
    check("时间随新顺序严格递增",
          [i["start_time"] for i in items],
          ["07:30", "08:45", "09:25", "12:10", "13:00",
           "14:30", "15:30", "19:15", "20:45", None])
    check("晚饭留白保留", items[7]["start_time"], "19:15")
    check("临时字段已清掉", any("_t_gap" in i or "_t_dur" in i for i in items), False)
    check("无时长的住宿原样保留", items[-1]["start_time"], None)

    print("\n-- 9. 求解失败时整份回退，不做部分替换 --")
    bad_day = {"day": 2, "items": [
        {"type": "attraction", "name": "幽灵点"},
        {"type": "attraction", "name": "月湖"},
    ]}
    back, tr = pa.reorder_day_with_solver([bad_day], P, [])
    check("返回原 groups", back[0] is bad_day or back[0] == bad_day, True)
    check("trace 说明失败", tr[0].get("ok"), False)
    check("顺序未被改动", [i["name"] for i in back[0]["items"]], ["幽灵点", "月湖"])

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
