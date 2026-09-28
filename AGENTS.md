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

1. 先读 `wiki/index.md` 定位相关页面，只加载需要的页面，不要全库扫读。
2. 页面中已过 `valid_until` 的内容视为存疑：现场调用高德/秘塔核实后，
   顺手走一次小型 Ingest 修复该页面。
3. 实时决策（今天去哪、带不带伞）永远以 `get_amap_weather`、
   `get_amap_direction` 的即时结果为准，wiki 只提供背景知识。

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
E:/code/MCP-test/.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --stats
E:/code/MCP-test/.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --dry-run --limit 20
E:/code/MCP-test/.venv/Scripts/python.exe scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --exclude 杭州市
```

## Lint（核查）

触发：用户要求"检查知识库"，或某页面被 Query 使用时发现存疑字段。

1. 扫描 `wiki/` 中 `valid_until` 已过期的页面/字段。
2. 用 MCP 工具重新核实，更新页面并在 `log.md` 记录"lint 修复"。
3. 检查断链（互链指向不存在的文件）与孤立页面（无任何入链）。
4. 上述 1–3 可先跑 `python scripts/lint_wiki.py` 自动扫一遍，再人工复核它标出的存疑项。
   脚本每次 ingest 后都应运行，退出码非 0 表示仍有问题待修。

## MCP 工具约定

- `.mcp.json` 中的 `travel` server 即 `E:/code/MCP-test/mcp_test.py`，
  工具统一返回 `{ok, error_code, error_msg, query, data, meta}`；
  `ok=false` 时读 `error_msg` 并向用户说明，不要盲试参数。
- 依赖环境变量 `AMAP_API_KEY`、`METASO_API_KEY`；两个 Key 统一放在
  `E:/code/MCP-test/.env`（该文件已被其 `.gitignore` 排除，且 `mcp_test.py`
  启动时会自行加载，因此 `.mcp.json` 里不需要再写 Key）。
  若 `METASO_API_KEY` 缺失导致 `ok=false`，可退回使用 agent 内置的 WebSearch/WebFetch，
  但必须在 `raw/web/` 快照头部注明实际使用的工具。

## Windows 控制台注意

本机的终端输出编码不是 UTF-8，**打印中文会变成乱码**（文件本身没问题）。
凡是让脚本输出中文摘要，一律写入临时文件再用 Read 查看，不要依赖 stdout。

## 工具脚本

系统 Python 无 mcp/httpx 依赖，跑脚本一律用 MCP-test 的解释器：

```
E:/code/MCP-test/.venv/Scripts/python.exe scripts/raw_snapshot.py <tool> <raw/相对路径> '<参数json>'
E:/code/MCP-test/.venv/Scripts/python.exe scripts/lint_wiki.py
```

`raw_snapshot.py` 会读 `E:/code/MCP-test/.env` 注入 Key，并把完整返回体落盘（Ingest 第 3 步用它，
不要手写 JSON）。返回中的 `steps`/`polyline` 会让单个快照达到几十 KB，属正常，勿截断。
