"""把 raw/ 快照解析成可读的 UTF-8 摘要，供 agent 或人直接 Read。

用法:
    python scripts/snapshot_digest.py <快照或目录>... -o .cache/digest.md

Windows 控制台不是 UTF-8，中文一律别指望 stdout：脚本只往 -o 指定的文件写，
stdout 仅输出 ASCII 的行数。
"""

import argparse
import json
import pathlib
import sys

TRUNC = 600


def clip(text, n=TRUNC):
    s = str(text).replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n}字)"


def fmt_min(seconds):
    try:
        return f"{int(float(seconds)) / 60:.1f}分"
    except (TypeError, ValueError):
        return "?"


def inner_payload(snap):
    """秘塔把业务 JSON 塞在 data.content[0].text 里，正文类返回则是裸 markdown。"""
    content = ((snap.get("data") or {}).get("content") or [{}])[0].get("text")
    if content is None:
        return snap.get("data")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content


def dig_metaso(snap, out):
    body = inner_payload(snap)
    if isinstance(body, str):  # metaso_web_reader
        lines = body.splitlines()
        heads = [ln for ln in lines if ln.startswith("#")]
        out.append(f"  正文 {len(body)} 字 / {len(lines)} 行，标题结构：")
        for h in heads[:12]:
            out.append(f"    {clip(h, 120)}")
        out.append(f"  开头：{clip(body, 400)}")
        return
    if isinstance(body, dict):
        for key in ("webpages", "webPages", "results", "items"):
            rows = body.get(key)
            if isinstance(rows, list):
                out.append(f"  {key}={len(rows)} 条（total={body.get('total')}）")
                for i, it in enumerate(rows, 1):
                    if not isinstance(it, dict):
                        continue
                    out.append(f"  {i}. {clip(it.get('title') or it.get('name'), 90)}")
                    out.append(f"     {it.get('link') or it.get('url')}  {it.get('date') or ''}")
                    if it.get("snippet"):
                        out.append(f"     摘要：{clip(it['snippet'])}")
                    if it.get("summary"):
                        out.append(f"     SUMMARY：{clip(it['summary'], 900)}")
                return
        out.append("  keys=" + ", ".join(body.keys()))


def dig_weather(data, out):
    for live in data.get("lives") or []:
        out.append(
            f"  {live.get('city')} adcode={live.get('adcode')} {live.get('weather')} "
            f"{live.get('temperature')}℃ 湿度{live.get('humidity')}% "
            f"{live.get('winddirection')}{live.get('windpower')}级 报时{live.get('reporttime')}"
        )
    for forecast in data.get("forecasts") or []:
        out.append(f"  {forecast.get('city')} 预报：")
        for day in forecast.get("casts") or []:
            out.append(f"    {day.get('date')} {day.get('dayweather')} "
                       f"{day.get('daytemp')}℃/{day.get('nighttemp')}℃")


def dig_poi(data, out):
    out.append(f"  count={data.get('count')}")
    for i, it in enumerate((data.get("pois") or [])[:8], 1):
        biz = it.get("biz_ext") or {}
        out.append(f"  {i}. {clip(it.get('name'), 60)} | {it.get('location')} | {clip(it.get('address'), 50)}")
        out.append(f"     类型={clip(it.get('type'), 40)} 评分={biz.get('rating')} "
                   f"人均={biz.get('cost')} 开放={clip(it.get('opentime'), 40)} 门票={biz.get('price')}")


def dig_direction(data, out):
    route = (data or {}).get("route") or {}
    for i, path in enumerate(route.get("paths") or [], 1):
        cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
        dur = path.get("time") or cost.get("duration")
        out.append(f"  路线{i}: {int(path.get('distance') or 0)}m，{fmt_min(dur)}"
                   f"（cost={clip(cost, 120)}）")
    transits = route.get("transits") or []
    if transits:
        out.append(f"  公交方案 {len(transits)} 个：")
        for i, t in enumerate(transits, 1):
            cost = t.get("cost") or {}
            lines = [b.get("name", "") for s in (t.get("segments") or [])
                     for b in ((s.get("bus") or {}).get("buslines") or [])]
            out.append(f"  {i}. {fmt_min(cost.get('duration') or t.get('duration'))} "
                       f"距离={t.get('distance')}m 步行={cost.get('walking_distance') or t.get('walking_distance')}m "
                       f"票价={cost.get('ticket_cost')} | {clip(' > '.join(lines), 120)}")
    if not transits and not route.get("paths"):
        out.append("  未识别的 route 结构，keys=" + ", ".join(route.keys()))


def digest(path):
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"\n## {path.name}\n  解析失败：{exc}"]
    name = path.name.lower()
    out = [f"\n## {path.as_posix()}"]
    if not snap.get("ok"):
        out.append(f"  ok=False {snap.get('error_code')} | {clip(snap.get('error_msg'), 200)}")
        return out
    out.append(f"  query={clip(json.dumps(snap.get('query'), ensure_ascii=False), 200)}")
    data = snap.get("data") or {}
    if "metaso" in name or isinstance(data, dict) and "content" in data:
        dig_metaso(snap, out)
    elif data.get("lives") or data.get("forecasts"):
        dig_weather(data, out)
    elif data.get("pois"):
        dig_poi(data, out)
    elif data.get("route"):
        dig_direction(data, out)
    else:
        out.append("  未识别的快照类型，data keys=" + ", ".join(data.keys()))
    return out


def expand(inputs):
    files = []
    for token in inputs:
        p = pathlib.Path(token)
        if p.is_dir():
            files += sorted(p.rglob("*.json"))
        elif p.exists():
            files.append(p)
        else:
            print(f"missing: {token}", file=sys.stderr)
    return files


def main() -> int:
    global TRUNC
    ap = argparse.ArgumentParser(description="raw 快照 → UTF-8 摘要文件")
    ap.add_argument("inputs", nargs="+", help="快照文件或目录（目录递归找 *.json）")
    ap.add_argument("-o", "--out", required=True, help="摘要输出路径（建议 .cache/ 下）")
    ap.add_argument("--chars", type=int, default=TRUNC, help="长字段截断字数")
    args = ap.parse_args()
    TRUNC = args.chars

    files = expand(args.inputs)
    lines = [f"# 快照摘要（{len(files)} 个文件）"]
    for f in files:
        lines += digest(f)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} lines, {len(files)} files -> {out.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
