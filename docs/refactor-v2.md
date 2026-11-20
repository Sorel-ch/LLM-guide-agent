# 旅行助手 v2：受控策展与可编辑行程

> 本文是 v2 当前行为与 Web/API 契约。`docs/frontend-plan.md` 保留 v1 建设过程与旧实测，
> 其中“只展示、不编辑”、旧 C/D/E 全文生成、模拟进度等描述不再是当前默认行为。

## 核心变化

默认 `run_plan()` 使用 `curated`：保留 A 澄清、B1 只读 wiki、B2 共享工具取数，
随后由一次小输出 LLM 做景点语义策展；代码完成分天、餐宿选择、相邻路段实测、时间排程、
营业冲突修复、预算与最终结构化。正常规划在带 `needs` 时为 1 次 LLM，不再让模型反复重写整份方案。

```bash
.venv/Scripts/python.exe scripts/plan_agent.py "从杭州去南京玩两天，预算1500，无特殊偏好" \
  --date 2026-11-20 --json --pipeline curated --max-external 32 --no-prompt
```

`--pipeline legacy` 保留旧管线供对照。`TRAVEL_PIPELINE` 可指定 web/CLI 默认值。
curated 目前只在 `--json` 下生效；不带 JSON 的 CLI 真跑须显式 `--pipeline legacy`。
`scripts/plan_agent.py` 仍是 CLI、共享取数/地理内核与 legacy 的兼容入口，尚未完全物理拆分。

## 数据与质量

`scripts/planning/` 中：

- `models.py`：需求、候选、草稿、时间线、预算与质量报告；旧交付字段保持兼容。
- `experts.py` / `orchestrator.py`：候选策展和共享状态编排；LLM 只能引用真实候选 ID。
- `routing.py` / `schedule.py`：复用 farthest-point、开放路径、坐标终审与实际相邻路段。
- `quality.py` / `audit.py`：类别、重复、营业窗口、时间等语义检查，不以 `degraded=false` 当合格。
- `budget.py`：已知至少金额 + 待询价条目；unknown 不等于免费，餐饮为人均、酒店为每晚；
  当前按 1 人合计，`party_size` 未接入 Web 与最终预算。
- `itinerary.py`：移动、删除、改时间/备注、真实候选替换；编辑 0 LLM、0 外部调用。
  时间/备注目前只在 API 提供，网页仅移动/删除/替换。
- `evidence.py`：保守网页价格归属；实体、费用类型或金额含义有歧义时保持待核实。

`audit_plans.py` 只读审计 `.cache/plans/`，报告写 `.cache/audits/`；缺失的旧字段记为 unknown，不能当成缺陷。

## Web 修订与导出

- `GET /api/plans/<plan_id>`：有 `latest.json` 时返回最新修订；否则返回原方案的内存适配版。
  旧历史在内存中补预算、质量报告和 item ID，不回写旧文件。
- `PUT /api/plans/<plan_id>/revisions`：`base_revision + operations` 保存新版本；版本冲突 409，非法操作 422。
- `GET /api/plans/<plan_id>/revisions/<revision>`：回看修订；`0` 为原方案的内存适配版，
  正整数修订为保存时的原文，字段丰富度可能不同；原始 plan 不覆盖。
- 新修订写 `.cache/revisions/<plan_id>/<revision>.json`，`latest.json` 记录当前版本，不使用数据库。
- 移动、删除、改时间/备注均为本地操作；移动/替换后旧实测折线标 missing。
  **删除条目尚不会使相关旧腿失效**，编辑也不自动重排时间或补测新腿。
- 替换候选由前端从本次方案 `edit_candidates` 提供；后端目前只校验结构，**没有快照白名单强校验**。
  无快照的旧历史无前端替换项，可以移动/删除/通过 API 改时间；没有自由文本新增地点功能。
  新城市、天数、日期与核心偏好应重新规划。
- PDF 使用浏览器打印（多页）；PNG 仅文字行程，不含动态地图/跨域照片；JSON 下载当前 revision。
- 规划进度事件包含 `sequence/stage/state/message`，旧 `stages` 文本仍保留。

## 当前 API 与交付字段

### 规划、任务和历史

- `POST /api/plan`：阻塞返回 final_plan（含 `job_id`）；`offline:true` 0 外部调用，
  不带 `needs` 时仍会花 1 次 LLM 澄清/抽取。240 秒 HTTP 超时不会终止后台线程。
- `GET /api/plan/<job_id>/status` 与 `GET /api/job/running`：保留 `stages` 文本，
  新增 `events[{sequence,stage,state,message}]`；仍是轮询，不是 SSE/WebSocket、增量事件或取消接口。
- `GET /api/plan/<job_id>`：读内存中的已完成任务结果；进程重启后任务丢失。
- `GET /api/plans`：按原始文件时间列最近 20 份摘要，不是修订历史列表。
- `PUT /api/plans/<plan_id>/revisions`：请求 `{"base_revision":0,"operations":[...]}`；
  支持 `move_item/remove_item/set_time/set_note/replace_item`，409 为版本冲突，422 为操作无效。

### final_plan 扩展

在原有 `days[].timeline`、天气和 `sources` 之外新增：

- `schema_version` / `revision` / `item_id`：版本与条目标识；新方案是否标 v2 需以实际产物为准，
  不把模型类默认值当成所有旧产物的版本。
- `budget_summary`：`categories`、`known_total`、`unknown_item_ids`、`target`、`over_budget`；
  每类有 `known_subtotal/unknown_item_ids`，未知不等于 0。
- `quality`：`schema_valid/feasible/issues[{code,severity,message,day,item_id}]`。
- `edit_candidates`：本次策展台账中可用于前端替换的真实候选，不是服务端白名单。
- `days[]`：保留 `sights/route_km/detour_ratio/route_note`；折返比是诊断指标，不是否决门。
- `sources`：可含 `agent_runs/repairs/curation_warnings/gap_asks/sight_trim` 与原有 failures/用量。
  curated 不带 needs 时当前 `llm_calls` 少算澄清一次，未修复前不作严格成本口径。

## 已验收与边界

- 离线契约、策展、营业冲突、预算、修订测试通过；离线 CLI 不调用外部接口或模型。
- 南京两日真实对照：每天 3 景点、1 次 LLM、34 次外部调用（含终稿补腿），第二次修复后无质量警告；
  该次 10.8 秒包含同参数缓存命中，末次为 16.3 秒；均为单样本观察，不是性能基线或普遍质量承诺。
- 票价与酒店房价仍可能查不到，不自动猜测；旧网页摘要中的数字也不等于当前有效价格。
- 天气仍按高德返回展示，超出预报期不能推断；目前只把明确营业冲突交给代码修复，
  不是完整的法定节假日/每周闭馆日约束求解器。
- 只读 `wiki/`、`raw/`，工具并发 2、web 主取数软上限 32、终稿补腿独立额度、只监听本机的约束不变。
