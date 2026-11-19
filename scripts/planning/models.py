"""内部规划与 Web 交付共用的数据契约。

内部输入拒绝未知字段，历史交付数据允许扩展字段；模型只负责校验与适配，
不从名称或价格猜出坐标、营业时间与票价。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = 2
ItemKind = Literal["attraction", "meal", "transit", "hotel"]
PathStatus = Literal["measured", "missing", "cross_city"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TravelNeeds(InputModel):
    """兼容旧 NEEDS_DEFAULTS 的需求；金额默认是一人的全程预算。"""

    origin: str = ""
    origin_city: str = ""
    destination_city: str = ""
    intercity: bool = False
    day_trip: bool = True
    days: int = Field(default=1, ge=1, le=30)
    budget_yuan: int = Field(default=0, ge=0)
    themes: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    need_intercity_ticket: bool = False
    date: str = ""
    party_size: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def derive_trip_shape(self) -> "TravelNeeds":
        self.day_trip = self.days == 1
        if self.intercity:
            self.need_intercity_ticket = True
        return self


class Location(OutputModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class Candidate(InputModel):
    """由工具台账构造的候选；模型只能引用 candidate_id，不能创建它。"""

    candidate_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ItemKind
    source_type: str = ""
    typecode: str = ""
    location: Location | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    open_time: str | None = None
    cost: int | None = Field(default=None, ge=0)
    cost_scope: Literal["unknown", "person", "night", "trip"] = "unknown"
    photo: str | None = None
    source: str = "amap"
    query: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class DraftItem(InputModel):
    candidate_id: str
    kind: ItemKind
    start_time: str | None = None
    end_time: str | None = None
    transport: str | None = None
    tip: str = ""


class DraftDay(InputModel):
    day: int = Field(ge=1)
    theme: str = ""
    items: list[DraftItem] = Field(default_factory=list)


class PlanDraft(InputModel):
    days: list[DraftDay] = Field(default_factory=list)


class BudgetCategory(OutputModel):
    known_subtotal: int = 0
    unknown_item_ids: list[str] = Field(default_factory=list)
    scope: str = "person"

    @property
    def complete(self) -> bool:
        return not self.unknown_item_ids


class BudgetSummary(OutputModel):
    currency: Literal["CNY"] = "CNY"
    scope: Literal["person"] = "person"
    categories: dict[str, BudgetCategory] = Field(default_factory=dict)
    known_total: int = 0
    estimated_min: int | None = Field(default=None, ge=0)
    estimated_max: int | None = Field(default=None, ge=0)
    unknown_item_ids: list[str] = Field(default_factory=list)
    target: int = Field(default=0, ge=0)
    over_budget: bool = False


class ValidationIssue(OutputModel):
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    day: int | None = None
    item_id: str | None = None


class QualityReport(OutputModel):
    schema_valid: bool = True
    feasible: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)


class TimelineItem(OutputModel):
    item_id: str | None = None
    candidate_id: str | None = None
    type: ItemKind
    name: str
    start_time: str | None = None
    end_time: str | None = None
    transport: str | None = None
    cost: int | None = None
    tip: str | None = None
    photo: str | None = None
    path: list[list[float]] | None = None
    path_status: PathStatus | None = None
    location: Location | None = None
    rating: float | None = None
    open_time: str | None = None
    verified: bool = False
    source: str = "none"


class DayPlan(OutputModel):
    day: int = Field(ge=1)
    date: str | None = None
    theme: str | None = None
    timeline: list[TimelineItem] = Field(default_factory=list)
    sights: int = Field(default=0, ge=0)
    route_km: float | None = None
    detour_ratio: float | None = None
    route_note: str | None = None


class PlanDocument(OutputModel):
    """兼容当前 final_plan 的交付模型；旧字段不因重构被重命名。"""

    schema_version: int = SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    plan_id: str
    question: str = ""
    destination: str | None = None
    origin: str | None = None
    days_count: int = Field(default=0, ge=0)
    start_date: str | None = None
    end_date: str | None = None
    budget_yuan: int = Field(default=0, ge=0)
    budget_summary: BudgetSummary | None = None
    weather_forecast: list[dict[str, Any]] = Field(default_factory=list)
    weather_note: str | None = None
    quality: QualityReport | None = None
    sources: dict[str, Any] = Field(default_factory=dict)
    days: list[DayPlan] = Field(default_factory=list)
    degraded: bool = False
    markdown: str | None = None
    reason: str | None = None


def adapt_legacy_plan(raw: dict[str, Any]) -> PlanDocument:
    """读取历史方案时只补版本元数据，不回填假预算或质量结论。"""
    data = dict(raw)
    data.setdefault("schema_version", SCHEMA_VERSION if "budget_summary" in raw or "quality" in raw else 1)
    data.setdefault("revision", 0)
    return PlanDocument.model_validate(data)
