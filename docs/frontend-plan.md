# 前端规划提示词 · LLM-guide-agent 网页版（v1）

> 用法：整份文档作为任务提示词交给编码 agent。执行前先读仓库根 `AGENTS.md`；
> 本文与 `AGENTS.md` 冲突时，以本文的 v1 范围为准，但 `AGENTS.md` 的铁律
> （raw/ 只增不改、Query 默认不写库、配额纪律）仍然全部有效。

## 1. 目标与定位

给本仓库的规划管线做一个"输题目 → 看方案"的单页前端。视觉与交互参考
FloatTrip（`E:\code\FloatTrip\frontend`，作设计参考，行程详情页的卡片式时间线 + 高德地图 + 景点照片这三件事照它的样子重做。

v1 只做展示，不做编辑：没有多轮对话、没有登录、没有拖拽改行程、没有 CP-SAT 重排、
没有贴士异步补全。这些是 FloatTrip 工作区的大头，全部砍掉，接口里也不预留假端点。

## 2. 架构（三层，全部在本仓库内）

```
浏览器 (webapp/static/)  ──HTTP──>  薄后端 (webapp/server.py, FastAPI)
                                      │ 直接 import
                                      v
                              scripts/plan_agent.py 的管线函数
                                      │
                              mcp/travel_mcp.py + mcp/wiki_mcp.py（不改动）
```

- 薄后端只做三件事：跑管线出 JSON、下发地图 key、托管静态文件。同进程同域，
  前端全用相对路径 `/api/...`，不出现任何硬编码 host。
- 管线逻辑一律 `import plan_agent` 复用现有函数（extract_needs / wiki_bundle /
  gather_external / build_context / gap_round / llm），**不要 fork 一份规划代码到 webapp 里**。

## 3. API 契约

### `POST /api/plan`

请求：`{"question": "题目原文", "date": "2026-10-13"（可省略）, "offline": false（true 时只回调用清单，不发外部请求）}`

响应即 `final_plan` JSON，字段命名对齐 FloatTrip 详情页组件的 `adaptPlan`，让它那些
展示逻辑几乎不用改就能喂进来：

```json
{
  "plan_id": "2026-10-12T14-03-21_成都",
  "destination": "成都",
  "days_count": 3,
  "start_date": "2026-10-13", "end_date": "2026-10-15",
  "budget_yuan": 2000,
  "weather_forecast": [{"date": "2026-10-13", "day": "晴", "night": "晴",
                        "temp_range": "16~26℃", "wind": "1-3级"}],
  "weather_note": "来自高德 forecasts，只对当场有效",
  "sources": {"wiki_pages": ["wiki/cities/成都.md"],
               "external_calls": 18, "llm_calls": 4, "seconds": 11.7},
  "days": [{"day": 1, "date": "2026-10-13", "theme": "熊猫与川菜",
             "timeline": [
      {"type": "transit", "name": "杭州东→成都东", "start_time": "08:00", "end_time": "14:30",
       "transport": "高铁，班次待核实", "cost": null,
       "verified": false, "source": "metaso"},
      {"type": "attraction", "name": "成都大熊猫繁育研究基地",
       "start_time": "09:00", "end_time": "12:00",
       "cost": 55, "open_time": "08:00-17:30", "rating": 4.8,
       "photo": "https://aos-comment.amap.com/...（POI photos[0].url）",
       "location": {"lng": 104.14, "lat": 30.72},
       "tip": "早去避高峰（来自 wiki/网页摘要）",
       "verified": true, "source": "amap_poi"}
  ]}],
  "origin_point": {"name": "杭州电子科技大学(下沙校区)", "location": {"lng": 120.3432, "lat": 30.3142}}
}
```

硬性要求：

- `verified` / `source` 两个字段是本仓库自己的规范扩展（区分"库里既有事实"与
  "本次现查"），FloatTrip 没有，前端要按第 5 节渲染。
  **M2 实测校准**：`ledger_lookup` 改为"精确命中优先、模糊命中只在唯一候选时才认"，
  且**照片只在精确命中时给**；`cost` 不再从 POI 台账回填（`business.cost` 是人均消费，
  景点上它等于游客综合消费——绍兴那次给沈园填过 163 元，真门票 40 元）。
  景点票价一律只认模型从【网页摘要】取到的值，取不到就 null，前端显示"待核实"。
- `photo` 与 `location` 直接取自高德 POI 台账，取不到就置 null，前端负责优雅降级。
- `origin_point` 是出发地那一个点（**2026-10-18 加**）：出发地一般只出现在 `"甲→乙"` 这种交通段
  名字里，而 transit 条目不回查台账，所以它没有自己的 `location`，地图上此前根本画不出起点。
  认法是按"哪一次 POI 检索带回的"（`q == needs.origin` 的第一条），不是按名字模糊匹配——
  题目写"杭电下沙校区"，台账里的正名是"杭州电子科技大学(下沙校区)"，字面互不包含。
  老方案（`.cache/plans/` 里 10-13 之前那批）没有这个字段，前端要按 null 跳过。
- 阶段 C 目前输出 Markdown 时间线，需要新增一个 JSON 输出模式：
  在 `SYSTEM_PLAN` 之外加 `SYSTEM_PLAN_JSON`（要求模型只输出上述结构的数组，
  每段一条），管线末尾把文本 parse 成 JSON；parse 失败时回退返回 Markdown 原文并带
  `"degraded": true`，前端两种都能显示。
  **M1 落地形态（2026-10-12）**：让模型直接输出 `{"days":[{"day":1,"theme":"…","items":[…]}]}`
  而不是平铺数组——分块由结构承担，比"平铺再猜哪天结束"少踩一次坑（第 9.3 条那个问题）。
  平铺数组仍兼容（`normalize_groups`）。题目要 N 天却全挤在 day=1 且条目≥8 时，自动补一轮
  LLM 按天整理（第 9.3 条的选择）；补轮丢条目则退回原分组，不静默少几条。
  `location`/`photo`/`rating`/`open_time` 一律由程序拿条目名回查高德台账填，模型不许自己写坐标；
  **type=transit 的条目不做台账回查**（它的 name 是"甲→乙"，模糊匹配会命中终点，
  等于给一张交通卡片贴上终点的照片与开放时间），地理信息只由 `path` 承担。
- POI 检索的 `show_fields` 带上 `business,photos`（v1 的渲染需要照片；这是对上次
  删掉的"强制 business"的一次带回，理由是渲染依赖，不是规范回潮）。
  **实测校准（2026-10-12）**：字段名是复数 `photos`，写 `photo` 高德会静默不给；
  带上后拿到 `photos[].url`、`business.rating`、`business.opentime_week`，
  但 `business.cost` 与 `biz_ext.price` 对景点仍是 null——票价只能靠秘塔/网页，`cost` 为 null 是常态。
- 路段 polyline：把 `get_amap_direction` 已请求的 `show_fields=cost,polyline,navi,tmc`
  结果里的坐标串按段附到 transit 条目的 `path: [[lng,lat],...]`，前端画实测折线；
  **不做浏览器端 AMap.Walking/Driving 现算**（省 JS 配额，且实测数据优先）。

### `GET /api/config`

响应：`{"amap_js_key": "...", "amap_js_security_code": "..."}`，
读 `.env` 的 `AMAP_JS_KEY` / `AMAP_JS_SECURITY_CODE`（用户自行申请 Web端(JS API) 类型 key；
现有 `AMAP_API_KEY` 是 Web服务类型，两者不通用）。key 缺失时返回空串，前端隐藏地图块。

### `GET /api/plans/{plan_id}` 与 `GET /api/plans`

每次成功的 final_plan 落盘 `.cache/plans/<plan_id>.json`；列表端点给历史页用。
纯文件读写，不加数据库。

## 4. 前端形态

无构建单页：`index.html` + `app.js` + `style.css`，原生 JS（或 CDN 单文件 Vue/React 均可，
但 v1 页面简单，建议原生 + 模板字符串，避免 FloatTrip 那种 Babel-standalone 运行转译）。
三个视图：

1. **输入页**：题目文本框 + 日期选择 + "离线预演"开关（打 `offline:true`，展示调用清单，
   不发外部请求——配额纪律的入口级体现）。管线要跑 10~30 秒，接口用 SSE 或轮询
   `/api/plan/<job_id>/status` 报进度（阶段 A/B2/C/D/E 五个点），别让人对着转圈猜。
2. **详情页**：按天分块的时间线卡片（type 图标：景点/餐/交通/住宿）、每卡片照片 +
   评分 + 开放时间 + 票价、`verified:false` 的条目加"待核实"角标、tooltip 标来源
   （wiki / amap / metaso / 本次现查）。页尾 `sources` 汇总：外部调用数、LLM 轮数、耗时。

   **M3 落地校准（2026-10-12，浏览器实测）**：
   - 层级照 FloatTrip 详情页：`时间列 38px → 圆形节点图标 23px → 卡片`；卡片内是
     "类型 + 已核实/待核实 tag + 建议停留 X 小时 → 名称 → 缩略图 148px + 摘要 → 警示行"，
     点卡片展开 8/5 大图与"开放时间 / 参考费用 / 评分 / 来源+坐标"明细（Enter/Space 同样能开合）。
   - **`type:"transit"` 不渲染成卡片，渲染成卡片之间的胶囊行**（对齐 FloatTrip 的"前往下一站"）：
     左写 transport，右写"实测 N 点折线"或"路线待实测"，未核实再加一枚"班次/票价待核实"。
     transport 文本里已含票价时不再重复拼 `cost`（实测出现过"5元 · 5 元"）。
   - 视觉 token 取自 FloatTrip：底 `#F5F8FC`、卡片白、ink `#151A20/#475569/#637287`、线 `#CEDAE6`、
     主色 `#2864A8`、警示 `#8C641F` on `#FFF5E3`、圆角 16/12/8、`font-variant-numeric: tabular-nums`。
   - **`[hidden] { display: none !important }` 必须写**：`.view` 是 `display:grid`，
     作者样式会盖掉 UA 的隐藏规则，不写这条三个视图全叠在一页上（实测踩过）。
   - 高德 `photos[].url` 给的是 **http** 链接，本机 http 直接超时、同 URL 换 https 就 200；
     已在源头 `plan_agent.first_photo()` 统一升级，前端不再处理。
   - `/api/plans` 的时间字段叫 `created`，取自 plan_id 里嵌的时间戳（文件 mtime 会被任何一次改写带偏）。
   - 窄屏（≤560px）把缩略图抬到文字上方整宽显示；地图块没有 location/path 时不显示。
3. **地图块**：`webapi.amap.com/loader.js` 官方加载方式（按高德文档自己写），
   打 marker（timeline 的 location）、按 `path` 画实测折线，InfoWindow 显示条目名与时间。
   JS key 从 `/api/config` 动态取，赋给 `window._AMapSecurityConfig`。

   **M4 落地要点（2026-10-12）**：
   - `_AMapSecurityConfig` 必须在注入 loader.js **之前**挂到 window 上，否则 2.0 报 `INVALID_USER_SCODE`。
   - **key 不对时 `AMapLoader.load()` 既不 resolve 也不 reject**（实测停在"地图加载中…"不动），
     所以 loader 与 SDK 各自包了 8 s / 12 s 超时，超时后地图块显示
     "地图没起来：高德 SDK：12 秒内没响应（不影响下面的时间线）"，时间线照常渲染。
   - 只 `try` 一次（`MAP.tried`）：key 被白名单拒时不要反复打外网。
   - 小屏（≤560px）默认收起地图块；**收起时容器 `display:none` 是零尺寸，
     `setFitView` 会算出错误的中心与缩放**，所以展开回调里必须 `map.resize()` 再 `setFitView` 一次
     （这条是被"隐藏视口下 center 落到 119.79/30.29、canvas 尺寸 0"暴露的）。
   - marker 用编号圆点（`.mk`，按 type 换描边色），只给有 `location` 的条目建；
     折线用 `path`（已抽稀 ≤300 点），`showDir` 标方向；点 marker 开 InfoWindow（名称/时间/类型/费用/核实状态），
     点时间线卡片会 `panTo` 对应 marker，点中转胶囊会把该段折线单独 `setFitView`。

响应式做到"手机上能看时间线"即可，地图块小屏可折叠。

## 5. 后端实现要点

- `webapp/server.py`：FastAPI，`uvicorn webapp.server:app --port 8300`。
  规划是 CPU 外 IO 型长任务，放 `run_in_executor`（内部已有 ThreadPoolExecutor，
  注意不要在事件循环线程直接 `asyncio.run` metaso 调用——沿用 plan_agent 的
  `run_one` 线程模型最稳）。
- 复用 CLI 的参数默认值：弱化版形态（不限外部调用、补查不限）。
  给 v1 设一个软上限 `MAX_EXTERNAL_WEB = 24`：网页用户手滑连点比 CLI 贵，
  这一条只作用于 web 入口，不改 CLI 默认。
- **并发不要放开**：`run_calls()` 的线程池是 `min(2, len(calls))`。高德个人 key 的并发上限是 3，
  实测 6 并发一波挂 5 条、3 并发一轮挂 2 条、2 并发 0 挂（`run_one` 另对 CUQPS 重发一次兜底）。
  web 入口多用户连点时排队，不要靠加并发换响应速度；`EXECUTOR` 也只给 1 个 worker。
- `MAX_EXTERNAL_WEB` 必须真的兜住**总量**，不只是截 POI：初版只在 `plan_wave1` 截断，
  路段（③已删，不限条数）与补查轮照发，一题绍兴跑出 30 次。现在 `gather_external` 把剩余额度
  当 `pick_legs` 的预算传，`run_plan` 再按剩余额度限补查条数、额度见底就跳过补查轮（省 1 次 LLM）。
- 并发防抖：同一 question+date 的 in-flight 任务去重，第二个请求直接挂到同一个 job。
- 失败语义：`final_plan` 里保留每条外部调用的 `ok/error_code`，前端把失败源标出来，
  不要静默吞。

## 6. 边界与非目标（agent 请勿越界）

- 不动 `wiki/`、`raw/`、`index.md`、`log.md`：web 走的是 Query 路径，默认不写库。
  用户明确要求"收录这条路线"仍只走 AGENTS.md 的 Ingest 流程，与本前端无关。
- 不改 `mcp/travel_mcp.py`、`mcp/wiki_mcp.py` 的工具语义；POI 加 `show_fields`
  是调用方传参，不是改服务端。
- 不做：登录/多用户、行程编辑、地图选点、后端渲染图片、把方案回写 wiki、
  浏览器端路线规划服务、SQLite 缓存（那是独立待办，落地后前端不用改）。
- 不复制 FloatTrip 源码文件；参考其信息层级与卡片样式，代码全部新写。

## 7. 分阶段验收

1. **M1 结构化输出**（纯后端）：`plan_agent.py` 出 `--json` 模式，同题跑一次
   成都三日线，`.cache/plans/` 落出合法 final_plan（photo/location/path 至少各命中 1 条）。
   验证：`python -c` 加载 JSON 查 schema 字段齐；不要求好看。
   ✅ **已完成（2026-10-12）**：`2026-10-12T20-47-52_成都.json`，67 KB，degraded=false，
   3 天 18 条目，photo/location/path = 8/9/3，20.0 s / 4 次 LLM / 21 次外部调用。
   管线编排已抽成 `run_plan()`（M2 的 webapp 直接 import 它，别再复制流程）。
   跑这一版时修掉了两个只有真跑才暴露的问题：补查轮把秘塔参数写成 `query`（工具签名是 `q`，
   一轮 4 条全作废）；高德个人 key 并发 >3 触发 `CUQPS_HAS_EXCEEDED_THE_LIMIT`
   （一波 6 并发有 5 条被挤掉，直接表现为 path/photo 命中数上不去）。
2. **M2 薄后端**：三个端点 + job 状态 + 离线预演。用 curl/浏览器验证，
   `offline:true` 必须 0 次外部调用。
   ✅ **已完成（2026-10-12）**：`webapp/server.py`（FastAPI + uvicorn，8300）。
   实测 `offline:true` 外部调用 **0 次**（只花 1 次 LLM 抽需求，返回体里 `llm_calls:1` 明写，
   高德/秘塔一次没发）；真实跑一题绍兴一日游：17.8 s / 4 次 LLM / 20 次外部调用
   （压在 `MAX_EXTERNAL_WEB=24` 内）/ 15 条目 / 7 条实测折线 / 0 次调用失败 / degraded=false。
   **调用流程**（前端照这个写）：
   - `POST /api/plan` 阻塞到跑完，响应体就是 final_plan（第 3 节契约不变）并多带一个 `job_id`；
     `offline:true` 时响应体是 `{offline, needs, planned_calls[], wiki_pages, external_calls:0}`。
   - 等响应期间用 `GET /api/job/running` 看进度（不用先知道 job_id，返回最近启动的在跑任务，
     `stages[]` 是 A/B1/B2/C/D/E 逐行文本）。`GET /api/plan/<job_id>/status` 是同结构的按 id 版本。
   - 同 question+date 的在跑任务会去重（job_id 是二者的哈希），跑过的任务重提会重新跑一遍——
     天气与"班次待核实"只对当天有效（第 8 节）。
   - 进程重启后内存任务清空，已完成方案仍从 `GET /api/plans` / `GET /api/plans/<plan_id>` 取回。
   - 失败语义：`sources.failures[]` 逐条带 `tool/args/error_code/error_msg`，前端要标出来。
3. **M3 前端时间线**：输入页 + 详情页跑通，Chrome 打开 localhost:8300 能出完整行程卡片。
   ✅ **已完成（2026-10-12）**：`webapp/static/{index.html,app.js,style.css}`，原生 JS 无构建。
   浏览器实测覆盖：输入页离线预演（进度行 + 计时 + 调用清单）、历史页 7 份列表、
   详情页成都 3 日（照片正常加载、中转胶囊、展开明细）、绍兴 1 日（14 条目含待核实角标）、
   降级载荷（标题退到 question、`— → —`、Markdown 原文照显示、无 location/path 时不显示地图块）、
   短题目客户端拦截（不发请求）、键盘 Enter 开合、窄屏 531px 堆叠布局；console 零报错。
   照片与降级两条的修法见第 4 节"落地校准"。
4. **M4 地图与照片**：配好 JS key 后 marker/折线/照片渲染，key 缺失时优雅降级不报错。
   🟡 **代码完成、视觉待看一眼**（2026-10-12）：真实 key 下 AMap 2.0 在 2.2 s 内加载完成，
   成都三日方案建出 **9 个 marker（全部落在成都经纬度区间）+ 3 条实测折线（279/279/113 点）**，
   `setFitView` 在展开回调里重算过一次；三条降级路径都验过——
   缺 key：不注入 loader.js、提示"未配置 AMAP_JS_KEY"、时间线照常；
   错 key：12 s 超时后提示"地图没起来…（不影响下面的时间线）"、无未捕获异常；
   无 location/path 的方案：整个地图块不显示。
   **唯一没拿到的是底图瓦片的截图**：内置浏览器面板处于隐藏状态（`visibilityState=hidden`、
   canvas 尺寸 0），AMap 在不渲染到屏幕时不画瓦片，所以截图这一步要人工把面板打开再看一眼。
5. **终检**：整链路跑一道新题，记录外部调用数与耗时；确认 `git status` 只有
   `webapp/`、`scripts/plan_agent.py`（JSON 模式）、`docs/` 三处改动。

## 8. 环境与操作提醒（会反复咬人）

- 一切脚本用项目 venv：`E:\code\LLM-guide-agent\.venv\Scripts\python.exe`。
  给用户的命令一律**反斜杠 + 绝对路径**，cmd 里正斜杠报"不是内部或外部命令"。
- PowerShell 跑相对路径要加 `.\`；cmd 从仓库根目录起。
- 本机控制台中文会乱码：需要人工看的输出一律写 UTF-8 文件再读。
- `.env` 已有 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL/AMAP_API_KEY/METASO_API_KEY`，
  v1 新增 `AMAP_JS_KEY/AMAP_JS_SECURITY_CODE` 两项，`webapp` 启动时加载同一个 `.env`。
- 高德/秘塔是个人免费额度：开发期用 `--offline` 和固定 `--needs` JSON 少打真请求，
  每阶段验收合并成一次真实跑。
- 日期敏感：天气与"班次待核实"结论只对 `date` 当天有效，final_plan 的
  `start_date` 必须由请求显式给出或按明天推算，不许用缓存里过期的。

## 9. 开放问题（动工前向用户确认）

> 2026-10-12 已拍板，三条都按下面的选择做，M2/M3/M4 不要再回来问一遍：
> **1) 仅本地 `localhost:8300` 自用，不做鉴权；2) 历史页要，平铺最近 20 条；
> 3) 多日题分块失败再花一轮 LLM 按天整理，不接受降级成单 `day`。**
> 原文保留如下，作为当时的取舍记录。

1. 部署形态：本地 `localhost:8300` 自用（默认假设），还是要挂公网？公网必须先加最小鉴权。
2. 历史页要不要：`/api/plans` 已预留，v1 可先只留最近 20 条平铺列表。
3. 多日题的结构化：现在模型输出是"每天一条时间线"拼一段，`days[]` 分块依赖 JSON 模式
   的 prompt 约束，若分块失败降级为单 `day`——接受还是再花一轮 LLM 整理？
   （已按"补一轮"实现，见第 3 节 M1 落地形态。）
