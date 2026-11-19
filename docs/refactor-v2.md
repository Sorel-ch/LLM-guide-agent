# 旅行助手 v2：受控策展与可编辑行程

## 核心变化

默认 `run_plan()` 使用 `curated`：保留 A 澄清、B1 只读 wiki、B2 共享工具取数，
随后由一次小输出 LLM 做景点语义策展；代码完成分天、餐宿选择、相邻路段实测、时间排程、
营业冲突修复、预算与最终结构化。正常规划在带 `needs` 时为 1 次 LLM，不再让模型反复重写整份方案。

```bash
.venv/Scripts/python.exe scripts/plan_agent.py "从杭州去南京玩两天，预算1500，无特殊偏好" \
  --date 2026-11-20 --json --pipeline curated --max-external 32 --no-prompt
```

`--pipeline legacy` 保留旧管线供对照。`TRAVEL_PIPELINE` 可指定 web/CLI 默认值。

## 数据与质量

`scripts/planning/` 中：

- `models.py`：需求、候选、草稿、时间线、预算与质量报告；旧交付字段保持兼容。
- `experts.py` / `orchestrator.py`：候选策展和共享状态编排；LLM 只能引用真实候选 ID。
- `routing.py` / `schedule.py`：复用 farthest-point、开放路径、坐标终审与实际相邻路段。
- `quality.py` / `audit.py`：类别、重复、营业窗口、时间等语义检查，不以 `degraded=false` 当合格。
- `budget.py`：已知至少金额 + 待询价条目；unknown 不等于免费，餐饮为人均、酒店为每晚。
- `itinerary.py`：移动、删除、改时间/备注、真实候选替换；编辑 0 LLM、0 外部调用。
- `evidence.py`：保守网页价格归属；实体、费用类型或金额含义有歧义时保持待核实。

`audit_plans.py` 只读审计 `.cache/plans/`，报告写 `.cache/audits/`；缺失的旧字段记为 unknown，不能当成缺陷。

## Web 修订与导出

- `GET /api/plans/<plan_id>`：旧历史在内存中补预算、质量报告和 item ID，不回写旧文件。
- `PUT /api/plans/<plan_id>/revisions`：`base_revision + operations` 保存新版本；版本冲突 409，非法操作 422。
- `GET /api/plans/<plan_id>/revisions/<revision>`：回看修订；原始 plan 不覆盖。
- 新修订写 `.cache/revisions/<plan_id>/<revision>.json`，不使用数据库。
- 移动、删除、改时间/备注均为本地操作；顺序或地点改变后旧实测折线标 missing，不能伪装成新路线。
- 替换只接受本次方案 `edit_candidates` 中的真实候选。无快照的旧历史可以移动/删除/改时间，
  没有自由文本新增地点功能；新城市、天数、日期与核心偏好应重新规划。
- PDF 使用浏览器打印（多页）；PNG 仅文字行程，不含动态地图/跨域照片；JSON 下载当前 revision。
- 规划进度事件包含 `sequence/stage/state/message`，旧 `stages` 文本仍保留。

## 已验收与边界

- 离线契约、策展、营业冲突、预算、修订测试通过；离线 CLI 不调用外部接口或模型。
- 南京两日真实对照：每天 3 景点、1 次 LLM、34 次外部调用（含终稿补腿），第二次修复后无质量警告；
  10.8 秒包含同参数缓存命中，不代表冷启动耗时。
- 票价与酒店房价仍可能查不到，不自动猜测；旧网页摘要中的数字也不等于当前有效价格。
- 天气仍按高德返回展示，超出预报期不能推断；目前只把明确营业冲突交给代码修复，
  不是完整的法定节假日/每周闭馆日约束求解器。
- 只读 `wiki/`、`raw/`，工具并发 2、web 主取数软上限 32、终稿补腿独立额度、只监听本机的约束不变。
