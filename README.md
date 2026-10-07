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

运行前的 Key 配置：两个 Key 放在仓库根 `.env`（`AMAP_API_KEY`、`METASO_API_KEY`，已被 `.gitignore`
排除），`travel_mcp.py` 启动时会自动加载，因此 `.mcp.json` 里不落密钥。变量名清单见 `.env.example`。

## 快速上手

1. 复制 `.env.example` 为 `.env` 并填入密钥；建虚拟环境并装依赖：
   `uv venv .venv` → `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`。
2. 对 agent 说："摄入杭州的攻略到 wiki" —— agent 会调用秘塔搜索/读取，快照存入 `raw/`，编译成 `wiki/cities/杭州.md` 等页面并更新 `wiki/index.md` 与 `wiki/log.md`。
3. 规划行程时："帮我规划 3 天杭州行程" —— agent 先读 `wiki/index.md` 定位页面，再调用高德做实时校验。

详细规则见 [AGENTS.md](AGENTS.md) 与 [schema/schema.md](schema/schema.md)。
