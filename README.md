# LLM-guide-agent

旅游出行路线推荐规划 Agent，基于 LLM Wiki 模式（Karpathy）构建**自我维护的目的地知识库**。

## 工作方式

三层结构（参考 [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) / Karpathy LLM Wiki）：

```
raw/     原始资料（不可变快照：秘塔搜索结果、高德 API 返回、网页原文）
wiki/    LLM 编译生成的知识页面（城市 / POI / 路线），带 index.md 导航、log.md 历史
schema/  契约：页面模板、命名与链接规则、摄入与核查规范
```

三大操作：**Ingest（摄入）→ Query（规划时查询）→ Lint（时效核查）**，由 agent 依照 `AGENTS.md` 执行。

## MCP 工具

本仓库通过 `.mcp.json` 接入本地 `travel` MCP server（`mcp/travel_mcp.py`，FastMCP stdio）：

| 工具 | 用途 | 在 wiki 中的角色 |
|---|---|---|
| `metaso_web_search` / `metaso_web_reader` | 网页搜索与正文读取 | Ingest 的主要来源 |
| `get_amap_weather` | 实况/预报天气 | Query 时实时校正；Lint 的季节性依据 |
| `get_amap_direction` | 驾车/公交/步行/骑行路径规划 | route 页面的数据来源 |
| `get_amap_poi_search` / `get_amap_input_tips` | POI 检索、地点补全 | poi 页面的数据来源 |

运行前的 Key 配置：两个 Key 放在仓库根 `.env`（`AMAP_API_KEY`、`METASO_API_KEY`，已被 `.gitignore`
排除），`travel_mcp.py` 启动时会自动加载，因此 `.mcp.json` 里不落密钥。变量名清单见 `.env.example`。

## 快速上手

1. 复制 `.env.example` 为 `.env` 并填入密钥；建虚拟环境并装依赖：
   `uv venv .venv` → `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`。
2. 对 agent 说："摄入杭州的攻略到 wiki" —— agent 会调用秘塔搜索/读取，快照存入 `raw/`，编译成 `wiki/cities/杭州.md` 等页面并更新 `wiki/index.md` 与 `wiki/log.md`。
3. 规划行程时："帮我规划 3 天杭州行程" —— agent 先读 `wiki/index.md` 定位页面，再调用高德做实时校验。

详细规则见 [AGENTS.md](AGENTS.md) 与 [schema/schema.md](schema/schema.md)。
