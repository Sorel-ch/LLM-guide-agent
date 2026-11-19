"""策展后的纯几何分天，不再按检索词二次筛选。"""

from __future__ import annotations

from typing import Any


def order_selected(pa, selected: list[str], pois: dict[str, dict], days: int,
                   adcode: str | None) -> dict[str, Any]:
    names = pa.merge_same_place(selected, pois)
    outliers = [n for n in names if all(pa.km_between(pois[n], pois[m]) > pa.BALANCE_MAX_KM
                                       for m in names if m != n)]
    pool = [n for n in names if n not in outliers]
    groups = pa.cluster_by_days(pool, pois, days)
    result = []
    for i, group in enumerate(groups, 1):
        sequence = pa.open_path(group, pois)
        result.append({"day": i, "names": sequence[:pa.SIGHTS_MAX_PER_DAY],
                       "km": round(pa.path_km(sequence, pois), 1)})
    thin = [d["day"] for d in result if len(d["names"]) < pa.SIGHTS_MIN_PER_DAY]
    thin += list(range(len(result) + 1, days + 1))
    return {"days": result, "outliers": outliers, "thin_days": thin,
            "spare": [n for d in groups for n in d[pa.SIGHTS_MAX_PER_DAY:]]}
