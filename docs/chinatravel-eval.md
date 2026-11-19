# ChinaTravel 基准评测（可选集成）

用途：用 [LAMDA-NeSy/ChinaTravel](https://github.com/LAMDA-NeSy/ChinaTravel)（ICLR'26）测本仓库旅行规划 agent 的能力。本文只讲**评测适配层**怎么跑、口径是什么、结果怎么读；产品管线（`scripts/plan_agent.py`、`webapp/`）不依赖它。

## 1. 为什么不能直接转换 `final_plan`

ChinaTravel 是**闭环、可机器验证**的基准：

- 实体必须在 sandbox 数据库里精确命中；
- 景点票价、营业时间、餐厅人均、酒店 `numbed` 必须与 sandbox 一致；
- 每个 activity 带 `transports[]`，metro 固定 `walk → metro → walk` 三段，单段只能是 walk/taxi；
- 城际必须是 day1 第一条、最后一天最后一条；
- 时间严格递增不重叠；餐次窗：早 06:00–09:00、午 11:00–14:00、晚 17:00–20:00；
- 最后用 DSL 硬约束 + commonsense + 偏好评分。

本仓库的 `final_plan` 是 4 类时间线（`attraction/meal/transit/hotel`），走实时高德/秘塔，实体与价格跟 sandbox 本来就不一致。直接把 `final_plan` 丢进官方评测，FPR 会接近 0。

所以适配层的做法是：**在 sandbox 内取数、按官方 schema 出方案、跑官方评测**；LLM 只决定“每天去哪几个点、顺序、吃哪家、住哪家”，时间/交通段/金额/城际由代码生成。

### 1.1 native 直测的坐标兜底（2026-11-15）

`--mode native` 不再把适配层算好的 `needs` 塞给 `plan_agent`，而是直接给原始题目并关掉澄清闸门（`clarify_first=False`），让 agent 自己抽取需求。`final_plan` 映射回 sandbox 的口径是：

1. 名字精确命中 → 唯一子串命中 → **坐标最近**；
2. 景点/酒店 3 km、餐厅 1.5 km 内才认；餐厅有菜系偏好时先在偏好池里找；
3. 午/晚餐按时间就近归档（<16:00 午、16:00–23:00 晚），早餐不接；
4. 每次替换的 `from → to → km` 记在 `meta.plan_meta.native.mapping`，agent 原始时间线记在 `agent_timeline`。

类别差异要人工看 `substitutions`：agent 的酒吧/Livehouse 在 sandbox 的景点表里常常只有博物馆，坐标兜底会保留位置但换掉语义；这类条目不能当成“agent 选了博物馆”。

这层仍会用 sandbox 重算时间/交通/金额，所以 native 测的是 agent 的**选点、顺序、分天**，不是它自己的排程和算账；也要和闭环结果分开列。native 现在打开补查轮（`max_extra=-1`），每题最多 4 次 LLM；补查条数受剩余外部额度限制。

## 2. 目录与环境

外部工作区（不入库）：

```text
E:\code\chinatravel-eval\
  ChinaTravel\                    # 官方仓库 clone
  .venv\                          # Python 3.12（官方依赖 + 本仓库 requirements）
  downloads\                      # 查询 CSV / sandbox zip
  runs\                           # 每次运行的 task/prompt/plan/validation/report
  .hf\                            # HF 缓存（可选）
```

仓库内只放代码：

```text
scripts/chinatravel_eval/
  sandbox.py      # CSV/JSONL + WorldEnv goto/intercity + 坐标解析
  queries.py      # 查询加载、needs、菜系抽取、本地 split 导出
  rule_plan.py    # rule_v0：确定性选点 + 排程 + 官方 schema
  llm_plan.py     # LLM 选点（只读 nature_language + 台账）
  native.py       # 现状 final_plan → sandbox 实体 → 同一排程器
  runner.py       # 批量、续跑、官方评测、报告
scripts/eval_chinatravel.py        # 薄 CLI
```

搭建：

```bash
git clone https://github.com/LAMDA-NeSy/ChinaTravel E:/code/chinatravel-eval/ChinaTravel
uv venv --python 3.12 E:/code/chinatravel-eval/.venv
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  --python E:/code/chinatravel-eval/.venv/Scripts/python.exe \
  -r E:/code/chinatravel-eval/ChinaTravel/requirements.txt \
  -r E:/code/LLM-guide-agent/requirements.txt
```

> `datasets` 依赖的 `pyarrow` 最新版要求 NumPy 2，而官方锁 `numpy==1.26.4`。实测装 `pyarrow==17.0.0` 可兼容：
> ```bash
> uv pip install --python E:/code/chinatravel-eval/.venv/Scripts/python.exe "pyarrow==17.0.0"
> ```

sandbox 下载（HF 直连不通时用镜像）：

```bash
# 中文
curl -L -o E:/code/chinatravel-eval/downloads/ChinaTravel_sandbox_zh.zip \
  "https://hf-mirror.com/datasets/LAMDA-NeSy/ChinaTravel-Sandbox/resolve/main/raw/ChinaTravel_sandbox_zh.zip"
# 英文
curl -L -o E:/code/chinatravel-eval/downloads/ChinaTravel_sandbox_en.zip \
  "https://hf-mirror.com/datasets/LAMDA-NeSy/ChinaTravel-Sandbox/resolve/main/raw/ChinaTravel_sandbox_en.zip"
```

解压到官方要求的位置：

```bash
cd E:/code/chinatravel-eval/ChinaTravel/chinatravel/environment
python -m zipfile -e E:/code/chinatravel-eval/downloads/ChinaTravel_sandbox_zh.zip .
python -m zipfile -e E:/code/chinatravel-eval/downloads/ChinaTravel_sandbox_en.zip .
```

查询集：用官方 `easy.csv`（或 ModelScope 镜像）即可，放到 `E:/code/chinatravel-eval/downloads/easy.csv`：

```bash
curl -L -o E:/code/chinatravel-eval/downloads/easy.csv \
  "https://hf-mirror.com/datasets/LAMDA-NeSy/ChinaTravel/resolve/main/easy.csv"
```

## 3. 运行

所有命令都用**外部 3.12 venv**：

```bash
cd E:/code/chinatravel-eval/ChinaTravel

# rule_v0：0 次 LLM、0 次高德/秘塔
E:/code/chinatravel-eval/.venv/Scripts/python.exe \
  E:/code/LLM-guide-agent/scripts/eval_chinatravel.py \
  --mode rule --split easy --days 1,2 --limit 10 --method rule_v0

# LLM 选点（复用 plan_agent.llm，只读 nature_language）
E:/code/chinatravel-eval/.venv/Scripts/python.exe \
  E:/code/LLM-guide-agent/scripts/eval_chinatravel.py \
  --mode llm --split easy --days 1,2 --limit 10 --method ours_llm

# 现状直测基线（真打高德/秘塔，注意额度）
E:/code/chinatravel-eval/.venv/Scripts/python.exe \
  E:/code/LLM-guide-agent/scripts/eval_chinatravel.py \
  --mode native --split easy --days 1,2 --limit 3 --method native_v0
```

产物：

```text
runs/<method>_<split>_<n>/
  queries.json
  uids/<uid>/task.json needs.json plan.json selection.json validation.json meta.json
  scores.json
  report.md
  eval.stdout.txt / eval.stderr.txt
```

`--resume` 跳过已 `done` 的题；`--no-eval` 只出方案不评分。

## 4. 口径与公平性

- 规划只读 `nature_language` + sandbox 台账 + 从 NL 抽到的偏好。
- `hard_logic_py` **只用于最终评分**，绝不喂给模型、也不用于修复；`build_needs()`/`llm_plan.py` 都不读它。
- 主榜是 `nl_only`；不要把 `hard_logic_py` 结构化喂给规划器的结果和主榜混排。
- 记录 `temperature=0`、prompt 版本、模型名、token、耗时、外部调用数。
- 修复只依据 schema/commonsense/结构错误；不依据隐藏 DSL。
- `wiki/`、`raw/` 仍只读；评测适配层不写知识库。

## 5. 实测结果（2026-11-15，easy split，10 题/组）

| 方法 | 题组 | FPR | C-LPR | Mac/Mic EPR | DAV | ATT | DDR | overall |
|---|---|---|---|---|---|---|---|---|
| rule_v0 | 1–2 天混合 10 | 100 | 100 | 100 / 100 | 57.5 | 84.6 | 63.3 | **95.27** |
| ours_llm | 1–2 天混合 10 | 100 | 100 | 100 / 100 | 72.5 | 88.4 | 63.3 | **96.21** |
| rule_v0 | 3 天 10 | 100 | 100 | 100 / 100 | 50.0 | 95.8 | 61.1 | **95.35** |
| ours_llm | 3 天 10 | 100 | 100 | 100 / 100 | 65.0 | 91.2 | 58.9 | **95.75** |
| native_v0 | 1–2 天混合 3 | 66.7 | 66.7 | 66.7 / 66.7 | 25.0 | 40.4 | 11.1 | **60.49** |

读法：

- 闭环两种方法在 10 题小样本上 **schema/commonsense/硬约束全过**；LLM 选点的 DAV（景点数）明显更高（72.5 vs 57.5），ATT/DDR 基本持平。
- native 基线在**只按名字映射的旧口径**下，3 题里 1 题 `native_plan_degraded`、另外 2 题 commonsense 过但 **DDR 只有 11.1**：实时高德的餐厅名大多不在 sandbox 里，映射不上就没有餐。更早一轮同样 3 题曾跑出 FPR 100 / overall 92.77，说明 native 基线受实时选点和映射覆盖率影响很大，**只作对照，不能当主评测**；坐标兜底后的 smoke 见 5.1。
- 小样本波动大；正式结论建议至少跑 50–100 题，并固定 uid 列表。

### 5.1 native 坐标兜底 smoke（2026-11-15，easy，5+10 题）

> 本节是 `max_extra=0` 的两段式 smoke（每题 2 次 LLM）；打开补查轮的抽样见 5.2。

| 题组 | 题量 | FPR | C-LPR | DAV | ATT | DDR | overall |
|---|---|---|---|---|---|---|---|
| 1–2 天 | 3 | 100 | 100 | 75.0 | 86.5 | 61.1 | **96.13** |
| 3 天 | 2 | 50.0 | 90.0 | 33.3 | 47.3 | 22.2 | **67.64** |
| 固定 10 题（4×1天 + 3×2天 + 3×3天） | 10 | 70.0 | 92.3 | 49.2 | 56.1 | 37.2 | **78.20** |

同一批 3 道 1–2 天题，旧 native（只按名字映射）是 FPR 66.7 / overall 60.49；改坐标兜底后 3 题全部生成并通过，其中之前 `native_plan_degraded` 的 2 天题也通了。

5 题共 84 个条目；落在 sandbox 的景点 28、餐厅 14、酒店 3 全部映射成功，其中 21/45 条靠坐标兜底，只丢了 1 条 08:30 早餐。3 天组唯一失败是 agent 把题目里的“鲁菜”抽成了 `themes:["美食"]`，方案里没有鲁菜餐厅——这是 agent 自己的抽取问题，不是映射问题；同一组“酒吧/酒馆”题 5/5 通过。

固定 10 题：1 天 4/4、2 天 2/3、3 天 1/3。3 个失败里，2 个是**类型词表对不上**（“自然景观”对应 sandbox 的 `自然风光`，agent 选了 `公园` 类的越秀公园；“文化特色”对应 `文化旅游区`，agent 选了人文景观/历史古迹），每题只错这一条；1 个是苏州“住一间大床房、酒店要有儿童乐园”没被 agent 抽到（`themes` 为空），选中的酒店也没有该特征。映射侧 83 个目标实体全部落进 sandbox（38 条靠坐标），只丢 3 个条目。若补一层类型同义词映射，前两题可望从 70% 到 90%（9/10）。

### 5.2 native 打开补查轮（2026-11-16，抽样）

`max_extra=-1`：跑完整“抽取 → 取数 → 初稿 → 补查 → 终稿”，每题最多 4 次 LLM；抽固定 uid，不全量。

| 题组 | 题量 | FPR | C-LPR | DAV | ATT | DDR | overall |
|---|---|---|---|---|---|---|---|
| medium 抽样（2/2/3/3/4 天） | 5 | 40.0 | 74.4 | 27.5 | 37.9 | 18.9 | **54.80** |
| human 抽样（1–5 天各 1） | 5 | 60.0 | 75.0 | 43.3 | 53.6 | 13.3 | **64.26** |
| preference0 | 2 | 50.0 | 90.0 | 33.3 | 39.6 | 27.8 | **67.54** |
| preference1 | 2 | 100 | 100 | 70.8 | 82.6 | 52.8 | **95.31** |
| preference2 | 2 | 50.0 | 50.0 | 33.3 | 46.5 | 22.2 | **47.60** |
| preference3 | 2 | 100 | 100 | 62.5 | 90.5 | 41.7 | **94.73** |
| preference4 | 2 | 100 | 100 | 66.7 | 86.6 | 52.8 | **95.30** |
| preference5 | 2 | 100 | 100 | 70.8 | 88.7 | 33.3 | **94.64** |

preference 六组共用同一批 2 道 base 题（2 天杭州→上海、3 天武汉→杭州），只换偏好句。官方偏好原始指标：p0 **2.58 景点/天**、p1 **33.3 分钟/段**、p2 **17.5 分钟**（只剩 1 份方案）、p3 **餐饮占比 49.3%**、p4 **住宿 1209.5 元**、p5 **离目标点 10.7 km**；p5 两题都把“上海迪士尼/岳王庙”抽进 `must_visit` 并排进方案。

失败主要集中在 agent 自己的输出：

- **3/22 题 `native_plan_degraded`**（medium 4 天、human 4 天、preference2 的 2 天题）；p2 的 2 天题同 base 的其他 variant 都成功，说明是终稿 JSON 输出波动，不是题目不可行。
- **p0 的 2 天题把三个餐位都写成“上海海洋水族馆”**（景点名、无坐标），映射不到餐厅，`restaurant_type` 硬约束失败；p0 的 3 天题同样把餐位写成景点名，只是被坐标兜底映射成了附近餐厅。
- **human 3 天题** agent 写了“苏州面馆/得月楼/山塘街小吃”这类通名，没有坐标，7 条全部映射失败，方案 0 餐；该题不查餐厅所以硬约束仍过，但 DDR 为 0。
- medium 的 budget 失败仍混着 sandbox 重算，room_count 也是适配层按床位数算的，不能全算 agent 的账。

## 6. 已知坑与处理

1. **HF `easy` split 的 `hard_logic_py` 是字符串**，官方 `_validate_query_record` 只认 list。不要改官方源码；适配层把查询导出成本地 split：
   - `chinatravel/data/<local_split>/<uid>.json`（`hard_logic_py` 为 list）
   - `chinatravel/evaluation/default_splits/<local_split>.txt`
   - `local_split` 不能是官方 HF split 名（如 `easy`），否则 `load_query` 仍走 HF。
2. **HF 直连不通**：用 `hf-mirror.com` 或 ModelScope；`HF_ENDPOINT` 对 `datasets` 同样有效。
3. **`pyarrow` 与 `numpy==1.26.4` 冲突**：装 `pyarrow==17.0.0`。
4. **`WorldEnv` 返回 `EnvOutput`**：`goto` 的列表要取 `o["data"]`；`intercity_transport_select` 的完整表要取 `o["whole_data"]`。
5. **列名以实际 CSV 为准**：attractions `id,name,type,lat,lon,opentime,endtime,price,recommendmintime,recommendmaxtime`；accommodations `id,name,hotelname_en,featurehoteltype,lat,lon,price,numbed`；restaurants `id,name,lat,lon,price,cuisine,opentime,endtime,recommendedfood`。
6. **城际班次**：`select` 返回按 `BeginTime` 升序；有跨夜车次（`EndTime < BeginTime`），排程要按“到达时间”理解，并给返程留足到站车程（实测远郊最后一站到深圳北要 85 分钟）。
7. **餐次窗**：酒吧/酒馆可能 18:00 才开门；选餐时按“窗口内任意 30 分钟营业”判断，不要只测 17:30。
8. **`your_tpc_scores.json` 是 append**：runner 每次先轮转再评测。
9. **官方测试**：52 项里 51 过；唯一失败是翻译工具的用户目录测试，与评测无关。

## 7. 下一步

- 扩大样本：50–100 题，固定 uid 列表，报告均值/方差与失败分类。
- 失败分类：`unsupported_city` / `llm_parse_failed` / `name_not_in_ledger` / `no_feasible_schedule` / `intercity_infeasible` / `schema_failed` / `commonsense_failed:<check>` / `hard_constraint_failed` / `native_mapping_incomplete`。
- 偏好优化：DAV 目前 50–64，距离满分（4 景点/天）还有空间；ATT 已 93–98。是否为了 DAV 增加景点，要先看时间/交通链是否还稳。
- native 基线：如需更公平，可加“未映射餐宿用 sandbox 规则补齐”的混合模式，但要单列，不能和纯 native 混排。
