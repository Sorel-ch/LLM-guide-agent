"""用中文维基百科刷新 wiki/cities 的 背景/交通 正文。

用法（人工维护；agent 只跑 dry-run / --preview-dir）：
    python scripts/refresh_wikipedia.py                      # API 模式 dry-run
    python scripts/refresh_wikipedia.py --only 宁波 绍兴 丁青 --preview-dir .cache/wp-preview
    python scripts/refresh_wikipedia.py --dump .cache/zhwiki-20261101.xml.bz2
    python scripts/refresh_wikipedia.py --apply --reindex   # 真正写 wiki/ 与 raw/
    python scripts/refresh_wikipedia.py --simplify-all      # 全库正文转简体（dry-run）

约定：
- 只替换 Wikipedia 拥有的 `## 背景` / `## 交通`，POI 表与旅行章节不动；
- 页面保留 `upstream: wikivoyage`，另加 `prose_upstream: wikipedia` 与 wikipedia_revid 等字段；
- 快照写到 raw/wikipedia/<date>_<标题>.md，沿用“只增不改不删”；
- 默认 API 增量；--dump 用于月度全量复核，--offline 则只读转储与本地标题缓存。
"""

from __future__ import annotations

import argparse
import bz2
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_page as wp
import wikivoyage_import as wi

REPO = Path(__file__).resolve().parent.parent
CITIES = REPO / "wiki" / "cities"
RAW_DIR = REPO / "raw" / "wikipedia"
CACHE_PATH = REPO / ".cache" / "wikipedia_titles.json"
API = "https://zh.wikipedia.org/w/api.php"
LATEST_URL = "https://dumps.wikimedia.org/zhwiki/latest/zhwiki-latest-pages-articles.xml.bz2"
USER_AGENT = "qoder-llm-guide-agent-wikipedia-refresh/1.0 (local wiki maintenance)"
BATCH = 50
MIN_BACKGROUND = 300


def api_request(params: dict, retries: int = 6) -> dict:
    params = dict(params)
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    params.setdefault("maxlag", "5")
    body = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    last = None
    for attempt in range(retries):
        wait = min(60, 5 * (attempt + 1))
        try:
            req = urllib.request.Request(
                API, data=body, headers={"User-Agent": USER_AGENT,
                                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if "error" in data:
                last = data["error"]
                time.sleep(wait)
                continue
            return data
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                try:
                    wait = max(wait, int(exc.headers.get("Retry-After", "10")))
                except (TypeError, ValueError):
                    pass
            time.sleep(min(60, wait))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(wait)
    raise RuntimeError("Wikipedia API 请求失败: %s" % last)


def load_pages(only: list[str], limit: int) -> list[dict]:
    wanted = {x.strip() for x in only if x.strip()}
    pages = []
    for path in sorted(CITIES.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fields, _body = wp.parse_frontmatter(text)
        if "upstream" not in fields:
            continue
        hit = re.search(r"^#\s+(.+)$", text, re.M)
        title = hit.group(1).strip() if hit else path.stem.replace("_", "/")
        if wanted and title not in wanted and path.stem not in wanted:
            continue
        pages.append({"path": path, "title": title, "text": text, "fm": fields})
        if limit and len(pages) >= limit:
            break
    return pages


def title_candidates(title: str) -> list[str]:
    raw = [title]
    if "/" in title:
        leaf = title.rsplit("/", 1)[-1]
        raw += [leaf + "区", leaf, leaf + "市", leaf + "县"]
    else:
        raw += [title + "市", title + "县", title + "区"]
    out = []
    for value in raw:
        for variant in (value, wp.to_simplified(value)):
            if variant and variant not in out:
                out.append(variant)
    return out


def _redirect_chain(query: dict, original: str) -> str | None:
    mapping = {}
    for item in query.get("normalized") or []:
        mapping[item["from"]] = item["to"]
    for item in query.get("redirects") or []:
        mapping.setdefault(item["from"], item["to"])
    # 有些响应会同时给 redirects 的 to 再被 normalize，单轮足够覆盖本站标题
    seen = set()
    cur = original
    while cur in mapping and cur not in seen:
        seen.add(cur)
        cur = mapping[cur]
    return cur


def resolve_titles(pages: list[dict], offline: bool = False) -> dict[str, str | None]:
    cache: dict[str, str | None] = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    out: dict[str, str | None] = {}
    pending = []
    for page in pages:
        known = wp.fm_get(page["fm"], "wikipedia_title")
        if known:
            out[page["title"]] = known
        elif page["title"] in cache:
            out[page["title"]] = cache[page["title"]]
        else:
            pending.append(page)

    if offline or not pending:
        return out

    def save_cache() -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    # 每 20 页一批：请求量小、每批落一次缓存，429 中断后重跑能接着走
    chunk_size = 20
    for start in range(0, len(pending), chunk_size):
        chunk = pending[start:start + chunk_size]
        candidates = {p["title"]: title_candidates(p["title"]) for p in chunk}
        flat = []
        for values in candidates.values():
            for value in values:
                if value not in flat:
                    flat.append(value)

        resolved: dict[str, str | None] = {}
        for bstart in range(0, len(flat), BATCH):
            batch = flat[bstart:bstart + BATCH]
            data = api_request({
                "action": "query", "prop": "info|pageprops", "ppprop": "disambiguation",
                "titles": "|".join(batch), "redirects": "1",
            })
            query = data.get("query") or {}
            existing = {p["title"] for p in query.get("pages") or []
                        if not p.get("missing") and p.get("ns") == 0
                        and "disambiguation" not in (p.get("pageprops") or {})}
            for original in batch:
                final = _redirect_chain(query, original)
                if final in existing:
                    resolved[original] = final
            time.sleep(1.0)

        for page in chunk:
            hit = next((resolved[c] for c in candidates[page["title"]] if c in resolved), None)
            out[page["title"]] = hit
            cache[page["title"]] = hit
        save_cache()
        print("标题解析 %d/%d（本批 %d 页）" % (
            min(start + len(chunk), len(pending)), len(pending), len(chunk)), flush=True)
    return out


def _page_map(query: dict, requested: list[str]) -> dict[str, dict]:
    """把请求标题按 normalized/redirects 链映射到最终 page 对象。"""
    pages = {p["title"]: p for p in query.get("pages") or []}
    out = {}
    for original in requested:
        final = _redirect_chain(query, original)
        if final in pages and not pages[final].get("missing"):
            out[original] = pages[final]
    return out


def fetch_pages(titles: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for start in range(0, len(titles), BATCH):
        batch = titles[start:start + BATCH]
        data = api_request({
            "action": "query", "prop": "revisions",
            "rvprop": "ids|timestamp|content", "rvslots": "main",
            "titles": "|".join(batch), "redirects": "1",
        })
        query = data.get("query") or {}
        for original, page in _page_map(query, batch).items():
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            rev = revisions[0]
            content = (((rev.get("slots") or {}).get("main") or {}).get("content"))
            if content:
                out[original] = {
                    "title": page["title"],
                    "revid": rev.get("revid"),
                    "timestamp": rev.get("timestamp"),
                    "text": content,
                }
        time.sleep(1.0)
        print("正文抓取 %d/%d" % (min(start + BATCH, len(titles)), len(titles)), flush=True)
    return out


def iter_dump_pages(dump_path: Path, wanted: set[str]):
    """流式读 MediaWiki 导出：只产出 wanted 里的标题，带 revision id。"""
    with bz2.open(dump_path, "rb") as fh:
        context = ET.iterparse(fh, events=("start", "end"))
        _event, root = next(context)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        for event, elem in context:
            if event != "end" or elem.tag != ns + "page":
                continue
            title_el = elem.find(ns + "title")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            redirect_el = elem.find(ns + "redirect")
            redirect = redirect_el.get("title") if redirect_el is not None else None
            rev = elem.find(ns + "revision")
            revid = None
            text = None
            if rev is not None:
                rid = rev.find(ns + "id")
                revid = rid.text.strip() if rid is not None and rid.text else None
                tel = rev.find(ns + "text")
                if tel is not None and tel.text:
                    text = tel.text
            if title in wanted and not redirect and text:
                yield title, revid, text
            elem.clear()
            root.clear()  # 3.4 GB 转储：逐页清空父节点，避免空元素累积


def snapshot_path(title: str, dump_date: str, revid: str | int | None = None) -> Path:
    suffix = "_r%s" % revid if revid else ""
    return RAW_DIR / ("%s_%s%s.md" % (dump_date, wi.snapshot_name(title), suffix))


def write_snapshot(path: Path, title: str, revid: str | int, dump_date: str, wikitext: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    url = "https://zh.wikipedia.org/wiki/%s" % urllib.parse.quote(title.replace(" ", "_"))
    header = (
        "Source: %s\nOldid: %s\nDump: %s\nFetched: %s\nLicense: CC BY-SA 4.0\n\n"
        % (url, revid, dump_date, date.today().isoformat())
    )
    path.write_text(header + wikitext.replace("\r\n", "\n"), encoding="utf-8")
    return True


def existing_background_len(text: str) -> int:
    _fm, body = wp.parse_frontmatter(text)
    _preamble, sections = wp.parse_sections(body)
    return len(wp.section_body(sections.get("背景", "")))


def process_page(page: dict, info: dict, dump_date: str, apply: bool,
                 preview_dir: Path | None, force: bool) -> tuple[str, str]:
    old = page["text"]
    if wp.fm_get(page["fm"], "prose_upstream") == "manual":
        return "skipped_manual", ""

    prose = wp.extract_wikipedia_sections(info["text"])
    background = prose.get("背景", "")
    if len(background) < MIN_BACKGROUND:
        return "skipped_short", "维基百科背景不足 %d 字" % MIN_BACKGROUND
    old_len = existing_background_len(old)
    if old_len and len(background) < old_len * 0.8:
        return "skipped_short", "新背景 %d 字 < 旧背景 %d 字的 80%%" % (len(background), old_len)

    same_rev = wp.fm_get(page["fm"], "wikipedia_revid") == str(info["revid"])
    same_hash = wp.fm_get(page["fm"], "prose_hash") == wp.sha1_text(
        "\n\n".join(prose[k] for k in sorted(prose)))
    if same_rev and same_hash and not force:
        return "unchanged", ""

    snap = snapshot_path(info["title"], dump_date, info["revid"])
    source = "raw/wikipedia/%s" % snap.name
    new = wp.merge_wikipedia_page(
        old, prose, info["title"], info["revid"], dump_date,
        date.today().isoformat(), source)
    if new == old:
        return "unchanged", ""

    detail = "背景 %d→%d 字" % (old_len, len(background))
    if apply:
        write_snapshot(snap, info["title"], info["revid"], dump_date, info["text"])
        page["path"].write_text(new, encoding="utf-8")
        page["text"] = new
    elif preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / page["path"].name).write_text(new, encoding="utf-8")
    return "changed", detail


def simplify_all(pages: list[dict], apply: bool, preview_dir: Path | None) -> dict[str, int]:
    stats = {"simplified": 0, "unchanged": 0}
    for page in pages:
        new = wp.simplify_page(page["text"])
        if new == page["text"]:
            stats["unchanged"] += 1
            continue
        stats["simplified"] += 1
        if apply:
            page["path"].write_text(new, encoding="utf-8")
            page["text"] = new
        elif preview_dir is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / page["path"].name).write_text(new, encoding="utf-8")
    return stats


def git_is_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                             capture_output=True, text=True, timeout=20)
        return bool(out.stdout.strip())
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", help="zhwiki pages-articles 转储；不给则走 API")
    ap.add_argument("--offline", action="store_true", help="只读转储与标题缓存，不访问 API")
    ap.add_argument("--only", nargs="*", default=[], help="只处理这些页面标题或文件名")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="真正写 wiki/ 与 raw/（默认 dry-run）")
    ap.add_argument("--reindex", action="store_true", help="兼容旧用法；--apply 后总是重建 index")
    ap.add_argument("--simplify-all", action="store_true", help="只做全库正文转简体")
    ap.add_argument("--preview-dir", help="dry-run 时把拟写页面落到这个目录")
    ap.add_argument("--force", action="store_true", help="忽略 revid 相同与 git 工作区不干净")
    ap.add_argument("--show", type=int, default=30)
    args = ap.parse_args()

    if args.apply and not args.force and git_is_dirty():
        print("git 工作区不干净；先提交/清理，或加 --force", file=sys.stderr)
        return 2

    pages = load_pages(args.only, args.limit)
    print("目标页面: %d" % len(pages))
    if not pages:
        return 2

    if args.simplify_all:
        stats = simplify_all(pages, args.apply, Path(args.preview_dir) if args.preview_dir else None)
        print("简体化: %s" % stats)
        if args.apply:
            print("index 重建 %d 条" % wi.reindex_from_disk())
        print("dry-run（未写库）" if not args.apply else "已写库")
        return 0

    resolved = resolve_titles(pages, offline=args.offline)
    missing = [p["title"] for p in pages if not resolved.get(p["title"])]
    if missing:
        print("无对应维基百科条目 %d 个（示例: %s）" % (
            len(missing), ", ".join(missing[:args.show])))

    wanted = {t for t in resolved.values() if t}
    dump_date = date.today().isoformat()
    infos: dict[str, dict] = {}
    if args.dump:
        dump_path = Path(args.dump)
        m = re.search(r"(\d{8})", dump_path.name)
        if m:
            dump_date = "%s-%s-%s" % (m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:8])
        if not dump_path.exists():
            print("转储不存在: %s" % dump_path, file=sys.stderr)
            return 2
        for title, revid, text in iter_dump_pages(dump_path, wanted):
            infos[title] = {"title": title, "revid": revid, "text": text}
    else:
        fetched = fetch_pages(sorted(wanted))
        for title in wanted:
            if title in fetched:
                infos[title] = fetched[title]

    stats: dict[str, int] = {}
    details = []
    preview = Path(args.preview_dir) if args.preview_dir else None
    for page in pages:
        title = resolved.get(page["title"])
        if not title:
            stats["no_article"] = stats.get("no_article", 0) + 1
            continue
        info = infos.get(title)
        if not info:
            stats["no_content"] = stats.get("no_content", 0) + 1
            continue
        status, detail = process_page(page, info, dump_date, args.apply, preview, args.force)
        stats[status] = stats.get(status, 0) + 1
        if detail:
            details.append("%s: %s" % (page["title"], detail))

    print("分类: %s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(stats.items())))
    for line in details[:args.show]:
        print("  " + line)
    if args.apply:
        print("index 重建 %d 条" % wi.reindex_from_disk())
    print("dry-run（未写库；--preview-dir 可看拟写页面）" if not args.apply else "已写库")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
