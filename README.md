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

本仓库通过 `.mcp.json` 接入两个本地 FastMCP server（均在 `mcp/` 下，用项目自己的 `.venv` 起）：

`travel`（`mcp/travel_mcp.py`）——外部数据源：

| 工具 | 用途 | 在 wiki 中的角色 |
|---|---|---|
| `metaso_web_search` / `metaso_web_reader` | 网页搜索与正文读取 | Ingest 的主要来源 |
| `get_amap_weather` | 实况/预报天气 | Query 时实时校正；Lint 的季节性依据 |
| `get_amap_direction` | 驾车/公交/步行/骑行路径规划 | route 页面的数据来源 |
| `get_amap_poi_search` / `get_amap_input_tips` | POI 检索、地点补全 | poi 页面的数据来源 |

`wiki`（`mcp/wiki_mcp.py`）——**只读**暴露 `wiki/`，供外部 agent 做规划查询：
`read_index` / `search_pages` / `read_page` / `read_pages`，无任何写操作，读取范围锁死在
`wiki/` 内。规划顺序固定为「先 wiki、再高德秘塔补数、最后综合」，见 `AGENTS.md`。

两个 server 的返回都是单份紧凑 JSON（去掉 FastMCP 默认的缩进正文 + `structuredContent` 双份），
`travel` 还在 15 分钟内复用同参数结果（`TRAVEL_DEDUP_TTL=0` 关闭），命中时标 `meta.reused_call`。
这两条是为了压上下文长度：实测模型往返耗时占一次规划的 123/140 秒，而耗时基本跟着上下文走。

在 VSCode Cline 里挂这两个 server 时，配置写进 Cline 的**全局** MCP 文件
`%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`，
`command` 必须写**绝对路径**（`.mcp.json` 里的相对路径只有从仓库根启动的宿主能解析）。
Cline 4.x 的自动批准字段名是 `autoApprove`（旧文档里的 `alwaysAllow` 已不适用）：

```json
{
  "mcpServers": {
    "wiki": {
      "type": "stdio",
      "command": "E:/code/LLM-guide-agent/.venv/Scripts/python.exe",
      "args": ["E:/code/LLM-guide-agent/mcp/wiki_mcp.py"],
      "autoApprove": ["read_index", "search_pages", "read_page", "read_pages"],
      "disabled": false
    },
    "travel": {
      "type": "stdio",
      "command": "E:/code/LLM-guide-agent/.venv/Scripts/python.exe",
      "args": ["E:/code/LLM-guide-agent/mcp/travel_mcp.py"],
      "autoApprove": [],
      "disabled": false
    }
  }
}
```

`autoApprove` 只给只读的 wiki 工具，让每次读库不必逐次点确认；`travel` 留空数组，因为它花真实
配额，保留人工确认顺便便于统计调用次数。Key 不用写进 `env`：`travel_mcp.py` 启动时会自己加载
仓库根 `.env`。

运行前的 Key 配置：密钥统一放仓库根 `.env`（已被 `.gitignore` 排除），`travel_mcp.py` 与
`plan_agent.py`、`webapp/server.py` 启动时都会自行加载，因此 `.mcp.json` 里不落密钥。
需要填的四组：`AMAP_API_KEY`、`METASO_API_KEY`、`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`（管线用）、
`AMAP_JS_KEY/AMAP_JS_SECURITY_CODE`（网页地图块用，**必须是「Web端(JS API)」类型，与 `AMAP_API_KEY` 不通用**；
不填则前端隐藏地图块而不是报错）。变量名清单见 `.env.example`。

## 快速上手

1. 复制 `.env.example` 为 `.env` 并填入密钥；建虚拟环境并装依赖：
   `uv venv .venv` → `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`。
2. 对 agent 说："摄入杭州的攻略到 wiki" —— agent 会调用秘塔搜索/读取，快照存入 `raw/`，编译成 `wiki/cities/杭州.md` 等页面并更新 `wiki/index.md` 与 `wiki/log.md`。
3. 规划行程时："帮我规划 3 天杭州行程" —— agent 先读 `wiki/index.md` 定位页面，再调用高德做实时校验。
4. 想要不依赖 agent 客户端多轮循环的规划：
   `.venv/Scripts/python.exe scripts/plan_agent.py "题目原文" --date 2026-10-13` ——
   固定顺序管线（抽取 → 读 wiki → 取数 → 初稿 → 补查 → 终稿），默认 4 次模型往返、
   单题十几秒；加 `--json` 直接产出结构化方案并落 `.cache/plans/<plan_id>.json`。
   详见 `AGENTS.md` 的"快速管线"一节（需 `.env` 里的 `LLM_*` 三项）。
5. 想用网页看结果：
   `E:\code\LLM-guide-agent\.venv\Scripts\python.exe -m uvicorn webapp.server:app --port 8300`
   然后开 `http://127.0.0.1:8300/`。

## 网页版（webapp/）

`输题目 → 看方案` 的单页前端 + 薄后端，三层都在本仓库内：

```
浏览器 (webapp/static/)  ──HTTP──>  薄后端 (webapp/server.py, FastAPI)
                                      │ 直接 import
                                      v
                              scripts/plan_agent.py 的 run_plan()
```

- **输入页**：题目 + 日期 + "离线预演"开关（只列将要发出的外部调用清单，**0 次外部请求**）；
  规划要跑 10~30 秒，等待期间轮询 `/api/job/running` 显示阶段进度。
- **详情页**：按天分块的时间线卡片（时间列 → 节点图标 → 卡片），中转渲染成卡片之间的胶囊；
  每卡片带景点照片、评分、开放时间、票价，点开展开大图与坐标明细。
- **来源可追溯**：每条结论带 `verified` + `source`（`wiki` / `amap_poi` / `amap_direction` / `metaso` / `none`），
  没核实到的显示"待核实"角标而不是编一个数；页尾列出命中的 wiki 页与**失败的外部调用**（不静默吞）。
- **地图块**：高德 JS API（`/api/config` 下发 key），marker 用条目坐标、折线用本次实测 polyline；
  没配 key 或 key 失效时显示说明文字，时间线照常可用。
- 只跑在本机，**无鉴权、不要挂公网**；网页走的是 Query 路径，**不写 `wiki/`、不新增 `raw/` 快照**。
- 设计与逐条实测校准见 `docs/frontend-plan.md`。

详细规则见 [AGENTS.md](AGENTS.md) 与 [schema/schema.md](schema/schema.md)。
