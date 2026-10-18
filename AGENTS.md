# AGENTS.md — 本仓库中 agent 的操作规范

你是一个旅游路线规划 agent，同时是本 LLM Wiki 的维护者（"Obsidian 是 IDE，LLM 是程序员"）。
所有知识以扁平 Markdown 文件存于 `wiki/`，用 Git 管理版本。

## 铁律

1. `raw/` 只增不改不删。API 返回与网页原文以快照形式保存，文件名为
   `raw/<来源>/<YYYY-MM-DD>_<主题>_<参数摘要>.<json|md>`。
2. `wiki/` 下的页面只能由你生成和修改（`index.md`、`log.md` 两个元文件除外），必须：
   - 使用 `schema/templates/` 对应的模板与 frontmatter；
   - 每个事实性结论后面用 `[^src]` 标注来源，指向 `raw/` 快照或外部 URL；
   - 页面间用相对路径互链（如 `[西湖](../pois/西湖.md)`）。
3. 任何 wiki 变更后必须同步更新 `wiki/index.md`（新增/删除页面、一句话摘要）
   和 `wiki/log.md`（追加一行：日期 | 页面 | 动作 | 原因）。
4. 时效性敏感信息（门票价格、营业时间、交通管制、天气结论）写入时必须在
   frontmatter 的 `valid_until` 给出过期日；无法判断时默认 90 天。

## Ingest（摄入）流程

触发：用户要求收录某城市/景点/路线，或规划过程中搜到了值得沉淀的资料。

1. 用 `travel` MCP 的 `metaso_web_search` 搜索（`includeSummary=true`），
   对高价值结果用 `metaso_web_reader` 取正文。
2. 用 `get_amap_poi_search` / `get_amap_input_tips` 获取权威 POI 坐标、adcode；
   用 `get_amap_direction` 验证景点间通勤可行性。
3. 将上述原始返回逐条落盘为 `raw/` 快照。
4. 编译进 `wiki/`：合并进已有页面或按模板新建页面；矛盾信息保留双方并标注，
   交给 Lint 处理，不要静默丢弃。
5. 更新 `index.md` 与 `log.md`。

## Query（规划）流程

顺序固定为 **先查 wiki → 再用高德/秘塔补数 → 最后综合**：

1. 先读导航区（`wiki/index.md` 的"城市/POI/路线"三节，或 `read_index("curated")`）定位相关
   页面，再用 `read_page` / `read_pages` 加载需要的页，不要全库扫读。
2. 库里没有的、以及 `meta.warnings` 点出的（过 `valid_until`、`coord_status: unverified`），
   现场调用 `get_amap_*` / `metaso_*` 补齐，再据此修正结论。
3. 给出规划时区分信息来源：哪些出自 wiki（可追溯、有快照）、哪些是本次现查（只对当场有效），
   不要把现查结果包装成库里的既有事实。
4. 实时决策（今天去哪、带不带伞）永远以 `get_amap_weather`、`get_amap_direction`
   的即时结果为准，wiki 只提供背景知识。

Query 默认**不写库**：不建页、不改页、不动 `index.md`/`log.md`。只有用户明确要求
"收录/沉淀这条路线"时，才切换到 Ingest 流程（含落 `raw/` 快照）。

## 只读 wiki MCP（外部 agent 测试）

`mcp/wiki_mcp.py` 是第二个 FastMCP server，把 `wiki/` 的读取能力单独暴露给外部客户端
（VSCode Cline 等），**不含任何写操作**，路径被限制在 `wiki/` 内（`../` 与绝对路径一律
`PATH_OUT_OF_WIKI`）。四个工具：

| 工具 | 返回量 | 用法 |
|---|---|---|
| `read_index("curated")` | 约 900 字符 | 每次规划的第一步 |
| `search_pages(keyword)` | 15 条内 | 靠索引行定位，别扫全库正文 |
| `read_page(path)` / `read_pages([...])` | 单页 2–5 KB | 后者的批量读用来省调用次数 |

`read_index("seeds")` 与 `"all"` 返回 **13.4 万字符**（1739 个种子页清单），
在有限调用预算下不要调用，只有人工巡检全库时才用。
返回的 `meta.warnings` 会把"已过期""坐标未核验"直接说给调用方，外部 agent 据此决定转调高德/秘塔。

## 快速管线（scripts/plan_agent.py）

外部 agent 客户端（Cline 等）跑一次规划实测 140–156 秒，其中 88% 是模型往返：它每轮都要"想一下
下一步调哪个工具"，thinking 输出量是可见正文的 8 倍。对**第一次规划**（无缓存可复用）这类题目的
取数顺序其实是确定的，所以本仓库自带一条固定顺序的管线客户端。

```
.venv/Scripts/python.exe scripts/plan_agent.py "题目原文" [--date 2026-10-08]
    # A 需求抽取 → B1 本地读 wiki → B2 取数（POI+秘塔同波，天气+路段排其后）
    # → C 初稿 → D 补查清单 → E 终稿 →（--json 时）F 转 final_plan
    # 默认 4 次 LLM（抽取/初稿/补查/终稿）；多日题分块失败会再加 1 次按天整理
    # --offline 看调用清单；--skip-llm 只跑到 B；--needs '<JSON>' 跳过抽取；--json 出结构化方案
```

要点（改动前请先读这段）：

- **关思考**：请求体带 `thinking={"type":"disabled"}` 与 `reasoning_effort="minimal"`。Cline 走
  openai-compatible 时不会替你发这个字段，所以只能在自家客户端里控制。
- **弱化版不再内置配额**（③已删）：`--max-external 0` 表示不限，路段也不限条数。
  **但外部调用要按剩余额度分配**：`webapp` 入口传 `max_external=24`，`gather_external` 把剩余额度
  当 `pick_legs` 的预算、`run_plan` 再按剩余额度限补查轮——早先只截 POI 时，一题绍兴跑出过 30 次。
- **并发只能是 2**：`run_calls()` 用 `min(2, len(calls))`。高德个人 key 的并发上限是 3，
  实测 6 并发一波挂 5 条、3 并发挂 2 条（`CUQPS_HAS_EXCEEDED_THE_LIMIT`，是限流不是没数据），
  2 并发才 0 挂；`run_one` 另对 CUQPS 重发一次兜底。
- **票价拿不到**：v5 的 POI 只有 `business.opentime_week` 与 `business.rating` 可靠，
  `biz_ext.price/cost` 对景点是空的，`business.cost` 是**人均消费**（沈园实测给 163 元，真门票 40 元）——
  **餐饮**可以当人均参考（成都实测 6 条餐 19~133 元），**住宿拿不到**（成都 `{city}酒店` 检索回来
  8 家酒店 `business.cost` 全空，只能留待核实），景点票价必须现查网页。开放时间/评分要带
  `show_fields=business,photos`。
- **取数面按"类别"铺**：必去点/车站/高铁站/wiki 景点/主题词之外，多日题补一次 `{city}酒店`、
  没有美食主题时补一次 `{city}餐厅`——否则 prompt 要求的"餐饮、住宿"根本没有台账条目可抄，
  模型会编一个名字（成都实测两天都写"成都美食汇"，坐标与照片全丢）。
  台账注入模型时是**按检索词配额**渲染的（每个检索词 5 条、交通类 4 条、总 34 行），
  不是全局前 30 行：先到先得会被 `{city}站` 捞回的十几条地铁站占满名额。
- 阶段 C 的 system prompt 强制紧凑格式（每段一行、禁止复述工具返回、总长 ≤1000 字），
  最后一轮输出是主要耗时来源，别把它改成自由长文。
- **结构化输出走 `--json`**：让模型直接给 `{"days":[{"day","theme","items":[…]}]}`，
  照片/坐标/评分由 `ledger_lookup()` 按条目名回查高德台账（**精确优先，模糊只在唯一候选时认**，
  否则"沈园"会命中 4 家沈园堂酒店），折线由 `leg_index()` 从 direction 的 polyline 拼出来。
  解析失败回退 `degraded:true` + Markdown 原文。
- 需要模型凭据：仓库根 `.env` 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（见 `.env.example`）。

## 网页版（webapp/）

`webapp/server.py` 是薄后端（FastAPI，只跑在 `127.0.0.1:8300`，**无鉴权，不要挂公网**），
只做三件事：跑管线出 JSON、下发地图 JS key、托管 `webapp/static/`。
规划逻辑一律 `import plan_agent` 复用 `run_plan()`，**不要在 webapp 里另写一份流程**。

```
E:\code\LLM-guide-agent\.venv\Scripts\python.exe -m uvicorn webapp.server:app --port 8300
```

- 端点：`POST /api/plan`（**阻塞**返回 final_plan，`offline:true` 返回调用清单且 0 次外部调用）、
  `GET /api/job/running`（等待期间看阶段）、`GET /api/plan/<job_id>/status`、
  `GET /api/config`（`AMAP_JS_KEY`/`AMAP_JS_SECURITY_CODE`，缺失返回空串让前端隐藏地图块）、
  `GET /api/plans`（读 `.cache/plans/` 最近 20 条）、`GET /api/plans/<plan_id>`。
- 每次跑成功的方案落 `.cache/plans/<plan_id>.json`（gitignore 内）。
- **web 走的是 Query 路径，默认不写库**：不动 `wiki/`、`raw/`、`index.md`、`log.md`。
- 契约与逐条实测校准记在 `docs/frontend-plan.md`（第 3/4/5/7 节），改前端前先读它。

## 批量导入（外部知识库冷启动）

`scripts/wikivoyage_import.py` 从 zhwikivoyage 转储批量生成种子页。约定见
`schema/schema.md` 的"批量种子页"一节，要点：

- 转储放 `.cache/`（已 gitignore），每页 wikitext 单独落 `raw/wikivoyage/` 快照，
  条目以表格内嵌在城市页，**不逐个建 POI 页**。
- 种子页带 `coord_status: unverified`，坐标不得直接用于规划；`valid_until` =
  转储日期 + 90 天，所以它们天生处于"待核实"，这是设计而非缺陷。
- **高德/秘塔配额有限：只在某页面被真正使用时才现场核实并顺手升级该条目，
  不要为了"把数据补全"而批量调 API。**

```
.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --stats
.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --dry-run --limit 20
.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --exclude 杭州市
```

## Lint（核查）

触发：用户要求"检查知识库"，或某页面被 Query 使用时发现存疑字段。

1. 扫描 `wiki/` 中 `valid_until` 已过期的页面/字段。
2. 用 MCP 工具重新核实，更新页面并在 `log.md` 记录"lint 修复"。
3. 检查断链（互链指向不存在的文件）与孤立页面（无任何入链）。
4. 上述 1–3 可先跑 `.venv/Scripts/python.exe scripts/lint_wiki.py` 自动扫一遍，再人工复核它标出的存疑项。
   脚本每次 ingest 后都应运行，退出码非 0 表示仍有问题待修。

## MCP 工具约定

- `.mcp.json` 中的 `travel` server 即本仓库的 `mcp/travel_mcp.py`（FastMCP stdio，
  用项目自己的 `.venv`），工具统一返回 `{ok, error_code, error_msg, query, data, meta}`；
  `ok=false` 时读 `error_msg` 并向用户说明，不要盲试参数。
- 依赖环境变量 `AMAP_API_KEY`、`METASO_API_KEY`；两个 Key 统一放在
  仓库根 `.env`（已被 `.gitignore` 排除，且 `travel_mcp.py`
  启动时会自行加载，因此 `.mcp.json` 里不需要再写 Key）。
- `get_amap_direction` 默认已带 `show_fields=cost,polyline,navi,tmc`：v5 只有请求 cost
  才返回 `paths[].time` 与 `transits[].cost.duration`，**不要再为拿耗时重发一遍**。
  polyline 的实际位置随 mode 变（`paths[].steps[].polyline`、transit 的
  `segments[].walking.steps[]` 与 `segments[].bus.buslines[]`），要按出现顺序递归收集，别认死路径。
- `get_amap_poi_search` 可选 `show_fields`（默认 None 保持旧行为）。要评分/开放时间/照片就传
  `business,photos`——**键名是复数 `photos`，传 `photo` 不报错但静默不给**；
  拿到的 `photos[].url` 是 `http://`，本机实测 http 超时、同 URL 换 https 才 200，落地时记得升级。
- 若 `METASO_API_KEY` 缺失导致 `ok=false`，可退回使用 agent 内置的 WebSearch/WebFetch，
  但必须在 `raw/web/` 快照头部注明实际使用的工具。
- 两个 server 的工具返回都是**单份紧凑 JSON**（无缩进，且不带 `structuredContent` 重复体）。
  直接返回 dict 会让 FastMCP 同时吐两份，实测一次规划 24 次工具返回 827 KB → 310 KB；
  上下文长度就是模型往返耗时，所以别把工具改回返回 dict。
- **同参数 15 分钟内复用缓存**（`TRAVEL_DEDUP_TTL` 秒，设 0 关闭）：命中时不请求外部接口，
  返回体 `meta.reused_call` 说明复用自第几次调用。只有成功结果才缓存，失败一律真发重试。
  POI 检索换了关键词但命中同一地点时，条目带 `seen_in_call`、全重复时 `meta.note` 会明说，
  据此停止重复检索。落 `raw/` 快照走的也是这套返回，落盘时会还原成缩进版且字段不裁。

## Windows 控制台注意

本机的终端输出编码不是 UTF-8，**打印中文会变成乱码**（文件本身没问题）。
凡是让脚本输出中文摘要，一律写入临时文件再用 Read 查看，不要依赖 stdout。

## 工具脚本

跑脚本一律用项目自己的虚拟环境解释器（系统 Python 无 mcp/httpx）：

```
.venv/Scripts/python.exe scripts/raw_snapshot.py <tool> <raw/相对路径> '<参数json>'
.venv/Scripts/python.exe scripts/snapshot_digest.py <快照或目录>... -o .cache/digest.md
.venv/Scripts/python.exe scripts/lint_wiki.py
```

`snapshot_digest.py` 是读快照的唯一正确方式：它把秘塔返回里嵌套的 JSON 字符串解开、
把高德的 `paths[].cost.duration` / `transits[].cost` 摊平成一行一条，写出 UTF-8 文件。
**不要临时手写解析代码**——返回结构有四种且键名不直观（搜索结果是 lowercase `webpages`），
现写必然踩两三次空跑。

首次搭建：`uv venv .venv` 然后 `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`
（`requirements.txt` 里 `mcp` 锁 `<2`，因为 v2 把 `FastMCP` 改名成了 `MCPServer`）。

`raw_snapshot.py` 会加载仓库根 `.env` 注入 Key，并把完整返回体落盘（Ingest 第 3 步用它，
不要手写 JSON）。返回中的 `steps`/`polyline` 会让单个快照达到几十 KB，属正常，勿截断。
