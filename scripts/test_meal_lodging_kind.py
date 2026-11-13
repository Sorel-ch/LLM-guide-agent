# -*- coding: utf-8 -*-
"""T5 餐宿类型一致性的离线回归（0 次外部调用）。

背景：台账里带高德类别（餐饮服务/住宿服务/购物服务…），但模型会把**非餐厅**
写进 meal——上海实测把「上海站南广场」当午餐、成都把「天府红购物中心」当饭点。
用户报的就是"景点之间没有给出吃饭的地点"。

本测试锁三件事：
1. 能吃饭的地方放行（**白名单会误杀老街/夜市，所以用黑名单**）；
2. 明确不能吃饭的类别（车站/购物中心/公司）→ 必须报出来；
3. 名字明确是吃的，类别再可疑也放行。

判据为什么是黑名单：本仓库用关键词判场所类型栽过一次（大雁塔景区 vs 大雁塔商圈，
只差两个字性质相反）。白名单要求"类别必须是餐饮服务"会把南塘老街、回民街、老外滩
一起误杀——而它们在宁波实测里正是模型排得对的饭点。

跑法：
    python scripts/test_meal_lodging_kind.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_agent as pa  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = 0, 0


def check(name, got, want, note=""):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{got!r}"
          + ("" if ok else f"（期望 {want!r}）") + (f" | {note}" if note else ""))


def row(kind, name):
    return {"type": kind, "name": name}


def poi(typecode_type, name=""):
    return {"type": typecode_type, "lon": "121.5", "lat": "29.8", "name": name}


def main() -> int:
    f = pa.check_meal_lodging_kind

    print("\n-- 1. 类别对得上就放行 --")
    check("餐厅 类=餐饮服务", f(row("meal", "阿毛饭店"), poi("餐饮服务;中餐厅")), None)
    check("酒店 类=住宿服务", f(row("hotel", "某某酒店"), poi("住宿服务;宾馆酒店")), None)

    print("\n-- 2. 类别对不上要报出来（这就是用户报的那个问题）--")
    r = f(row("meal", "上海站南广场"), poi("交通设施服务;火车站"))
    check("车站当午餐", r is not None, True, str(r))
    r = f(row("meal", "天府红购物中心"), poi("购物服务;购物中心"))
    check("购物中心当饭点", r is not None, True, str(r))
    r = f(row("hotel", "南京路步行街"), poi("购物服务;步行街"))
    check("步行街当住宿", r is not None, True, str(r))

    print("\n-- 3. 觅食区放行：类别不是餐饮，但确实能吃饭 --")
    for name, tp in (("南塘老街", "风景名胜;风景名胜"), ("回民街", "购物服务;特色街区"),
                     ("城隍庙小吃街", "风景名胜"), ("老外滩", "风景名胜;风景名胜"),
                     ("宽窄巷子", "风景名胜;风景名胜")):
        check(f"{name} 放行", f(row("meal", name), poi(tp, name)), None,
              "老街/夜市/街区能吃饭，白名单会误杀它们，所以用黑名单")
    check("商场里有食街也放行",
          f(row("meal", "某某美食广场"), poi("购物服务;购物中心")), None,
          "名字明确是吃的，类别再可疑也放行")

    print("\n-- 4. 查不到台账时不判（不制造第二套口径）--")
    check("poi=None 放行", f(row("meal", "某个编的名字"), None), None,
          "查不到由补查轮负责，这里不重复判")

    print("\n-- 5. 交通段/景点不参与这个校验 --")
    check("景点条目不判", f(row("attraction", "外滩"), poi("风景名胜")), None)
    check("交通段不判", f(row("transit", "甲→乙"), poi("交通设施服务")), None)

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
