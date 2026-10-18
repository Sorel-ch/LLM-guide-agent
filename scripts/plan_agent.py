"""管线规划客户端（约束删减版，2026-10-12）。

完整版（全部硬约束）保存在分支 `archive/pipeline-full-2026-10-12`。
本版按编号删掉的约束：①固定三波取数顺序 ③外部调用配额 ④防编造三条
⑤预算两行制 ⑦补查白名单与参数夹取 ⑧跨城 transit 腿禁令 ⑨秘塔 NO_RESULT 原参数重发
⑩POI 强制 show_fields=business。保留：②不碰 read_index(seeds/all) 大清单
⑥紧凑格式与 ≤1000 字上限 ⑪thinking=disabled。
⑩以 `business,photos` 的形式因前端渲染依赖被带回一次（照片与开放时间是卡片必需项）。

阶段：
  A 需求抽取   LLM #1，关思考，小输出
  B1 知识库     本地读 wiki，0 次外部调用
  B2 取数       POI 与秘塔同波并发（秘塔摘要参与选枢纽）；天气与路段等 POI 坐标就绪后发出
  C 初稿        LLM #2，紧凑格式，≤1000 字
  D 补查        LLM #3 递缺口清单，清单原样执行（不分白名单）
  E 终稿        LLM #4，带着补查数据改写初稿
  F 结构化      --json 时先把终稿里"相邻却没测过"的腿补成 direction 请求（≤6 条，跨城腿不补），
                再把终稿转成 final_plan；题目要 N 天而模型全塞一天就补一轮 LLM #5

用法:
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" [--date 2026-10-08]
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" --offline   # 只打印调用清单
    .venv/Scripts/python.exe scripts/plan_agent.py "题目" --fact '杭州东→绍兴北 二等座 24 元'
        # --fact 可重复：agent 侧核实过的事实，点名的车站会被选成到达枢纽
    .venv/Scripts/python.exe scripts/plan_agent.py "题目" --json
        # 阶段 C 走 JSON 模式，方案落 .cache/plans/<plan_id>.json（webapp 用同一份 run_plan）
    可选：--thinking off|low|on  --max-external 0（0=不限）
          --max-extra -1（-1=补查不限，0=关补查轮，N=最多 N 次）
          --skip-llm  --needs '<JSON>'  --out .cache/plan_run.md
"""

import argparse
import asyncio
import concurrent.futures as cf
import inspect
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp"))


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

import travel_mcp  # noqa: E402
import wiki_mcp  # noqa: E402

EXTRACTION_SCHEMA = """{"origin":"","origin_city":"","destination_city":"","intercity":true,
"day_trip":true,"days":3,"budget_yuan":300,"themes":["美食"],"must_visit":["校区参观"],
"need_intercity_ticket":true,"date":"YYYY-MM-DD"}"""

# 下游是按键名直接取值的（plan_wave1 要 needs["destination_city"]），
# 模型整块漏给某个字段时靠这份默认值兜住。
NEEDS_DEFAULTS = {"origin": "", "origin_city": "", "destination_city": "", "intercity": False,
                  "day_trip": True, "days": 1, "budget_yuan": 0, "themes": [], "must_visit": [],
                  "need_intercity_ticket": False, "date": ""}

# 保留 ⑥：输出篇幅仍是硬上限；其余配额（路段条数 / 外部调用数）已删。
PLAN_CHAR_LIMIT = 1000      # 阶段 C 的字数上限
WIKI_MAX_PAGES = 8          # 注入上下文的 wiki 页数
WIKI_MAX_CHARS = 14000      # wiki 块总预算
POI_SHOW_FIELDS = "business,photos"   # 前端卡片要 rating/opentime/photo
LODGING_TYPECODE = "100000"           # 高德分类编码：住宿服务
LODGING_RADIUS_M = 4000               # 住宿周边搜半径：一天走到的范围
LODGING_KEEP = 5                      # 台账里最多留几条住宿行（按离游览区最近排）
SUGGEST_STOPS_PER_DAY = 4             # 建议链每天最多几站：放宽到 5 实测会把海曙与东部新城
                                      # 混进同一天（day1 折返比 1.0 → 1.58），这条不能松
# 台账里不渲染这几类：放宽到 44 行后多出来的 11 行全是它们（售票处/公司/商务住宅/地名地址/购物），
# 既不能当景点也不能当餐宿，白占模型视野。景点/餐/宿/交通四类留着。
LEDGER_SKIP_KINDS = ("公司企业", "商务住宅", "地名地址信息", "生活服务",
                     "购物服务", "公共设施")

# 补查轮里模型爱加引号的数字参数，按工具列出只修类型（不做范围夹取，那是已删的⑦）。
NUMERIC_ARGS = {"get_amap_poi_search": ("offset", "page", "radius"),
                "get_amap_direction": ("strategy", "cartype", "nightflag", "alternative_route"),
                "metaso_web_search": ("size",)}

SYSTEM_EXTRACT = (
    "你只做需求抽取，不要规划、不要解释。读用户题目，只输出一个 JSON 对象，字段与含义：\n"
    + EXTRACTION_SCHEMA
    + "\n规则：origin 是出发地原名，origin_city 是它所在的城市；destination_city 只写城市名；"
      "days 是题目要求的总天数（含首尾日），一日游填 1，题目没说清填 1；"
      "budget_yuan 没有就填 0；need_intercity_ticket 只要 intercity 为 true 就置 true"
      "（不要等题目明说票价）。"
      "不要输出 JSON 以外的任何字符。"
)

SYSTEM_PLAN = (
    # ④防编造三条与⑤预算两行制已删；只保留⑥（紧凑格式与字数上限）。
    "你是行程规划器。依据后面给出的【知识库】【实时数据】输出当天方案，要求：\n"
    "1. 只输出紧凑方案本身：每段一行，形如 'HH:MM-HH:MM 地点 | 交通方式与耗时 | 花费'；\n"
    "2. 严禁复述工具返回内容、严禁解释你的推理、严禁逐条标注来源；\n"
    "2b. 【POI】给的人均是消费水平，不是门票价：景点花费没在数据里明确出现过就写待核实；\n"
    "2c. 类=餐饮服务/住宿服务 的条目可以按【POI】人均写花费，并在同行末尾写全那句\"人均参考 100 元\""
    "（带数字；只写\"人均参考\"等于把取到的金额弄丢；这个数字是从数据里取到的，别再补一句\"未实测\"）；"
    "多日题在每个要过夜的晚上末尾要有住宿条目（最后一天返程就不排），name 照抄【POI】里的酒店名，"
    "并挑离当天最后一站最近的那家；同一个景点或餐厅不要重复出现（住宿除外）；\n"
    "2d. 有【建议顺序】就照它给的先后排：那是按坐标聚类算出来的，每天只走属于那一天那一段，"
    "不许把后面某天的点抓到当天再折回去；确有理由偏离就在该行末写一句为什么；\n"
    f"3. 总长不超过 {PLAN_CHAR_LIMIT} 字：每段一行可以短，但题目要求的内容类别（景点、餐饮、住宿、往返）"
    "都要各自出现。\n"
)

# 补查轮：模型递清单，我们不再做白名单与参数夹取（⑦已删）。
SYSTEM_GAP = (
    "你只做缺口清单，不要改方案、不要解释。给你【题目】【已有数据】【初稿】，"
    "找出初稿里明显单薄或缺数据的地方，按 JSON 输出：\n"
    '{"asks":[{"tool":"get_amap_poi_search|get_amap_direction|get_amap_weather|'
    'metaso_web_search","args":{...},"why":"缺什么"}]}\n'
    "args 的键名必须照抄工具签名，写错就整条作废：\n"
    "  get_amap_poi_search: keywords, city, offset, show_fields\n"
    "  get_amap_direction: origin, destination, mode(walking|transit|driving), city1, city2"
    "（city1/city2 只认 adcode 如 510100，写城市名会整条失败；不知道就别写，管线会补）\n"
    "  get_amap_weather: city(adcode), extensions\n"
    "  metaso_web_search: q, size, scope（问题放 q，不要写 query）\n"
    "优先补金额最大的那几笔缺口（城际车票、门票、当日住宿）。"
    "如果下面给了【代码已经排进队列的缺口】，那些我们已经在查了，不要重复列，"
    "你只补自己判断出来的其它缺口。"
    "没有值得补的就输出 {\"asks\":[]}。只输出 JSON。"
)


SYSTEM_PLAN_JSON = (
    # 前端渲染用的结构化输出（docs/frontend-plan.md 第 3 节）。格式约束与 SYSTEM_PLAN 同源（⑥），
    # 只是把"每段一行"换成"按天分组的 JSON 对象"——分块由模型直接给结构，
    # 比"平铺数组再让程序猜哪天结束"少一轮补救（第 9 节那个坑）。
    "你是行程规划器。依据后面给出的【知识库】【实时数据】，只输出一个 JSON 对象，结构：\n"
    '{"days":[{"day":1,"theme":"当天主题","items":[{"start_time":"HH:MM","end_time":"HH:MM",'
    '"type":"attraction|meal|transit|hotel","name":"","transport":"","cost":55,'
    '"tip":"","source":"wiki|amap|metaso|none"}]}]}\n'
    "规则：\n"
    "1. 题目是 N 天就给 N 个 day，day 从 1 连续编号；一日游就一个 day；每天给一个 theme；\n"
    "2. 景点/餐饮/住宿的 name 必须照抄【POI】或【知识库】里出现的名称，不要改写或加简称——"
    "坐标与照片由程序按这个名字回查；\n"
    "2b. 多日题在**每个要过夜的晚上**末尾放一条 type=hotel 的住宿（连续几天写同一家可以），"
    "最后一天要返程就不排住宿；"
    "住宿与餐饮的 name 要取【POI】里 类=住宿服务/餐饮服务 的那条（坐标与人均按这个名字回查），"
    "住宿挑其中离当天最后一站最近的，不要写知识库里顺带提到的连锁名；"
    "同一个 name 在整个方案里不要重复出现（住宿除外）；\n"
    "2d. 有【建议顺序】就照它给的先后排：那是按坐标聚类算出来的，"
    "每天只走属于那一天那一段，不许把后面某天的点抓到当天再折回去；"
    "确有理由偏离（开放时间、预约、用餐时段）就在该条 tip 里写一句为什么；\n"
    "3. 交通段 name 写 '甲→乙'，transport 写方式与耗时；没有任何实测数据时 transport 写\"交通待核实\"；\n"
    "4. cost 只填有依据的数字（单位元）：【POI】【路段】或【网页摘要】里给过的才填，免费填 0，"
    "查不到就填 null，不许估算；【POI】里的人均是消费水平不是门票价，"
    "景点没给过明确票价就别把 cost 填成人均；类=餐饮服务/住宿服务 的可以拿人均填 cost，"
    "并在 tip 里把那句写全、带数字（形如\"人均参考 100 元，本地排队王\"），"
    "只写\"人均参考\"四个字等于把已经取到的金额弄丢；"
    "人均取到就说明数字有依据，这条 tip 里不要再出现\"未实测\"，"
    "只有 cost 填 null 时才写\"未实测\"；\n"
    "5. source：这条的依据出自【知识库】写 wiki，出自【POI】【路段】【天气】写 amap，"
    "出自【网页摘要】写 metaso，凭空来的写 none；\n"
    f"6. 严禁解释、严禁 markdown 代码块以外的字符，总长不超过 {PLAN_CHAR_LIMIT} 字，"
    "但题目要求的类别（景点、餐饮、住宿、往返）都要出现。\n"
)


def unwrap(res):
    """工具层返回 CallToolResult（单份紧凑正文），这里取回 dict。"""
    text = res.content[0].text if hasattr(res, "content") else res
    return json.loads(text) if isinstance(text, str) else text


def llm(messages, *, max_tokens: int, temperature: float = 0.0, thinking: str = "off"):
    base = (os.getenv("LLM_BASE_URL") or "").rstrip("/")
    key = os.getenv("LLM_API_KEY")
    model = os.getenv("LLM_MODEL")
    if not (base and key and model):
        raise SystemExit(
            "缺少 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。请在仓库根 .env 补这三行"
            "（值同你在 Cline 里填的 OpenAI Compatible 配置）。"
        )
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # DeepSeek 系思考模型：thinking.type=disabled 才会真的不产 reasoning_content；
    # 部分网关认 reasoning_effort，两个都带上，不认的会忽略。
    if thinking == "off":
        body["thinking"] = {"type": "disabled"}
        body["reasoning_effort"] = "minimal"
    elif thinking == "low":
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "low"

    t0 = time.perf_counter()
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=float(os.getenv("LLM_HTTP_TIMEOUT", "90")),
    )
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    reasoning = choice.get("reasoning_content") or ""
    return {
        "text": choice.get("content") or "",
        "reasoning_chars": len(reasoning),
        "in_tokens": usage.get("prompt_tokens", 0),
        "out_tokens": usage.get("completion_tokens", 0),
        "seconds": time.perf_counter() - t0,
    }


def extract_needs(question: str, date: str | None, thinking: str) -> dict:
    res = llm(
        [
            {"role": "system", "content": SYSTEM_EXTRACT},
            {"role": "user", "content": f"日期提示：{date or '未给出，按明天'}\n题目：{question}"},
        ],
        max_tokens=320,
        thinking=thinking,
    )
    m = re.search(r"\{.*\}", res["text"], re.S)
    if not m:
        raise SystemExit(f"需求抽取没拿到 JSON：{res['text'][:200]}")
    needs = json.loads(m.group(0))
    needs["_llm"] = res
    return needs


# --------------------------------------------------------------------------
# 阶段 A2：多轮澄清 —— 信息不足时不开始规划，先追问
# --------------------------------------------------------------------------

# 顺序就是追问优先级：没有目的地整趟都无从谈起，起点与预算往后排。
CLARIFY_SLOTS = ("目的地", "起点", "时间", "预算", "偏好")

# 模型没问到某一项时的兜底问句。都给了"没说清就回 X"的出口，
# 因为规则是"缺项才追问，明确说不限算通过"。
CLARIFY_ASK = {
    "起点": "你从哪儿出发？只想在本地玩就回\"本地\"。",
    "目的地": "想去哪个城市或景区？",
    "时间": "哪天出发、玩几天？让我定就回\"你看着安排\"。",
    "预算": "总预算大概多少元？没有上限就回\"不限\"。",
    "偏好": "有什么必去的地方或偏好？（美食、博物馆、亲子、避开人流…）没有就回\"都行\"。",
}

SYSTEM_CLARIFY = (
    "你在帮人规划行程。信息不全时**不许开始规划**，先追问。读【对话】，只输出一个 JSON 对象：\n"
    '{"needs":{...见下...},"filled":{"起点":"stated|waived|unknown","目的地":"...","时间":"...",'
    '"预算":"...","偏好":"..."},'
    '"questions":[{"slot":"预算","text":"一句中文追问","options":["300","800","不限"]}]}\n'
    "needs 的字段与含义：\n" + EXTRACTION_SCHEMA + "\n"
    "规则：\n"
    "1. filled 五项都要给状态：题目或回答里明确说过 = stated；"
    "用户明确表态\"不限 / 都行 / 你看着安排 / 就在本地\" = waived；压根没说 = unknown；\n"
    "1b. 时间这一项：说了哪天出发、或说了玩几天，都算 stated（没给具体日期时系统按明天推算，"
    "不要为了追问日期而反复问）；只有既没说日期也没说时长才算 unknown；\n"
    "2. 目的地只能是 stated 或 unknown，**不许 waived**（不知道去哪就没法规划）；\n"
    "3. 只为 unknown 的槽提问，顺序 目的地→起点→时间→预算→偏好，一次最多 3 条；"
    "已经答过的槽不要再问；\n"
    "4. 五项全是 stated/waived 时 questions 给空数组 []；\n"
    "5. needs 里没说的字段照默认填（预算不限填 0、日期没给按日期提示），"
    "但缺口以 filled 为准，别用默认值冒充 stated；【日期提示】给了具体日期时，"
    "时间就算 stated（那是用户在表单/参数里选过的）；\n"
    "6. options 给 2~4 个可直接点短答案，不确定就别给；"
    "目的地的选项里不要放\"就在本地/你推荐\"这种没有具体地名的答案；\n"
    "7. 不要输出 JSON 以外的任何字符。"
)


def clarify(turns: list[str], date: str | None, thinking: str) -> dict:
    """把整段对话喂给模型，拿回合并后的 needs 与五项状态。

    这**取代**了 extract_needs（同一次 LLM 调用，只是多输出一个状态块），
    所以题目本来就写全时不会多花时间。missing 由代码算，不信模型自己说"齐了"：
    模型漏问的槽用 CLARIFY_ASK 兜底，多问的槽（已明确的）直接丢掉。
    """
    dialogue = "\n".join(f"用户：{t}" for t in turns if str(t).strip())
    res = llm(
        [{"role": "system", "content": SYSTEM_CLARIFY},
         {"role": "user", "content": f"日期提示：{date or '未给出，按明天'}\n【对话】\n{dialogue}"}],
        max_tokens=560,
        thinking=thinking,
    )
    got = extract_json(res["text"]) or {}
    needs = got.get("needs") if isinstance(got.get("needs"), dict) else {}
    filled = got.get("filled") if isinstance(got.get("filled"), dict) else {}
    missing = [s for s in CLARIFY_SLOTS if str(filled.get(s) or "unknown").lower() not in ("stated", "waived")]
    # 目的地不接受"不限/你推荐"：没有地名，路线与 POI 都无从查起，只能继续问。
    if "目的地" not in missing and str(filled.get("目的地") or "").lower() == "waived":
        missing.insert(0, "目的地")

    questions = [q for q in (got.get("questions") or [])
                 if isinstance(q, dict) and q.get("slot") in missing]
    asked = {q["slot"] for q in questions}
    questions += [{"slot": s, "text": CLARIFY_ASK[s], "options": []}
                  for s in missing if s not in asked]
    order = {s: i for i, s in enumerate(CLARIFY_SLOTS)}
    questions.sort(key=lambda q: order.get(q["slot"], len(order)))   # 别按模型给的顺序问
    res["missing"] = missing
    res["questions"] = questions[:3]
    res["ready"] = not missing
    res["needs"] = needs
    res["filled"] = {s: str(filled.get(s) or "unknown") for s in CLARIFY_SLOTS}
    return res


# --------------------------------------------------------------------------
# 阶段 B：确定性数据装配
# --------------------------------------------------------------------------

def wiki_bundle(needs: dict, total_chars: int = WIKI_MAX_CHARS) -> tuple[str, list[str]]:
    """本地读，零外部调用。返回注入 prompt 的知识块 + 命中页路径。

    search_pages 是按索引整行匹配的，`绍兴` 会把"北与绍兴市相邻"的台州页也捞进来，
    而标题不等于关键词的页基本只是顺带提到。所以先按"标题即关键词"排序，
    再把字符预算优先给主目录页——否则 6000 字符的整块截断会把城市页砍成半截。
    """
    city = needs.get("destination_city") or ""
    kws = [k for k in filter(None, [city, needs.get("origin"), *(needs.get("must_visit") or [])])]
    ranked: list[tuple[int, int, str]] = []  # (优先级, 首次出现序号, path)
    order = 0
    for kw in kws:
        hits = unwrap(wiki_mcp.search_pages(kw, limit=10))["data"]["results"]
        for h in hits[:8]:
            if not h["exists"]:
                continue
            stem = h["path"].rsplit("/", 1)[-1].removesuffix(".md")
            # 标题正好等于关键词的页（`cities/宁波.md`）要压过"前缀撞上"的页
            # （`pois/宁波博物馆.md`）：宁波那题实测每词只取前 4 条命中时，
            # 城市页被同名前缀的 POI 页挤了出去，see 表里的景点一个都没进检索。
            prio = 0 if stem == kw else (1 if stem.startswith(kw) else 2)
            if any(p[2] == h["path"] for p in ranked):
                continue
            ranked.append((prio, order, h["path"]))
            order += 1
    pages = [p for _, _, p in sorted(ranked, key=lambda x: (x[0], x[1]))][:WIKI_MAX_PAGES]
    if not pages:
        return "（知识库无相关页面）", []
    # 首屏页（一般是目的地城市页）要给够：它的 see 表里带着票价列，截断了等于白命中。
    # 原来的 `total - 2200*(n-1)` 在 8 页时算出负数，被 max(600, …) 兜成 600 字，
    # 宁波那题的城市页就是这么被截没的。
    first_budget = 4000
    per_extra = min(2200, max(600, (total_chars - first_budget) // max(1, len(pages) - 1)))
    texts: list[str] = []
    used = 0
    batch = unwrap(wiki_mcp.read_pages(pages))["data"]["pages"]
    for i, p in enumerate(batch):
        meta = p.get("meta") or {}
        warns = "；".join(meta.get("warnings") or [])
        budget = (total_chars - used - per_extra * (len(batch) - i - 1)
                  if i == 0 else per_extra)
        budget = max(first_budget if i == 0 else 600, budget)
        body = (p.get("content") or "")[:budget]
        used += len(body)
        texts.append(
            f"### {meta.get('path')}（{meta.get('type')}，valid_until={meta.get('valid_until')}）\n"
            f"{body}\n" + (f"[警告] {warns}\n" if warns else "")
        )
    return "\n\n".join(texts) or "（知识库无相关页面）", pages


def wiki_sights(wiki_text: str, needs: dict, limit: int = 3) -> list[str]:
    """从 wiki 知识块里取要去的地点名，交给高德换成坐标。

    这是"先 wiki 再高德"真正该长的样子：`{city}景点` 这种泛检索在高德那边返回的是一堆
    公园绿地（成都实测：9 个结果里 7 个是公园），模型照它排的行程全是免费公园，
    武侯祠、杜甫草堂这些 wiki 里明明有的景点反倒没有坐标、写成一串"交通待核实"。

    **只认目的地城市的页**：知识块里还会混进出发地城市与邻市页（`宁波` 那题实测捞进了
    杭州页与嘉兴页），照单全收就会拿"乌镇/西栅/南湖"去宁波检索，三发 POI 白打，
    而块里明明有 `wiki/pois/宁波博物馆.md`、`南塘老街.md` 两页却没被用，
    结果第二天的条目全没坐标、地图上只剩第一天。
    """
    city = needs.get("destination_city") or ""
    # 知识块是按页拼的（`### <路径>（<类型>，valid_until=…）`），先切回一页一段再判归属。
    sections = re.split(r"(?m)^### ", wiki_text)
    poi_names: list[str] = []
    see_names: list[str] = []

    def add(bucket: list[str], n: str) -> None:
        n = re.split(r"[，,、；;（(]", n.strip())[0].strip()
        if len(n) >= 2 and n not in ("缺", "-") and n not in bucket:
            bucket.append(n)

    for sec in sections[1:]:
        header, _, body = sec.partition("\n")
        path = header.split("（")[0].strip()
        if path.endswith(f"cities/{city}.md"):
            for m in re.finditer(r"^\|\s*(?:see|do|eat)\s*\|\s*([^|]{2,26}?)\s*\|", body, re.M):
                # 单元格里常见"秋瑾故居，含秋瑾烈士纪念碑"这种带说明的名字（绍兴页实测），
                # 整串拿去搜高德会搜不到；逗号/顿号/括号之前的部分才是地点本体。
                add(see_names, m.group(1))
        elif "/pois/" in path and city and city in body:
            # POI 页的标题本身就是个地点名，而且它单独成页说明值得去，排在 see 表前面。
            add(poi_names, path.rsplit("/", 1)[-1].removesuffix(".md"))
    names = poi_names + see_names
    must = needs.get("must_visit") or []
    themes = needs.get("themes") or []

    def prio(n: str) -> int:
        if any(m and (m in n or n in m) for m in must):
            return 0
        if any(t and (t in n or n in t) for t in themes):
            return 1
        return 2

    ordered = sorted(((prio(n), i, n) for i, n in enumerate(names)), key=lambda x: (x[0], x[1]))
    return [n for _, _, n in ordered[:limit]]


def metaso_webpages(payload: dict) -> list[dict]:
    """秘塔这条链上返回体套了三层：payload.data.content[0].text 是一段 JSON 字符串，
    解开才是 {credits, total, webpages[]}。直接 json.dumps 整包会让模型看到一堆转义噪音。"""
    d = payload.get("data") or {}
    for key in ("webpages", "result"):
        if isinstance(d.get(key), list):
            return d[key]
    text = None
    for part in d.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            break
    if not isinstance(text, str):
        return []
    try:
        inner = json.loads(text)
    except json.JSONDecodeError:
        return []
    return inner.get("webpages") or inner.get("result") or []


def poi_call(keywords: str, city: str | None = None, offset: int = 5) -> tuple[str, dict]:
    # ⑩已删后又带回一次：网页卡片要照片与开放时间，所以这里固定请求 business,photos。
    # 理由是渲染依赖，不是规范回潮——票价照样指望不上（v5 的 biz_ext.cost 实测为空）。
    args: dict = {"keywords": keywords, "offset": offset, "show_fields": POI_SHOW_FIELDS}
    if city:
        args["city"] = city
    return "get_amap_poi_search", args


def first_photo(poi: dict) -> str | None:
    """v5 show_fields=...,photos 才有 poi["photos"]；老返回体没这个键就是 None，前端自行降级。
    高德给的是 http 链接，实测在本机 http 直接超时、同 URL 换 https 就 200——统一升级成 https。"""
    for ph in poi.get("photos") or []:
        url = (ph or {}).get("url")
        if url:
            return url.replace("http://", "https://", 1) if url.startswith("http://") else url
    return None


def collect_pois(results: list, pois: dict[str, dict], city: str) -> str | None:
    """把 POI 检索返回摊进台账（同名保留第一次出现的坐标）。返回本市 adcode。"""
    adcode = None
    for tool_name, args, payload in results:
        if tool_name != "get_amap_poi_search":
            continue
        for p in (payload.get("data") or {}).get("pois") or []:
            if not p.get("location"):
                continue
            lon, lat = p["location"].split(",")
            if args.get("city") == city and p.get("adcode"):
                adcode = adcode or p["adcode"]
            biz = p.get("business") or {}
            pois.setdefault(p["name"], {"coord": f"{lon},{lat}", "lon": lon, "lat": lat,
                                        "adcode": p.get("adcode"),
                                        "type": p.get("type"), "typecode": p.get("typecode"),
                                        # 记住这条是哪一次检索带回来的：渲染台账时按检索词配额，
                                        # 否则先回来的那一批（车站的十几条地铁站）会把后面的酒店餐厅整批挤掉。
                                        "q": args.get("keywords"),
                                        "price": (p.get("biz_ext") or {}).get("price"),
                                        "cost": (p.get("biz_ext") or {}).get("cost") or biz.get("cost"),
                                        "rating": biz.get("rating"),
                                        "photo": first_photo(p),
                                        "opentime": p.get("opentime") or biz.get("opentime_week")})
    return adcode


def clean_snippet(text: str) -> str:
    """秘塔的 snippet 里塞满 markdown 图片、链接和换行，直接进上下文等于给模型一屏噪音。
    实测（成都那次）票价那条摘要前半段全是 `![](https://inews.gtimg.com/...)`。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", str(text or ""))
    t = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def price_signal(text: str) -> int:
    """给摘要排序用：一条里"数字+元/¥/小时"越密，越可能是真给了价。"""
    t = clean_snippet(text)
    return len(re.findall(r"\d+(?:\.\d+)?\s*(?:元|¥)", t)) * 2 + len(re.findall(r"\d+\s*(?:分钟|小时|分)", t))


def metaso_summaries(payload: dict, limit: int = 3) -> list[str]:
    """挑最有料的几条摘要：先按含价密度排，再按原序补齐。"""
    pages = metaso_webpages(payload)
    rows = []
    for w in pages:
        body = w.get("snippet") or w.get("summary") or w.get("description") or ""
        text = clean_snippet(body)
        if not text:
            continue
        title = clean_snippet(w.get("title") or w.get("name") or "（无标题）")
        date = w.get("date") or "日期未给"
        rows.append((price_signal(text), f"  · {title}（{date}）：{text[:300]}"))
    rows.sort(key=lambda x: -x[0])
    return [t for _, t in rows[:limit]]


def plan_wave1(needs: dict, wiki_text: str, max_external: int) -> list[tuple[str, dict]]:
    """第一波取数清单（POI + 秘塔）。天气与路段要等 POI 坐标回来才能拼，所以不在这里。
    单独拆出来是为了 web 的"离线预演"：把清单原样给用户看，一条外部请求都不发。

    排列只是为了 --max-external 截断时先砍谁：必去点 → 车站 → 高铁站 → 出发地
    → wiki 点名的景点 → 住宿 → 主题词 → 餐厅 → 泛景点。
    """
    city = needs["destination_city"]
    origin = needs.get("origin") or ""
    origin_city = needs.get("origin_city") or ""

    need_ticket = bool(needs.get("intercity") or needs.get("need_intercity_ticket")) \
        and "高铁" not in wiki_text

    calls: list[tuple[str, dict]] = []
    # 必去点与主题各放宽到 3 个：只检索前两个时，用户写"想看 A、B、C"里的 C
    # 从来没进过台账，卡片上就没有坐标、照片和开放时间。
    for kw in (needs.get("must_visit") or [])[:3]:
        calls.append(poi_call(kw if city in kw else f"{city}{kw}", city, 3))
    calls.append(poi_call(f"{city}站", city, 4))
    # 只搜 `{city}站` 拿到的是普速站（成都那次连"成都站(建设中)"都进来了），
    # 高铁到达站基本都叫 `{city}东站/南站/北站`，单独补一次检索。
    calls.append(poi_call(f"{city}高铁站", city, 4))
    if origin and origin != origin_city:
        # 出发地按其所在城市检索；origin 只是一个城市名时不查（"杭州"会捞回杭州大厦、
        # 杭州站(出口)这类无关条目，白吃一个 POI 名额）
        calls.append(poi_call(origin, origin_city or None, 3))
    # 多日题要给更多候选点：只取 3 个时第二天往往一个坐标都没有（宁波两日实测）。
    sight_limit = min(7, 3 + 2 * max(0, int(_num(needs.get("days")) or 1) - 1))
    for name in wiki_sights(wiki_text, needs, limit=sight_limit):
        if not any(name in c[1]["keywords"] or c[1]["keywords"] in name for c in calls):
            calls.append(poi_call(name, city, 3))
    # 住宿要排在主题词之前：台账渲染有 30 行上限，`{city}美食` 一次就吃掉 8 行，
    # 排在它后面的酒店检索就整批进不了模型视野（成都实测：上下文里 0 条住宿服务）。
    if (_num(needs.get("days")) or 1) >= 2:
        calls.append(poi_call(f"{city}酒店", city, 5))
    for th in (needs.get("themes") or [])[:3]:
        calls.append(poi_call(f"{city}{th}", city, 5))
    # 题目没点"美食"也得有餐厅可写：prompt 要求餐饮这一类必须出现，没有台账条目撑着，
    # 模型就凭空编一个名字，回查不到坐标与人均（成都那次两天都写"成都美食汇"）。
    if not any(re.search(r"[食餐吃菜味]", str(th)) for th in (needs.get("themes") or [])):
        calls.append(poi_call(f"{city}餐厅", city, 5))
    calls.append(poi_call(f"{city}景点", city, 10))
    metaso_calls = ([("metaso_web_search",
                      {"q": f"{origin_city or city}到{city} 高铁时刻表 票价", "size": 5})]
                    if need_ticket else [])
    if max_external and max_external > 0:
        calls = calls[:max(4, max_external - len(metaso_calls))]
    return calls + metaso_calls


def gather_external(needs: dict, wiki_text: str, max_external: int, quiet: bool,
                    facts: list[str] | None = None) -> dict:
    """①③已删：不再分三波、不再按类型配额。POI 与秘塔同一波并发，秘塔摘要也参与选枢纽
    （原实现特意把秘塔压到末波、只让 --fact 参与决策，是为了防站名把枢纽带偏——这条保护已移除）。
    天气与路段仍排在 POI 之后，那是坐标依赖，不是顺序约束。
    max_external<=0 表示不限；>0 时只按截断顺序砍 POI。

    取数面按 themes 与 must_visit 全量铺开：只搜 `{city}景点` 时，题目里的"美食"根本没进检索，
    模型最后那句"绍兴菜午餐"就是凭空写的（成都那次实测：9 次调用里 0 次查过餐厅）。
    """
    city = needs["destination_city"]
    calls = plan_wave1(needs, wiki_text, max_external)

    wave1 = run_calls(calls, quiet)

    pois: dict[str, dict] = {}
    adcode = collect_pois(wave1, pois, city)

    # hints：--fact 与秘塔摘要都参与选枢纽（①删掉了"秘塔不参与决策"的保护）
    hints = " ".join(facts or [])
    for t_name, _args, payload in wave1:
        if t_name == "metaso_web_search" and payload.get("ok"):
            hints += "\n" + "\n".join(metaso_summaries(payload))

    wave2: list[tuple[str, dict]] = []
    if adcode:
        wave2.append(("get_amap_weather", {"city": adcode, "extensions": "all"}))
    # 住宿按半径周边搜：`{city}酒店` 是全城检索，宁波连着两次捞回 30 km 外的宾馆，
    # 于是"当天末站→住宿"这条腿的直线距离就成了 30 km，地图视野也被拽走。
    # 周边搜给的是真走得到的那几家；关键词那次照旧留着，周边搜空了就还有条目可抄。
    if (_num(needs.get("days")) or 1) >= 2 and pois:
        center = lodging_center(pois, tour_stops(needs, pois, adcode))
        if center:
            wave2.append(("get_amap_poi_search",
                          {"types": LODGING_TYPECODE, "location": center,
                           "radius": LODGING_RADIUS_M, "offset": 4,
                           "show_fields": POI_SHOW_FIELDS}))
    # ③删了管线内部的配额，但 web 入口传的 max_external 得真的兜住总量：
    # 绍兴一日游实测，只截 POI 的话路段会铺到 15 条，23+7=30 次把软上限穿过去。
    leg_budget = max(0, max_external - len(wave1) - len(wave2)) if max_external and max_external > 0 else 0
    legs = pick_legs(needs, pois, leg_budget, adcode, hints)
    for a_name, b_name, mode in legs:
        a, b = pois[a_name], pois[b_name]
        # city1/city2 只认 adcode（传城市名 INVALID_PARAMS，实测），端点没有就退回本市 adcode。
        wave2.append(("get_amap_direction",
                      {"origin": a["coord"], "destination": b["coord"], "mode": mode,
                       "city1": a.get("adcode") or adcode or city,
                       "city2": b.get("adcode") or adcode or city}))

    wave2_results = run_calls(wave2, quiet)
    # 周边搜回来的住宿要并进台账，否则模型没有条目可抄，只能编名字。
    collect_pois(wave2_results, pois, city)

    return {
        "pois": pois,
        "adcode": adcode,
        "results": wave1 + wave2_results,
        "legs": legs,
        "external_count": len(wave1) + len(wave2_results),
    }


def run_calls(calls: list[tuple[str, dict]], quiet: bool) -> list:
    out = []
    if not calls:
        return out
    # 并发 2：个人 key 的高德并发上限是 3。实测 6 并发一轮挂 5 条，3 并发一轮挂 2 条
    # （限流是突发起不来的那种，重发也常撞上同一波），所以宁可多花约 1 秒也别烧配额。
    with cf.ThreadPoolExecutor(max_workers=min(2, len(calls))) as pool:
        futures = [pool.submit(run_one, name, args, quiet) for name, args in calls]
        for f in futures:
            out.append(f.result())
    return out


# --------------------------------------------------------------------------
# 补查轮：⑦已删——不再有工具白名单与参数夹取，清单原样执行。
# POI/天气 → 路段 → 秘塔 的分批只是坐标解析依赖，不是校验。
# --------------------------------------------------------------------------


def resolve_ref(ref: str, pois: dict) -> str | None:
    """把补查轮写的地点名解析成坐标：先精确，再取"名字互相包含"里最短的那个。"""
    if not isinstance(ref, str):
        return None
    if re.fullmatch(r"\d{2,3}\.\d+,\d{2,3}\.\d+", ref.strip()):
        return ref.strip()
    hits = [n for n in pois if n == ref] or [n for n in pois if ref and (ref in n or n in ref)]
    return pois[min(hits, key=len)]["coord"] if hits else None


def gap_calls(asks: list) -> tuple[list, list[str]]:
    """原样接收清单：tool 与 args 不做校验、不夹取，只剔掉结构坏掉和重复的条目。
    坐标不核对台账（⑦删了）：模型给什么坐标就往高德发什么，地名端点留给下面的批次解析。"""
    out: list[tuple[str, dict]] = []
    notes: list[str] = []
    for item in asks or []:
        if not isinstance(item, dict) or not item.get("tool"):
            continue
        tool = str(item["tool"])
        # 只做"函数存在"的防御（防整跑崩溃），不是白名单：四个之外的工具照样执行。
        if not callable(getattr(travel_mcp, tool, None)):
            notes.append(f"工具不存在，丢弃：{tool!r}")
            continue
        args = item.get("args")
        if not isinstance(args, dict):
            args = {}
        # 秘塔的参数名是 q，模型按直觉写 query（实测一轮 4 条全因此作废）。
        if tool.startswith("metaso") and "q" not in args and args.get("query"):
            args = {**args, "q": args.pop("query")}
        # 数字参数模型常写成字符串（"size":"5"），工具内部拿它跟 int 比大小就 TypeError，
        # 一条本来有效的补查就这么丢了。这里只修类型，不做⑦那套白名单与夹取。
        for key in NUMERIC_ARGS.get(tool, ()):
            v = args.get(key)
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                args = {**args, key: int(v.strip())}
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if any(json.dumps(a, sort_keys=True, ensure_ascii=False) == key for _, a in out):
            continue
        out.append((tool, args))
    return out, notes


def draft_items(draft: str) -> list[dict]:
    """把初稿拆成条目。JSON 模式直接解析；Markdown 模式按"HH:MM-HH:MM 名称 | 交通 | 花费"切行。
    只用来算缺口，解析不出来就返回空，补查轮退回原来的纯模型判断。"""
    got = extract_json(draft)
    if isinstance(got, dict) or isinstance(got, list):
        items = []
        for g in normalize_groups(got):
            for seg in g["items"]:
                items.append({"type": str(seg.get("type") or "attraction"),
                              "name": str(seg.get("name") or "")})
        return items
    items = []
    for line in draft.splitlines():
        m = re.match(r"^\s*\d{1,2}:\d{2}\s*[-–~]\s*\d{1,2}:\d{2}\s+(.+)$", line)
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split("|")]
        if not parts or not parts[0]:
            continue
        name = parts[0]
        kind = "transit" if "→" in name else "attraction"
        items.append({"type": kind, "name": name})
    return items


def forced_gap_asks(draft: str, pois: dict, city: str, year: str, cap: int = 8) -> list[dict]:
    """代码算出"初稿里哪些条目没坐标、哪些没价格"，直接排进补查队列。

    补查轮原先完全由模型自由发挥，它看不到"哪几条 cost 是 null"，
    于是宁波那题的宁波罗城（票价）与望江宾馆（房价）双双留成待核实——**是没查，不是查不到**。
    景点票价与住宿房价高德都不给（v5 无票价字段、住宿 business.cost 实测全空），只能走网页。
    """
    asks: list[dict] = []
    seen: set[str] = set()
    for it in draft_items(draft):
        name, kind = it["name"], it["type"]
        if kind == "transit" or not name or name in seen:
            continue
        seen.add(name)
        poi, _exact = ledger_lookup(name, pois)
        if poi is None:
            asks.append({"tool": "get_amap_poi_search", "why": f"初稿用到「{name}」但台账里没有，缺坐标",
                         "args": {"keywords": name, "city": city, "offset": 3,
                                  "show_fields": POI_SHOW_FIELDS}})
            continue
        has_price = poi.get("price") or poi.get("cost")
        if kind == "attraction" and not has_price:
            asks.append({"tool": "metaso_web_search", "why": f"「{name}」门票价高德不给，要现查网页",
                         "args": {"q": f"{name} 门票 价格 {year}", "size": 5}})
        elif kind == "hotel" and not has_price:
            asks.append({"tool": "metaso_web_search", "why": f"「{name}」房价高德 business.cost 为空，要现查",
                         "args": {"q": f"{city} {name} 一晚 房价 {year}", "size": 5}})
        elif kind == "meal" and not has_price:
            asks.append({"tool": "metaso_web_search", "why": f"「{name}」人均没给，补一次网页",
                         "args": {"q": f"{name} 人均 消费 {city}", "size": 5}})
        if len(asks) >= cap:
            break
    return asks


def gap_round(context: str, question: str, pois: dict, city: str, adcode: str | None,
              limit: int, thinking: str, quiet: bool, draft: str = "") -> dict:
    """一轮补查：代码先算出必查缺口，LLM 再补它自己的判断，合并后原样执行，
    按 POI/天气 → 路段 → 秘塔 分三小批，让路段能用上同批里刚补查到的坐标（成都实测：一起发就解析不到坐标）。
    limit<=0 表示不限条数，>0 只截清单长度（代码算出的缺口优先占额度）。
    asks 为空时一次外部调用都不发。
    """
    forced = forced_gap_asks(draft, pois, city, str(datetime.now().year)) if draft else []
    gap_note = ("\n\n【代码已经排进队列的缺口，不要再重复列】\n"
                + "\n".join(f"  - {a['why']}" for a in forced) if forced else "")
    res = llm(
        [{"role": "system", "content": SYSTEM_GAP},
         {"role": "user", "content": f"【题目】{question}\n\n{context}\n\n"
                                     f"【已有数据（地名 | 坐标）】direction 可引用这些地名：\n"
                                     + "\n".join(f"  {n} | {p['coord']}" for n, p in list(pois.items())[:40])
                                     + gap_note}],
        max_tokens=700,
        thinking=thinking,
    )
    m = re.search(r"\{.*\}", res["text"], re.S)
    try:
        asks = json.loads(m.group(0)).get("asks") if m else None
    except json.JSONDecodeError:
        asks = None
    if not isinstance(asks, list):
        asks = []
    if limit and limit > 0:
        asks = asks[:max(0, limit - len(forced))]
    calls, notes = gap_calls(forced + asks)

    results: list = []
    first = [c for c in calls if c[0] in ("get_amap_poi_search", "get_amap_weather")]
    results += run_calls(first, quiet)
    collect_pois(results, pois, city)          # 新补查到的 POI 进台账，供下面的路段解析

    second = []
    by_coord = {p["coord"]: p for p in pois.values()}
    for tool, args in calls:
        if tool != "get_amap_direction":
            continue
        a = dict(args)
        o = resolve_ref(str(a.get("origin") or ""), pois)
        d = resolve_ref(str(a.get("destination") or ""), pois)
        if not o or not d:
            miss = a.get("origin") if not o else a.get("destination")
            notes.append(f"路段端点“{miss}”补查后仍解析不到坐标，丢弃这条路段")
            continue
        a["origin"], a["destination"] = o, d
        # transit 的 city1/city2 要的是**城市编码**（传"成都"会被高德判 INVALID_PARAMS，实测），
        # 端点坐标能回查到台账就用自己的 adcode，否则用本次已经探到的城市 adcode。
        if str(a.get("mode") or "") == "transit":
            for k, ref in (("city1", o), ("city2", d)):
                if not a.get(k):
                    a[k] = (by_coord.get(ref) or {}).get("adcode") or adcode or city
        second.append((tool, a))
    results += run_calls(second, quiet)

    third = [c for c in calls if c[0].startswith("metaso")]
    results += run_calls(third, quiet)

    return {"llm": res, "asks": asks, "forced": len(forced),
            "calls": calls, "notes": notes, "results": results}


def run_one(tool_name: str, args: dict, quiet: bool):
    # ⑨已删：秘塔 UPSTREAM_NO_RESULT 不再原参数重发，失败即返回。
    # 例外是高德的 CUQPS（个人 key 的并发上限）——那不是数据没有，是请求被挤掉了。
    # 并发压到 2 之后实测仍会撞（2026-10-18 一跑撞两次，都是重发一次仍挂那一档），
    # 所以退避着重试到第 3 次：0.6s、1.2s。
    attempts = 0
    t0 = time.perf_counter()
    while True:
        attempts += 1
        payload = _run_once(tool_name, args)
        msg = str(payload.get("error_msg") or "")
        if attempts < 3 and not payload.get("ok") and "CUQPS" in msg:
            time.sleep(0.6 * attempts)
            continue
        break
    if not quiet:
        err = "" if payload.get("ok") else \
            f" err={payload.get('error_code')}:{msg[:80]}"
        retry = "" if attempts == 1 else " (CUQPS 重发)"
        print(f"    · {tool_name} {json.dumps(args, ensure_ascii=False)[:70]} "
              f"ok={payload.get('ok')} {time.perf_counter() - t0:.1f}s{retry}{err}", file=sys.stderr)
    return tool_name, args, payload


def _run_once(tool_name: str, args: dict):
    fn = getattr(travel_mcp, tool_name)
    try:
        res = fn(**args)
        # metaso_* 是 async：线程池工作线程里没有运行中的 loop，直接 asyncio.run
        if inspect.isawaitable(res):
            res = asyncio.run(res)
        payload = unwrap(res)
    except Exception as exc:  # 补查清单不再校验参数，坏参数只折这一条，别让整跑崩掉
        payload = {"ok": False, "error_code": "LOCAL_BAD_CALL",
                   "error_msg": f"{type(exc).__name__}: {exc}"[:150], "data": {}}
    return payload


def pick_legs(needs: dict, pois: dict, budget: int, adcode: str | None = None,
              hints: str = "") -> list:
    """主干路段确定性选取：出发地→到达枢纽→最近景点→依次最近→回枢纽。不算两两矩阵。

    用 adcode 前 4 位区分本市与外地：出发地在外市才需要那条跨城腿；同名的本市分校区
    （如"杭州电子科技大学绍兴校区"）应当作景点，不能被当成出发地吃掉。
    ⑧已删：出发地在外市时照常请求"出发地→枢纽"的 transit 腿——原实现彻底禁掉这条，
    是因为高德会给"地铁 1 号线 > 5 号线 > 轨道交通 1 号线，169 分"这种跨城公共交通方案，
    数值真但不能当城际用，模型照着它编高铁车次与票价（实测两轮都栽在这条腿上）。
    budget<=0 表示不限路段条数（③已删）。
    """
    origin = needs.get("origin") or ""
    dest_city = needs.get("destination_city") or ""
    prefix = (adcode or "")[:4]

    def city_of(name: str) -> str:
        return (pois[name].get("adcode") or "")[:4]

    def in_target(name: str) -> bool:
        return not prefix or city_of(name) == prefix

    named = [n for n in pois if origin and origin[:4] in n]
    origin_out = [n for n in named if prefix and city_of(n) != prefix]

    def dist(a, b):
        ax, ay = float(pois[a]["lon"]), float(pois[a]["lat"])
        bx, by = float(pois[b]["lon"]), float(pois[b]["lat"])
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 * 111_000  # 度→米粗算

    station_junk = re.compile(r"[(（][^()（）]{0,6}(建设中|在建|进站|出站|检票|售票|停车|进出口|入口|出口)")
    stations = [n for n in pois
                if (n.endswith("站") or "火车站" in n) and in_target(n)
                and not station_junk.search(n)]
    # 车站/停车场类条目不能当景点：`成都站(建设中)` 从 stations 里被剔掉后，会作为"景点"
    # 混进链条，于是行程里出现"成都南站→成都站(建设中)"这种腿（实测）；地铁站同理。
    # 判据统一走 is_transit_stop（含 typecode 15 与停车场/售票处这类设施名），
    # 别再在这里维护第二套名字规则。
    def is_station_like(n: str) -> bool:
        return is_transit_stop(n, pois[n])

    # 住宿不进景点链：typecode 100xxx 是高德的"住宿服务"，检索回来的酒店散在全城，
    # 让它们进链条既白吃路段配额，又会把地图视野拽到几十公里外。
    lodging_name = re.compile(r"酒店|宾馆|旅馆|民宿|客栈|招待所|青年旅舍")

    def is_lodging_like(n: str) -> bool:
        return str(pois[n].get("typecode") or "").startswith("100") or bool(lodging_name.search(n))

    sights = [n for n in pois if in_target(n) and n not in origin_out
              and not is_station_like(n) and not is_lodging_like(n)]
    # 同地点细分要合并（"鲁迅故里-鲁迅故居"相距 72 m、园内"4号别墅"与母点同园区）：
    # 让它们进链条只会白吃路段配额，真实景点之间反而测不到通勤。判据 same_place 见下面
    # "建议顺序"一节——路段链和聚类分天要合的是同一批点，不能各留一套标准。
    must = needs.get("must_visit") or []

    def hit_must(n: str) -> bool:
        return any(m and (m in n or n in m) for m in must)

    def merge_sights(names: list[str]) -> list[str]:
        """同地点归组，每组只留一个代表。代表先看 must_visit 命中，其次看高德评分。
        成都实测：30 条台账里 `成都大熊猫繁育研究基地`(4.8) 与 `成都大熊猫博物馆`(4.4) 会并进
        同一组，按"名字最短"来留会选成博物馆，链条起点就错了——所以用评分，不用长度。"""
        reps: list[str] = []
        groups: dict[str, list[str]] = {}
        for s in names:
            g = next((r for r in reps if same_place(r, s) or dist(r, s) < 400), None)
            if g is None:
                reps.append(s)
                groups[s] = [s]
            else:
                groups[g].append(s)
        return [sorted(groups[r], key=lambda n: (not hit_must(n),
                                                 -float(pois[n].get("rating") or 0)))[0]
                for r in reps]

    sights = merge_sights(sights)
    if not sights:
        return []

    seed = next((s for s in sights if hit_must(s)), sights[0])
    # ①删掉后 hints 里除了 --fact 还混有秘塔摘要：摘要里"绍兴东站/绍兴站"混着出现，
    # 子串命中会把枢纽选成上虞方向的绍兴东（实测烧掉一条 156 分废腿）——这个风险照旧存在。
    def hinted(s: str) -> str | None:
        cands = [s] + ([s[:-1]] if s.endswith("站") else [])
        hits = [c for c in cands if len(c) >= 3 and c in hints]
        return max(hits, key=len) if hits else None

    scored = sorted(((hinted(s), s) for s in stations), key=lambda x: len(x[0] or ""), reverse=True)
    if stations and scored[0][0]:
        hub = scored[0][1]
    elif stations:
        # 跨城题里"市名+方位+站"基本就是高铁站（绍兴北、杭州东），光秃秃的"绍兴站"多是普速站：
        # 先按这个筛一轮，同组内再取离主景点最近的；没有方位站就退回全部站点取最近。
        intercity = (needs.get("origin_city") or "") != (needs.get("destination_city") or "")
        pool = [s for s in stations
                if intercity and re.fullmatch(re.escape(dest_city) + r"[东南西北]站", s)] or stations
        hub = min(pool, key=lambda s: dist(s, seed))
    else:
        hub = seed
    chain = [seed]
    remaining = [s for s in sights if s != seed and s != hub]
    # ③已删：budget<=0 时链条覆盖全部合并后的景点，不再限 3~6 个。
    # 上限从 6 提到 10：终稿每天有 5~6 站、多日题两三天，6 个点的链覆盖不住，
    # 宁波实测请求 7 条路段而方案里有 11 条交通段——4 条只能显示"路线待实测"。
    chain_cap = max(3, min(10, int(budget))) if budget and budget > 0 else len(sights)
    while remaining and len(chain) < chain_cap:
        nxt = min(remaining, key=lambda c: dist(chain[-1], c))
        chain.append(nxt)
        remaining.remove(nxt)

    legs = []
    # ⑧已删：出发地与枢纽分属两座城市时也照常请求那条 transit 腿。
    # 原实现禁它是因高德会给"地铁 1 号线 > 5 号线 > 轨道交通 1 号线，169 分"这种
    # 数值真但不能当城际用的方案，模型照着编高铁车次与票价（实测两轮都栽在这条腿上）。
    if origin_out and hub != origin_out[0]:
        legs.append((origin_out[0], hub, "transit"))
    if hub != chain[0]:
        legs.append((hub, chain[0], "walking" if dist(hub, chain[0]) < 2500 else "transit"))
    for a, b in zip(chain, chain[1:]):
        legs.append((a, b, "walking" if dist(a, b) < 2500 else "transit"))
    if len(chain) > 1 and hub != chain[-1]:
        legs.append((chain[-1], hub, "transit"))
    return legs[: int(budget)] if budget and budget > 0 else legs


# --------------------------------------------------------------------------
# 建议顺序（A）：直线距离聚类分天 + 天内开放路径
# --------------------------------------------------------------------------
# 终稿的先后过去完全由模型自由写，pick_legs 那条最近邻链只决定"发哪几条 direction 请求"，
# 两者对不上，于是出现相邻两点 30 km、下午在城东晚上折回城西（宁波实测）。
# 距离一律用球面直线：FloatTrip 把真实路网做成可选变体后盲评质量不达标又回退了默认值，
# 说明"顺序排错"的瓶颈不在距离精度，而直线距离是 0 次外部调用、同输入必同输出。

EARTH_R_KM = 6371.0088
BALANCE_MAX_KM = 10.0     # 分天平衡时允许挪动的最远距离，超过就当离群点不挪
SAME_PLACE_MAX_KM = 0.4   # 相距小于这个数的台账条目算同一个地点
# 泛检索词带回来的东西不能算"这道题点名的"：`{city}景点` 捞回的是儿童公园/文化广场/五一广场，
# 高德评分 4.5+ 一堆，按评分排会把真景点挤掉（放宽到每天 5 站那次实测，day2 整条变成逛广场，
# 宁波博物院反而出局）。带回来的那次检索是 targeted 的，这个判据不受名字变体影响
# （ledger 叫"宁波博物院"、wiki 写"宁波博物馆"，`n in wiki_text` 判不中）。
GENERIC_QUERY_RE = re.compile(r"景点|美食|餐厅|酒店|购物|娱乐|高铁站|站$|市区|城区")
POI_JUNK_RE = re.compile(
    r"[(（][^()（）]{0,8}(建设中|在建|进站|出站|检票|售票|停车|进出口|入口|出口|装修)"
    r"|站(北|南|东|西)?广场")
LODGING_NAME_RE = re.compile(r"酒店|宾馆|旅馆|民宿|客栈|招待所|青年旅舍")
# 不带括号也认的设施名：高德的"停车场/售票处/候车室"名字里往往没有括号，
# POI_JUNK_RE 那套要括号的规则罩不住它们。
POI_FACILITY_RE = re.compile(r"停车场|售票处|检票口|候车|出站|进出站|地下广场|旅游码头")


def km_between(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(b["lon"]) - float(a["lon"]))
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_R_KM * math.asin(math.sqrt(h))


def path_km(seq: list[str], pois: dict) -> float:
    return sum(km_between(pois[x], pois[y]) for x, y in zip(seq, seq[1:]))


def is_transit_stop(name: str, poi: dict) -> bool:
    """车站、停车场、售票处这类"交通/设施"条目：既不能当游览点排进链，也不能当住宿挑。
    `宁波站南广场地下停车场` 是实测踩到的：它 typecode 属交通设施、名字又不以"站"结尾，
    于是混进路段链，成了枢纽的最近邻，白吃一条 300 m 的步行腿。"""
    tc = str(poi.get("typecode") or "")
    return (tc[:2] == "15" or name.endswith("站") or "火车站" in name or "地铁站" in name
            or "汽车站" in name or "客运中心" in name or bool(POI_JUNK_RE.search(name))
            or bool(POI_FACILITY_RE.search(name)))


def is_lodging_or_meal(name: str, poi: dict) -> bool:
    tc = str(poi.get("typecode") or "")
    return tc[:2] in ("10", "05") or bool(LODGING_NAME_RE.search(name))


def _centroid(members: list[str], pois: dict) -> dict:
    return {"lon": sum(float(pois[n]["lon"]) for n in members) / len(members),
            "lat": sum(float(pois[n]["lat"]) for n in members) / len(members)}


def same_place(a: str, b: str) -> bool:
    """同地点判定：包含关系，或共享 ≥3 字连续片段。
    3 字是为了躲开"鲁迅故里 / 书圣故里"这种只共享"故里"的真实不同景点，
    同时抓住"成都大熊猫繁育研究基地 / 成都大熊猫博物馆 / 熊猫书店(成都大熊猫基地店)"
    这一窝园内子点。pick_legs 的路段链和分天聚类合的是同一批点，所以判据放在一处。"""
    if a in b or b in a:
        return True
    for n in range(min(len(a), len(b)), 2, -1):
        for i in range(len(a) - n + 1):
            if a[i:i + n] in b:
                return True
    return False


def merge_same_place(names: list[str], pois: dict) -> list[str]:
    """同一个地点只留一个代表（先按评分高、再按名字短）。
    不合并会整批带偏聚类：宁波实测一次 `宁波景点` 就带回"南塘老街"的 A 区/B 区/2 期/牌楼/
    水上游码头，【建议顺序】的 day1 被排成"蒋氏故居售票处 → 蒋氏故居游客中心 → 景区导览图"。
    光靠名称包含不够（园点子点常不含母点全称，成都实测），再补一条 400 m 距离判据。"""
    def rank(n: str) -> tuple:
        return (-(_num(pois[n].get("rating")) or 0.0), len(n))

    reps: list[str] = []
    for n in sorted(names, key=rank):
        if any(same_place(n, r) or km_between(pois[n], pois[r]) <= SAME_PLACE_MAX_KM
               for r in reps):
            continue
        reps.append(n)
    return reps


def cluster_by_days(names: list[str], pois: dict, k: int) -> list[list[str]]:
    """确定性 k-means（k=天数）：按经度等距取种子，迭代到不再变动，上限 12 轮。
    分天这件事交给代码，同一片区的点才会真的落在同一天。
    最后再平衡一次：k-means 只管"就近"，不管条数——宁波 6 个点试跑分成 4+1，
    而"第二天只排得下一个点"正是用户在抱怨的那件事。把最靠近别组的点挪过去，
    直到组间条数差不超过 1。"""
    if not names:
        return []
    if k <= 1 or len(names) <= k:
        return [list(names)]
    srt = sorted(names, key=lambda n: float(pois[n]["lon"]))
    centroids = [_centroid([srt[round(i * (len(srt) - 1) / (k - 1))]], pois) for i in range(k)]
    groups: list[list[str]] = []
    for _ in range(12):
        groups = [[] for _ in range(k)]
        for n in srt:
            j = min(range(k), key=lambda i: km_between(pois[n], centroids[i]))
            groups[j].append(n)
        moved = False
        for i, g in enumerate(groups):
            if not g:
                continue
            c = _centroid(g, pois)
            moved = moved or abs(c["lon"] - centroids[i]["lon"]) > 1e-6
            centroids[i] = c
        if not moved:
            break
    groups = [g for g in groups if g]
    for _ in range(len(names)):
        big = max(groups, key=len)
        small = min(groups, key=len)
        if len(big) - len(small) <= 1:
            break
        c = _centroid(small, pois)
        mover = min(big, key=lambda n: km_between(pois[n], c))
        # 离群点不为了凑条数硬挪：溪口离市区 30 km，挪进哪一组都是当天白跑几十公里，
        # 宁可让那一天薄一点——"相邻两点过远"比"某天只有一个点"更伤方案质量。
        if km_between(pois[mover], c) > BALANCE_MAX_KM:
            break
        big.remove(mover)
        small.append(mover)
    return sorted(groups, key=lambda g: _centroid(g, pois)["lon"])


def open_path(names: list[str], pois: dict, start: str | None = None) -> list[str]:
    """开放路径（终点不折回起点）：最近邻构造 + 2-opt 反转段消交叉。
    start 给了就当第一站（day1 从落脚点起步），没给就逐个试起点取最短——
    只按经度硬选首点会把南北向的点集钉成一条来回的线
    （宁波实测 5 个点：首点选南塘老街 11.3 km，选宁波博物院 8.2 km）。
    点数就是台账里本市景点那十来个，每次全量重算总长，不用局部增量。"""
    if not names:
        return []
    seeds = [start] if (start and start in names) else list(names)
    best: list[str] | None = None
    best_len = math.inf
    for s in seeds:
        rest = [n for n in names if n != s]
        seq = [s]
        while rest:
            nxt = min(rest, key=lambda n: km_between(pois[seq[-1]], pois[n]))
            rest.remove(nxt)
            seq.append(nxt)
        length = path_km(seq, pois)
        improved = True
        while improved and len(seq) > 3:
            improved = False
            for i in range(1, len(seq) - 1):
                for j in range(i + 1, len(seq)):
                    cand = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                    d = path_km(cand, pois)
                    if d < length - 1e-9:
                        seq, length, improved = cand, d, True
        if length < best_len:
            best, best_len = seq, length
    return best or []


def arrival_hub(needs: dict, pois: dict, prefix: str) -> str | None:
    """day1 的落脚点：本市的火车站/汽车站，`{市}[东南西北]?站` 优先，地铁与进出站口不要。"""
    city = needs.get("destination_city") or ""
    cands = [n for n, p in pois.items()
             if is_transit_stop(n, p) and not POI_JUNK_RE.search(n) and "地铁站" not in n
             and (not prefix or str(p.get("adcode") or "")[:4] == prefix)]
    if not cands:
        return None
    pref = [h for h in cands if city and re.fullmatch(re.escape(city) + r"(高铁)?[东南西北]?站", h)]
    return (pref or cands)[0]


def tour_stops(needs: dict, pois: dict, adcode: str | None) -> list[str]:
    """本市、非车站非住宿非餐饮、同地点已合并的台账条目——能排进游览链的那一批。"""
    prefix = (adcode or "")[:4]
    names = [n for n, p in pois.items()
             if (not prefix or str(p.get("adcode") or "")[:4] == prefix)
             and not POI_JUNK_RE.search(n)
             and not is_transit_stop(n, p) and not is_lodging_or_meal(n, p)]
    return merge_same_place(names, pois)


def lodging_center(pois: dict, names: list[str]) -> str | None:
    """住宿周边搜的检索中心：取"到其它景点总距离最短"的那个真实坐标（medoid）。
    几何中心会被 30 km 外的溪口一把拽走，medoid 不随离群点漂。"""
    if not names:
        return None
    return pois[min(names, key=lambda a: sum(km_between(pois[a], pois[b])
                                             for b in names if b != a))]["coord"]


def suggest_order(needs: dict, pois: dict, adcode: str | None, hints: str = "") -> dict:
    """本市景点分到天、每天给一条不走回头路的开放路径。
    车站/餐饮/住宿不进链：前者是锚点，后两者按"离哪一站最近"来挑，排进几何链会把顺序带歪。
    只有一个点、又离其它组 >BALANCE_MAX_KM 的（溪口那种）不当成一天：
    硬凑条数会让当天白跑几十公里，单独占一天又排成"一个点的半天"——
    它另列成 outliers，交回模型判断要不要专门跑一趟。
    候选按每天 SUGGEST_STOPS_PER_DAY 站截断（必去点 → wiki 点名 → 高德评分），
    不截的话一次 `{city}景点` 带回的"儿童公园/文化广场/生态走廊"会把建议链铺到 23 km，
    模型只肯抄走其中一段，剩下的行数是白占 prompt。"""
    prefix = (adcode or "")[:4]
    stops = tour_stops(needs, pois, adcode)
    if len(stops) < 2:
        return {"days": [], "outliers": []}
    days = max(1, min(7, int(_num(needs.get("days")) or 1)))
    cap = SUGGEST_STOPS_PER_DAY * days
    if len(stops) > cap:
        must = needs.get("must_visit") or []

        def rank(n: str) -> tuple:
            named = not GENERIC_QUERY_RE.search(str(pois[n].get("q") or "")) or (
                len(n) >= 4 and n in hints)
            return (not any(m and (m in n or n in m) for m in must),
                    not named,
                    -(_num(pois[n].get("rating")) or 0.0))

        stops = sorted(stops, key=rank)[:cap]
    # 离群点先摘掉再聚类：溪口跟市区的点相距 28 km，留在聚类里它会自成一组，
    # 平衡那一步又因为"超过 10 km 不挪"而不动它，于是 7+1 的分组把剩下 7 个点全塞进一天。
    lone = [n for n in stops
            if all(km_between(pois[n], pois[m]) > BALANCE_MAX_KM for m in stops if m != n)]
    stops = [n for n in stops if n not in lone]
    if len(stops) < 2:
        return {"days": [], "outliers": lone}
    hub = arrival_hub(needs, pois, prefix)
    groups = cluster_by_days(stops, pois, days)
    out = []
    for i, g in enumerate(groups, 1):
        seed = None
        if i == 1 and hub:
            seed = min(g, key=lambda n: km_between(pois[n], pois[hub]))
        seq = open_path(g, pois, seed)
        out.append({"day": i, "names": seq, "km": round(path_km(seq, pois), 1)})
    return {"days": out, "outliers": lone}


# --------------------------------------------------------------------------
# 阶段 C
# --------------------------------------------------------------------------

def build_context(needs: dict, wiki_text: str, ext: dict, facts: list[str] | None = None) -> str:
    lines = [f"【需求】{json.dumps({k: v for k, v in needs.items() if not k.startswith('_')}, ensure_ascii=False)}"]
    lines.append(f"【知识库】\n{wiki_text[:WIKI_MAX_CHARS]}")
    if ext["pois"]:
        # business.cost 是人均消费，对餐饮/住宿有用，对景点是游客综合消费——别当门票用（沈园实测 163）。
        lines.append("【POI（坐标/开放时间/评分，来自高德 v5；v5 不返回门票价格，人均≠票价；"
                     "开放= 里的 00:00-24:00 是景区大门口径，不是馆内开放时间）】")
        # 按检索词给配额：全局 30 行先到先得时，`{city}站` 带回的一堆地铁站会把后面的
        # 酒店、餐厅整批挤出去（实测两种排法各挤掉一类，被挤掉的那一类模型只能凭空编名字）。
        shown: list[tuple[str, dict, str]] = []
        per_q: dict[str, int] = {}
        n_trans = 0
        # 住宿行只留"离游览区最近的几家里再卡一个半径"：`{city}酒店` 是全城检索，
        # 34 行名额又是先到先得的——上一轮实测模型把"宁波望江宾馆"(溪口, 30 km) 排进当晚，
        # 第二天等于从溪口出发。半径卡死会整批落空（市区一家都没进半径时模型就没住宿可抄了），
        # 所以落空时退回"只按最近几家"，宁可留远的也不能一条都不给。
        center = lodging_center(ext["pois"], tour_stops(needs, ext["pois"], ext.get("adcode")))
        keep_lodging: set[str] = set()
        if center:
            clon, clat = center.split(",")
            here = {"lon": clon, "lat": clat}
            ranked = sorted(((km_between(p, here), n) for n, p in ext["pois"].items()
                             if (p.get("type") or "").startswith("住宿服务")))[:LODGING_KEEP]
            near = {n for d, n in ranked if d <= LODGING_RADIUS_M / 1000.0 * 1.5}
            keep_lodging = near or {n for _d, n in ranked}
        for n, p in sorted(ext["pois"].items(),
                           key=lambda kv: 0 if kv[0] in keep_lodging else 1):
            # 稳定排序：只把"离游览区最近的那几家住宿"提到前面。它们本来排在台账末尾
            # （周边搜是第二波才回来的），34 行名额轮不到它，模型就只能看见溪口那几家。
            kind = (p.get("type") or "").split(";")[0] or "未给"
            if kind in LEDGER_SKIP_KINDS:
                continue
            if keep_lodging and kind == "住宿服务" and n not in keep_lodging:
                continue
            if kind == "交通设施服务":
                if n_trans >= 4:
                    continue
                n_trans += 1
            q = p.get("q")
            if q and per_q.get(str(q), 0) >= 6:
                continue
            if q:
                per_q[str(q)] = per_q.get(str(q), 0) + 1
            shown.append((n, p, kind))
            # 34 行顶到过：宁波实测台账正好渲染 34 行（后面还有内容被砍），
            # 而模型要在"必去点/餐/宿/交通"四类之间挑，砍掉的那类它就凭空编名字。
            # 行数是输入侧，多几百字符不影响耗时（瓶颈在最后一轮的解码）。
            if len(shown) >= 44:
                break
        for n, p, kind in shown:
            price = p.get("price") or p.get("cost") or "未给"
            # 类别直接给出来，模型才知道这条人均能不能当花费：餐饮服务=餐费、住宿服务=住宿费。
            lines.append(f"  {n} | 类={kind} | {p['lon']},{p['lat']} | 人均={price} "
                         f"评分={p.get('rating') or '未给'} | 开放={str(p.get('opentime'))[:70]}")
    sugg = suggest_order(needs, ext["pois"], ext.get("adcode"), wiki_text)
    if sugg["days"]:
        lines.append("【建议顺序】管线按坐标聚类 + 最近邻 + 2-opt 算的（0 次外部调用），"
                     "非交通条目照它给的先后排：")
        for s in sugg["days"]:
            lines.append(f"  day{s['day']}（直线 {s['km']} km）: " + " → ".join(s["names"]))
        if sugg["outliers"]:
            lines.append("  离群候选（离其它点 >10 km，专门跑一趟还是跳过你定）: "
                         + "、".join(sugg["outliers"]))
        lines.append("  餐饮与住宿不在这条链里：按离当天哪一站最近来挑。")
    for tool_name, args, payload in ext["results"]:
        d = payload.get("data") or {}
        if payload.get("ok") and d.get("lives"):
            live = d["lives"][0]
            lines.append(f"【天气】{live.get('city')} {live.get('weather')} {live.get('temperature')}℃")
        if payload.get("ok") and d.get("forecasts"):
            fc = d["forecasts"][0]
            casts = []
            for c in (fc.get("casts") or [])[:3]:
                casts.append(f"{c.get('date')} {c.get('dayweather')}/{c.get('nightweather')} "
                             f"{c.get('nighttemp')}~{c.get('daytemp')}℃ {c.get('daypower')}级")
            lines.append(f"【天气预报 {fc.get('city')}（发布 {fc.get('reporttime')}）】" + "；".join(casts))
        if payload.get("ok") and d.get("route"):
            r = d["route"]
            summ = []
            for t in (r.get("transits") or [])[:2]:
                c = t.get("cost") or {}
                dur = c.get("duration") or t.get("duration")
                walk = c.get("walking_distance") or t.get("walking_distance") or 0
                fee = c.get("transit_fee") or t.get("cost_desc")
                fee_txt = f" 票{fee}元" if fee not in (None, "", "0") else ""
                seq = " > ".join(bl.get("name", "") for s in (t.get("segments") or [])
                                 for bl in ((s.get("bus") or {}).get("buslines") or []))
                summ.append(f"{int(float(dur or 0)) // 60}分 步行{walk}m{fee_txt} {seq[:60]}")
            for pth in (r.get("paths") or [])[:1]:
                cost = pth.get("cost") or {}
                dur = pth.get("time") or cost.get("duration")
                summ.append(f"{int(float(dur or 0)) // 60}分 {pth.get('distance')}m")
            coord_names = {v["coord"]: k for k, v in ext["pois"].items()}
            a_name = coord_names.get(args["origin"], args["origin"])
            b_name = coord_names.get(args["destination"], args["destination"])
            lines.append(f"【路段 {a_name}→{b_name}（{args.get('mode')}）】" + "；".join(summ))
        if tool_name == "metaso_web_search":
            if payload.get("ok"):
                lines.append("【网页摘要（秘塔，仅当场有效）】")
                lines += metaso_summaries(payload)
            else:
                lines.append(f"【网页摘要取数失败】{str(payload.get('error_msg'))[:80]}")
    if facts:
        lines.append("【补充事实（外部检索或人工核实，只对当场有效）】")
        lines += [f"  - {f}" for f in facts]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 阶段 F：结构化输出（docs/frontend-plan.md 第 3 节的 final_plan）
# --------------------------------------------------------------------------

MAX_PATH_POINTS = 300   # 一条腿的实测折线抽稀到这个点数，够画且不让 JSON 爆掉


def extract_json(text: str):
    """模型偶尔包 ```json 或前后带一句话：从第一个 { 或 [ 起做括号配对扫描，
    取第一个能解析成功的 JSON 值。比正则贪婪匹配可靠（摘要里也出现过嵌套花括号）。"""
    for start, open_ch in [(i, c) for i, c in enumerate(text or "") if c in "[{"]:
        close_ch = "]" if open_ch == "[" else "}"
        depth, in_str, esc = 0, False, False
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:j + 1])
                    except json.JSONDecodeError:
                        break
    return None


def leg_index(ext: dict) -> list[dict]:
    """把已请求的路段结果摊成可回查的清单：起终点名、耗时、票价、实测折线。
    transit 只取第一个方案（与 build_context 给模型看的保持一致）。"""
    coord_names = {p["coord"]: n for n, p in ext["pois"].items()}
    out = []
    for tool_name, args, payload in ext["results"]:
        if tool_name != "get_amap_direction" or not payload.get("ok"):
            continue
        route = (payload.get("data") or {}).get("route") or {}
        node = (route.get("transits") or [None])[0] or (route.get("paths") or [None])[0]
        if node is None:
            continue
        cost = node.get("cost") or {}
        duration = cost.get("duration") or node.get("duration")
        fee = _num(cost.get("transit_fee"))
        out.append({
            "a": coord_names.get(args.get("origin"), args.get("origin")),
            "b": coord_names.get(args.get("destination"), args.get("destination")),
            # 端点坐标也留着：模型写"宁波站→天一阁博物院"，链上却叫"宁波火车站/天一阁"，
            # 纯按名字对就整段丢折线（宁波实测 7 条腿只落 1 条）。
            "a_coord": args.get("origin"), "b_coord": args.get("destination"),
            "mode": args.get("mode"),
            "minutes": max(1, int(float(duration or 0)) // 60) if duration else None,
            "fee": fee,
            "path": polyline_of(node),
        })
    return out


def polyline_of(node: dict) -> list[list[float]]:
    """沿返回体的字段顺序拼出实测折线。v5 把 polyline 放在 paths[].steps[].polyline
    与 transit segments[].walking.steps[] / segments[].bus.buslines[] 里，
    层级随 mode 变，所以按出现顺序递归收集，不认死路径。"""
    chunks: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "polyline" and isinstance(v, str) and v:
                    chunks.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(node)
    pts: list[list[float]] = []
    for chunk in chunks:
        for pair in chunk.strip().rstrip(";").split(";"):
            x, _, y = pair.partition(",")
            try:
                lon, lat = float(x), float(y)
            except ValueError:
                continue
            if pts and pts[-1] == [lon, lat]:
                continue
            pts.append([lon, lat])
    if len(pts) > MAX_PATH_POINTS:
        stride = -(-len(pts) // MAX_PATH_POINTS)
        pts = pts[::stride] + [pts[-1]]
    return pts


ALL_DAY_RE = re.compile(r"00:00\s*[-–~]\s*24:00")


def useful_hours(kind: str, value) -> str | None:
    """高德的 `business.opentime_week` 对景区给的是**大门口径**：天一阁实测是
    "周一至周日 00:00-24:00"，而 wiki 里记的馆内时间是 8:30 起（还带着"两说并存"的矛盾）。
    这种全天值摆在卡片上比不摆更容易骗人，所以景点/住宿一律当"没给"；
    街区、广场这类真全天开放的，模型会在 tip 里自己说清楚，不靠这个字段。"""
    text = str(value or "").strip()
    if not text:
        return None
    if kind in ("attraction", "hotel") and ALL_DAY_RE.search(text):
        return None
    return text


# 人均与"未实测"不能同时出现在一条卡片上：cost 里的数字要么就是从台账人均回填的、
# 要么就是模型照抄【POI】人均的，两种都是取到了的数据。宁波实测 3 条餐（阿娇 100、
# 意卡菲 85、南塘 100）都写着"人均参考未实测"，钱明明在场——那是 prompt 里"tip 写明
# 人均参考"被模型自行补的后半句。cost 为 null 时这句是真话，必须留着。
PEOPLE_UNVERIFIED_RE = re.compile(r"(人均[^，。；]{0,6}?)未实测")
PEOPLE_LABEL_RE = re.compile(r"(人均参考|人均)")
# "带数字"只认紧跟在"人均"后面那几个字里的数字，否则"人均参考，10:30 后好坐"
# 会因为句中有时间数字而被当成已经写全。
PEOPLE_WITH_NUM_RE = re.compile(r"(?:人均参考|人均)[^，。；]{0,4}?\d")


def normalize_people_tip(row: dict) -> None:
    """餐宿卡片上"人均"这句话必须带数字：模型要么写成"人均参考未实测"（钱明明在场），
    要么被规则逼成只剩"人均参考"四个字（宁波实测两家餐厅的正文就只剩标签，
    100 元与 80 元只藏在展开的详情里，看上去像数据缺失）。两种都由代码按 cost 归一。"""
    if row["cost"] is None or row["type"] not in ("meal", "hotel"):
        return
    money = f"人均参考 {int(round(row['cost']))} 元"
    tip = PEOPLE_UNVERIFIED_RE.sub(r"\1", str(row.get("tip") or "")).strip().strip("，。； ")
    if not tip:
        row["tip"] = money
    elif PEOPLE_WITH_NUM_RE.search(tip):
        row["tip"] = tip
    else:
        row["tip"] = PEOPLE_LABEL_RE.sub(money, tip, count=1)


def ledger_lookup(name: str, pois: dict) -> tuple[dict | None, bool]:
    """模型写的条目名回查高德台账，返回 (条目, 是否精确)。

    模糊匹配只在"唯一候选"时才认：绍兴那次实测，模型写"沈园"，台账里含"沈园"的有
    4 家"沈园堂"酒店加一个公交站，而真景点那条叫"沈氏园"（字面不含"沈园"）——
    按最长匹配就给了酒店的照片、坐标和 163 元人均。歧义时宁可让卡片空着，
    前端按 null 降级，也比渲染出一个错的地方强。
    """
    if not isinstance(name, str) or not name:
        return None, False
    if name in pois:
        return pois[name], True
    hits = [n for n in pois if name in n or n in name]
    return (pois[hits[0]], False) if len(hits) == 1 else (None, False)


def origin_point(needs: dict, pois: dict) -> dict | None:
    """出发地在台账里的那一个点，给前端画"起"标记用。

    出发地通常只出现在交通段的名字里（"甲→乙"），而 transit 条目故意不做台账回查
    （防蹭终点的照片与开放时间），所以方案跑到现在地图上一直没有起点。
    认法用"哪次检索带回的"而不是名字匹配：`杭电下沙校区` 检索回来的第一条就是
    `杭州电子科技大学(下沙校区)`，拿名字去 ledger_lookup 反而一条都不含（"杭电"是简称）。
    """
    origin = str(needs.get("origin") or "").strip()
    if not origin or origin == str(needs.get("origin_city") or "").strip():
        return None
    name = next((n for n, p in pois.items() if str(p.get("q") or "") == origin), None)
    if name is None:
        poi, _ = ledger_lookup(origin, pois)
        if not poi:
            return None
        return {"name": origin, "location": {"lng": float(poi["lon"]), "lat": float(poi["lat"])}}
    p = pois[name]
    return {"name": name, "location": {"lng": float(p["lon"]), "lat": float(p["lat"])}}


def match_leg(name: str, legs: list[dict], pois: dict | None = None,
              claimed: set | None = None) -> dict | None:
    """交通段回查实测路段。

    两端都能从台账解析出坐标时，**坐标就是终审**：对不上说明这条腿没实测过，不再按名字猜。
    名字兜底只留给解析不出坐标的情况（模型写了简称），且要求两端**各自**在正确方向上命中。
    宁波栽过两次：`宁波站 → 宁波站南广场地下停车场` 这条 300 m 步行腿同时命中了
    "杭州东→宁波站""宁波站→南塘老街""宁波站→杭州东"三段（"宁波站"既是它的起点，
    又是"宁波站南广场…"的子串），三条不同的腿共用一条折线——在地图上就是"线又没了"。
    同一条折线也不能分给两段（`claimed`）。
    """
    parts = [p.strip() for p in str(name or "").split("→")]
    if len(parts) != 2:
        return None
    taken = claimed or set()

    def free(leg: dict) -> bool:
        return (leg.get("a_coord"), leg.get("b_coord")) not in taken

    if pois:
        a, b = resolve_ref(parts[0], pois), resolve_ref(parts[1], pois)
        if a and b:
            for leg in legs:
                if leg.get("a_coord") == a and leg.get("b_coord") == b and free(leg):
                    return leg
            return None        # 坐标都对得上号却没这条腿：就是没测过

    def rel(p: str, leg_name: str) -> bool:
        return bool(p) and bool(leg_name) and (leg_name == p or p in leg_name or leg_name in p)

    for leg in legs:
        if not free(leg) or leg.get("a_coord") == leg.get("b_coord"):
            continue
        if rel(parts[0], leg["a"]) and rel(parts[1], leg["b"]):
            return leg
    return None


def assign_days(segments: list[dict], needs: dict, thinking: str, quiet: bool) -> tuple[list, int]:
    """分块兜底（第 9 节决定：再花一轮 LLM 按天整理，而不是降级成单 day）。
    只让它搬运条目，不许改字段内容。"""
    res = llm(
        [{"role": "system", "content":
            "你只做按天分组：给你一份行程条目 JSON 数组，把它们分到 1..N 天里，"
            "输出 {\"days\":[{\"day\":1,\"items\":[...]}]}。"
            "条目必须原样搬运，不许增删、不许改字段值，只能改 day。只输出 JSON。"},
         {"role": "user", "content":
             f"题目天数：{needs.get('destination_city') or ''} 共 "
             f"{needs.get('days') or '未给，按行程跨度推算'} 天\n\n"
             + json.dumps(segments, ensure_ascii=False)}],
        max_tokens=2400, thinking=thinking)
    if not quiet:
        print(f"    · 分块补轮 LLM {res['seconds']:.1f}s reasoning={res['reasoning_chars']}字",
              file=sys.stderr)
    got = extract_json(res["text"])
    days = (got or {}).get("days") if isinstance(got, dict) else None
    if not isinstance(days, list) or not days:
        return [{"day": 1, "items": segments}], 1
    moved = sum(len(d.get("items") or []) for d in days if isinstance(d, dict))
    if moved != len(segments):
        # 少搬运就等于丢条目，这种"整理"比不分块更糟，退回原样。
        return [{"day": 1, "items": segments}], 1
    out = []
    for i, d in enumerate(days, start=1):
        items = [it for it in (d.get("items") or []) if isinstance(it, dict)]
        if items:
            out.append({"day": int(d.get("day") or i), "theme": d.get("theme"), "items": items})
    return (out or [{"day": 1, "items": segments}]), 1


def chunk_days(segments: list[dict], needs: dict, thinking: str, quiet: bool) -> tuple[list, int]:
    """按模型给的 day 分块。题目明说 N 天、条目却全挤在 day=1 时判为分块失败，
    补一轮 LLM 按天整理（第 9 节的决定）。返回 (days, 额外 LLM 次数)。"""
    days = sorted({s.get("day") for s in segments if isinstance(s.get("day"), int)})
    if len(days) > 1:
        return [{"day": d,
                 "theme": next((s.get("theme") for s in segments
                                if s.get("day") == d and s.get("theme")), None),
                 "items": [s for s in segments if s.get("day") == d]} for d in days], 0
    expected = int(needs.get("days") or 0)
    if expected <= 1 or len(segments) < 8:
        # 单日题本就该是一个 day；条目太少时也值不回一轮 LLM。
        return [{"day": 1, "theme": None, "items": segments}], 0
    return assign_days(segments, needs, thinking, quiet)


def weather_rows(ext: dict) -> tuple[list[dict], str]:
    for _tool, _args, payload in ext["results"]:
        if not payload.get("ok"):
            continue
        for fc in (payload.get("data") or {}).get("forecasts") or []:
            rows = [{"date": c.get("date"), "day": c.get("dayweather"),
                     "night": c.get("nightweather"),
                     "temp_range": f"{c.get('nighttemp')}~{c.get('daytemp')}℃",
                     # daypower 本身就是 "1-3" 这种区间，再拼 nightpower 会变成 "1-3-1-3级"。
                     "wind": (c.get("daypower") if c.get("daypower") == c.get("nightpower")
                              else f"{c.get('daypower')}-{c.get('nightpower')}") + "级"}
                    for c in fc.get("casts") or []]
            if rows:
                return rows, f"高德天气预报，发布 {fc.get('reporttime')}，只对当场有效"
    return [], "未取得天气预报"


def _num(v):
    """高德给的是字符串（"55.00"），模型给的是数字或 null；统一成 int/float/None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def normalize_groups(got) -> list[dict]:
    """把模型输出归一成 [{day, theme, items[]}]。两种形态都要认：
    新 prompt 的 {"days":[{day,theme,items}]}，以及旧/偷懒的平铺数组。"""
    if isinstance(got, dict) and isinstance(got.get("days"), list):
        groups = []
        for i, d in enumerate(got["days"], start=1):
            if not isinstance(d, dict):
                continue
            day_no = d["day"] if isinstance(d.get("day"), int) else i
            items = [s for s in (d.get("items") or []) if isinstance(s, dict) and s.get("name")]
            for s in items:
                s.setdefault("day", day_no)
            if items:
                groups.append({"day": day_no, "theme": d.get("theme"), "items": items})
        return groups
    if isinstance(got, list):
        segs = [s for s in got if isinstance(s, dict) and s.get("name")]
        if not segs:
            return []
        days = sorted({s["day"] if isinstance(s.get("day"), int) else 1 for s in segs})
        return [{"day": d, "theme": None,
                 "items": [s for s in segs
                           if (s["day"] if isinstance(s.get("day"), int) else 1) == d]}
                for d in days]
    return []


def call_failures(ext: dict) -> list[dict]:
    """第 5 节的失败语义：哪条外部调用没成、为什么，原样带进 final_plan 给前端标出来，
    不静默吞。补查轮的结果也在 ext["results"] 里，所以这里天然是全量。"""
    return [{"tool": tool,
             # 紧凑分隔符 + 200 字上限：按 90 字硬切会把 args 的 JSON 字符串截成半截（实测），
             # 前端拿到的是坏 JSON，读不出参数。
             "args": json.dumps(args, ensure_ascii=False, separators=(",", ":"))[:200],
             "error_code": payload.get("error_code"),
             "error_msg": str(payload.get("error_msg") or "")[:140]}
            for tool, args, payload in ext["results"] if not payload.get("ok")]


def route_quality(rows: list[dict]) -> dict:
    """当天这一串点实走的直线距离，和它到最短开放路径的比值（折返比）。
    比值 >1.25 就写一句 route_note——它是验收指标，不做否决：FloatTrip 把绕行比
    只当诊断是有道理的，纯按最短距离排会牺牲用餐与开放时间那几笔账。
    同地点先合并（相距 ≤200 m）：南塘老街在同一天里既是午餐又是一站，
    两条 0 km 边会把比值算虚。"""
    kept: list[dict] = []
    for r in rows:
        loc = r.get("location")
        if not loc:
            continue
        p = {"lon": loc.get("lng"), "lat": loc.get("lat")}
        if any(km_between(p, q) <= 0.2 for q in kept):
            continue
        kept.append(p)
    if len(kept) < 3:
        return {"route_km": round(sum(km_between(kept[i], kept[i + 1])
                                      for i in range(len(kept) - 1)), 1),
                "detour_ratio": None, "stops": len(kept)}
    idx = {str(i): p for i, p in enumerate(kept)}
    walked = path_km(list(idx), idx)
    # 分母取"端点自由的最短开放路径"（open_path 不给 start 时会试遍起点）。
    # 它仍是启发式下界，不是精确解——折返比只用来当验收指标，不当否决门。
    best = path_km(open_path(list(idx), idx), idx)
    ratio = round(walked / best, 2) if best > 0.05 else None
    note = (f"有折返：实走 {walked:.1f} km，最短可达 {best:.1f} km"
            if ratio and ratio > 1.25 else None)
    return {"route_km": round(walked, 1), "detour_ratio": ratio,
            "route_note": note, "stops": len(kept)}


LEG_TOPUP_CAP = 10        # 终稿补腿的条数上限：direction 是个人 key 日配额 5000 的便宜接口


def plan_leg_topup(text: str, pois: dict, legs: list[dict], adcode: str | None,
                   city: str, cap: int = LEG_TOPUP_CAP) -> list[tuple[str, dict]]:
    """终稿成形后，把交通段自己声明、却没测过的端点对补成 direction 请求。

    `pick_legs` 的链是在方案成形之前按台账最近邻挑的，模型自己插进来的餐饮/住宿与
    "从到达枢纽出发的那一段"从来不在那条链里——宁波实测 11 条交通段有 5 条根本没发过请求，
    前端只能显示"路线待实测"。
    配对直接用交通段写的"甲→乙"，走 `match_leg` 同一套 `resolve_ref` 解析——
    测的与回查的必须是一套口径。改成按"非交通条目的相邻对"补实测过一次，
    结果模型声明 `宁波站→天一阁·月湖`、补腿却只测了中间夹着餐厅的两段，照样缺线。
    跨城腿两端都在台账里时照旧会补（⑧的禁令已删），对侧没坐标就自然补不到。
    """
    by_coord = {p["coord"]: p for p in pois.values() if p.get("coord")}
    seen = {(l["a_coord"], l["b_coord"]) for l in legs}
    out: list[tuple[str, dict]] = []
    for seg in (s for g in normalize_groups(extract_json(text)) for s in g["items"]):
        if len(out) >= cap:
            break
        if (seg.get("type") or "attraction") != "transit":
            continue
        parts = [p.strip() for p in str(seg.get("name") or "").split("→")]
        if len(parts) != 2:
            continue
        a, b = resolve_ref(parts[0], pois), resolve_ref(parts[1], pois)
        pa_, pb_ = by_coord.get(a), by_coord.get(b)
        if not a or not b or a == b or (a, b) in seen or not pa_ or not pb_:
            continue
        seen.add((a, b))
        # mode 与主波同一口径：2.5 km 内算步行，否则公交（city1/city2 只认 adcode）。
        out.append(("get_amap_direction",
                    {"origin": a, "destination": b,
                     "mode": "walking" if km_between(pa_, pb_) <= 2.5 else "transit",
                     "city1": pa_.get("adcode") or adcode or city,
                     "city2": pb_.get("adcode") or adcode or city}))
    return out


def build_plan_json(needs: dict, question: str, text: str, ext: dict, pages: list,
                    date_used: str, llm_calls: int, seconds: float,
                    thinking: str, quiet: bool) -> dict:
    plan_id = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}_{needs.get('destination_city') or 'plan'}"
    got = extract_json(text)
    groups = normalize_groups(got)
    if not groups:
        return {"plan_id": plan_id, "question": question, "degraded": True,
                "markdown": text,
                "reason": "阶段 C 没给出可解析的 JSON，前端按 Markdown 原文渲染",
                "sources": {"wiki_pages": pages, "external_calls": ext["external_count"],
                            "llm_calls": llm_calls, "seconds": round(seconds, 1),
                            "failures": call_failures(ext)}}
    legs = leg_index(ext)
    claimed: set[tuple[str, str]] = set()   # 一条实测折线只能分给一段交通
    forecast, weather_note = weather_rows(ext)
    extra_llm = 0
    expected = int(needs.get("days") or 0)
    flat = [s for g in groups for s in g["items"]]
    if expected > 1 and len(groups) == 1 and len(flat) >= 8:
        # 题目要 N 天，模型却全塞进一天：按第 9 节的决定补一轮 LLM 整理，而不是降级成单 day。
        try:
            groups, extra_llm = chunk_days(flat, needs, thinking, quiet)
        except Exception as exc:   # 补轮挂了不影响出方案，退回原分组
            groups, extra_llm = [{"day": 1, "theme": None, "items": flat}], 0
            print(f"    · 分块补轮失败，沿用原分组：{type(exc).__name__}: {exc}", file=sys.stderr)

    start = date_used
    origin_token = ((needs.get("origin_city") or needs.get("origin") or "")
                    if needs.get("intercity") else "")[:2]
    out_days = []
    for d in groups:
        items = []
        for seg in d["items"]:
            src = str(seg.get("source") or "none")
            cost = _num(seg.get("cost"))
            row = {"type": seg.get("type") or "attraction", "name": seg["name"],
                   "start_time": seg.get("start_time"), "end_time": seg.get("end_time"),
                   "transport": seg.get("transport"), "cost": cost,
                   "tip": seg.get("tip"), "photo": None, "path": None,
                   "path_status": None,
                   "location": None, "rating": None, "open_time": None,
                   "verified": src in ("wiki", "amap"),
                   "source": src if src in ("wiki", "amap", "metaso") else "none"}
            # 交通段不做 POI 台账回查：它的 name 是"甲→乙"，模糊匹配会命中终点，
            # 于是给一张交通卡片贴上终点的照片与开放时间。地理信息由 path 承担。
            if row["type"] == "transit":
                poi, exact = None, False
            else:
                poi, exact = ledger_lookup(row["name"], ext["pois"])
            if poi:
                row["location"] = {"lng": float(poi["lon"]), "lat": float(poi["lat"])}
                row["rating"] = _num(poi.get("rating"))
                row["open_time"] = useful_hours(row["type"], poi.get("opentime"))
                # 照片只在精确命中时给：卡片上贴错地方的图比空白更糟（前端按 null 降级）。
                row["photo"] = poi.get("photo") if exact else None
                # business.cost 对餐饮/住宿是人均消费（能用），对景点是游客综合消费（不能用）；
                # 沈园那次就是被这条回填塞进过 163 元，票价一律只认模型从摘要里取到的。
                if (exact and row["cost"] is None and row["type"] in ("meal", "hotel")
                        and _num(poi.get("price") or poi.get("cost")) is not None):
                    row["cost"] = _num(poi.get("price") or poi.get("cost"))
                    row["source"], row["verified"] = "amap_poi", True
                elif src == "wiki":
                    row["source"], row["verified"] = "wiki+amap", True
            leg = match_leg(row["name"], legs, ext["pois"], claimed) if row["type"] == "transit" else None
            if leg:
                claimed.add((leg["a_coord"], leg["b_coord"]))
                row["path"] = leg["path"]
                row["verified"] = True
                row["source"] = "amap_direction"
                if row["cost"] is None and leg["fee"] is not None:
                    row["cost"] = leg["fee"]
            if row["type"] == "transit":
                # 前端要分得开"这条没测到"和"城际段本来就不实测折线"，
                # 否则用户看到的都是"路线待实测"，以为又是连线丢了。
                row["path_status"] = ("measured" if leg else
                                      "cross_city" if origin_token and origin_token in row["name"]
                                      else "missing")
            normalize_people_tip(row)
            items.append(row)
        date = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=d["day"] - 1)).strftime("%Y-%m-%d")
        out_days.append({"day": d["day"], "date": date, "theme": d.get("theme"),
                         "timeline": items, **route_quality(items)})

    return {
        "plan_id": plan_id,
        "question": question,
        "destination": needs.get("destination_city"),
        "origin": needs.get("origin"),
        "days_count": len(out_days),
        "start_date": start,
        "end_date": out_days[-1]["date"],
        "budget_yuan": needs.get("budget_yuan") or 0,
        "weather_forecast": forecast,
        "weather_note": weather_note,
        "sources": {"wiki_pages": pages, "external_calls": ext["external_count"],
                    "llm_calls": llm_calls + extra_llm, "seconds": round(seconds, 1),
                    "failures": call_failures(ext)},
        "days": out_days,
        "origin_point": origin_point(needs, ext["pois"]),
        "degraded": False,
    }


def resolve_start_date(question_date: str | None, needs: dict) -> str:
    """天气与"班次待核实"只对当天有效，所以 start_date 必须由请求给出或按明天推算，
    绝不从缓存或 needs 里的怪值沿用（第 8 节）。"""
    for cand in (question_date, needs.get("date")):
        if cand and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(cand)):
            try:
                datetime.strptime(str(cand), "%Y-%m-%d")
                return str(cand)
            except ValueError:
                continue
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def run_plan(question: str, *, date: str | None = None, needs: dict | None = None,
             thinking: str = "off", max_external: int = 0, max_extra: int = -1,
             facts: list[str] | None = None, json_mode: bool = False,
             skip_llm: bool = False, dry_run: bool = False, quiet: bool = False,
             turns: list[str] | None = None, clarify_first: bool = True,
             on_stage=None) -> dict:
    """阶段 A→F 的编排。CLI 与 webapp 共用这一份，调用方不要再复制一遍流程。

    needs 传入即跳过阶段 A（省 1 次 LLM，用于分段计时与调试）。
    skip_llm=True 只跑到阶段 B，把注入 prompt 的上下文带回来，一次模型都不调。
    dry_run=True 到"取数清单"为止：跑阶段 A（1 次 LLM 抽需求）与本地 wiki，
    然后原样返回将要发出的高德/秘塔请求列表，**一条外部请求都不发**（web 的离线预演）。
    json_mode=True 时阶段 C 换成 SYSTEM_PLAN_JSON，并多返回一个 plan（final_plan dict）。
    on_stage(一行进度文本) 给 web 的轮询用，每个阶段结束时回调一次。
    """
    t_start = time.perf_counter()
    trace: list[str] = []
    llm_calls = 0
    facts = list(facts or [])

    def say(line: str) -> None:
        if not quiet:
            print(line, file=sys.stderr)

    def mark(line: str) -> None:
        trace.append(line)
        say("   " + line)
        if on_stage:
            try:
                on_stage(line)
            except Exception as exc:   # 进度回调是附加信息，挂了不能把规划带崩
                say(f"   [on_stage 异常 {type(exc).__name__}: {exc}]")

    if needs is not None:
        # 调用方（web 的澄清会话）给的 needs 可能整块漏字段，先补齐再往下走。
        needs = {**NEEDS_DEFAULTS, **needs}
        mark("A 需求抽取   由调用方提供，省掉 1 次 LLM")
    elif clarify_first:
        # 澄清与抽取是同一次 LLM：题目写全了就直接往下走，不多花时间。
        clr = clarify([question, *(turns or [])], date, thinking)
        llm_calls += 1
        mark(f"A 澄清+抽取  LLM {clr['seconds']:.1f}s  in={clr['in_tokens']} "
             f"out={clr['out_tokens']} reasoning={clr['reasoning_chars']}字  "
             f"缺 {len(clr['missing'])} 项{'' if clr['ready'] else '：' + '、'.join(clr['missing'])}")
        if not clr["ready"]:
            return {"stage": "clarify", "question": question, "turns": list(turns or []),
                    "missing": clr["missing"], "questions": clr["questions"],
                    "filled": clr["filled"], "needs": {**NEEDS_DEFAULTS, **clr["needs"]},
                    "llm_calls": llm_calls,
                    "seconds": time.perf_counter() - t_start, "trace": trace}
        needs = {**NEEDS_DEFAULTS, **clr["needs"]}
    else:
        needs = extract_needs(question, date, thinking)
        llm1 = needs.pop("_llm")
        llm_calls += 1
        mark(f"A 需求抽取   LLM {llm1['seconds']:.1f}s  in={llm1['in_tokens']} "
             f"out={llm1['out_tokens']} reasoning={llm1['reasoning_chars']}字")
    start_date = resolve_start_date(date, needs)
    say(f"   需求：{json.dumps(needs, ensure_ascii=False)}")

    wiki_text, pages = wiki_bundle(needs)
    mark(f"B1 知识库     本地读 {len(pages)} 页，0 次外部调用")

    if dry_run:
        # 到清单为止：天气与路段要等 POI 坐标回来才能拼，这里按口径写清"之后还会发什么"。
        calls = plan_wave1(needs, wiki_text, max_external)
        poi_n = sum(1 for t, _ in calls if t == "get_amap_poi_search")
        mark(f"B2 离线预演   将发 {len(calls)} 次外部调用（POI {poi_n} + 秘塔 {len(calls) - poi_n}），"
             f"之后按坐标再发 1 次天气 + 多日题 1 次住宿周边搜 + 路段若干；本次一条都没发")
        return {"needs": needs, "start_date": start_date, "wiki_pages": pages,
                "wiki_text": wiki_text, "ext": None, "context": "", "draft": "",
                "final": "", "gap": None, "trace": trace, "llm_calls": llm_calls,
                "external_calls": 0, "planned_calls": [{"tool": t, "args": a} for t, a in calls],
                "seconds": time.perf_counter() - t_start}

    ext = gather_external(needs, wiki_text, max_external, quiet=quiet, facts=facts)
    mark(f"B2 外部取数   {ext['external_count']} 次（并行两波）+ 路段 {len(ext['legs'])} 条")
    context = build_context(needs, wiki_text, ext, facts)

    if skip_llm:
        return {"needs": needs, "start_date": start_date, "wiki_pages": pages,
                "wiki_text": wiki_text, "ext": ext, "context": context, "draft": "",
                "final": "", "gap": None, "trace": trace, "llm_calls": llm_calls,
                "external_calls": ext["external_count"],
                "seconds": time.perf_counter() - t_start}

    system = SYSTEM_PLAN_JSON if json_mode else SYSTEM_PLAN
    llm2 = llm([{"role": "system", "content": system},
                {"role": "user", "content": context + f"\n\n【题目】{question}"}],
               max_tokens=2400, thinking=thinking)
    llm_calls += 1
    mark(f"C 初稿        LLM {llm2['seconds']:.1f}s  in={llm2['in_tokens']} "
         f"out={llm2['out_tokens']} reasoning={llm2['reasoning_chars']}字")
    draft, final = llm2["text"], llm2["text"]
    gap_info: dict | None = None

    if max_extra != 0:
        # 补查轮也要在软上限内：剩多少额度就最多补多少条，额度见底就不补。
        gap_limit = max_extra
        if max_external and max_external > 0:
            left = max_external - ext["external_count"]
            if left <= 0:
                mark("D 补查清单     外部调用已达软上限 "
                     f"{max_external} 次，跳过补查轮（省 1 次 LLM）")
                gap_limit = 0
            else:
                gap_limit = left if max_extra < 0 else min(max_extra, left)
        if gap_limit:
            gap_info = gap_round(context, question, ext["pois"], needs["destination_city"],
                                 ext.get("adcode"), gap_limit, thinking, quiet=quiet, draft=draft)
            g = gap_info["llm"]
            llm_calls += 1
            mark(f"D 补查清单     LLM {g['seconds']:.1f}s 发 {len(gap_info['calls'])} 次调用"
                 f"（清单 {len(gap_info['asks'])} 条 + 代码必查 {gap_info['forced']} 条，上限 {gap_limit}） "
                 f"reasoning={g['reasoning_chars']}字")
            if gap_info["calls"]:
                collect_pois(gap_info["results"], ext["pois"], needs["destination_city"])
                ext["results"] = ext["results"] + gap_info["results"]
                ext["external_count"] += len(gap_info["results"])
                context = build_context(needs, wiki_text, ext, facts)
                llm3 = llm([{"role": "system", "content": system},
                            {"role": "user", "content": context + f"\n\n【题目】{question}"
                             + f"\n\n【初稿，按补查到的数据改写它，别重写结构】\n{draft}"}],
                           max_tokens=2400, thinking=thinking)
                llm_calls += 1
                mark(f"E 终稿        LLM {llm3['seconds']:.1f}s  in={llm3['in_tokens']} "
                     f"out={llm3['out_tokens']} reasoning={llm3['reasoning_chars']}字")
                final = llm3["text"]
            else:
                mark("E 终稿        补查清单为空或无可执行项，直接采用初稿")

    total = time.perf_counter() - t_start
    out = {"needs": needs, "start_date": start_date, "wiki_pages": pages, "wiki_text": wiki_text,
           "ext": ext, "context": context, "draft": draft, "final": final,
           "gap": gap_info, "trace": trace, "llm_calls": llm_calls,
           "external_calls": ext["external_count"], "seconds": total}
    if json_mode:
        # F 补腿：终稿的相邻关系此时才确定，主波那条按台账铺的链盖不住模型自己插进去的
        # 餐宿与次日首站。放在 build_plan_json 之前，折线才回查得到。
        topup = plan_leg_topup(final, ext["pois"], leg_index(ext), ext.get("adcode"),
                               needs["destination_city"])
        if topup:
            results = run_calls(topup, quiet)
            ext["results"] = ext["results"] + results
            ext["external_count"] += len(topup)
            out["external_calls"] = ext["external_count"]
        mark(f"F 补腿         发 {len(topup)} 条 direction"
             f"（上限 {LEG_TOPUP_CAP}，跨城腿不补）")
        out["plan"] = build_plan_json(needs, question, final, ext, pages, start_date,
                                      llm_calls, total, thinking, quiet)
        if gap_info:
            # 补查轮真正发出去的清单要留在产物里：它发的是**初稿**上的名字，
            # 终稿换了条目就查错对象（实测有一条去查"宁波站 一晚 房价"），
            # 不留痕就没法回答"这家酒店是没查还是查不到"，只能重跑一遍。
            out["plan"]["sources"]["gap_asks"] = [
                {"tool": t, "args": json.dumps(a, ensure_ascii=False)[:160]}
                for t, a in gap_info["calls"]]
    return out


def save_plan(plan: dict, out_dir: str = ".cache/plans") -> Path:
    """每次成功的 final_plan 落一份 JSON，历史页读的就是这个目录。"""
    target = REPO_ROOT / out_dir
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{plan['plan_id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def render_markdown(res: dict) -> str:
    """CLI 的 .cache/plan_run.md：与 JSON 模式共用同一份 run_plan 结果。"""
    gap_info = res["gap"]
    gap_block = []
    if gap_info:
        gap_block = ["", "## 补查轮要的数据",
                     "```json", json.dumps(gap_info["asks"], ensure_ascii=False, indent=2), "```"]
        if gap_info["notes"]:
            gap_block += ["", "丢弃说明：", *[f"- {n}" for n in gap_info["notes"]]]
    ext = res["ext"]
    return "\n".join([
        f"# 管线规划结果（{res['seconds']:.0f} s）",
        "",
        "## 耗时与用量",
        *[f"- {t}" for t in res["trace"]],
        f"- 合计 LLM 调用 {res['llm_calls']} 次，外部 API {ext['external_count']} 次，"
        f"总 {res['seconds']:.1f} s",
        "",
        "## 抽取到的需求",
        "```json",
        json.dumps(res["needs"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 方案",
        res["final"],
    ] + gap_block + ([
        "",
        "## 初稿（补查前）",
        res["draft"],
    ] if res["final"] != res["draft"] else []) + [
        "",
        "## 注入模型的上下文",
        "```",
        res["context"],
        "```",
        "",
        "## 知识库命中页",
        *[f"- {p}" for p in res["wiki_pages"]],
    ]) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="顺序管线规划客户端（含补查轮）")
    ap.add_argument("question")
    ap.add_argument("--date")
    ap.add_argument("--thinking", default="off", choices=["off", "low", "on"])
    ap.add_argument("--needs", help="跳过抽取，直接给需求 JSON（用于分段计时与调试）")
    ap.add_argument("--skip-llm", action="store_true", help="只做阶段 A/B，把注入 prompt 的上下文写出来")
    ap.add_argument("--max-external", type=int, default=0,
                    help="0（默认）= 不限外部调用；>0 才按该值截断 POI 检索（③配额已删）")
    ap.add_argument("--max-extra", type=int, default=-1,
                    help="补查轮：-1（默认）不限条数，0 关补查轮（回到 2 次 LLM），N>0 最多 N 条")
    ap.add_argument("--offline", action="store_true", help="不联网、不调模型，只打印计划")
    ap.add_argument("--json", action="store_true",
                    help="阶段 C 走 JSON 模式，方案落 .cache/plans/<plan_id>.json（前端契约）")
    ap.add_argument("--fact", action="append", default=[],
                    help="补充事实（可重复），用于秘塔不可用时把现查到的城际票价喂给阶段 C")
    ap.add_argument("--out", default=".cache/plan_run.md")
    ap.add_argument("--answer", action="append", default=[],
                    help="对上一轮追问的回答（可重复）。终端里直接跑会交互式追问，脚本里用这个续答")
    ap.add_argument("--no-clarify", action="store_true",
                    help="关掉追问闸门：信息不全也按默认值直接规划（旧行为）")
    ap.add_argument("--no-prompt", action="store_true",
                    help="缺信息时不交互式提问，打印问题清单后以退出码 3 结束")
    args = ap.parse_args()

    if args.offline:
        # 不调模型，所以题目里的城市/天数抽不出来：默认用这份夹具，
        # 要看某一题的取数面就配 --needs 一起用。
        needs = json.loads(args.needs) if args.needs else {
            "origin": "出发地", "destination_city": "绍兴", "intercity": True,
            "days": 1, "budget_yuan": 300, "must_visit": [], "themes": ["美食"],
            "need_intercity_ticket": True}
        wiki_text, pages = wiki_bundle(needs)
        calls = plan_wave1(needs, wiki_text, args.max_external)
        print(
            f"[offline] 需求夹具：{json.dumps(needs, ensure_ascii=False)}\n"
            f"[offline] 知识库命中 {len(pages)} 页（上限 {WIKI_MAX_PAGES}）：{pages}\n"
            f"[offline] 取数不设三波顺序与配额（①③已删）：全部 POI 检索 + 1 次秘塔"
            f"（跨城且 wiki 没写高铁票价时）同波并发 → 1 次天气 + 全部主干路段"
            f"（秘塔摘要参与选枢纽）"
            + (f"；--max-external={args.max_external} 截断 POI" if args.max_external
               else "（--max-external 0=不限）") + "\n"
            f"[offline] POI 检索统一带 show_fields={POI_SHOW_FIELDS}"
            f"（前端卡片要照片与开放时间）\n"
            f"[offline] 补查清单原样执行（⑦白名单已删）："
            + ("已关闭" if args.max_extra == 0
               else "不限条数" if args.max_extra < 0 else f"最多 {args.max_extra} 条") + "\n"
            f"[offline] 方案上限 {PLAN_CHAR_LIMIT} 字（⑥保留）；防编造三条已删（④）；"
            f"模型调用 {'4 次' if args.max_extra != 0 else '2 次'}"
            f"（抽取 + 初稿 [+ 缺口 + 终稿]）"
            + ("，JSON 模式再加 0~1 次分块补轮" if args.json else "") + "；"
            f"注入上下文字符 {len(wiki_text)}")
        print("[offline] 第一波清单（POI 与秘塔同波并发；天气与路段要等 POI 坐标回来，不列在这里）：")
        for i, (t_name, t_args) in enumerate(calls, 1):
            print(f"    {i:2d}. {t_name} {json.dumps(t_args, ensure_ascii=False)}")
        return 0

    res = None
    turns: list[str] = list(args.answer)
    while True:
        res = run_plan(
            args.question,
            date=args.date,
            needs=json.loads(args.needs) if args.needs else None,
            thinking=args.thinking,
            max_external=args.max_external,
            max_extra=args.max_extra,
            facts=args.fact,
            json_mode=args.json,
            skip_llm=args.skip_llm,
            turns=turns,
            clarify_first=not args.no_clarify,
        )
        if res.get("stage") != "clarify":
            break

        def ask_list() -> None:
            print("[clarify] 信息不足，没有开始规划。缺：" + "、".join(res["missing"]))
            for q in res["questions"]:
                opt = f"（可答：{' / '.join(str(o) for o in q['options'])}）" if q.get("options") else ""
                print(f"  · {q['slot']}：{q['text']}{opt}")

        if args.no_prompt or not sys.stdin.isatty():
            # 非交互（脚本、CI、agent 调用）就把问题原样吐出去，让调用方带 --answer 重来。
            ask_list()
            print("[clarify] 补齐后重跑同一条命令并加 --answer '你的回答'（或直接在终端里跑，会交互式追问）")
            return 3
        print("[clarify] 信息不足，先回答这几项：")
        answered = 0
        try:
            for q in res["questions"]:
                opt = f" 可答 {' / '.join(str(o) for o in q['options'])}" if q.get("options") else ""
                reply = input(f"  {q['text']}{opt}\n  > ").strip()
                if reply:
                    turns.append(reply)
                    answered += 1
        except EOFError:
            # 终端里 stdin 被关闭（管道、后台跑）时别抛栈，退回打印问题清单。
            if not answered:
                ask_list()
                print("[clarify] （读不到输入，改用清单）带 --answer 重跑即可续答")
                return 3
        if not answered:
            print("[clarify] 一条都没答，停在这里。", file=sys.stderr)
            return 3

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.skip_llm:
        out_path.write_text("\n".join([f"# 注入模型的上下文（{res['seconds']:.1f} s）", "",
                                       *[f"- {t}" for t in res["trace"]], "", "```",
                                       res["context"], "```"]) + "\n", encoding="utf-8")
        print(f"skip_llm total={res['seconds']:.1f}s external={res['external_calls']} "
              f"context_chars={len(res['context'])} -> {args.out}")
        return 0

    out_path.write_text(render_markdown(res), encoding="utf-8")
    line = (f"total_seconds={res['seconds']:.1f} llm_calls={res['llm_calls']} "
            f"external_calls={res['external_calls']} -> {args.out}")
    if args.json:
        plan = res["plan"]
        saved = save_plan(plan)
        items = [it for d in plan.get("days", []) for it in d["timeline"]]
        line += (f" | plan_id={plan['plan_id']} degraded={plan['degraded']} "
                 f"days={plan.get('days_count')} 条目={len(items)} "
                 f"photo/location/path={sum(1 for i in items if i.get('photo'))}/"
                 f"{sum(1 for i in items if i.get('location'))}/"
                 f"{sum(1 for i in items if i.get('path'))}"
                 " 折返比=" + "/".join(str(d.get("detour_ratio") or "-")
                                    for d in plan.get("days", [])) + f" -> {saved}")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
