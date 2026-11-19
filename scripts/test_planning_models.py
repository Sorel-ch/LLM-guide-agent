"""规划契约、历史兼容与语义审计的零网络回归。"""

from planning.audit import audit_plan, visit_fits_open
from planning.budget import enrich_plan
from pydantic import ValidationError

from planning.models import BudgetSummary, Candidate, PlanDraft, TravelNeeds, adapt_legacy_plan


def test_models():
    needs = TravelNeeds.model_validate({"destination_city": "南京", "days": 2, "intercity": True})
    assert needs.day_trip is False and needs.need_intercity_ticket and needs.party_size == 1
    poi = Candidate.model_validate({"candidate_id": "p1", "name": "博物馆", "kind": "attraction",
                                   "location": {"lng": 118.7, "lat": 32.0}})
    assert poi.candidate_id == "p1"
    try:
        Candidate.model_validate({"candidate_id": "bad", "name": "虚构点", "kind": "attraction",
                                  "unexpected": "不允许"})
    except ValidationError:
        pass
    else:
        raise AssertionError("内部候选必须拒绝未声明字段")
    draft = PlanDraft.model_validate({"days": [{"day": 1, "items": [{"candidate_id": "p1", "kind": "attraction"}]}]})
    assert draft.days[0].items[0].candidate_id == "p1"
    old = adapt_legacy_plan({"plan_id": "old", "degraded": True, "markdown": "原文"})
    assert old.schema_version == 1 and old.revision == 0 and old.degraded
    assert BudgetSummary(known_total=20, unknown_item_ids=["x"]).unknown_item_ids == ["x"]


def test_opening():
    assert visit_fits_open({"start_time": "11:40", "end_time": "12:30",
                            "open_time": "周一至周日 16:00-24:00"}) is False
    assert visit_fits_open({"start_time": "16:00", "end_time": "17:00",
                            "open_time": "16:00-24:00"}) is True
    assert visit_fits_open({"start_time": "23:00", "end_time": "23:30",
                            "open_time": "16:00-02:00"}) is True
    assert visit_fits_open({"start_time": "11:00", "open_time": None}) is None


def test_audit():
    raw = {"plan_id": "fixture", "days_count": 1, "start_date": "2026-01-01",
           "days": [{"day": 1, "date": "2026-01-01", "sights": 2,
                     "timeline": [
                         {"type": "meal", "name": "晚间餐厅", "start_time": "11:40",
                          "end_time": "12:30", "cost": 40, "open_time": "16:00-24:00"},
                         {"type": "attraction", "name": "博物馆", "start_time": "12:00",
                          "end_time": "13:00", "cost": None},
                         {"type": "attraction", "name": "博物馆", "start_time": "14:00",
                          "end_time": "15:00", "cost": 20}]}]}
    report = audit_plan(raw)
    assert report["issue_counts"] == {"opening_conflict": 1, "time_overlap": 1, "duplicate_place": 1}
    assert report["unknown_cost_non_transit"] == 1
    enriched = enrich_plan(raw)
    assert enriched["quality"]["issues"][0]["code"] == "opening_conflict"
    assert enriched["budget_summary"]["known_total"] == 60
    assert len(enriched["budget_summary"]["unknown_item_ids"]) == 1
    raw["days"][0]["timeline"][2]["cost"] = None
    assert len(enrich_plan(raw)["budget_summary"]["unknown_item_ids"]) == 2
    legacy = audit_plan({"plan_id": "old", "days_count": 1, "days": [{"day": 1, "timeline": []}]})
    assert legacy["unknown"]["sights"] == 1 and "sights_count" not in legacy["issue_counts"]


if __name__ == "__main__":
    test_models()
    test_opening()
    test_audit()
    print("3 组契约与审计测试通过")
