# -*- coding: utf-8 -*-
"""T6 端到端对照：同一份候选点集，开/关求解器各跑一遍，比**选中的点集**。

判据只有一条（T4 已定）：**点集必须一模一样，只允许顺序变**。
不看输出序列——上一轮教训：选点不变、顺序变化不代表偏好变了，
第一版用序列比对得出过 same_selection=false 的误导结论。

0 次外部调用、0 次 LLM：直接喂构造好的 groups 与台账，只跑重排这一步。

跑法：
    python scripts/test_solver_e2e.py
    TRAVEL_SOLVER=0 python scripts/test_solver_e2e.py   # 强制关掉求解器
"""
from __future__ import annotations

import json
import os
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


def strip(items):
    return [{"type": i.get("type") or "attraction", "name": i.get("name")} for i in items]


def main() -> int:
    # 一份真实形态的两天行程（来自 2026-10-20 南京方案的形状，坐标用近似值）
    pois = {
        "浦口公园": {"coord": "118.7210,32.0850", "lon": "118.7210", "lat": "32.0850"},
        "南京石头城遗址公园": {"coord": "118.7560,32.0660", "lon": "118.7560", "lat": "32.0660"},
        "金陵兵工厂旧址": {"coord": "118.7830,32.0120", "lon": "118.7830", "lat": "32.0120"},
        "中山陵景区": {"coord": "118.8530,32.0610", "lon": "118.8530", "lat": "32.0610"},
        "钟山风景名胜区": {"coord": "118.8450,32.0700", "lon": "118.8450", "lat": "32.0700"},
        "南京大牌档": {"coord": "118.7900,32.0450", "lon": "118.7900", "lat": "32.0450"},
        "桑启民宿": {"coord": "118.7920,32.0440", "lon": "118.7920", "lat": "32.0440"},
    }
    groups = [
        {"day": 1, "items": [
            {"type": "transit", "name": "南京南站→浦口公园"},
            {"type": "attraction", "name": "浦口公园"},
            {"type": "meal", "name": "南京大牌档"},
            {"type": "transit", "name": "浦口公园→南京石头城遗址公园"},
            {"type": "attraction", "name": "南京石头城遗址公园"},
            {"type": "transit", "name": "南京石头城遗址公园→桑启民宿"},
            {"type": "hotel", "name": "桑启民宿"},
        ]},
        {"day": 2, "items": [
            {"type": "transit", "name": "桑启民宿→金陵兵工厂旧址"},
            {"type": "attraction", "name": "金陵兵工厂旧址"},
            {"type": "transit", "name": "金陵兵工厂旧址→中山陵景区"},
            {"type": "attraction", "name": "中山陵景区"},
            {"type": "meal", "name": "南京大牌档"},
            {"type": "transit", "name": "中山陵景区→钟山风景名胜区"},
            {"type": "attraction", "name": "钟山风景名胜区"},
        ]},
    ]
    before = [strip(g["items"]) for g in groups]

    out, trace = pa.reorder_day_with_solver(groups, pois, [])
    after = [strip(g["items"]) for g in out]

    print("\n-- 1. 主判据：选中的点集一模一样 --")
    for i, (b, a) in enumerate(zip(before, after), 1):
        check(f"day{i} 点集不变",
              sorted(x["name"] for x in b if x["type"] == "attraction"),
              sorted(x["name"] for x in a if x["type"] == "attraction"))
        check(f"day{i} 条目总数不变", len(a), len(b))

    print("\n-- 2. 餐宿不参与求解：条数与相对归属都不变 --")
    for i, (b, a) in enumerate(zip(before, after), 1):
        for kind in ("meal", "hotel"):
            check(f"day{i} {kind} 条数不变",
                  sum(1 for x in a if x["type"] == kind),
                  sum(1 for x in b if x["type"] == kind))

    print("\n-- 3. 交通段一条都不能丢 --")
    for i, (b, a) in enumerate(zip(before, after), 1):
        check(f"day{i} 交通段条数不变",
              sum(1 for x in a if x["type"] == "transit"),
              sum(1 for x in b if x["type"] == "transit"))

    print("\n-- 4. 求解器状态如实反映在 trace 里 --")
    asked, why = pa._solver_available()
    print(f"       本机求解器可用={asked} {why}")
    if trace:
        check("trace 有记录", len(trace), len(groups))
        if asked:
            check("开着时应该成", all(t.get("ok") for t in trace), True,
                  json.dumps(trace, ensure_ascii=False)[:160])
        else:
            check("关着时说明原因", bool(trace[0].get("reason")), True, trace[0].get("reason"))

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
