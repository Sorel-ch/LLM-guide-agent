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
    # A 澄清+抽取 → B1 本地读 wiki → B2 取数（POI+秘塔同波，天气+路段排其后）
    # → C 初稿 → D 补查清单 → E 终稿 →（--json 时）F 转 final_plan
    # 默认 4 次 LLM（澄清+抽取/初稿/补查/终稿）；多日题分块失败会再加 1 次按天整理
    # --offline 看调用清单；--skip-llm 只跑到 B；--needs '<JSON>' 跳过抽取；--json 出结构化方案
    # 信息不全时不规划：终端里交互式追问；脚本里 --no-prompt 打印问题清单并以退出码 3 结束，
    # 带 --answer '你的回答'（可重复）续答；--no-clarify 关掉闸门回到旧的一次出方案
```

要点（改动前请先读这段）：

- **A 阶段是"澄清 + 抽取"的一次调用**（`clarify()`）：五项（目的地/起点/时间/预算/偏好）只要还有
  `unknown` 就不进 B，直接返回 `{"stage":"clarify", "missing":[…], "questions":[…]}`。
  状态定义：明确说过 = `stated`，用户表态"不限/都行/你看着安排" = `waived`，没说 = `unknown`；
  **目的地不许 waived**（没有地名规划无从谈起）。`missing` 由代码算，不信模型自称"齐了"：
  模型漏问的槽用 `CLARIFY_SLOTS`/`CLARIFY_ASK` 兜底，追问已明确的槽丢掉，一次最多 3 问、按固定优先级排。
  题目本来就写全时它取代原来的抽取，**不多花一次 LLM 也不多花时间**。
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
  台账注入模型时是**按检索词配额**渲染的（每个检索词 6 条、交通类 4 条、总 44 行），
  不是全局前 30 行：先到先得会被 `{city}站` 捞回的十几条地铁站占满名额。
  34 行顶到过（宁波实测正好渲染 34 行、后面还有内容被砍），行数是输入侧、
  多几百字符不影响耗时——**瓶颈在最后一轮的解码，`PLAN_CHAR_LIMIT` 别跟着放宽**。
  同样放宽的：路段链 `chain_cap` 6 → 10（终稿每天 5~6 站，6 个点的链盖不住，
  实测请求 7 条路段却有 11 条交通段）、web 软上限 24 → 32、
  `forced_gap_asks` 6 → 8、必去点与主题词的检索 `[:2]` → `[:3]`。
- **住宿还要一次按半径的周边搜**（`types=100000`，中心取 `lodging_center()` 的 medoid，
  半径 4 km）：`{city}酒店` 是全城检索，宁波连着两次捞回**溪口（30 km 外）**的宾馆，
  模型把它排进当晚，第二天等于从溪口出发。周边搜回来的市区住宿在台账里排在**最后**
  （第二波才回来），台账名额轮不到它——所以渲染前要把"离游览区最近的至多 5 家"提到前面，
  并且半径卡空时要退回"只按最近几家"，宁可留远的也不一条都不给。
- **游览顺序由代码算，模型照抄**（`suggest_order()`）：直线距离（`km_between`，0 次外部调用）
  → 同地点合并（`merge_same_place`：包含/共享 ≥3 字/≤400 m，代表按评分再名字长度）
  → 离群点先摘掉（到其它点都 >10 km 的另列 `outliers`，不占一天也不硬挪进某天）
  → k-means 分天（k=天数，确定性；**种子用 farthest-point**，别改回"按经度等距取点"：
  成都实测 9 个候选点里老城 5 个、南部新区 4 个，等距取种三颗全落进老城，分出 1+4+4，
  平衡又把 7 km 外的金融城双子塔搬进老城那天 → day1 成 11.6 km 的折返链。
  换 farthest-point 后同一题变成 4.5/4.1/9.3 km，且 7178 组管线真实输入里再没出现"某天分空"）
  + 条数平衡（最空的组先补、donor 挨个试；只搬 `BALANCE_MAX_KM` 以内的点，
  溪口那种不为凑条数搬家）→ 每天开放路径
  （`open_path`：最近邻 + 2-opt，**试遍起点**，只按经度钉首点实测多出 3 km）。
- **每天景点 2~3 个是上下限，两个都由代码兜**（`SIGHTS_MIN/MAX_PER_DAY`，不含餐饮住宿交通段）：
  候选先摘离群点、再按"必去 → 这道题点名的检索 → 评分"截到 `3×天数`（不截的话一次
  `{city}景点` 带回的儿童公园/文化广场会把建议链铺到 21 km，模型只抄其中一段）；
  几何挡住平衡、某天仍超 3 站时，多出来的另列 `spare`（"当天排不下的备选"），不塞进某天。
  下限给不出两天各 2 站时记进 `thin_days`，【建议顺序】里那句"从【POI】另挑 类=风景名胜
  /科教文化 补上，别把当天留给一顿午饭"是**明说给模型的**——day2 排得薄原先是靠用户目视发现的。
  模型自己往某天塞第 4、5 个景点时由 `cap_day_sights()` 裁（prompt 规则 2e 拦不住，
  上海实测 day2 写了 5 个、当天折返比 1.94）：留哪几个按**当天这几个点走一遍的最短开放路径**选，
  不是按评分——第一版按"在建议链里 → 评分"留，三天折返比反而从 1.63/1.94/1.40 顶到 1.89/2.04/1.92，
  因为模型多写的往往是同一片区的近邻，删掉它们等于留下散落全城的高分锚点。
  **这一步必须排在 F 补腿之前**（`run_plan` 里那道 pre-pass）：裁掉景点要把它两侧的腿缝回相邻
  两个留存点，排在后面的话等于删条目顺便删折线（上海实测同一天裁之前 4 条线、裁完只剩 2 条）。
  每天的 `sights`（同地点先合并后计数）进 JSON，CLI 汇总行跟着报 `景点=3/3/3`。
  验收仍看 `detour_ratio`（实走 ÷ 最短可达，>1.25 写 `route_note`）——它是指标不是否决门。
- **台账不收关门的店**（`POI_CLOSED_RE`，在 `collect_pois` 就拦）：高德把停业状态写进名字里，
  上海实测 `老城隍庙小吃广场(豫园路店)(暂停营业)` 进了台账、于是被 day1 与 day3 各排了一次午餐。
  台账是候选池，池子里不该有关门条目。
- **缺口由代码算，不靠模型自觉**（`forced_gap_asks()`）：扫初稿，台账里没有的点必发一次
  `get_amap_poi_search`，景点/住宿/餐饮在台账里没价格就必发一次秘塔（v5 无票价字段、
  住宿 `business.cost` 实测全空，只能现查网页）。宁波实测：**模型自己给的清单 0 条、代码算出 6 条**，
  之前"罗城免费与否、宾馆房价"全是"没查"而不是"查不到"。
  补查轮真正发出去的清单现在落在 `plan.sources.gap_asks` 里（工具 + 参数截 160 字），
  下次判断"没查 / 查不到"直接看这份产物，别重跑：宁波实测 `汉庭酒店(儿童公园地铁站店)`
  这条**发了两条查询**（这一家的具体名 + 海曙经济型酒店均价），秘塔只给到别的分店的价
  （"汉庭宁波火车站店 198 元/晚"）——那是另一家店，不能搬进 `cost`，卡片就照实写"房价未实测"。
- **餐宿 tip 里那句"人均"必须带数字**（`normalize_people_tip()`）：`cost` 有值时把
  "人均参考未实测"改口、把只剩"人均参考"四个字的补成"人均参考 100 元"——
  金额本来就在（多数来自台账人均），只写在展开的详情里看上去就像数据缺失。
  `cost` 为 null 时那句"未实测"是真话，必须留着。
- **`wiki_sights` 只认目的地城市的页**：知识块里混着出发地城市与邻市页（`search_pages` 整行匹配），
  照单全收会拿"乌镇/西栅/南湖"去宁波检索，而 `wiki/pois/宁波博物馆.md` 这类真候选反倒没被拿去换坐标，
  结果第二天条目全没坐标、地图上只剩第一天。POI 页的标题优先于城市页 see 表，多日题取词随天数放大。
  配套：`wiki_bundle` 的首屏页预算固定给 4000 字（旧算法在 8 页时算出负预算、被兜成 600 字，
  城市页连带票价的 see 表都被截没）。
- **折线回查：坐标是终审**（`match_leg` + `leg_index` 的 `a_coord/b_coord`）：模型写
  "宁波站→天一阁博物院"，路段链上却叫"宁波火车站/天一阁"，所以先按台账坐标比；
  **两端都解析得出坐标却没这条腿 = 没测过，返回 None**，不许再按名字猜。
  名字兜底只服务解析不出坐标的简称，且要求两段各自在正确方向命中（反方向不给，方向箭头会画反）。
  一条实测折线不能分给两段（`claimed`）——宁波栽过：`宁波站→宁波站南广场地下停车场` 这条 300 m 步行腿
  同时命中了四段带"宁波站"的交通段（"宁波站"既是起点又是另一端的子串），
  于是"9/10 有折线"实际只有 2 条线。**验收数折线要用 `md5(path)` 去重后的条数和 `path_status`，
  不能数"有 path 的条目"。**
  `path_status` 三值：`measured` / `missing` / `cross_city`，城际段本来就测不到，前端要分开写。
- **终稿成形后还要补腿**（`plan_leg_topup()`，阶段 F 的前半）：`pick_legs` 那条链是在方案成形
  **之前**按台账最近邻铺的，模型自己插进来的餐饮/住宿与"从到达枢纽出发的那一段"从来不在里面——
  宁波实测 11 条交通段有 5 条根本没发过请求，**加外部额度治不了这个**。
  做法：读交通段自己写的"甲→乙"，两端都能 `resolve_ref` 出台账坐标、又没测过的才发
  （≤10 条，2.5 km 内 walking 否则 transit 带 adcode）。
  **配对必须用 `match_leg` 同一套解析**：改成按"非交通条目顺序推相邻对"实测会漏——
  模型声明 `宁波站→天一阁·月湖`，而那个序列里中间夹了餐厅，两边都测不到。
  补腿走自己那份独立额度、不计进 `max_external`（direction 是日配额 5000 的便宜接口），
  所以一次方案的 `external_calls` 会看到 32 → 37 这种数，不是 bug。
- **城际段只是信息，不是路径**（2026-10-19）：不检索出发地坐标、不请求"出发地→枢纽"的跨城
  direction 腿（高德给这种 OD 只给得出"地铁1号线>5号线…169 分"，模型照它编过高铁车次），
  车次与票价一律走那条秘塔查询。`origin_point` 字段与前端的"起"标记已删——地图只画目的城市；
  对应的交通段用 `path_status:"cross_city"` 显示成"城际段不实测折线"，而不是"路线待实测"。
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

- 端点：`POST /api/chat`（多轮澄清：`{sid?, message, date?}` → `{sid, ready, missing, questions, needs}`，
  会话落 `.cache/chats/<sid>.json`，`GET /api/chat/<sid>` 刷新后接着问）、
  `POST /api/plan`（**阻塞**返回 final_plan，`offline:true` 返回调用清单且 0 次外部调用；
  澄清齐了要带 `needs`+`turns` 回来，否则阶段 A 会又抽一遍）、
  `GET /api/job/running`（等待期间看阶段）、`GET /api/plan/<job_id>/status`、
  `GET /api/config`（`AMAP_JS_KEY`/`AMAP_JS_SECURITY_CODE`，缺失返回空串让前端隐藏地图块）、
  `GET /api/plans`（读 `.cache/plans/` 最近 20 条）、`GET /api/plans/<plan_id>`。
- 前端首页是**对话气泡流**，不是表单：`ready=false` 期间"开始规划"按钮一直禁用，
  追问的可点短答案来自模型给的 `questions[].options`。
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
  **transit 的 `city1`/`city2` 只认 adcode**：传"成都"直接 `INVALID_PARAMS`（0 条方案），
  传 `510100` 才给 5 条——同一对坐标实测。
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
