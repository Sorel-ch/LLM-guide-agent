"""行程修订零网络、零模型、不可变历史回归。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from planning.itinerary import RevisionConflict, revise_plan


def fixture():
    return json.loads(Path(".cache/plans/2026-10-20T18-30-07_南京.json").read_text(encoding="utf-8"))


def test_edit():
    raw = fixture()
    original = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    revised = revise_plan(raw, 0, [{"op": "remove_item", "item_id": "d1-i3"}])
    assert revised["revision"] == 1 and revised["sources"]["edit_history"][0]["llm_calls"] == 0
    assert len(revised["days"][0]["timeline"]) == len(raw["days"][0]["timeline"]) - 1
    assert json.dumps(raw, ensure_ascii=False, sort_keys=True) == original
    replaced = revise_plan(raw, 0, [{"op": "replace_item", "item_id": "d1-i2", "candidate": {
        "candidate_id": "real-poi", "name": "真实候选", "type": "attraction",
        "location": {"lng": 118.8, "lat": 32.1}, "cost": 20, "source": "amap"}}])
    assert replaced["days"][0]["timeline"][2]["name"] == "真实候选"
    try:
        revise_plan(raw, 0, [{"op": "replace_item", "item_id": "d1-i2", "candidate": {"name": "虚构点"}}])
    except ValueError:
        pass
    else:
        raise AssertionError("无坐标候选必须拒绝")
    try:
        revise_plan(revised, 0, [{"op": "remove_item", "item_id": "d1-i3"}])
    except RevisionConflict:
        pass
    else:
        raise AssertionError("过期 revision 必须拒绝")


if __name__ == "__main__":
    test_edit()
    print("1 组修订测试通过")
