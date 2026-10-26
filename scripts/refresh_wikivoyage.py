"""用新版 zhwikivoyage 转储刷新 wiki 种子页：只重写上游内容变化了的页。

用法（人工维护，agent 不执行）：
    python scripts/refresh_wikivoyage.py                    # 自动下载 latest 转储，dry-run 报告
    python scripts/refresh_wikivoyage.py --apply          # 真正写库（含改名/删除跟进），末尾自动重建 index
    python scripts/refresh_wikivoyage.py --dump .cache/zhwy.xml.bz2 --offline

前提（2026-10-26 起成立）：wiki/cities 完全由转储派生、无人工编辑，
所以变化页可以无条件覆盖；逐页 diff 的基线是该页 sources 指向的 raw/ 快照
（快照头部 4 行之后就是导入时的原始 wikitext，无需额外清单）。

分类口径（--apply 时全自动，无需人工判断）：
- unchanged：新转储 wikitext 与快照一致 → 整页跳过，不写快照不写页；
- changed  ：wikitext 变化 → 重渲染覆盖 + 新快照；
- renamed  ：上游改成重定向 → 删旧页；目标条目合格（有 listing、不在库内）则按导入路径新建；
- deleted  ：转储里彻底消失 → 删种子页（raw 快照按只增契约保留）；
- new      ：BFS 范围里出现、库里还没有且有 listing → --add-new 时按导入脚本同路径入库。
"""

import argparse
import bz2
import hashlib
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikivoyage_import as wi

REPO = wi.REPO
RAW_DIR = REPO / "raw" / "wikivoyage"
CITIES = REPO / "wiki" / "cities"
LATEST_URL = "https://dumps.wikimedia.org/zhwikivoyage/latest/zhwikivoyage-latest-pages-articles.xml.bz2"
USER_AGENT = "qoder-llm-guide-agent-refresh/1.0 (local wiki maintenance)"


def sha1(text: str) -> str:
    return hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def split_snapshot(text: str) -> tuple:
    """raw 快照 = 头部键值行 + 空行 + 原样 wikitext。返回 (头部字段, 正文)。"""
    head, sep, body = text.partition("\n\n")
    if not sep:
        return {}, text
    fields = dict(re.findall(r"^(\w+): (.*)$", head, re.M))
    return fields, body


def load_seed_pages():
    """从磁盘读种子页现状：H1 标题 → {page, snapshot_path, old_sha}。"""
    out = {}
    for path in sorted(CITIES.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not fm or "upstream: wikivoyage" not in fm.group(1):
            continue
        title = re.search(r"^#\s+(.+)$", text, re.M).group(1).strip()
        snap_rel = re.search(r"^  - (raw/wikivoyage/.+)$", fm.group(1), re.M)
        snap_path = REPO / snap_rel.group(1) if snap_rel else None
        if snap_path is None or not snap_path.exists():
            cand = sorted(RAW_DIR.glob("*_" + wi.snapshot_name(title) + ".md"))
            snap_path = cand[-1] if cand else None
        if snap_path is None:
            out[title] = {"page": path, "snapshot": None, "old_sha": None}
            continue
        _f, body = split_snapshot(snap_path.read_text(encoding="utf-8"))
        out[title] = {"page": path, "snapshot": snap_path, "old_sha": sha1(body)}
    return out


def dump_hashes(dump_path: Path, wanted: set):
    """单遍流式解析：只记录 wanted 里标题的 (sha, redirect)，其余页面即时丢弃。"""
    cur = {}
    for title, _ts, wikitext, red in wi.iter_pages(dump_path):
        if title in wanted:
            cur[title] = (None, red) if red else (sha1(wikitext), None)
    return cur


def fetch_wikitext(dump_path: Path, wanted: set):
    out = {}
    for title, _ts, wikitext, red in wi.iter_pages(dump_path):
        if title in wanted and not red:
            out[title] = wikitext
    return out


def detect_latest_dump_date():
    req = urllib.request.Request(LATEST_URL, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        lm = resp.headers.get("Last-Modified")
    if lm:
        return datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
    return date.today().isoformat()


def download_dump(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    tmp.replace(dest)
    print("已下载 %s（%.1f MB）" % (dest.name, dest.stat().st_size / 1e6))


def write_refreshed(title, wikitext, page_path, dump_date, valid_until):
    rel_snap = "raw/wikivoyage/%s_%s.md" % (dump_date, wi.snapshot_name(title))
    snap_path = REPO / rel_snap
    body, _summary, _n = wi.render_city(
        title, wi.parse_listings(wikitext), wi.section_map(wikitext),
        dump_date, rel_snap, valid_until)
    if not snap_path.parent.exists():
        snap_path.parent.mkdir(parents=True, exist_ok=True)
    if not snap_path.exists():  # raw 只增不改：同日期同题已有快照就不覆盖
        snap_path.write_text(
            "Source: https://zh.wikivoyage.org/wiki/%s\nDump: %s\nFetched: %s\nLicense: CC BY-SA 4.0\n\n%s"
            % (wi.snapshot_name(title).replace("_", "%20"), dump_date,
               date.today().isoformat(), wikitext.replace("\r\n", "\n")),
            encoding="utf-8")
    page_path.write_text(body, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", help="转储路径；缺省自动下载 latest 到 .cache/")
    ap.add_argument("--offline", action="store_true", help="禁止下载，只用已有 --dump")
    ap.add_argument("--apply", action="store_true", help="真正写 wiki/ 与 raw/（默认 dry-run）")
    ap.add_argument("--add-new", action="store_true", help="--apply 时把 BFS 新条目也入库")
    ap.add_argument("--scope", default="china", choices=["china", "off"],
                    help="新条目发现方式（off=不做 BFS，省一遍解析）")
    ap.add_argument("--root", default="中华人民共和国")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--show", type=int, default=30, help="报告里最多列出多少个标题")
    args = ap.parse_args()

    if args.dump:
        dump_path = Path(args.dump)
        m = re.search(r"(\d{8})", dump_path.name)
        d = m.group(1)
        dump_date = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if m else date.today().isoformat()
    else:
        dump_date = detect_latest_dump_date()
        dump_path = REPO / ".cache" / ("zhwikivoyage-%s-latest-pages-articles.xml.bz2" % dump_date.replace("-", ""))
        if not dump_path.exists():
            if args.offline:
                print("离线且缺转储: %s" % dump_path, file=sys.stderr)
                return 2
            download_dump(dump_path)
    if not dump_path.exists():
        print("转储不存在: %s" % dump_path, file=sys.stderr)
        return 2
    valid_until = (datetime.strptime(dump_date, "%Y-%m-%d").date() + timedelta(days=90)).isoformat()
    print("转储: %s (dump_date=%s, valid_until=%s)" % (dump_path, dump_date, valid_until))

    seeds = load_seed_pages()
    print("库内种子页: %d" % len(seeds))
    cur = dump_hashes(dump_path, set(seeds))

    changed, deleted, renamed, missing_snap = [], [], {}, []
    for title, info in seeds.items():
        got = cur.get(title)
        if got is None:
            deleted.append(title)
            continue
        sha, red = got
        if red:
            renamed[title] = red
            continue
        if info["old_sha"] is None:
            missing_snap.append(title)
        elif sha != info["old_sha"]:
            changed.append(title)
    unchanged = len(seeds) - len(changed) - len(deleted) - len(renamed) - len(missing_snap)

    new_candidates = set()
    if args.scope == "china":
        cn, _links = wi.china_titles(dump_path, args.root, args.depth)
        new_candidates = {t for t in cn if t not in seeds}
        print("BFS 范围 %d 个条目，其中库里没有的 %d 个" % (len(cn), len(new_candidates)))

    print("分类: unchanged=%d changed=%d renamed=%d deleted=%d 缺快照=%d" % (
        unchanged, len(changed), len(renamed), len(deleted), len(missing_snap)))
    for label, lst in (("changed", changed), ("deleted", deleted), ("缺快照", missing_snap)):
        if lst:
            print("  %s: %s%s" % (label, ", ".join(sorted(lst)[:args.show]),
                                  " …" if len(lst) > args.show else ""))
    for old, new in sorted(renamed.items()):
        print("  renamed: %s → %s" % (old, new))

    if not args.apply:
        print("dry-run 结束（未写任何文件）；加 --apply 才写库")
        return 0

    need = set(changed) | set(renamed.values()) | (set(new_candidates) if args.add_new else set())
    texts = fetch_wikitext(dump_path, need)
    written = 0
    for title in sorted(changed):
        txt = texts.get(title)
        if txt is None:
            print("  [warn] %s 判为 changed 但转储里取不到正文，跳过" % title)
            continue
        write_refreshed(title, txt, seeds[title]["page"], dump_date, valid_until)
        written += 1
    for old, new in sorted(renamed.items()):
        seeds[old]["page"].unlink()
        new_path = CITIES / (wi.snapshot_name(new) + ".md")
        txt = texts.get(new)
        if new_path.exists():
            print("  renamed: 删旧页 %s（%s 已在库内）" % (old, new))
        elif txt and wi.parse_listings(txt):
            write_refreshed(new, txt, new_path, dump_date, valid_until)
            print("  renamed: %s → 新建 %s，删旧页" % (old, new_path.name))
        else:
            print("  renamed: 删旧页 %s（目标 %s 无 listing，不入库）" % (old, new))
    for title in sorted(deleted):
        seeds[title]["page"].unlink()
        print("  deleted: 删种子页 %s（raw 快照保留）" % seeds[title]["page"].name)
    if args.add_new:
        from wikivoyage_import import render_city  # 新页沿用导入脚本同一渲染路径
        added = 0
        for title in sorted(new_candidates):
            txt = texts.get(title)
            if not txt or len(wi.parse_listings(txt)) < 1:
                continue
            city_path = CITIES / (wi.snapshot_name(title) + ".md")
            if city_path.exists():
                continue
            body, _s, _n = render_city(title, wi.parse_listings(txt), wi.section_map(txt),
                                       dump_date, "raw/wikivoyage/%s_%s.md" % (dump_date, wi.snapshot_name(title)), valid_until)
            write_refreshed(title, txt, city_path, dump_date, valid_until)
            added += 1
        print("新增入库 %d 页" % added)
    print("覆盖刷新 %d 页 -> %s / raw 新快照同数（unchanged 未写任何文件）" % (written, "wiki/cities"))
    print("index 种子区块已按磁盘重建，共 %d 条" % wi.reindex_from_disk())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
