"""固定顺序管线的规划客户端：把 agent loop 压成"两次 LLM 调用 + 一批并行取数"。

为什么这么做（实测数据）：Cline + DeepSeek 跑同一道题 156 秒，其中 138 秒是 6 个模型轮次
的往返，而 89% 的解码量是模型在想"下一步调哪个工具"。取数顺序对这类题目其实是确定的，
所以这里写死，模型只在两端各出场一次。

阶段：
  A 需求抽取   LLM #1，关思考，小输出
  B 数据装配   0 次 LLM：wiki 本地读 → 高德并行 → 秘塔最多 1 次
  C 方案生成   LLM #2，关思考，限定紧凑格式

用法:
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" [--date 2026-10-08]
    .venv/Scripts/python.exe scripts/plan_agent.py "题目原文" --offline   # 只打印调用清单
    可选：--thinking off|low|on   --max-external 8   --out .cache/plan_run.md
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

SYSTEM_EXTRACT = (
    "你只做需求抽取，不要规划、不要解释。读用户题目，只输出一个 JSON 对象，字段与含义：\n"
    + EXTRACTION_SCHEMA
    + "\n规则：origin 是出发地原名，origin_city 是它所在的城市；destination_city 只写城市名；"
      "budget_yuan 没有就填 0；need_intercity_ticket 仅在跨城且题目涉及火车票价时置 true。"
      "不要输出 JSON 以外的任何字符。"
)

SYSTEM_PLAN = (
    "你是行程规划器。依据后面给出的【知识库】【实时数据】输出当天方案，要求：\n"
    "1. 只输出紧凑方案本身：每段一行，形如 'HH:MM-HH:MM 地点 | 交通方式与耗时 | 花费'；\n"
    "2. 严禁复述工具返回内容、严禁解释你的推理、严禁逐条标注来源；\n"
    "3. 城际往返（若 need_intercity）必须出现在方案里，用给定的车程与票价，不要自己编；\n"
    "4. 门票/人均取自给定数据，缺数据就写'待核实'，不要用常识补；\n"
    "5. 末尾给一行 '预算合计：X 元 / 上限 Y 元'，只用给定数字相加；\n"
    "6. 总长不超过 400 字。\n"
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

def wiki_bundle(needs: dict) -> tuple[str, list[str]]:
    """本地读，零外部调用。返回注入 prompt 的知识块 + 命中页路径。"""
    city = needs.get("destination_city") or ""
    texts: list[str] = []
    pages: list[str] = []
    for kw in filter(None, [city, needs.get("origin"), *(needs.get("must_visit") or [])]):
        hits = unwrap(wiki_mcp.search_pages(kw, limit=6))["data"]["results"]
        for h in hits[:4]:
            if h["path"] not in pages and h["exists"]:
                pages.append(h["path"])
    pages = pages[:5]
    if pages:
        batch = unwrap(wiki_mcp.read_pages(pages))["data"]["pages"]
        for p in batch:
            meta = p.get("meta") or {}
            warns = "；".join(meta.get("warnings") or [])
            texts.append(
                f"### {meta.get('path')}（{meta.get('type')}，valid_until={meta.get('valid_until')}）\n"
                f"{p.get('content')}\n" + (f"[警告] {warns}\n" if warns else "")
            )
    return "\n\n".join(texts) or "（知识库无相关页面）", pages


def gather_external(needs: dict, wiki_text: str, max_external: int, quiet: bool) -> dict:
    """两波并行：先拿 POI（为了坐标与 adcode），再拿天气与主干路段。"""
    city = needs["destination_city"]
    origin = needs.get("origin") or ""
    origin_city = needs.get("origin_city") or ""
    calls: list[tuple[str, dict]] = [
        ("get_amap_poi_search", {"keywords": f"{city}景点", "city": city, "offset": 8}),
        ("get_amap_poi_search", {"keywords": f"{city}站", "city": city, "offset": 3}),
    ]
    for kw in (needs.get("must_visit") or [])[:1]:
        calls.append(("get_amap_poi_search", {"keywords": kw, "city": city, "offset": 3}))
    if origin:
        # 出发地按其所在城市检索；不知道城市就留空，让高德按全国范围返回
        args = {"keywords": origin, "offset": 3}
        if origin_city:
            args["city"] = origin_city
        calls.append(("get_amap_poi_search", args))

    wave1 = run_calls(calls[:4], quiet)

    pois: dict[str, dict] = {}
    adcode = None
    for tool_name, args, payload in wave1:
        for p in (payload.get("data") or {}).get("pois") or []:
            if not p.get("location"):
                continue
            lon, lat = p["location"].split(",")
            if args.get("city") == city and p.get("adcode"):
                adcode = adcode or p["adcode"]
            pois.setdefault(p["name"], {"coord": f"{lon},{lat}", "lon": lon, "lat": lat,
                                        "adcode": p.get("adcode"),
                                        "price": (p.get("biz_ext") or {}).get("price"),
                                        "cost": (p.get("biz_ext") or {}).get("cost"),
                                        "opentime": p.get("opentime")})

    wave2: list[tuple[str, dict]] = []
    if adcode:
        wave2.append(("get_amap_weather", {"city": adcode, "extensions": "all"}))

    need_ticket = bool(needs.get("need_intercity_ticket")) and "高铁" not in wiki_text
    leg_slots = max_external - len(wave1) - len(wave2) - (1 if need_ticket else 0)
    legs = pick_legs(needs, pois, leg_slots, adcode)
    for a_name, b_name, mode in legs:
        a, b = pois[a_name], pois[b_name]
        wave2.append(("get_amap_direction",
                      {"origin": a["coord"], "destination": b["coord"], "mode": mode,
                       "city1": a.get("adcode") or city, "city2": b.get("adcode") or city}))

    if need_ticket and len(wave1) + len(wave2) < max_external:
        wave2.append(("metaso_web_search",
                      {"q": f"{origin_city or '杭州'}到{city} 高铁 二等座 票价 车程", "size": 3}))

    wave2_results = run_calls(wave2, quiet)
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
    with cf.ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
        futures = [pool.submit(run_one, name, args, quiet) for name, args in calls]
        for f in futures:
            out.append(f.result())
    return out


def run_one(tool_name: str, args: dict, quiet: bool):
    t0 = time.perf_counter()
    fn = getattr(travel_mcp, tool_name)
    res = fn(**args)
    # metaso_* 是 async：线程池工作线程里没有运行中的 loop，直接 asyncio.run
    if inspect.isawaitable(res):
        res = asyncio.run(res)
    payload = unwrap(res)
    dt = time.perf_counter() - t0
    if not quiet:
        print(f"    · {tool_name} {json.dumps(args, ensure_ascii=False)[:70]} "
              f"ok={payload.get('ok')} {dt:.1f}s", file=sys.stderr)
    return tool_name, args, payload


def pick_legs(needs: dict, pois: dict, budget: int, adcode: str | None = None) -> list:
    """主干路段确定性选取：出发地→到达枢纽→最近景点→依次最近→回枢纽。不算两两矩阵。

    用 adcode 前 4 位区分本市与外地：出发地在外市才需要那条跨城腿；同名的本市分校区
    （如"杭州电子科技大学绍兴校区"）应当作景点，不能被当成出发地吃掉。
    """
    origin = needs.get("origin") or ""
    prefix = (adcode or "")[:4]

    def city_of(name: str) -> str:
        return (pois[name].get("adcode") or "")[:4]

    def in_target(name: str) -> bool:
        return not prefix or city_of(name) == prefix

    named = [n for n in pois if origin and origin[:4] in n]
    origin_out = [n for n in named if prefix and city_of(n) != prefix]
    stations = [n for n in pois if (n.endswith("站") or "火车站" in n) and in_target(n)]
    sights = [n for n in pois if in_target(n) and n not in stations and n not in origin_out]
    if not sights:
        return []

    def dist(a, b):
        ax, ay = float(pois[a]["lon"]), float(pois[a]["lat"])
        bx, by = float(pois[b]["lon"]), float(pois[b]["lat"])
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 * 111_000  # 度→米粗算

    must = needs.get("must_visit") or []
    seed = next((s for s in sights if any(m and (m in s or s in m) for m in must)), sights[0])
    hub = stations[0] if stations else seed
    chain = [seed]
    remaining = [s for s in sights if s != seed and s != hub]
    while remaining and len(chain) < 3:
        nxt = min(remaining, key=lambda c: dist(chain[-1], c))
        chain.append(nxt)
        remaining.remove(nxt)

    legs = []
    if origin_out and hub != origin_out[0]:
        legs.append((origin_out[0], hub, "transit"))
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

def build_context(needs: dict, wiki_text: str, ext: dict) -> str:
    lines = [f"【需求】{json.dumps({k: v for k, v in needs.items() if not k.startswith('_')}, ensure_ascii=False)}"]
    lines.append(f"【知识库】\n{wiki_text[:6000]}")
    if ext["pois"]:
        lines.append("【POI（坐标/票价/开放时间，来自高德）】")
        for n, p in list(ext["pois"].items())[:14]:
            lines.append(f"  {n} | {p['lon']},{p['lat']} | 门票={p['price'] or '未给'} "
                         f"人均={p['cost'] or '未给'} | 开放={str(p['opentime'])[:40]}")
    for tool_name, args, payload in ext["results"]:
        d = payload.get("data") or {}
        if payload.get("ok") and d.get("lives"):
            live = d["lives"][0]
            lines.append(f"【天气】{live.get('city')} {live.get('weather')} {live.get('temperature')}℃")
        if payload.get("ok") and d.get("route"):
            r = d["route"]
            summ = []
            for t in (r.get("transits") or [])[:2]:
                c = t.get("cost") or {}
                dur = c.get("duration") or t.get("duration")
                walk = c.get("walking_distance") or t.get("walking_distance") or 0
                seq = " > ".join(bl.get("name", "") for s in (t.get("segments") or [])
                                 for bl in ((s.get("bus") or {}).get("buslines") or []))
                summ.append(f"{int(float(dur or 0)) // 60}分 步行{walk}m {seq[:60]}")
            for pth in (r.get("paths") or [])[:1]:
                cost = pth.get("cost") or {}
                dur = pth.get("time") or cost.get("duration")
                summ.append(f"{int(float(dur or 0)) // 60}分 {pth.get('distance')}m")
            lines.append(f"【路段 {args['origin']}→{args['destination']}（{args.get('mode')}）】" + "；".join(summ))
        if tool_name == "metaso_web_search" and payload.get("ok"):
            lines.append(f"【网页摘要】{json.dumps(d, ensure_ascii=False)[:1200]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="固定顺序管线规划客户端")
    ap.add_argument("question")
    ap.add_argument("--date")
    ap.add_argument("--thinking", default="off", choices=["off", "low", "on"])
    ap.add_argument("--needs", help="跳过抽取，直接给需求 JSON（用于分段计时与调试）")
    ap.add_argument("--skip-llm", action="store_true", help="只做阶段 A/B，把注入 prompt 的上下文写出来")
    ap.add_argument("--max-external", type=int, default=9, help="外部 API 调用上限")
    ap.add_argument("--offline", action="store_true", help="不联网、不调模型，只打印计划")
    ap.add_argument("--out", default=".cache/plan_run.md")
    args = ap.parse_args()

    t_start = time.perf_counter()
    trace: list[str] = []

    if args.offline:
        needs = {"origin": "出发地", "destination_city": "绍兴", "intercity": True,
                 "budget_yuan": 300, "must_visit": [], "need_intercity_ticket": True}
        wiki_text, pages = wiki_bundle(needs)
        print(f"[offline] 知识库命中 {len(pages)} 页：{pages}\n"
              f"[offline] 外部调用上限 {args.max_external}：≤4 次 POI（景点/到站枢纽/必去点/出发地）"
              f" + 1 天气 + ≤3 主干路段 + ≤1 秘塔（仅缺城际票价时）")
        print(f"[offline] 关思考={args.thinking}；模型调用固定 2 次（抽取 + 出方案）；"
              f"注入上下文字符 {len(wiki_text)}")
        return 0

    if args.needs:
        needs = json.loads(args.needs)
        trace.append("A 需求抽取   用 --needs 提供，省掉 1 次 LLM")
    else:
        needs = extract_needs(args.question, args.date, args.thinking)
        llm1 = needs.pop("_llm")
        trace.append(f"A 需求抽取   LLM {llm1['seconds']:.1f}s  in={llm1['in_tokens']} "
                     f"out={llm1['out_tokens']} reasoning={llm1['reasoning_chars']}字")
        print("   " + trace[-1], file=sys.stderr)
    print(f"   需求：{json.dumps(needs, ensure_ascii=False)}", file=sys.stderr)

    wiki_text, pages = wiki_bundle(needs)
    trace.append(f"B1 知识库     本地读 {len(pages)} 页，0 次外部调用")
    ext = gather_external(needs, wiki_text, args.max_external, quiet=False)
    trace.append(f"B2 外部取数   {ext['external_count']} 次（并行两波）+ 路段 {len(ext['legs'])} 条")

    context = build_context(needs, wiki_text, ext)

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
        max_tokens=1200,
        thinking=args.thinking,
    )
    trace.append(f"C 方案生成   LLM {llm2['seconds']:.1f}s  in={llm2['in_tokens']} "
                 f"out={llm2['out_tokens']} reasoning={llm2['reasoning_chars']}字")
    total = time.perf_counter() - t_start

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join([
            f"# 管线规划结果（{total:.0f} s）",
            "",
            "## 耗时与用量",
            *[f"- {t}" for t in trace],
            f"- 合计 LLM 调用 2 次，外部 API {ext['external_count']} 次，总 {total:.1f} s",
            "",
            "## 抽取到的需求",
            "```json",
            json.dumps(needs, ensure_ascii=False, indent=2),
            "```",
            "",
            "## 方案",
            llm2["text"],
            "",
            "## 知识库命中页",
            *[f"- {p}" for p in pages],
        ], encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    print(f"total_seconds={total:.1f} llm_calls=2 external_calls={ext['external_count']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
