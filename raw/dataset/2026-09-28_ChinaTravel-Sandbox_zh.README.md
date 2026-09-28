# ChinaTravel Sandbox（中文语料快照）

- 文件：`2026-09-28_ChinaTravel-Sandbox_zh.zip`（1,027,737 字节，未改动）
- 来源：https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox （release `2026.08.2`）
- 原始仓库：https://github.com/LAMDA-NeSy/ChinaTravel （arXiv:2412.13682）
- 许可：**CC BY 4.0**（数据集官方声明；仓库代码为 MIT）
- 下载时间：2026-09-28，工具：agent 内置 HTTP 抓取（非 MCP）

## 内容

`database/<类别>/<城市拼音>/<类别>.csv`，10 个城市：
beijing / chengdu / chongqing / guangzhou / hangzhou / nanjing / shanghai / shenzhen / suzhou / wuhan

| 类别 | 行数（每语言） |
|---|---|
| attractions | 3,413（杭州 377、北京 335） |
| restaurants | 4,655 |
| accommodations | 3,866 |
| trains | 5,345 |
| flights | 720 |
| poi | 12,172 |
| subway_stations | 3,635 |

attractions 字段：`id, name, type, lat, lon, opentime, endtime, price, recommendmintime, recommendmaxtime`

## 使用前必须知道的两件事（2026-09-28 实测）

1. **坐标系是百度 BD-09，不是 WGS-84，也不是高德的 GCJ-02。**
   直接喂给高德做路线会整体偏移约 900m；按"WGS-84→GCJ-02"转换会更差（偏差升至 1159–1373m）。
   实测：BD-09→GCJ-02 后与高德 POI 坐标差 **10–35 m**（灵隐寺 23m / 杭州宋城 35m / 故宫 10m）。
2. **`price` 与营业时间不可直接采信。** `0.0` 无法区分"免费"与"缺失"（故宫博物院标 0.0，实际收费），
   `00:00–24:00` 是占位值（西湖）。论文自述为"collect public information to construct the database"，
   即人工汇集的**静态快照**，不随时间更新。
   → 按本库 schema：坐标经 BD09→GCJ02 校正后仍须用 `get_amap_poi_search` 复核；
     票价/时间写入时 `valid_until` 取快照发布日 + 90 天，默认视为已过期存疑。

## 署名要求

CC BY 4.0 要求署名。凡由本快照派生的 wiki 页面，需在 `sources` 中保留本文件路径，
并在页面脚注写明 "ChinaTravel Sandbox (LAMDA-NeSy, CC BY 4.0)"。
