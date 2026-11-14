# -*- coding: utf-8 -*-
"""餐点时段校验的离线回归（0 次外部调用）。

宁波 2026-11-14 实测：一日游首站直接是 11:05 的午饭，两顿正餐只隔 2 小时。
这里锁住 check_meal_timing() 的三条判据，以及它只提示、不改时间的行为：
1. 首站是正餐（早餐除外）→ 记一条，并挂到该条 tip；
2. 两顿正餐间隔 < 4 小时 → 记一条，两顿都挂 tip；
3. 用餐时间不在午餐/晚餐时段 → 记一条；
4. 健康时间线不误报；没有可解析时间的条目不崩、不改。

跑法：
    python scripts/test_meal_timing.py
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


def row(kind, name, start=None, end=None, tip=None):
    return {"type": kind, "name": name, "start_time": start, "end_time": end, "tip": tip}


def main() -> int:
    print("\n-- 1. 首站是 11:05 的午饭 + 两顿只隔 3 小时 --")
    rows = [
        row("meal", "阿娇", "11:05", "12:20", "人均参考 100 元"),
        row("transit", "阿娇→生态走廊", "12:20", "12:50"),
        row("attraction", "生态走廊", "12:50", "13:50"),
        row("meal", "阿毛", "14:05", "15:00", "人均参考 80 元"),
    ]
    before = [(r["name"], r["start_time"], r["end_time"]) for r in rows]
    notes = pa.check_meal_timing(rows)
    check("两条提示", len(notes), 2)
    check("首站提示", "首站是餐饮" in notes[0], True, notes[0])
    check("间隔提示", "3 小时" in notes[1] and "不足" not in notes[1], True, notes[1])
    check("阿娇 tip 挂两条", "首站是餐饮" in rows[0]["tip"] and "不足 4 小时" in rows[0]["tip"], True,
          rows[0]["tip"])
    check("阿毛 tip 挂间隔", "不足 4 小时" in rows[3]["tip"], True, rows[3]["tip"])
    check("时间一个字没改",
          [(r["name"], r["start_time"], r["end_time"]) for r in rows], before)

    print("\n-- 2. 健康时间线：不误报 --")
    healthy = [
        row("attraction", "天一阁", "09:00", "10:30"),
        row("meal", "午餐店", "12:00", "13:00"),
        row("attraction", "月湖", "13:30", "15:00"),
        row("meal", "晚餐店", "18:00", "19:00"),
    ]
    check("无提示", pa.check_meal_timing(healthy), [])
    check("tips 原样", [r.get("tip") for r in healthy], [None, None, None, None])

    print("\n-- 3. 早餐开局不算首站违规 --")
    breakfast = [
        row("meal", "早餐摊", "08:00", "08:40"),
        row("attraction", "天一阁", "09:00", "10:30"),
        row("meal", "午餐店", "12:00", "13:00"),
    ]
    check("无首站提示", pa.check_meal_timing(breakfast), [])

    print("\n-- 4. 晚餐排在 16:20：时段异常 --")
    early_dinner = [
        row("attraction", "罗城", "09:10", "10:40"),
        row("meal", "午餐店", "12:20", "13:30"),
        row("meal", "晚餐店", "16:20", "17:20"),
    ]
    notes = pa.check_meal_timing(early_dinner)
    check("恰好一条时段提示", len(notes), 1, notes)
    check("指向 16:20 那顿", "16:20" in notes[0], True, notes[0])

    print("\n-- 5. 首站 11:00 才开始（不是餐饮）--")
    late = [row("attraction", "罗城", "11:00", "12:00"), row("meal", "午餐店", "12:30", "13:30")]
    notes = pa.check_meal_timing(late)
    check("提示上午空置", any("上午空置" in n for n in notes), True, notes)

    print("\n-- 6. 没有可解析时间：不崩、不报 --")
    no_time = [row("meal", "某餐厅"), row("attraction", "某景点")]
    check("无提示", pa.check_meal_timing(no_time), [])
    check("时间仍为空", (no_time[0]["start_time"], no_time[1]["start_time"]), (None, None))

    print(f"\n自测 {'全过' if FAIL == 0 else '有失败'}：{PASS} 过 / {FAIL} 败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
