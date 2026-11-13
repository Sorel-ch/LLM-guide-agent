# -*- coding: utf-8 -*-
"""T3 车程矩阵的离线回归（0 次外部调用）。

锁住三条口径：
1. 有实测腿时用实测值，并按坐标匹配（不按名字，台账名与正文名常不一致）；
2. 没实测时回退直线折算，且 basis 必须标出来，不许冒充实测；
3. 步行/驾车两条折算法的分界与方向上合理（近处步行更慢、远处驾车更快）。

跑法：
    python scripts/test_travel_matrix.py
"""
from __future__ import annotations

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
    if ok:
        PASS += 1
    else:
        FAIL += 1
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}：{got!r}" + (f"（期望 {want!r}）" if not ok else "") + (f" | {note}" if note else ""))


def pois_of(*items):
    """items: (name, lon, lat)"""
    return {n: {"coord": f"{lon},{lat}", "lon": str(lon), "lat": str(lat)} for n, lon, lat in items}


def main() -> int:
    # 宁波三个真实点（约等于实际坐标）
    pois = pois_of(
        ("天一阁", "121.5357", "29.8734"),
        ("宁波博物院", "121.5409", "29.8173"),
        ("东钱湖", "121.6514", "29.7658"),
    )

    print("\n-- 1. 有实测腿时用实测值 --")
    legs = [{
        "a": "天一阁", "b": "宁波博物院",
        "a_coord": "121.5357,29.8734", "b_coord": "121.5409,29.8173",
        "mode": "transit", "minutes": 41, "fee": 2, "path": [],
    }]
    m = pa.travel_matrix(["天一阁", "宁波博物院", "东钱湖"], pois, legs)
    check("实测对命中", m["minutes"][("天一阁", "宁波博物院")], 41)
    check("来源标 measured", m["basis"][("天一阁", "宁波博物院")], "measured:transit")
    check("反向借值并标 measured_reverse",
          m["basis"][("宁波博物院", "天一阁")], "measured_reverse:transit",
          "direction 的 OD 有向，反向只能借值且必须标出来，不能当实测")

    print("\n-- 2. 无实测时回退直线，且标 basis --")
    check("缺实测的点有值", ("天一阁", "东钱湖") in m["minutes"], True)
    basis = m["basis"][("天一阁", "东钱湖")]
    check("basis 是折算而非 measured", basis.startswith("straight_"), True, f"实际 {basis}")
    check("折算值 > 0", m["minutes"][("天一阁", "东钱湖")] > 0, True)
    check("统计里 measured / measured_reverse / 折算三类分开计",
          sorted(m["stats"]) == ["measured", "measured_reverse", "straight_drive"],
          True, str(m["stats"]))

    print("\n-- 3. 近处走步行口径 --")
    near = pois_of(("A", "121.5000", "29.8700"), ("B", "121.5060", "29.8700"))
    m2 = pa.travel_matrix(["A", "B"], near, [])
    km = pa.km_between(near["A"], near["B"])
    check("两点不足 2.5 km", km < 2.5, True, f"{km:.2f} km")
    check("basis=straight_walk", m2["basis"][("A", "B")], "straight_walk")
    # 步行 4.5 km/h 比市区驾车 20 km/h 慢，近处就该更慢
    walk_min = m2["minutes"][("A", "B")]
    drive_would_be = max(1, round(km / 20.0 * 60))
    check("步行耗时不低于同距离驾车估算", walk_min >= drive_would_be, True,
          f"walk {walk_min}′ vs drive {drive_would_be}′")

    print("\n-- 4. 不认识的条目跳过，不崩 --")
    m3 = pa.travel_matrix(["天一阁", "不存在的点", "东钱湖"], pois, [])
    check("只在已知点之间建矩阵",
          all("不存在的点" not in k for k in m3["minutes"]), True)

    print("\n-- 5. 实测值优先于折算（同名坐标冲突时不静默降级）--")
    legs2 = [{
        "a": "东钱湖", "b": "天一阁",
        "a_coord": "121.6514,29.7658", "b_coord": "121.5357,29.8734",
        "mode": "driving", "minutes": 77, "fee": None, "path": [],
    }]
    m4 = pa.travel_matrix(["天一阁", "东钱湖"], pois, legs2)
    check("有向实测按 OD 方向落位", m4["minutes"][("东钱湖", "天一阁")], 77)
    check("来源记 driving", m4["basis"][("东钱湖", "天一阁")], "measured:driving")

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
