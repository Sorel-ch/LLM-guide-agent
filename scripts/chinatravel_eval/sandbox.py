"""ChinaTravel sandbox 的只读数据层。

只做三件事：读 CSV/JSONL、调用官方 `WorldEnv` 的 `goto`/`intercity_transport_select`、
把字段归一成普通 dict。实体名一律原样保留（评测要求精确命中）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DEFAULT_HOME = Path(os.getenv("CHINATRAVEL_HOME", r"E:/code/chinatravel-eval/ChinaTravel"))

CITY_SLUGS = {
    "北京": "beijing",
    "上海": "shanghai",
    "南京": "nanjing",
    "苏州": "suzhou",
    "杭州": "hangzhou",
    "深圳": "shenzhen",
    "成都": "chengdu",
    "武汉": "wuhan",
    "广州": "guangzhou",
    "重庆": "chongqing",
}

# 目的城市的“本地菜”默认映射；只用于 NL 说“当地特色菜/本地小吃”时选菜系。
LOCAL_CUISINE = {
    "北京": "北京菜",
    "上海": "本帮菜",
    "南京": "江浙菜",
    "苏州": "江浙菜",
    "杭州": "江浙菜",
    "深圳": "粤菜",
    "成都": "川菜",
    "武汉": "湖北菜",
    "广州": "粤菜",
    "重庆": "川菜",
}


def hhmm(value) -> str | None:
    """把 '9:5' / '09:05' / 905 统一成 'HH:MM'；无法解析返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if ":" in text:
        h, _, m = text.partition(":")
        try:
            return f"{int(h):02d}:{int(m[:2]):02d}"
        except ValueError:
            return None
    if text.isdigit():
        n = int(text)
        return f"{n // 60:02d}:{n % 60:02d}"
    return None


def to_min(value) -> int | None:
    t = hhmm(value)
    if t is None:
        return None
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def from_min(minutes: int) -> str:
    minutes = int(round(minutes)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def open_at(opentime, endtime, t: str) -> bool:
    """餐厅/景点在 t 是否营业；支持跨午夜与 00:00-24:00。"""
    o, e = to_min(opentime), to_min(endtime)
    tm = to_min(t)
    if o is None or e is None or tm is None:
        return False
    if e <= o:
        e += 24 * 60
    if tm < o:
        tm += 24 * 60
    return o <= tm <= e


def _num(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


@dataclass
class Sandbox:
    home: Path = DEFAULT_HOME
    lang: str = "zh"

    def __post_init__(self):
        self.home = Path(self.home)
        self.db = self.home / "chinatravel" / "environment" / (
            "database_en" if self.lang == "en" else "database"
        )
        self._world = None
        self._cache: dict[tuple[str, str], list[dict]] = {}
        self._poi: dict[str, list[dict]] = {}

    # ---- lazy WorldEnv -----------------------------------------------------
    @property
    def world(self):
        if self._world is None:
            from chinatravel.environment.world_env import WorldEnv

            self._world = WorldEnv(lang=self.lang)
        return self._world

    def close(self):
        self._world = None

    # ---- CSV tables --------------------------------------------------------
    def city_slug(self, city: str) -> str:
        if city not in CITY_SLUGS:
            raise KeyError(f"unsupported_city: {city}")
        return CITY_SLUGS[city]

    def _csv(self, kind: str, city: str) -> list[dict]:
        key = (kind, city)
        if key in self._cache:
            return self._cache[key]
        slug = self.city_slug(city)
        if kind == "attractions":
            path = self.db / "attractions" / slug / "attractions.csv"
        elif kind == "accommodations":
            path = self.db / "accommodations" / slug / "accommodations.csv"
        elif kind == "restaurants":
            path = self.db / "restaurants" / slug / f"restaurants_{slug}.csv"
        else:
            raise ValueError(kind)
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path)
        rows = df.to_dict("records")
        self._cache[key] = rows
        return rows

    def attractions(self, city: str) -> list[dict]:
        out = []
        for r in self._csv("attractions", city):
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "type": str(r.get("type") or ""),
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "opentime": hhmm(r.get("opentime")),
                    "endtime": hhmm(r.get("endtime")),
                    "price": _num(r.get("price")),
                    "recommendmin": _num(r.get("recommendmintime")),
                    "recommendmax": _num(r.get("recommendmaxtime")),
                }
            )
        return out

    def restaurants(self, city: str) -> list[dict]:
        out = []
        for r in self._csv("restaurants", city):
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "price": _num(r.get("price")),
                    "cuisine": str(r.get("cuisine") or "").strip(),
                    "opentime": hhmm(r.get("opentime")),
                    "endtime": hhmm(r.get("endtime")),
                    "food": str(r.get("recommendedfood") or ""),
                }
            )
        return out

    def hotels(self, city: str) -> list[dict]:
        out = []
        for r in self._csv("accommodations", city):
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "feature": str(r.get("featurehoteltype") or ""),
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "price": _num(r.get("price")),
                    "numbed": int(r["numbed"]) if _num(r.get("numbed")) is not None else 1,
                }
            )
        return out

    # ---- POI 坐标解析 ------------------------------------------------------
    def poi(self, city: str, name: str) -> dict | None:
        """精确优先；唯一子串才认（与仓库 ledger_lookup 的口径一致）。"""
        if city not in self._poi:
            slug = self.city_slug(city)
            path = self.db / "poi" / slug / "poi.json"
            self._poi[city] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        rows = self._poi[city]
        for r in rows:
            if r.get("name") == name:
                lat, lon = r["position"]
                return {"name": name, "lat": float(lat), "lon": float(lon)}
        fuzzy = [r for r in rows if name and (name in str(r.get("name")) or str(r.get("name")) in name)]
        if len(fuzzy) == 1:
            lat, lon = fuzzy[0]["position"]
            return {"name": name, "lat": float(lat), "lon": float(lon)}
        return None

    def resolve(self, city: str, name: str) -> dict | None:
        """把任意实体名解析成带坐标的 dict：先查三类台账，再查 POI。"""
        for kind, fn in (("attractions", self.attractions), ("restaurants", self.restaurants), ("accommodations", self.hotels)):
            hits = [r for r in fn(city) if r["name"] == name]
            if hits:
                return hits[0]
        return self.poi(city, name)

    # ---- intercity ---------------------------------------------------------
    def trains(self, start: str, end: str) -> list[dict]:
        path = self.db / "intercity_transport" / "train" / f"from_{start}_to_{end}.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            {
                "id": str(r["TrainID"]),
                "type": "train",
                "from": str(r["From"]),
                "to": str(r["To"]),
                "begin": hhmm(r["BeginTime"]),
                "end": hhmm(r["EndTime"]),
                "cost": _num(r["Cost"]),
            }
            for r in data
        ]

    def flights(self, start: str, end: str) -> list[dict]:
        path = self.db / "intercity_transport" / "airplane.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if start in str(r.get("From", "")) and end in str(r.get("To", "")):
                out.append(
                    {
                        "id": str(r["FlightID"]),
                        "type": "airplane",
                        "from": str(r["From"]),
                        "to": str(r["To"]),
                        "begin": hhmm(r["BeginTime"]),
                        "end": hhmm(r["EndTime"]),
                        "cost": _num(r["Cost"]),
                    }
                )
        return out

    def intercity_options(self, start: str, end: str, preferred: str | None = None) -> list[dict]:
        """火车 + 飞机一起返回；preferred='train'/'airplane' 时只留该类型。

        排序按出发时间；具体选哪班由 `select_intercity` 按“当天可用时间”决定。
        """
        kinds = ["train", "airplane"] if preferred not in ("train", "airplane") else [preferred]
        options: list[dict] = []
        for kind in kinds:
            rows = self.trains(start, end) if kind == "train" else self.flights(start, end)
            options += [
                o for o in rows
                if o["begin"] and o["end"] and o["cost"] is not None
            ]
        return sorted(options, key=lambda o: to_min(o["begin"]) or 0)

    # ---- local transport ---------------------------------------------------
    def goto(self, city: str, a: str, b: str, t: str, mode: str) -> list[dict] | None:
        if a == b:
            return []
        out = self.world(f"goto({city!r}, {a!r}, {b!r}, {t!r}, {mode!r})")
        if not out["success"]:
            return None
        data = out["data"]
        if not isinstance(data, list):
            return None
        return data
