# Wiki Schema 契约

本文件定义 `wiki/` 页面的类型、命名、frontmatter 与互链规则。agent 生成页面时必须遵守。

## 页面类型

| 类型 | 目录 | 文件名 | 模板 |
|---|---|---|---|
| 城市 | `wiki/cities/` | `<城市名>.md`（如 `杭州.md`） | `templates/city.md` |
| POI/景点 | `wiki/pois/` | `<POI名>.md`（如 `西湖.md`） | `templates/poi.md` |
| 路线 | `wiki/routes/` | `<天数>天_<起点>_<主题>.md`（如 `3天_杭州_江南风光.md`） | `templates/route.md` |

## Frontmatter（全部页面必填）

```yaml
---
type: city | poi | route      # 页面类型
summary: 一句话描述             # 供 index.md 直接引用
sources:                        # 主要依据的 raw/ 快照相对路径
  - raw/amap/2026-09-28_weather_110000_base.json
updated: 2026-09-28             # 最后一次编辑日期
valid_until: 2026-12-27         # 过期后 Query 时必须现场核实
---
```

## 内容规则

- 事实来源标注：正文中关键事实用 `[^n]` 脚注，脚注区列出 `raw/` 路径或 URL。
- 互链：提到其他已建页面必须链接；未建页面写纯文本，由 Lint 提示是否值得建页。
- 数据呈现：价格/时间/距离等使用表格；坐标统一 `lon,lat` 格式（与高德 API 一致）。
- 矛盾信息：保留双说法并注明各自来源与日期，`valid_until` 设为较早那个的日期，
  强制下一次 Query 时核实。
- 禁止写入：无法溯源的"常识"、模型记忆中的门票价格等，一律先 Ingest 再写。
- 脚注定义行必须是**单一裸路径**，后面不加括号说明（`lint_wiki.py` 按行解析）：
  对
  - `[^3]: raw/amap/2026-09-28_dir_西湖到灵隐寺_driving.json`
  错
  - `[^3]: raw/amap/...driving.json（对照公交方案见 ...transit.json）`
- `wiki/index.md`、`wiki/log.md` 是元文件，不需要 frontmatter。
- 自动核查：`python scripts/lint_wiki.py`（检查 frontmatter 字段、过期 valid_until、
  断链、脚注路径是否存在、引用与定义是否匹配、孤立页面）。
