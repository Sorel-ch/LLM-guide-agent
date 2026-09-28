---
type: city
summary: {{一句话城市旅游定位}}
sources: []
updated: YYYY-MM-DD
valid_until: YYYY-MM-DD
---

# {{城市名}}

## 速览
adcode、坐标中心、最佳季节、建议天数。[^1]

## 天气与最佳出行时间
用 `get_amap_weather(city=<adcode>, extensions=all)` 的预报规律 + 攻略常识，
只写季节性结论；当日天气永远现场查询。

| 月份 | 气候特征 | 适合度 | 备注 |
|---|---|---|---|

## 交通枢纽与市内交通
机场/高铁站坐标（来自 `get_amap_poi_search`）、地铁/打车大致费用与耗时。

## 必去 POI 索引
链接到 `../pois/` 页面，附一句推荐理由。

- [{{POI名}}](../pois/{{POI名}}.md) — {{理由}}

## 美食与住宿区域
按片区给出建议，注明来源。

## 坑与提醒
闭馆日、门票预约、节假日限流、拉客点位等。

---
[^1]: raw/... （来源快照路径或 URL）
