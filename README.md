# LLM-guide-agent

旅游路线规划 agent：先把需求问全，再用「只读知识库 + 高德/秘塔实时数据」生成带来源的行程单。
仓库包含规划管线、本地网页版，以及一份可追溯的旅行知识库。

## 现在的形态

- **网页版是主入口**：多轮对话问清五项（目的地 / 起点 / 时间 / 预算 / 偏好）→ 固定顺序取数 →
  地图 + 逐日时间线 + 历史记录。见 [webapp/](webapp/)。
- **命令行可直接跑管线**（[scripts/plan_agent.py](scripts/plan_agent.py)）：支持交互追问、
  离线预演、`--json` 结构化输出，单题通常 20–40 秒。
- **不依赖 Cline 或其他外部 agent 客户端**：澄清、取数顺序、模型调用、结果落盘都在本仓库内完成。
  `.mcp.json` 里的 MCP server 现在是可选的对外集成面，不是运行前提。
- **`wiki/` 与 `raw/` 对 agent 只读**：规划时只查不改。知识库由人工维护并提交，
  不再有 Ingest / 自动写页流程。
- 高德 / 秘塔是个人免费额度：调用按剩余额度分配，并发固定为 2，不做批量补数。

## 架构

```
浏览器 (webapp/static/) ──HTTP──> 薄后端 (webapp/server.py, FastAPI)
                                      │ import run_plan()
                                      v
                            scripts/plan_agent.py
                              ├─ wiki/         只读背景知识（过期项现查）
                              ├─ travel MCP   高德 / 秘塔实时数据
                              └─ LLM          澄清、初稿、补查、终稿
                                      │
                                      v
                            .cache/plans/<plan_id>.json
```

知识库只是"背景知识 + 可追溯来源"：票价、营业时间、坐标这类会过期的事实，规划时一律以
本次高德/秘塔结果为准；`wiki/` 页面上的 `valid_until`、`coord_status` 会通过 `meta.warnings`
提示调用方哪些必须现查。

## 目录

| 路径 | 作用 |
|---|---|
| `scripts/plan_agent.py` | 固定顺序规划管线（澄清 → 读库 → 取数 → 初稿 → 补查 → 终稿） |
| `webapp/` | FastAPI 薄后端 + 无构建单页前端 |
| `mcp/travel_mcp.py` | 高德 / 秘塔工具（MCP stdio） |
| `mcp/wiki_mcp.py` | 只读 wiki 查询工具（MCP stdio） |
| `wiki/` | 知识页面（城市 / POI / 路线），**只读** |
| `raw/` | 不可变资料快照，**只增不改不删** |
| `schema/` | wiki 页面格式与命名契约 |
| `docs/frontend-plan.md` | 网页版 API 契约与 v1 建设记录 |
| `session-notes/` | 本地交接纪要（已 gitignore，不入库） |

## 快速开始

### 1. 环境

```bash
cp .env.example .env        # 填入真实 Key
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

需要四组配置，变量名都在 [.env.example](.env.example)：

- `AMAP_API_KEY`：高德「Web 服务」Key（POI / 路线 / 天气）
- `METASO_API_KEY`：秘塔搜索 Key
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`：OpenAI Compatible 模型
- `AMAP_JS_KEY` / `AMAP_JS_SECURITY_CODE`：高德「Web 端 (JS API)」Key，只给前端地图用；
  留空时前端隐藏地图块而不是报错

### 2. 网页版

```bat
E:\code\LLM-guide-agent\.venv\Scripts\python.exe -m uvicorn webapp.server:app --port 8300
```

打开 `http://127.0.0.1:8300/`。服务只监听本机，**无鉴权，不要挂公网**。

- 首页左栏是行程海报，右栏是对话卡；五项需求没问齐时"开始规划"按钮保持禁用，
  卡片顶部的槽位条会标出还缺哪几项。
- "离线预演"只列外部调用清单：**0 次外部请求**（请求没带 `needs` 时仍会花 1 次 LLM 抽需求）。
- 界面是暖纸 + 陶土红的行程单风格，含分批入场、详情展开与地图折线动效；
  系统开启"减少动态效果"时自动降级。
- 规划结果包含封面、地图、逐日时间线、可展开的条目详情与来源清单；
  跑成功的方案落在 `.cache/plans/`，历史页可回看最近 20 条。
- 网页走 Query 路径，不写 `wiki/`、`raw/`；接口与字段契约见
  [docs/frontend-plan.md](docs/frontend-plan.md)。

### 3. 命令行

```bat
E:\code\LLM-guide-agent\.venv\Scripts\python.exe scripts/plan_agent.py "从杭州去成都玩三天，预算 2000，想吃川菜、看大熊猫" --date 2026-10-21 --json
```

常用开关：

| 开关 | 作用 |
|---|---|
| `--offline` | 不联网、不调模型，只打印将发出的调用清单 |
| `--skip-llm` | 只跑到取数阶段，检查注入模型的上下文 |
| `--json` | 出结构化 `final_plan` 并落 `.cache/plans/` |
| `--needs '<JSON>'` | 跳过抽取，直接给需求 |
| `--answer '…'` | 回答上一轮追问（可重复）；脚本里配 `--no-prompt` 使用 |
| `--no-clarify` | 关掉追问闸门，按默认值直接规划 |

终端里直接跑会在信息不全时交互式追问；脚本里加 `--no-prompt` 会打印问题清单并以退出码 3 结束。

### 4. 只读检查

```bat
E:\code\LLM-guide-agent\.venv\Scripts\python.exe scripts/lint_wiki.py
```

检查 frontmatter、过期 `valid_until`、断链、脚注与孤立页面，只输出问题，不改文件。

## MCP 工具（可选）

`.mcp.json` 注册了两个本地 FastMCP server，供兼容 MCP 的客户端按需接入。
**网页版和命令行都不依赖外部客户端**，MCP 只是另一种调用面。

`travel`（[mcp/travel_mcp.py](mcp/travel_mcp.py)）——外部数据源：

| 工具 | 用途 |
|---|---|
| `metaso_web_search` / `metaso_web_reader` | 网页搜索与正文读取 |
| `get_amap_weather` | 实况 / 预报天气 |
| `get_amap_direction` | 驾车 / 公交 / 步行 / 骑行路径与折线 |
| `get_amap_poi_search` / `get_amap_input_tips` | POI 检索、地点补全 |

`wiki`（[mcp/wiki_mcp.py](mcp/wiki_mcp.py)）——**只读**暴露 `wiki/`，没有任何写操作，
路径锁死在 `wiki/` 内。四个工具：`read_index` / `search_pages` / `read_page` / `read_pages`。
规划顺序固定为「先 wiki、再高德/秘塔补数、最后综合」。
`read_index("seeds")` / `"all"` 会返回十几万字符的种子清单，只有人工巡检才用。

两个 server 的工具返回都是单份紧凑 JSON；`travel` 在 15 分钟内复用同参数结果
（`TRAVEL_DEDUP_TTL=0` 关闭），命中时标 `meta.reused_call`。Key 由服务启动时从仓库根 `.env` 读取，
`.mcp.json` 里不落密钥。宿主若不是从仓库根启动，把 `command` 改成 `.venv` 解释器的绝对路径即可。

## 知识库的边界

- **`raw/` 只增不改不删**：API 返回与网页原文以快照保存，文件名
  `raw/<来源>/<YYYY-MM-DD>_<主题>_<参数摘要>.<json|md>`。
- **`wiki/` 由人工维护**：页面必须遵循 [schema/schema.md](schema/schema.md) 的模板、
  frontmatter、脚注与互链规则；每个事实性结论要能指回 `raw/` 快照或外部 URL。
- **agent 没有写库权限**：规划 agent 不新建 / 修改 / 删除 `wiki/` 与 `raw/`，也不跑落盘脚本。
  需要更新知识库时，由人执行维护脚本、检查 diff 后提交。
- 时效性字段（门票、营业时间、交通管制、天气结论）必须带 `valid_until`；过期项在 Query 时现查。
- 知识库是**背景与线索**，不是实时事实源；不要把库里过期数据包装成本次核实结果。

### 知识库维护（人工执行）

```bat
rem 取一条工具返回，落成 raw 快照
E:\code\LLM-guide-agent\.venv\Scripts\python.exe scripts/raw_snapshot.py <tool> <raw/相对路径> '<参数json>'

rem 从 Wikivoyage 转储批量生成种子页（坐标标 unverified，valid_until = 转储日期 + 90 天）
E:\code\LLM-guide-agent\.venv\Scripts\python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --dry-run --limit 20

rem 读快照（唯一正确方式，勿手写解析）
E:\code\LLM-guide-agent\.venv\Scripts\python.exe scripts/snapshot_digest.py <快照或目录>... -o .cache/digest.md
```

人工维护时也要守配额纪律：只在页面被真正使用时核实，不为"补全数据"批量调 API。

## 开发约定

- 一切脚本用项目自己的虚拟环境解释器；系统 Python 没有 `mcp` / `httpx`。
- Windows 下给用户的命令用**反斜杠绝对路径**（cmd 里正斜杠会报"不是内部或外部命令"）。
- 脚本输出中文时先 `sys.stdout.reconfigure(encoding="utf-8")`；从 cmd 或重定向到文件仍可能乱码，
  需要人工阅读的报告一律写 UTF-8 文件，再打开查看。
- 静态资源改完要 bump `index.html` 里的 `?v=`，否则浏览器会继续用缓存。
- 高德个人 Key 并发上限实测为 3，管线固定 2 并发；不要为提速放开。
- 规划管线内部细节、踩坑与改动约束见 [AGENTS.md](AGENTS.md)；wiki 页面格式见
  [schema/schema.md](schema/schema.md)。
