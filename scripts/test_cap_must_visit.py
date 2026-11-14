# -*- coding: utf-8 -*-
"""必去点不被 cap_day_sights 裁掉的离线回归（0 次外部调用）。

成都 2026-11-14 实测：题目点名「大熊猫繁育研究基地」，模型写了 5 个景点，
cap_day_sights 按最短开放路径留下城南三个免费公园，把熊猫基地和熊猫博物馆裁了，
终稿主题还写着“熊猫基地”。这里锁住：
1. 必去点（含同园子点、短名、解析不出坐标的）一律不裁；
2. 必去点自身超过每天上限时全留，非必去点让位，并返回超限说明；
3. 没有必去点时行为与旧算法一致；
4. 裁景点后两侧交通段仍缝合。

跑法：
    python scripts/test_cap_must_visit.py
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
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{got!r}"
          + ("" if ok else f"（期望 {want!r}）") + (f" | {note}" if note else ""))


POIS = {
    "成都大熊猫繁育研究基地": {"coord": "104.146609,30.733489", "lon": "104.146609",
                              "lat": "30.733489", "rating": "4.7"},
    "成都大熊猫博物馆": {"coord": "104.146000,30.733000", "lon": "104.146000",
                        "lat": "30.733000", "rating": "4.5"},
    "熊猫塔": {"coord": "104.147000,30.734000", "lon": "104.147000",
              "lat": "30.734000", "rating": "4.4"},
    "熊猫书店(成都大熊猫基地店)": {"coord": "104.146500,30.733500", "lon": "104.146500",
                                 "lat": "30.733500", "rating": "4.2"},
    "交子公园": {"coord": "104.072828,30.582568", "lon": "104.072828",
                "lat": "30.582568", "rating": "4.5"},
    "铁像寺水街": {"coord": "104.050038,30.557844", "lon": "104.050038",
                  "lat": "30.557844", "rating": "4.4"},
    "大源中央公园": {"coord": "104.049182,30.549059", "lon": "104.049182",
                    "lat": "30.549059", "rating": "4.3"},
}
MUST = "成都大熊猫繁育研究基地"
SOUTH = ["交子公园", "铁像寺水街", "大源中央公园"]


def day_of(names, with_transits=False):
    items = []
    for i, n in enumerate(names):
        if with_transits and i:
            items.append({"type": "transit", "name": f"{names[i - 1]}→{n}"})
        items.append({"type": "attraction", "name": n})
    return {"day": 1, "items": items}


def kept_names(groups):
    return [i["name"] for i in groups[0]["items"] if i.get("type") == "attraction"]


def main() -> int:
    print("\n-- 1. 没有必去点：仍按旧算法留 3 个、裁最远的 --")
    g, dropped, over = pa.cap_day_sights(
        [day_of(["熊猫塔", "成都大熊猫博物馆", "成都大熊猫繁育研究基地", "交子公园"])], POIS)
    check("留 3 个", len(kept_names(g)), 3)
    check("裁掉的是远处的交子公园", [n for _, n in dropped], ["交子公园"])
    check("无超限说明", over, [])

    print("\n-- 2. 必去点本身 + 同园子点全保，非必去让位 --")
    names = [MUST, "成都大熊猫博物馆", "熊猫塔", *SOUTH]
    g, dropped, over = pa.cap_day_sights([day_of(names, with_transits=True)], POIS, [MUST])
    kept = kept_names(g)
    check("三个熊猫条目都在", sorted(kept), sorted([MUST, "成都大熊猫博物馆", "熊猫塔"]))
    check("必去点没进 dropped", [MUST in n for _, n in dropped], [False] * len(dropped))
    check("三个城南点全裁", sorted(n for _, n in dropped), sorted(SOUTH))
    check("恰好 3 个不触发超限", over, [])

    print("\n-- 3. 用户只写短名「大熊猫基地」也能保护 --")
    g, _dropped, _ = pa.cap_day_sights([day_of(names)], POIS, ["大熊猫基地"])
    check("基地被保留", MUST in kept_names(g), True)
    check("园内子点一起保留", "熊猫塔" in kept_names(g), True)

    print("\n-- 4. 必去点超过每日上限：全留并记一条说明 --")
    over_names = [MUST, "成都大熊猫博物馆", "熊猫塔", "熊猫书店(成都大熊猫基地店)", *SOUTH]
    g, dropped, over = pa.cap_day_sights([day_of(over_names, with_transits=True)], POIS, [MUST])
    check("4 个必去点全留", len(kept_names(g)), 4)
    check("非必去点全裁", sorted(n for _, n in dropped), sorted(SOUTH))
    check("超限说明 1 条", len(over), 1)
    check("说明里带 day 与上限", (over[0]["day"], over[0]["limit"]), (1, pa.SIGHTS_MAX_PER_DAY))

    print("\n-- 5. 解析不出坐标的必去点也照样保留 --")
    odd = "神秘必去点"
    g, _dropped, _ = pa.cap_day_sights(
        [day_of([odd, *SOUTH])], POIS, [odd])
    check("无坐标必去点保留", odd in kept_names(g), True)

    print("\n-- 6. 幂等：第二遍不再裁，也不再报超限 --")
    day = day_of(names, with_transits=True)
    pa.cap_day_sights([day], POIS, [MUST])
    g2, dropped2, over2 = pa.cap_day_sights([day], POIS, [MUST])
    check("第二遍 dropped 为空", dropped2, [])
    check("第二遍无超限", over2, [])
    check("第二遍景点数仍为 3", len(kept_names(g2)), 3)

    print("\n-- 7. 裁景点后两侧交通段要缝合 --")
    stitched = day_of(["成都大熊猫繁育研究基地", "交子公园", "成都大熊猫博物馆",
                       "铁像寺水街", "熊猫塔"], with_transits=True)
    g, _dropped, _ = pa.cap_day_sights([stitched], POIS, [MUST])
    transits = [i["name"] for i in g[0]["items"] if i.get("type") == "transit"]
    check("裁掉交子公园后缝出 基地→博物馆", "成都大熊猫繁育研究基地→成都大熊猫博物馆" in transits,
          True, f"实际 {transits}")

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
