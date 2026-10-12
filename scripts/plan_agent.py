"""顺序管线的规划客户端：取数顺序写死，模型只在两端出场，最后再加一轮定向补查。

为什么这么做（实测数据）：Cline + DeepSeek 跑同一道题 156 秒，其中 138 秒是 6 个模型轮次
的往返，而 89% 的解码量是模型在想"下一步调哪个工具"。取数顺序对这类题目其实是确定的，
所以这里写死；但"确定"不等于"够全"，所以末尾留一轮让模型点名要它还缺什么。

阶段：
  A 需求抽取   LLM #1，关思考，小输出
  B1 知识库     本地读 wiki，0 次外部调用
  B2 高德       POI（按 themes/must_visit 全量）→ 天气 → 主干路段
  B3 秘塔       城际车次票价（永远排在高德之后）
  C 初稿        LLM #2，紧凑格式，≤1000 字
  D 补查        LLM #3 只递结构化缺口清单，参数经白名单校验后才发请求（高德先、秘塔后）
  E 终稿        LLM #4，带着补查数据改写初稿

用法:
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" [--date 2026-10-08]
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" --offline   # 只打印调用清单
    .venv/Scripts/python.exe scripts/plan_agent.py "题目" --fact '杭州东→绍兴北 二等座 24 元'
        # --fact 可重复：agent 侧核实过的事实，点名的车站会被选成到达枢纽
    可选：--thinking off|low|on  --max-external 16  --max-extra 5（0 关补查轮）
          --skip-llm  --needs '<JSON>'  --out .cache/plan_run.md
"""

import argparse
import asyncio
import concurrent.futures as cf
import inspect
import json
import os
import re
import sys
import time
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
"day_trip":true,"budget_yuan":300,"themes":["美食"],"must_visit":["校区参观"],
"need_intercity_ticket":true,"date":"YYYY-MM-DD"}"""

# 放宽的三处上限：输出篇幅、wiki 页数、路段腿数。防编造的硬约束（SYSTEM_PLAN 3/4 条）一个字没动。
PLAN_CHAR_LIMIT = 1000      # 阶段 C 的字数上限（原 400）
WIKI_MAX_PAGES = 8          # 注入上下文的 wiki 页数（原 4）
WIKI_MAX_CHARS = 14000      # wiki 块总预算（原 9000）
MAX_LEGS = 6                # 主干路段条数上限（原 3~4，两条被园内子点吃掉）
DEFAULT_MAX_EXTERNAL = 16   # --max-external 默认值（原 9）：10 次 POI + 1 天气 + ≤6 路段 + 1 秘塔

SYSTEM_EXTRACT = (
    "你只做需求抽取，不要规划、不要解释。读用户题目，只输出一个 JSON 对象，字段与含义：\n"
    + EXTRACTION_SCHEMA
    + "\n规则：origin 是出发地原名，origin_city 是它所在的城市；destination_city 只写城市名；"
      "budget_yuan 没有就填 0；need_intercity_ticket 只要 intercity 为 true 就置 true"
      "（不要等题目明说票价）。"
      "不要输出 JSON 以外的任何字符。"
)

SYSTEM_PLAN = (
    "你是行程规划器。依据后面给出的【知识库】【实时数据】输出当天方案，要求：\n"
    "1. 只输出紧凑方案本身：每段一行，形如 'HH:MM-HH:MM 地点 | 交通方式与耗时 | 花费'；\n"
    "2. 严禁复述工具返回内容、严禁解释你的推理、严禁逐条标注来源；\n"
    "3. 城际往返（若 need_intercity）必须出现在方案里，只用给定的车次/车程/票价，不要自己编；"
    "给定的发车时刻只对写它的那个方向有效，反向没有时刻就写'班次待核实'；\n"
    "4. 门票/人均取自给定数据，缺数据就写'待核实'，不要用常识补；"
    "没有对应【路段】数据的两点之间，不要写具体线路与耗时，一律'交通待核实'；\n"
    "5. 预算分两行：'已核实小计：X 元（逐项列出）' 只加给定过的数字；"
    "'未核实项：门票/餐费/市内票价若干' 单列，不要把没数据的项算进小计，也不要用没数据的小计去对比上限；\n"
    f"6. 总长不超过 {PLAN_CHAR_LIMIT} 字：每段一行可以短，但题目要求的内容类别（景点、餐饮、住宿、往返）"
    "都要各自出现，缺数据的那一项写成待核实而不是省略。\n"
)

# 补查轮：模型看完初稿，只允许按结构化清单要数据，不许它自由发挥。
SYSTEM_GAP = (
    "你只做缺口清单，不要改方案、不要解释。给你【题目】【已有数据】【初稿】，"
    "找出初稿里因为缺数据而写'待核实'或明显单薄的地方，按 JSON 输出：\n"
    '{"asks":[{"tool":"get_amap_poi_search|get_amap_direction|get_amap_weather|'
    'metaso_web_search","args":{...},"why":"缺什么"}]}\n'
    "规则：最多 {limit} 条；args 只准用该工具的参数名（POI 用 keywords/city/offset，"
    "direction 用 origin/destination/mode，weather 用 city/extensions，metaso 用 q/size）；"
    "direction 的两个端点写地名即可（管线按 POI→路段→秘塔 的顺序执行，"
    "同批里先补的 POI 会把坐标带进来，解析不到的那条会丢弃并在结果里说明）；"
    "优先补金额最大的那几笔缺口（城际车票、门票、当日住宿），补票价/营业时间用 metaso 的问句，"
    "补地点用 POI；初稿里写成'交通待核实'的相邻两站，只要两个地名都在【已有数据】里，"
    "就用 get_amap_direction 把它们补测出来（mode 用 transit 或 walking）。"
    "不要补主观建议，不要补今天天气之外的实时项。"
    "没有值得补的就输出 {\"asks\":[]}。只输出 JSON。"
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
        hits = unwrap(wiki_mcp.search_pages(kw, limit=6))["data"]["results"]
        for h in hits[:4]:
            if not h["exists"]:
                continue
            stem = h["path"].rsplit("/", 1)[-1].removesuffix(".md")
            prio = 0 if stem == kw or stem.startswith(kw) else 1
            if any(p[2] == h["path"] for p in ranked):
                continue
            ranked.append((prio, order, h["path"]))
            order += 1
    pages = [p for _, _, p in sorted(ranked, key=lambda x: (x[0], x[1]))][:WIKI_MAX_PAGES]
    if not pages:
        return "（知识库无相关页面）", []
    per_extra = 2200
    texts: list[str] = []
    used = 0
    batch = unwrap(wiki_mcp.read_pages(pages))["data"]["pages"]
    for i, p in enumerate(batch):
        meta = p.get("meta") or {}
        warns = "；".join(meta.get("warnings") or [])
        budget = (total_chars - used - per_extra * (len(batch) - i - 1)
                  if i == 0 else per_extra)
        body = (p.get("content") or "")[: max(600, budget)]
        used += len(body)
        texts.append(
            f"### {meta.get('path')}（{meta.get('type')}，valid_until={meta.get('valid_until')}）\n"
            f"{body}\n" + (f"[警告] {warns}\n" if warns else "")
        )
    return "\n\n".join(texts) or "（知识库无相关页面）", pages


def wiki_sights(wiki_text: str, needs: dict, limit: int = 3) -> list[str]:
    """从 wiki 城市页的条目表里取要去的地点名，交给高德换成坐标。

    这是"先 wiki 再高德"真正该长的样子：`{city}景点` 这种泛检索在高德那边返回的是一堆
    公园绿地（成都实测：9 个结果里 7 个是公园），模型照它排的行程全是免费公园，
    武侯祠、杜甫草堂这些 wiki 里明明有的景点反倒没有坐标、写成一串"交通待核实"。
    """
    names: list[str] = []
    for m in re.finditer(r"^\|\s*(?:see|do|eat)\s*\|\s*([^|]{2,26}?)\s*\|", wiki_text, re.M):
        n = m.group(1).strip()
        if n and n not in ("缺", "-") and n not in names:
            names.append(n)
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
    """POI 检索统一带上 show_fields=business，否则 v5 连开放时间都不返回。"""
    args: dict = {"keywords": keywords, "offset": offset, "show_fields": "business"}
    if city:
        args["city"] = city
    return "get_amap_poi_search", args


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
                                        "price": (p.get("biz_ext") or {}).get("price"),
                                        "cost": (p.get("biz_ext") or {}).get("cost") or biz.get("cost"),
                                        "rating": biz.get("rating"),
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


def gather_external(needs: dict, wiki_text: str, max_external: int, quiet: bool,
                    facts: list[str] | None = None) -> dict:
    """三波取数，顺序固定：wiki（本地，调用方已读）→ 高德 POI → 高德天气与路段 → 秘塔。

    秘塔压到最后一波单独发，只补城际车次票价，不参与选枢纽——实测秘塔摘要里
    "绍兴东站/绍兴站"混着出现，子串命中就会选错枢纽、烧掉一条 156 分的上虞方向腿。

    取数面按 themes 与 must_visit 全量铺开：只搜 `{city}景点` 时，题目里的"美食"根本没进检索，
    模型最后那句"绍兴菜午餐"就是凭空写的（成都那次实测：9 次调用里 0 次查过餐厅）。
    """
    city = needs["destination_city"]
    origin = needs.get("origin") or ""
    origin_city = needs.get("origin_city") or ""

    need_ticket = bool(needs.get("intercity") or needs.get("need_intercity_ticket"))         and "高铁" not in wiki_text
    # 天气 1 + 路段 MAX_LEGS + 秘塔 1 是后两波的固定开销，剩下的配额都给 POI
    poi_cap = max(4, max_external - 2 - MAX_LEGS)
    calls: list[tuple[str, dict]] = []
    # 槽位分配（超限时从后往前砍）：必去点 → 车站 → 出发地 → wiki 点名的景点 → 主题词 → 泛景点。
    # 成都实测两件事：只搜 `成都景点` 时高德给回 9 条里 7 条是公园绿地，三日行程全是免费公园；
    # 而把车站与出发地挤到检索词之后，到达枢纽没了、第一公里也测不到，满屏"交通待核实"。
    for kw in (needs.get("must_visit") or [])[:2]:
        calls.append(poi_call(kw if city in kw else f"{city}{kw}", city, 3))
    calls.append(poi_call(f"{city}站", city, 4))
    # 只搜 `{city}站` 拿到的是普速站（成都那次连"成都站(建设中)"都进来了），
    # 高铁到达站基本都叫 `{city}东站/南站/北站`，单独补一次检索。
    calls.append(poi_call(f"{city}高铁站", city, 4))
    if origin and origin != origin_city:
        # 出发地按其所在城市检索；origin 只是一个城市名时不查（"杭州"会捞回杭州大厦、
        # 杭州站(出口)这类无关条目，白吃一个 POI 名额）
        calls.append(poi_call(origin, origin_city or None, 3))
    for name in wiki_sights(wiki_text, needs, limit=3):
        if not any(name in c[1]["keywords"] or c[1]["keywords"] in name for c in calls):
            calls.append(poi_call(name, city, 3))
    for th in (needs.get("themes") or [])[:2]:
        calls.append(poi_call(f"{city}{th}", city, 5))
    calls.append(poi_call(f"{city}景点", city, 10))

    wave1 = run_calls(calls[:poi_cap], quiet)

    pois: dict[str, dict] = {}
    adcode = collect_pois(wave1, pois, city)

    wave2: list[tuple[str, dict]] = []
    if adcode:
        wave2.append(("get_amap_weather", {"city": adcode, "extensions": "all"}))
    leg_slots = min(MAX_LEGS, max_external - len(wave1) - len(wave2) - (1 if need_ticket else 0))
    legs = pick_legs(needs, pois, leg_slots, adcode, " ".join(facts or []))
    for a_name, b_name, mode in legs:
        a, b = pois[a_name], pois[b_name]
        wave2.append(("get_amap_direction",
                      {"origin": a["coord"], "destination": b["coord"], "mode": mode,
                       "city1": a.get("adcode") or city, "city2": b.get("adcode") or city}))

    wave2_results = run_calls(wave2, quiet)

    wave3_results = run_calls(
        [("metaso_web_search", {"q": f"{origin_city or city}到{city} 高铁时刻表 票价", "size": 5})]
        if need_ticket else [], quiet)

    return {
        "pois": pois,
        "adcode": adcode,
        "results": wave1 + wave2_results + wave3_results,
        "legs": legs,
        "external_count": len(wave1) + len(wave2_results) + len(wave3_results),
    }


def run_calls(calls: list[tuple[str, dict]], quiet: bool) -> list:
    out = []
    if not calls:
        return out
    with cf.ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
        futures = [pool.submit(run_one, name, args, quiet) for name, args in calls]
        for f in futures:
            out.append(f.result())
    return out


# --------------------------------------------------------------------------
# 补查轮：模型只准递清单，参数由我们校验，顺序仍然高德在前、秘塔在后
# --------------------------------------------------------------------------

GAP_TOOL_ARGS = {
    "get_amap_poi_search": {"keywords", "types", "city", "location", "radius",
                            "offset", "page", "show_fields"},
    "get_amap_direction": {"origin", "destination", "mode", "city", "city1", "city2",
                           "show_fields"},
    "get_amap_weather": {"city", "extensions"},
    "metaso_web_search": {"q", "scope", "includeSummary", "includeRawContent", "size"},
}
GAP_MODES = {"driving", "walking", "bicycling", "transit"}


def resolve_ref(ref: str, pois: dict) -> str | None:
    """把补查轮写的地点名解析成坐标：先精确，再取"名字互相包含"里最短的那个。"""
    if not isinstance(ref, str):
        return None
    if re.fullmatch(r"\d{2,3}\.\d+,\d{2,3}\.\d+", ref.strip()):
        return ref.strip()
    hits = [n for n in pois if n == ref] or [n for n in pois if ref and (ref in n or n in ref)]
    return pois[min(hits, key=len)]["coord"] if hits else None


def sanitize_gap_asks(asks: list, pois: dict, adcode: str | None, limit: int) -> tuple[list, list[str]]:
    """白名单 + 参数夹取。direction 的端点允许写地点名（执行时先补 POI 再解析），
    但**直接给坐标必须是台账里已有的那个**：模型很乐意编一个经纬度出来，那比"待核实"更糟
    ——它会变成一条看起来实测过的路段。"""
    known_coords = {p["coord"] for p in pois.values()}
    out: list[tuple[str, dict]] = []
    notes: list[str] = []
    for item in asks or []:
        if len(out) >= limit:
            notes.append("达到补查上限，后面的清单丢弃")
            break
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        if tool not in GAP_TOOL_ARGS:
            notes.append(f"工具名不在白名单，丢弃：{tool!r}")
            continue
        raw = item.get("args") or {}
        if not isinstance(raw, dict):
            continue
        args = {k: v for k, v in raw.items() if k in GAP_TOOL_ARGS[tool]}
        why = str(item.get("why") or "")[:60]
        if tool == "get_amap_poi_search":
            if not isinstance(args.get("keywords"), str) or not args["keywords"].strip():
                notes.append(f"POI 缺 keywords，丢弃：{why}")
                continue
            args["offset"] = min(int(args.get("offset") or 6), 10)
            args.setdefault("city", adcode or None)
            args["show_fields"] = "business"
        elif tool == "get_amap_direction":
            bad = None
            for key in ("origin", "destination"):
                ref = str(args.get(key) or "")
                if re.fullmatch(r"\d{2,3}\.\d+,\d{2,3}\.\d+", ref):
                    # 直接给坐标：必须是台账里已有的那个。模型很乐意编一个经纬度出来，
                    # 那比"待核实"更糟——它会变成一条看起来实测过的路段。
                    if ref not in known_coords:
                        bad = f"路段坐标不在台账里（疑似编造）：{why}"
                        break
                elif not ref.strip():
                    bad = f"路段端点为空：{why}"
                    break
                # 端点写地名：此刻可能还不在台账里（同批的 POI 补查会把它带进来），
                # 所以不在这儿判死，留到 gap_round 解析后再决定丢不丢。
            if bad:
                notes.append(bad)
                continue
            if args.get("mode") not in GAP_MODES:
                args["mode"] = "transit"
            if adcode:
                args.setdefault("city1", adcode)
                args.setdefault("city2", adcode)
        elif tool == "get_amap_weather":
            if not str(args.get("city") or "").isdigit():
                args["city"] = adcode or ""
            if not args["city"]:
                notes.append("天气补查缺 adcode，丢弃")
                continue
            args["extensions"] = "all" if args.get("extensions") == "all" else "base"
        elif tool == "metaso_web_search":
            if not isinstance(args.get("q"), str) or not args["q"].strip():
                notes.append(f"秘塔缺 q，丢弃：{why}")
                continue
            args["size"] = min(int(args.get("size") or 5), 5)
            args["includeRawContent"] = False
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if any(json.dumps(a, sort_keys=True, ensure_ascii=False) == key for _, a in out):
            continue
        out.append((tool, args))
    return out, notes


def gap_round(context: str, question: str, pois: dict, city: str, adcode: str | None,
              limit: int, thinking: str, quiet: bool) -> dict:
    """一轮补查：LLM 递清单 → 我们校验 → 按 POI/天气 → 路段 → 秘塔 的顺序执行。

    分三小批而不是一次并行，是为了让路段能用上同批里刚补查到的坐标：模型经常先要
    "武侯祠"这个 POI、再要"宽窄巷子→武侯祠"的通勤，一起发就解析不到坐标（成都实测）。
    asks 为空时一次外部调用都不发。
    """
    res = llm(
        [{"role": "system", "content": SYSTEM_GAP.replace("{limit}", str(limit))},
         {"role": "user", "content": f"【题目】{question}\n\n{context}\n\n"
                                     f"【已有数据（地名 | 坐标）】direction 只能引用这些地名：\n"
                                     + "\n".join(f"  {n} | {p['coord']}" for n, p in list(pois.items())[:40])}],
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
    calls, notes = sanitize_gap_asks(asks, pois, adcode, limit)

    results: list = []
    first = [c for c in calls if c[0] in ("get_amap_poi_search", "get_amap_weather")]
    results += run_calls(first, quiet)
    collect_pois(results, pois, city)          # 新补查到的 POI 进台账，供下面的路段解析

    second = []
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
        second.append((tool, a))
    results += run_calls(second, quiet)

    third = [c for c in calls if c[0].startswith("metaso")]
    results += run_calls(third, quiet)

    return {"llm": res, "asks": asks, "calls": calls, "notes": notes, "results": results}
    """返回 {llm, asks, notes, results}；asks 为空时一次外部调用都不发。"""
    res = llm(
        [{"role": "system", "content": SYSTEM_GAP.replace("{limit}", str(limit))},
         {"role": "user", "content": f"【题目】{question}\n\n{context}\n\n"
                                     f"【可用数据摘要】只列地名与坐标，补查只能引用这些坐标：\n"
                                     + "\n".join(f"  {n} | {p['coord']}" for n, p in list(pois.items())[:40])}],
        max_tokens=700,
        thinking=thinking,
    )
    m = re.search(r"\{.*\}", res["text"], re.S)
    asks = json.loads(m.group(0)).get("asks") if m else None
    if not isinstance(asks, list):
        asks = []
    calls, notes = sanitize_gap_asks(asks, pois, adcode, limit)
    amap = [c for c in calls if not c[0].startswith("metaso")]
    metaso = [c for c in calls if c[0].startswith("metaso")]
    results = run_calls(amap, quiet) + run_calls(metaso, quiet)
    return {"llm": res, "asks": asks, "calls": calls, "notes": notes, "results": results}


def run_one(tool_name: str, args: dict, quiet: bool):
    t0 = time.perf_counter()
    fn = getattr(travel_mcp, tool_name)
    payload = None
    # 秘塔这条是抖动，不是查询写错：同一句"杭州到绍兴 高铁 二等座 票价 车程"、同一组参数，
    # 实测连发 5 次全 ok、紧接着两次都"未找到相关数据"。所以见 NO_RESULT 就原样重发一次；
    # 服务端只缓存成功结果，重试一定是真请求。
    for attempt in range(2 if tool_name.startswith("metaso_") else 1):
        res = fn(**args)
        # metaso_* 是 async：线程池工作线程里没有运行中的 loop，直接 asyncio.run
        if inspect.isawaitable(res):
            res = asyncio.run(res)
        payload = unwrap(res)
        if payload.get("ok") or payload.get("error_code") != "UPSTREAM_NO_RESULT":
            break
        if attempt == 0 and not quiet:
            print(f"    · {tool_name} 无结果，原参数重发第 2 次", file=sys.stderr)
    dt = time.perf_counter() - t0
    if not quiet:
        err = "" if payload.get("ok") else \
            f" err={payload.get('error_code')}:{str(payload.get('error_msg'))[:80]}"
        print(f"    · {tool_name} {json.dumps(args, ensure_ascii=False)[:70]} "
              f"ok={payload.get('ok')} {dt:.1f}s{err}", file=sys.stderr)
    return tool_name, args, payload


def pick_legs(needs: dict, pois: dict, budget: int, adcode: str | None = None,
              hints: str = "") -> list:
    """主干路段确定性选取：出发地→到达枢纽→最近景点→依次最近→回枢纽。不算两两矩阵。

    用 adcode 前 4 位区分本市与外地：出发地在外市才需要那条跨城腿；同名的本市分校区
    （如"杭州电子科技大学绍兴校区"）应当作景点，不能被当成出发地吃掉。
    hints 只接受 --fact 里明确点名的车站（高铁停绍兴北这类信息得人工/外部核过才可信）；
    wiki 正文与秘塔摘要里"绍兴东站""绍兴站"都会被子串命中，实测据此选枢纽会得到一条
    156 分的上虞方向废腿。出发地在外市时也**不**请求"出发地→枢纽"那条 transit 腿：
    高德会给出"地铁 1 号线 > 5 号线 > 轨道交通 1 号线，169 分"这种跨城公共交通方案，
    数值真但不能当城际用，模型照着它编高铁车次与票价（实测两轮都栽在这条腿上）。
    城际那一段交给秘塔车次摘要或 --fact，第一公里宁可留"交通待核实"。
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
    # 车站类条目不能当景点：`成都站(建设中)` 从 stations 里被剔掉后，会作为"景点"混进链条，
    # 于是行程里出现"成都南站→成都站(建设中)"这种腿（实测）。地铁站同理。
    def is_station_like(n: str) -> bool:
        return (n.endswith("站") or "火车站" in n or "地铁站" in n
                or "汽车站" in n or "客运中心" in n or station_junk.search(n))

    sights = [n for n in pois if in_target(n) and n not in origin_out and not is_station_like(n)]
    # 同地点细分要合并（"鲁迅故里-鲁迅故居"相距 72 m、园内"4号别墅"与母点同园区）：
    # 让它们进链条只会白吃路段配额，真实景点之间反而测不到通勤。
    # 光靠名称包含不够——成都实测：`成都大熊猫繁育研究基地` 的园点子点名不含母点全称，
    # 4 条腿里 2 条花在 184 m / 1203 m 的园内挪动上。所以再加一条 400 m 距离判据。
    def same_place(a: str, b: str) -> bool:
        """同地点判定：包含关系，或共享 ≥3 字连续片段。
        3 字是为了躲开"鲁迅故里 / 书圣故里"这种只共享"故里"的真实不同景点，
        同时抓住"成都大熊猫繁育研究基地 / 成都大熊猫博物馆 / 熊猫书店(成都大熊猫基地店)"
        这一窝园内子点——补查轮实测它们把 4 条腿吃成了园内散步。"""
        if a in b or b in a:
            return True
        for n in range(min(len(a), len(b)), 2, -1):
            for i in range(len(a) - n + 1):
                if a[i:i + n] in b:
                    return True
        return False

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
    # 枢纽优先选 --fact 里点名的车站（高铁停绍兴北这类信息只有人工/外部核过才可信）：
    # wiki 正文与秘塔摘要里"绍兴东站""绍兴站"都会被子串命中，实测据此选枢纽会得到一条
    # 156 分的上虞方向废腿。没有明确事实时退回"离主景点最近的站"，比按检索列表顺序取第一个稳。
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
    chain_cap = max(3, min(6, int(budget) if budget else 3))
    while remaining and len(chain) < chain_cap:
        nxt = min(remaining, key=lambda c: dist(chain[-1], c))
        chain.append(nxt)
        remaining.remove(nxt)

    legs = []
    # 出发地与枢纽分属两座城市时，那条 transit 腿语义是错的：高德会给"地铁 1 号线 > 5 号线 >
    # 轨道交通 1 号线，169 分"这种跨城公共交通方案，数值真但不能当城际交通用——模型照着它编高铁。
    # 城际那一段交给秘塔/补充事实的车次票价，市内腿照常保留。
    if hub != chain[0]:
        legs.append((hub, chain[0], "walking" if dist(hub, chain[0]) < 2500 else "transit"))
    for a, b in zip(chain, chain[1:]):
        legs.append((a, b, "walking" if dist(a, b) < 2500 else "transit"))
    if len(chain) > 1 and hub != chain[-1]:
        legs.append((chain[-1], hub, "transit"))
    return legs[: min(4, max(int(budget), 0))]


# --------------------------------------------------------------------------
# 阶段 C
# --------------------------------------------------------------------------

def build_context(needs: dict, wiki_text: str, ext: dict, facts: list[str] | None = None) -> str:
    lines = [f"【需求】{json.dumps({k: v for k, v in needs.items() if not k.startswith('_')}, ensure_ascii=False)}"]
    lines.append(f"【知识库】\n{wiki_text[:WIKI_MAX_CHARS]}")
    if ext["pois"]:
        lines.append("【POI（坐标/开放时间/评分，来自高德 v5；v5 不返回门票价格）】")
        for n, p in list(ext["pois"].items())[:30]:
            price = p.get("price") or p.get("cost") or "未给"
            lines.append(f"  {n} | {p['lon']},{p['lat']} | 票价={price} "
                         f"评分={p.get('rating') or '未给'} | 开放={str(p.get('opentime'))[:70]}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="顺序管线规划客户端（含补查轮）")
    ap.add_argument("question")
    ap.add_argument("--date")
    ap.add_argument("--thinking", default="off", choices=["off", "low", "on"])
    ap.add_argument("--needs", help="跳过抽取，直接给需求 JSON（用于分段计时与调试）")
    ap.add_argument("--skip-llm", action="store_true", help="只做阶段 A/B，把注入 prompt 的上下文写出来")
    ap.add_argument("--max-external", type=int, default=DEFAULT_MAX_EXTERNAL,
                    help=f"第一/二波外部 API 调用上限（默认 {DEFAULT_MAX_EXTERNAL}）")
    ap.add_argument("--max-extra", type=int, default=5,
                    help="补查轮最多再发几次外部调用，0 = 关掉补查轮（回到 2 次 LLM）")
    ap.add_argument("--offline", action="store_true", help="不联网、不调模型，只打印计划")
    ap.add_argument("--fact", action="append", default=[],
                    help="补充事实（可重复），用于秘塔不可用时把现查到的城际票价喂给阶段 C")
    ap.add_argument("--out", default=".cache/plan_run.md")
    args = ap.parse_args()

    t_start = time.perf_counter()
    trace: list[str] = []
    llm_calls = 0

    if args.offline:
        needs = {"origin": "出发地", "destination_city": "绍兴", "intercity": True,
                 "budget_yuan": 300, "must_visit": [], "themes": ["美食"],
                 "need_intercity_ticket": True}
        wiki_text, pages = wiki_bundle(needs)
        print(f"[offline] 知识库命中 {len(pages)} 页（上限 {WIKI_MAX_PAGES}）：{pages}\n"
              f"[offline] 主取数预算 {args.max_external}，三波固定顺序："
              f"POI（必去点→车站→高铁站→出发地→wiki 点名的景点→主题词→泛景点，"
              f"上限 {max(4, args.max_external - 2 - MAX_LEGS)} 次）"
              f"→ 1 次天气 + ≤{MAX_LEGS} 主干路段 → 1 次秘塔（跨城且 wiki 没写高铁票价时）\n"
              f"[offline] 补查预算 {args.max_extra} 次（LLM 递清单，管线校验后执行；0 关闭）\n"
              f"[offline] 方案上限 {PLAN_CHAR_LIMIT} 字；"
              f"模型调用 {'4 次' if args.max_extra else '2 次'}（抽取 + 初稿 [+ 缺口 + 终稿]）；"
              f"注入上下文字符 {len(wiki_text)}")
        return 0

    if args.needs:
        needs = json.loads(args.needs)
        trace.append("A 需求抽取   用 --needs 提供，省掉 1 次 LLM")
    else:
        needs = extract_needs(args.question, args.date, args.thinking)
        llm1 = needs.pop("_llm")
        llm_calls += 1
        trace.append(f"A 需求抽取   LLM {llm1['seconds']:.1f}s  in={llm1['in_tokens']} "
                     f"out={llm1['out_tokens']} reasoning={llm1['reasoning_chars']}字")
        print("   " + trace[-1], file=sys.stderr)
    print(f"   需求：{json.dumps(needs, ensure_ascii=False)}", file=sys.stderr)

    wiki_text, pages = wiki_bundle(needs)
    trace.append(f"B1 知识库     本地读 {len(pages)} 页，0 次外部调用")
    ext = gather_external(needs, wiki_text, args.max_external, quiet=False, facts=args.fact)
    trace.append(f"B2 外部取数   {ext['external_count']} 次（并行两波）+ 路段 {len(ext['legs'])} 条")

    context = build_context(needs, wiki_text, ext, args.fact)

    if args.skip_llm:
        out_path = REPO_ROOT / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join([f"# 注入模型的上下文（{time.perf_counter() - t_start:.1f} s）", "",
                                       *[f"- {t}" for t in trace], "", "```", context, "```"]) + "\n",
                            encoding="utf-8")
        print(f"skip_llm total={time.perf_counter() - t_start:.1f}s external={ext['external_count']} "
              f"context_chars={len(context)} -> {args.out}")
        return 0

    llm2 = llm(
        [{"role": "system", "content": SYSTEM_PLAN},
         {"role": "user", "content": context + f"\n\n【题目】{args.question}"}],
        max_tokens=2400,
        thinking=args.thinking,
    )
    llm_calls += 1
    trace.append(f"C 初稿        LLM {llm2['seconds']:.1f}s  in={llm2['in_tokens']} "
                 f"out={llm2['out_tokens']} reasoning={llm2['reasoning_chars']}字")
    print("   " + trace[-1], file=sys.stderr)
    draft, final = llm2["text"], llm2["text"]
    gap_info: dict | None = None

    if args.max_extra > 0:
        gap_info = gap_round(context, args.question, ext["pois"], needs["destination_city"],
                             ext.get("adcode"), args.max_extra, args.thinking, quiet=False)
        g = gap_info["llm"]
        llm_calls += 1
        trace.append(f"D 补查清单     LLM {g['seconds']:.1f}s 要 {len(gap_info['calls'])} 次调用"
                     f"（清单 {len(gap_info['asks'])} 条，校验后丢弃 {len(gap_info['asks']) - len(gap_info['calls'])} 条）"
                     f" reasoning={g['reasoning_chars']}字")
        print("   " + trace[-1], file=sys.stderr)
        if gap_info["calls"]:
            collect_pois(gap_info["results"], ext["pois"], needs["destination_city"])
            ext["results"] = ext["results"] + gap_info["results"]
            ext["external_count"] += len(gap_info["results"])
            context = build_context(needs, wiki_text, ext, args.fact)
            llm3 = llm(
                [{"role": "system", "content": SYSTEM_PLAN},
                 {"role": "user", "content": context + f"\n\n【题目】{args.question}"
                                  f"\n\n【初稿，按补查到的数据改写它，别重写结构】\n{draft}"}],
                max_tokens=2400,
                thinking=args.thinking,
            )
            llm_calls += 1
            trace.append(f"E 终稿        LLM {llm3['seconds']:.1f}s  in={llm3['in_tokens']} "
                         f"out={llm3['out_tokens']} reasoning={llm3['reasoning_chars']}字")
            print("   " + trace[-1], file=sys.stderr)
            final = llm3["text"]
        else:
            trace.append("E 终稿        补查清单为空或被全部校验丢弃，直接采用初稿")

    total = time.perf_counter() - t_start

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gap_block = []
    if gap_info:
        gap_block = ["", "## 补查轮要的数据",
                     "```json", json.dumps(gap_info["asks"], ensure_ascii=False, indent=2), "```"]
        if gap_info["notes"]:
            gap_block += ["", "校验丢弃说明：", *[f"- {n}" for n in gap_info["notes"]]]
    out_path.write_text("\n".join([
        f"# 管线规划结果（{total:.0f} s）",
        "",
        "## 耗时与用量",
        *[f"- {t}" for t in trace],
        f"- 合计 LLM 调用 {llm_calls} 次，外部 API {ext['external_count']} 次，总 {total:.1f} s",
        "",
        "## 抽取到的需求",
        "```json",
        json.dumps(needs, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 方案",
        final,
    ] + gap_block + ([
        "",
        "## 初稿（补查前）",
        draft,
    ] if final != draft else []) + [
        "",
        "## 注入模型的上下文",
        "```",
        context,
        "```",
        "",
        "## 知识库命中页",
        *[f"- {p}" for p in pages],
    ]) + "\n", encoding="utf-8")
    print(f"total_seconds={total:.1f} llm_calls={llm_calls} external_calls={ext['external_count']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
