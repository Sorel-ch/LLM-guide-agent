"""只读历史行程审计。这里只报告可证明的问题，不猜缺失事实。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from .models import PlanDocument, ValidationIssue, adapt_legacy_plan


TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)")
WINDOW_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*[-–~]\s*([01]?\d|2[0-4]):([0-5]\d)")


def minutes(value: str | None) -> int | None:
    match = TIME_RE.search(value or "")
    return int(match[1]) * 60 + int(match[2]) if match else None


def opening_windows(value: str | None) -> list[tuple[int, int]]:
    """返回当天可解析的时间窗；跨午夜保留结束小于开始的语义。"""
    return [(int(a) * 60 + int(b), int(c) * 60 + int(d))
            for a, b, c, d in WINDOW_RE.findall(value or "")]


def overlaps_open(start: int, end: int, window: tuple[int, int]) -> bool:
    lo, hi = window
    if hi == 1440:
        hi = 1439
    if hi < lo:  # 16:00-02:00：营业跨到次日。
        return end > lo or start < hi
    return start < hi and end > lo


def visit_fits_open(item: dict[str, Any]) -> bool | None:
    start, end = minutes(item.get("start_time")), minutes(item.get("end_time"))
    windows = opening_windows(item.get("open_time"))
    if start is None or not windows:
        return None
    if end is None or end <= start:
        end = start + 1
    return any(overlaps_open(start, end, w) for w in windows)


def issue(code: str, message: str, *, day: int | None = None,
          item_id: str | None = None, severity: str = "warning") -> ValidationIssue:
    return ValidationIssue(code=code, message=message, day=day, item_id=item_id,
                           severity=severity)  # type: ignore[arg-type]


def audit_plan(raw: dict[str, Any]) -> dict[str, Any]:
    plan = adapt_legacy_plan(raw)
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    path_hashes: set[str] = set()
    path_counts: Counter[str] = Counter()
    item_count = unknown_cost = 0
    unknown: Counter[str] = Counter()
    per_day: list[dict[str, Any]] = []

    if not plan.degraded:
        expected = plan.days_count
        actual = [d.day for d in plan.days]
        if actual != list(range(1, len(actual) + 1)) or (expected and expected != len(actual)):
            issues.append(issue("day_count", f"声明 {expected} 天，实际分组为 {actual}", severity="error"))

    for day in plan.days:
        previous_end: int | None = None
        day_issues = 0
        for idx, item in enumerate(day.timeline):
            item_id = item.item_id or f"d{day.day}-i{idx}"
            item_count += 1
            if item.cost is None and item.type != "transit":
                unknown_cost += 1
            if item.type != "hotel":
                key = item.name.strip().lower()
                if key in seen:
                    issues.append(issue("duplicate_place", f"与第 {seen[key]} 天重复：{item.name}",
                                        day=day.day, item_id=item_id))
                else:
                    seen[key] = day.day
            fits = visit_fits_open(item.model_dump())
            if fits is False:
                issues.append(issue("opening_conflict",
                                    f"{item.name} 的安排不在已记录营业时间内：{item.open_time}",
                                    day=day.day, item_id=item_id))
            start, end = minutes(item.start_time), minutes(item.end_time)
            if start is not None and end is not None and end <= start and item.type != "transit":
                issues.append(issue("time_order", f"{item.name} 的结束时间不晚于开始时间",
                                    day=day.day, item_id=item_id))
            if start is not None and previous_end is not None and start < previous_end:
                issues.append(issue("time_overlap", f"{item.name} 与前一项时间重叠",
                                    day=day.day, item_id=item_id))
            if end is not None:
                previous_end = end
            if item.path:
                digest = hashlib.md5(json.dumps(item.path, separators=(",", ":")).encode()).hexdigest()
                path_hashes.add(digest)
            if item.path_status:
                path_counts[item.path_status] += 1
        if not plan.degraded:
            raw_day = next((d for d in raw.get("days", []) if d.get("day") == day.day), {})
            if "sights" not in raw_day:
                unknown["sights"] += 1
            elif not 2 <= day.sights <= 3:
                issues.append(issue("sights_count", f"当天记录为 {day.sights} 个景点，目标为 2~3 个",
                                    day=day.day))
        day_issues = sum(1 for x in issues if x.day == day.day)
        per_day.append({"day": day.day, "sights": day.sights, "route_km": day.route_km,
                        "detour_ratio": day.detour_ratio, "issues": day_issues})

    return {
        "plan_id": plan.plan_id,
        "degraded": plan.degraded,
        "days": per_day,
        "items": item_count,
        "unknown_cost_non_transit": unknown_cost,
        "unknown": dict(unknown),
        "unique_paths": len(path_hashes),
        "path_status": dict(path_counts),
        "issues": [x.model_dump(exclude_none=True) for x in issues],
        "issue_counts": dict(Counter(x.code for x in issues)),
    }
