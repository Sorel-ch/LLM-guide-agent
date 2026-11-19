"""费用按证据汇总；未知价格绝不按免费计入。"""

from __future__ import annotations

from typing import Any

from .models import BudgetCategory, BudgetSummary, PlanDocument, adapt_legacy_plan


CATEGORY_BY_KIND = {"attraction": "attractions", "meal": "meals", "hotel": "lodging"}


def calculate_budget(plan: PlanDocument, party_size: int = 1) -> BudgetSummary:
    categories = {key: BudgetCategory() for key in
                  ("attractions", "meals", "lodging", "local_transport", "intercity_transport")}
    for day in plan.days:
        for idx, item in enumerate(day.timeline):
            item_id = item.item_id or f"d{day.day}-i{idx}"
            if item.item_id is None:
                item.item_id = item_id
            if item.type == "transit":
                category = "intercity_transport" if item.path_status == "cross_city" else "local_transport"
            else:
                category = CATEGORY_BY_KIND[item.type]
            bucket = categories[category]
            if item.cost is None:
                if item_id not in bucket.unknown_item_ids:
                    bucket.unknown_item_ids.append(item_id)
                continue
            # 餐饮/住宿价格沿用旧方案的人均参考口径；酒店每晚出现一次。
            multiplier = party_size if item.type == "meal" else 1
            bucket.known_subtotal += item.cost * multiplier
    unknown = list(dict.fromkeys(x for b in categories.values() for x in b.unknown_item_ids))
    total = sum(b.known_subtotal for b in categories.values())
    return BudgetSummary(categories=categories, known_total=total, unknown_item_ids=unknown,
                         target=plan.budget_yuan,
                         over_budget=bool(plan.budget_yuan and total > plan.budget_yuan))


def enrich_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """输出新质量与预算字段；历史方案不补造“已校验”的结论。"""
    from .audit import audit_plan
    from .models import QualityReport, ValidationIssue

    plan = adapt_legacy_plan(raw)
    report = audit_plan(raw)
    issues = [ValidationIssue.model_validate(i) for i in report["issues"]]
    plan.quality = QualityReport(schema_valid=True,
                                 feasible=not any(i.severity == "error" for i in issues),
                                 issues=issues)
    plan.budget_summary = calculate_budget(plan)
    # 同时把审计的临时 ID 固定到条目，供编辑与费用待核实项对齐。
    for day in plan.days:
        for idx, item in enumerate(day.timeline):
            item.item_id = item.item_id or f"d{day.day}-i{idx}"
    return plan.model_dump(exclude_none=True)
