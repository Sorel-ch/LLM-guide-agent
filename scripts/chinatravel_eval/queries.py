"""查询加载、needs 构造与本地 split 导出。

关键兼容点：HF 发布的 `hard_logic_py` 是字符串，官方 `_validate_query_record` 只认 list。
本模块在**本地**把它解析成 list，并导出成官方 `load_query_local` 能读的 split 目录；
不修改官方仓库源码。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import LOCAL_CUISINE, Sandbox

FIXED_DATE = "2026-01-05"  # sandbox 无天气，固定参考日期保证可复现


@dataclass
class Task:
    uid: str
    split: str
    start_city: str
    target_city: str
    days: int
    people: int
    nature_language: str
    hard_logic_py: list[str] = field(default_factory=list)
    limit_rooms: bool = False
    limits_room_type: bool = False
    raw: dict = field(default_factory=dict)


def _parse_hard_logic(value) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if value is None or str(value).strip() == "":
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        parsed = ast.literal_eval(str(value))
    if not isinstance(parsed, list):
        raise ValueError(f"hard_logic_py is not a list: {type(parsed)}")
    return [str(x) for x in parsed]


def _row_to_task(row: dict, split: str) -> Task:
    row = dict(row)
    raw = dict(row)
    raw["hard_logic_py"] = _parse_hard_logic(row.get("hard_logic_py"))
    raw["days"] = int(row["days"])
    raw["people_number"] = int(row["people_number"])
    raw["limit_rooms"] = str(row.get("limit_rooms", "False")).lower() == "true"
    raw["limits_room_type"] = str(row.get("limits_room_type", "False")).lower() == "true"
    return Task(
        uid=str(row["uid"]),
        split=split,
        start_city=str(row["start_city"]),
        target_city=str(row["target_city"]),
        days=int(row["days"]),
        people=int(row["people_number"]),
        nature_language=str(row["nature_language"]),
        hard_logic_py=raw["hard_logic_py"],
        limit_rooms=raw["limit_rooms"],
        limits_room_type=raw["limits_room_type"],
        raw=raw,
    )


def load_tasks(source: str | Path, split: str = "easy", uids: list[str] | None = None) -> list[Task]:
    """source 可以是官方查询 CSV，也可以是已导出的 `chinatravel/data/<split>` 目录。"""
    import csv

    source = Path(source)
    wanted = set(uids or [])
    if source.is_dir():
        rows = []
        for path in sorted(source.glob("*.json")):
            if wanted and path.stem not in wanted:
                continue
            rows.append(json.loads(path.read_text(encoding="utf-8")))
    else:
        with open(source, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if wanted:
            rows = [r for r in rows if r["uid"] in wanted]
    return [_row_to_task(r, split) for r in rows]


def export_local_split(home: str | Path, split: str, tasks: list[Task]) -> Path:
    """写 `chinatravel/data/<split>/<uid>.json` + `default_splits/<split>.txt`。

    split 名不能落在官方 HF split 集合里，否则 `load_query` 仍会走 HF。
    """
    from chinatravel.data.load_datasets import DEFAULT_HUGGINGFACE_SPLITS

    if split in DEFAULT_HUGGINGFACE_SPLITS:
        raise ValueError(f"local split name must not be an HF split: {split}")
    home = Path(home)
    data_dir = home / "chinatravel" / "data" / split
    data_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        (data_dir / f"{task.uid}.json").write_text(
            json.dumps(task.raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    split_file = home / "chinatravel" / "evaluation" / "default_splits" / f"{split}.txt"
    split_file.write_text("\n".join(t.uid for t in tasks) + "\n", encoding="utf-8")
    return split_file


def _budget(nl: str) -> int:
    m = re.search(r"预算\s*(\d+)", nl)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:元|块)", nl)
    return int(m.group(1)) if m else 0


# NL 同义说法 → sandbox 的 `cuisine` 值；按顺序命中，越具体的放前面。
CUISINE_HINTS = (
    (("喝酒", "酒吧", "小酌", "夜生活"), "酒吧/酒馆"),
    (("甜食", "甜品", "面包", "蛋糕", "烘焙"), "面包甜点"),
    (("咖啡", "咖啡馆"), "咖啡店"),
    (("茶馆", "喝茶"), "茶馆/茶室"),
    (("创意菜", "创意菜肴", "创意料理", "有创意的菜"), "创意菜"),
    (("融合菜", "融合菜肴", "融合料理"), "融合菜"),
    (("日料", "日本料理"), "日本料理"),
    (("韩餐", "韩国料理"), "韩国料理"),
    (("粤菜", "广东菜"), "粤菜"),
    (("川菜", "四川菜"), "川菜"),
    (("湘菜", "湖南菜"), "湘菜"),
    (("本帮菜", "上海菜"), "本帮菜"),
    (("北京菜", "北京特色"), "北京菜"),
    (("东南亚菜", "泰国菜"), "东南亚菜"),
    (("江浙菜", "江浙"), "江浙菜"),
    (("海鲜",), "海鲜"),
    (("烧烤",), "烧烤"),
    (("素食", "吃素"), "素食"),
    (("快餐",), "快餐简餐"),
    (("西餐",), "西餐"),
    (("东北菜",), "东北菜"),
    (("西北菜",), "西北菜"),
    (("云南菜",), "云南菜"),
    (("新疆菜",), "新疆菜"),
    (("自助餐",), "自助餐"),
    (("农家菜",), "农家菜"),
    (("清真",), "清真菜"),
    (("徽菜",), "徽菜"),
)


def find_cuisine(nl: str, city: str, cuisines: list[str]) -> str | None:
    """从 NL 找菜系：先最长子串命中，再同义说法，再本地特色映射。"""
    text = str(nl)
    hits = [c for c in cuisines if len(c) >= 2 and c in text]
    if hits:
        return max(hits, key=len)
    for keys, cuisine in CUISINE_HINTS:
        if any(k in text for k in keys) and cuisine in cuisines:
            return cuisine
    if any(k in text for k in ("当地小吃", "本地小吃", "特色小吃")):
        if "小吃" in cuisines:
            return "小吃"
    if any(k in text for k in ("火锅",)):
        if "火锅" in cuisines:
            return "火锅"
    if any(k in text for k in ("当地特色", "本地特色", "特色菜", "当地菜", "本地菜", "当地美食", "本地美食")):
        local = LOCAL_CUISINE.get(city)
        if local in cuisines:
            return local
    if "快餐" in text and "快餐简餐" in cuisines:
        return "快餐简餐"
    return None


def build_needs(task: Task, sandbox: Sandbox) -> dict:
    """只从 `nature_language` + 查询的任务元数据构造 needs；不读 hard_logic_py。"""
    cuisines = sorted({r["cuisine"] for r in sandbox.restaurants(task.target_city) if r["cuisine"]})
    cuisine = find_cuisine(task.nature_language, task.target_city, cuisines)
    nl = task.nature_language
    transport_pref = None
    if "地铁" in nl or "轨交" in nl:
        transport_pref = "metro"
    elif "打车" in nl or "出租" in nl:
        transport_pref = "taxi"
    elif "步行" in nl:
        transport_pref = "walk"
    intercity_pref = None
    if any(k in nl for k in ("高铁", "动车", "火车", "列车")):
        intercity_pref = "train"
    elif any(k in nl for k in ("飞机", "航班", "机票")):
        intercity_pref = "airplane"
    hotel_pref = None
    if "经济型" in nl or "便宜" in nl:
        hotel_pref = "经济型"
    elif "高档" in nl or "豪华" in nl or "五星" in nl:
        hotel_pref = "高档"
    elif "民宿" in nl:
        hotel_pref = "民宿"
    spot_pref = None
    for key, value in (
        ("博物馆", "博物馆"),
        ("历史", "历史"),
        ("古迹", "历史"),
        ("自然", "自然"),
        ("公园", "自然"),
        ("夜景", "夜景"),
    ):
        if key in nl:
            spot_pref = value
            break

    must_visit = []
    for pattern in (r"想去([^，。；、]{2,18})", r"必去([^，。；、]{2,18})"):
        must_visit += [m.strip() for m in re.findall(pattern, nl) if m.strip()]

    return {
        "origin": task.start_city,
        "origin_city": task.start_city,
        "destination_city": task.target_city,
        "intercity": task.start_city != task.target_city,
        "day_trip": task.days == 1,
        "days": task.days,
        "budget_yuan": _budget(nl),
        "themes": [x for x in (cuisine, spot_pref) if x],
        "must_visit": must_visit,
        "need_intercity_ticket": task.start_city != task.target_city,
        "date": FIXED_DATE,
        # 适配层扩展字段（不写回产品 needs）
        "people_number": task.people,
        "cuisine_pref": cuisine,
        "transport_pref": transport_pref,
        "intercity_pref": intercity_pref,
        "hotel_pref": hotel_pref,
        "spot_pref": spot_pref,
    }
