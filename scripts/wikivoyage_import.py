"""把 zhwikivoyage 转储导入本 wiki：每个条目生成 raw 快照 + 一个城市页。

设计取舍（见 AGENTS.md 的"批量导入"章节）：
- 条目内 POI 以表格形式留在城市页里，不逐个建页；需要时再由 Query/Lint 提升为独立 POI 页，
  避免一次性产生数万个文件。
- 坐标原样记录并标注 coord_status: unverified，不调用高德，省 API 配额。
- 每个页面都写 sources 指向 raw 快照，快照头部带 dump 日期与 CC BY-SA 署名。

用法:
    python scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --stats
    python scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china --limit 20 --dry-run
    python scripts/wikivoyage_import.py --dump .cache/zhwy.xml.bz2 --scope china
"""

import argparse
import bz2
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NS = "{http://www.mediawiki.org/xml/export-0.11/}"

# 保守经纬度框：仅用于剔除明显跨境误链。它不能单独做判据，也不能单独封住邻国，
# 所以再加一层"境外空洞"——只列出实测确实泄漏过的区域，不是完备判据。
CHINA_BBOX = (17.0, 54.0, 73.5, 135.1)  # min_lat, max_lat, min_lon, max_lon
FOREIGN_HOLES = (
    ("朝鲜半岛", 33.0, 39.6, 124.0, 130.0),
    ("日本", 30.0, 46.0, 128.0, 146.0),
    ("中南半岛", 5.0, 20.6, 97.0, 109.5),
    ("南亚次大陆", 6.0, 36.0, 60.0, 79.0),
    # 不设"俄罗斯远东"空洞：抚远/佳木斯/鹤岗位于东经 130–135，与哈巴羅夫斯克
    # (48.48N,135.06E) 相距仅数十公里，矩形切不开，实测会误杀 8 个黑龙江城市。
    # 代价是哈巴羅夫斯克作为 1/1719 的已知误收留在库里。
)

# 境外"大区级"条目：一旦穿过它们，其下整片国家/城市都会涌进来。
# 用精确标题匹配，所以"内蒙古""中蒙古"这类中国条目不受影响。
FOREIGN_REGION_TITLES = {
    "蒙古", "朝鲜", "越南", "日本", "韩国", "俄罗斯", "泰国", "柬埔寨", "缅甸",
    "老挝", "马来西亚", "新加坡", "印度", "尼泊尔", "不丹", "斯里兰卡", "巴基斯坦",
    "哈萨克斯坦", "吉尔吉斯斯坦", "塔吉克斯坦", "乌兹别克斯坦", "土库曼斯坦",
    "吉尔吉斯", "阿富汗", "伊朗", "土耳其", "格鲁吉亚", "亚美尼亚", "阿塞拜疆",
    "菲律宾", "印度尼西亚", "文莱", "东帝汶", "孟加拉国", "朝鲜半岛", "蒙古帝國",
    "欧洲", "亚洲", "非洲", "北美洲", "南美洲", "大洋洲", "中东", "中亚", "南亚",
    "东南亚", "东亚",
}


# {{位于}} 只被少数条目填写（全库 88 个取值、约 130 页），当正向判据覆盖不足，
# 但作为反向排除很好用：境外城市往往会自报国家/境外分区。
# 取值来自对转储的全量枚举，不是臆测；"东亚/亚洲" 等中文条目也会用，故不列入。
def outside_china_geo(lat, lon):
    """坐标落在已知境外空洞内则返回空洞名，否则 None。"""
    if lat is None or lon is None:
        return None
    if not (CHINA_BBOX[0] <= lat <= CHINA_BBOX[1] and CHINA_BBOX[2] <= lon <= CHINA_BBOX[3]):
        return "包围盒外"
    for name, la1, la2, lo1, lo2 in FOREIGN_HOLES:
        if la1 <= lat <= la2 and lo1 <= lon <= lo2:
            return name
    return None
# {{位于|<值>}} 判为中国范围的取值集合（省级 + 直辖 + 特别行政区）
CN_LOCATIONS = {
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
    "中华人民共和国", "中国",
    # 维基旅行自用的次区域名（出现在 {{位于}} 里，容易被误判成境外）
    "北臺灣", "南臺灣", "中臺灣", "东臺灣", "新北", "辽中", "辽西", "鲁中", "关中",
    "陕北", "北疆", "南疆", "华东", "华北", "华南", "华中", "西南", "西北", "东北",
    "中国西北", "中国东北", "中国华南", "中国华东", "中国华中", "中国西南", "中国华北",
    "中国台湾", "中国香港", "中国澳门",
}
LISTING_KINDS = ("see", "do", "buy", "eat", "drink", "sleep")
FIELD_RE = re.compile(r"\|\s*(name|alt|address|lat|long|directions|phone|hours|price|content|url)\s*=\s*([^|\n]*)")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
SECTION_RE = re.compile(r"^(={2,4})\s*(.+?)\s*=+\s*$", re.M)
NOISE_RE = re.compile(
    r"^\s*(?:\[\[\s*(?:File|Image)\s*:|thumb\||\{\{\s*(?:Mapframe|Mapshape|Geo圈|PrintDistricts|Regionlist)\b)",
    re.I,
)


def oneline(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", strip_templates(text)).strip()
    return text[:limit].rstrip()

ATTRIBUTION = (
    "本页面部分内容由 Wikivoyage – The free worldwide travel guide 转化而来，"
    "依据 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 授权；"
    "原文作者与修订历史见 source 链接。"
)


def strip_templates(text: str) -> str:
    """去掉 {{...}} 模板与 HTML 注释，保留可读正文（用于城市简介）。"""
    out, depth, buf = [], 0, io.StringIO()
    i = 0
    while i < len(text):
        if text.startswith("<!--", i):
            j = text.find("-->", i)
            i = len(text) if j < 0 else j + 3
            continue
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth = max(0, depth - 1)
            i += 2
            continue
        if depth == 0:
            buf.write(text[i])
        i += 1
    out = buf.getvalue()
    out = re.sub(r"<[^>]+>", "", out)
    out = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", out)
    out = re.sub(r"'''?([^'']+?)'''?", r"**\1**", out)
    out = re.sub(r"''([^']+?)''", r"*\1*", out)

    kept = []
    for line in out.splitlines():
        if NOISE_RE.match(line):
            continue
        m = re.match(r"^\s*(={2,6})\s*(.*?)\s*=+\s*$", line)
        if m:
            head = m.group(2).strip()
            if head:
                kept.append("**%s**" % head)
            continue
        if re.match(r"^\s*[*#;:|]\s*$", line):
            continue
        kept.append(line.rstrip())
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def parse_listings(wikitext: str):
    items = []
    for kind, body in re.findall(r"\{\{\s*(%s)\b(.*?)\}\}" % "|".join(LISTING_KINDS), wikitext, re.S | re.I):
        f = {k.lower(): v.strip() for k, v in FIELD_RE.findall(body)}
        name = clean_inline(re.sub(r"\s+", " ", f.get("name", "")).strip(), 60)
        if not name:
            continue
        try:
            lat = float(f["lat"]) if f.get("lat") else None
            lon = float(f["long"]) if f.get("long") else None
        except ValueError:
            lat = lon = None
        items.append({
            "kind": kind.lower(), "name": clean_inline(name, 60), "lat": lat, "lon": lon,
            "address": clean_inline(f.get("address", ""), 60), "hours": clean_inline(f.get("hours", ""), 50),
            "price": clean_inline(f.get("price", ""), 60), "content": clean_inline(f.get("content", ""), 160),
        })
    return items


def located_in(wikitext: str) -> str:
    m = re.search(r"\{\{\s*位于\s*\|\s*([^|}]+)", wikitext)
    return m.group(1).strip() if m else ""


def article_geo(wikitext: str):
    m = re.search(r"\{\{\s*geo\s*\|\s*(-?[\d.]+)\s*\|\s*(-?[\d.]+)", wikitext, re.I)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def in_china(items, loc: str, ageo) -> bool:
    if loc:
        return loc in CN_LOCATIONS
    if ageo[0] is not None:
        return CHINA_BBOX[0] <= ageo[0] <= CHINA_BBOX[1] and CHINA_BBOX[2] <= ageo[1] <= CHINA_BBOX[3]
    pts = [(i["lat"], i["lon"]) for i in items if i["lat"] is not None and i["lon"] is not None]
    if not pts:
        return False
    inside = [p for p in pts if CHINA_BBOX[0] <= p[0] <= CHINA_BBOX[1] and CHINA_BBOX[2] <= p[1] <= CHINA_BBOX[3]]
    return len(inside) / len(pts) >= 0.6


def iter_pages(dump_path: Path):
    """逐个产出 ns0 页面；redirect_target 非空表示该标题是重定向。"""
    with bz2.BZ2File(dump_path) as fh:
        for _, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag != NS + "page":
                continue
            title = elem.findtext(NS + "title") or ""
            ns_id = elem.findtext(NS + "ns")
            wikitext = ""
            timestamp = ""
            rev = elem.find(NS + "revision")
            if rev is not None:
                wikitext = rev.findtext(NS + "text") or ""
                timestamp = rev.findtext(NS + "timestamp") or ""
            red_elem = elem.find(NS + "redirect")
            redirect_target = red_elem.get("title") if red_elem is not None else None
            elem.clear()
            if ns_id == "0":
                yield title, timestamp, wikitext, redirect_target


REGION_ITEMS_RE = re.compile(r"\|\s*region\d+items\s*=\s*([^\n|(]*)", re.I)


def region_children(wikitext: str):
    """只取 {{Regionlist}} 里显式声明的下级条目。

    用任意 [[内链]] 会把"下一站"里的跨境邻国（韩国、日本…）整片拉进来；
    Regionlist 才是维基旅行自己的行政/旅游层级。
    """
    kids = set()
    for body in REGION_ITEMS_RE.findall(wikitext):
        for m in LINK_RE.finditer(body):
            kids.add(m.group(1).strip().replace("_", " "))
    return kids


def china_titles(dump_path: Path, root: str, max_depth: int):
    """得到 Wikivoyage 自认的"中国目的地"集合。

    混合策略，两半各有一个失败模式，实测都踩过：
    - 只用自由内链 BFS：中国根条目的"下一站"会指向韩国/日本等国家页面，
      穿过它们就把整国城市（全州市、平昌）拉进来了。
    - 只用 {{Regionlist}} 下级：省级条目是用正文内链列市的，
      depth=4 也只到 497 页，三亚这类主城市会被漏掉。
    所以：第一跳只认 Regionlist 声明的省级下级，之后从省页沿内链展开市/区，
    再补上 "父页/子页" 形式的分区条目。
    """
    links: dict[str, set] = {}
    kids: dict[str, set] = {}
    ageos: dict[str, tuple] = {}
    locs: dict[str, str] = {}
    redirects: dict[str, str] = {}
    for title, _ts, wikitext, red in iter_pages(dump_path):
        if red:
            redirects[title] = red
            continue
        links[title] = {m.group(1).strip().replace("_", " ") for m in LINK_RE.finditer(wikitext)}
        kids[title] = region_children(wikitext)
        ageos[title] = article_geo(wikitext)
        locs[title] = located_in(wikitext)

    def keep(t: str) -> bool:
        """判断条目是否属于中国范围，两条判据按可信度分层：

        1) 条目自报坐标 —— 最可靠，直接用它（落在境外空洞/包围盒外的剔掉）；
        2) 无坐标时才看 {{位于}} —— 它填的不一定是省份名，也有"上饶"这类地级名，
           所以只在没有坐标可依据时当作兜底否决，避免误杀中国县城。
        """
        lat, lon = ageos.get(t, (None, None))
        if t in FOREIGN_REGION_TITLES:
            return False
        # 消歧后缀直接写了国家名的（"平昌 (韩国)"、"金寺 (韩国)"），坐标往往缺失，只能靠标题判定
        m = re.search(r"[\(（]([^\)）]+)[\)）]\s*$", t)
        if m and m.group(1).strip() in FOREIGN_REGION_TITLES:
            return False
        if lat is not None:
            return outside_china_geo(lat, lon) is None
        loc = locs.get(t, "")
        return not (loc and loc not in CN_LOCATIONS)

    def resolve(t: str) -> str:
        seen = set()
        while t in redirects and t not in seen:
            seen.add(t)
            t = redirects[t]
        return t

    root_page = resolve(root)
    reached = {root_page} | {
        resolve(k) for k in kids.get(root_page, ()) if resolve(k) in links and keep(resolve(k))
    }
    frontier = set(reached)
    depth = 1
    while depth < max_depth and frontier:
        nxt = set()
        for t in frontier:
            for link in links.get(t, ()):
                r = resolve(link)
                if r in links and r not in reached and keep(r):
                    reached.add(r)
                    nxt.add(r)
        frontier = nxt
        depth += 1

    # 下钻："父页/子页" 形式的分区条目（北京/朝阳 等），归属明确，不会引入境外条目
    changed = True
    while changed:
        add = {t for t in links if "/" in t and t not in reached
               and resolve(t.rsplit("/", 1)[0]) in reached and keep(t)}
        changed = bool(add)
        reached |= add
    return reached, links


def section_map(wikitext: str):
    """按二级章节切分，返回 {标题: 正文}；正文从标题行的下一行开始，避免标题重复。"""
    heads = [(m.start(), m.end(), m.group(2)) for m in SECTION_RE.finditer(wikitext) if len(m.group(1)) == 2]
    out = {}
    for idx, (_, body_start, head) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(wikitext)
        out[head.strip()] = wikitext[body_start:end]
    return out


def clean_inline(text: str, limit: int = 160) -> str:
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text or "")
    text = re.sub(r"'{2,3}", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def first_paragraph(sections_text: str) -> str:
    """取章节内第一段真正的正文，跳过 ===子标题=== 与空行。"""
    for block in re.split(r"\n\s*\n", strip_templates(sections_text)):
        b = block.strip()
        if not b or re.fullmatch(r"\*\*[^*]{1,20}\*\*", b):
            continue
        return b
    return ""


def dedupe(items):
    seen, ordered = set(), []
    for it in items:
        if it["name"] in seen:
            continue
        seen.add(it["name"])
        ordered.append(it)
    return ordered


def render_city(title, items, sections, dump_date, source_url, valid_until):
    ordered = dedupe(items)
    intro_src = sections.get("了解", "") or sections.get("简介", "")
    summary = oneline(first_paragraph(intro_src), 70) or (title + "（Wikivoyage 转化种子页）")
    # 章节里的 ===历史=== 一类子标题会被转成 **历史** 并与正文同块，摘要有时需要剥掉
    summary = re.sub(r"^\*\*[^*]{1,12}\*\*\s*", "", summary)
    lines = [
        "---", "type: city",
        "summary: %s" % summary.replace(":", "："),
        "upstream: wikivoyage",
        "sources:",
        "  - %s" % source_url,
        "updated: %s" % date.today().isoformat(),
        "valid_until: %s" % valid_until,
        "coord_status: unverified",
        "---", "", "# " + title, "",
        "> " + ATTRIBUTION, "",
    ]
    intro = strip_templates(sections.get("了解", "") or sections.get("简介", ""))
    if intro:
        lines += ["## 背景", "", intro[:1500], ""]
    for label, key in (("抵达", "抵达"), ("当地交通", "周游"), ("购物", "购物"), ("住宿", "住宿")):
        body = strip_templates(sections.get(key, ""))
        if body:
            lines += ["## %s" % label, "", body[:1200], ""]
    lines += ["## 内嵌 POI（%d 条，坐标未经高德核验）" % len(ordered), "",
              "| 类型 | 名称 | 坐标 (lat,lon) | 地址 | 门票/价格 | 开放时间 | 简介 |",
              "|---|---|---|---|---|---|---|"]
    for it in ordered:
        coord = "%.6f,%.6f" % (it["lat"], it["lon"]) if (it["lat"] is not None and it["lon"] is not None) else "缺"
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            it["kind"], it["name"].replace("|", "/"), coord,
            (it["address"] or "-").replace("|", "/"), (it["price"] or "-").replace("|", "/"),
            (it["hours"] or "-").replace("|", "/"), (it["content"] or "-").replace("|", "/")))
    lines += ["", "## 待办", "",
              "- [ ] `coord_status: unverified`：被 Query 命中时用 `get_amap_poi_search` 复核坐标并升级为独立 POI 页",
              "- [ ] 门票与开放时间来自 Wikivoyage 编辑者贡献，最后随转储固定于 %s，出行前须现场核实" % dump_date, ""]
    return "\n".join(lines), summary, len(ordered)


def snapshot_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:80]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--reindex", action="store_true", help="只根据磁盘上的种子页重建 index 区块，不读转储")
    ap.add_argument("--scope", default="china", choices=["china", "bbox", "all"],
                    help="china=沿维基旅行层级 BFS（默认）；bbox=经纬度包围盒（会把日韩俄误纳，仅供对比）；all=不限")
    ap.add_argument("--root", default="中华人民共和国", help="层级 BFS 的根条目")
    ap.add_argument("--depth", type=int, default=2, help="BFS 层数：1=仅省级，2=省市两级，3=再下钻一区/景点")
    ap.add_argument("--min-listings", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--exclude", default="", help="逗号分隔的标题，跳过不覆盖")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--prune", action="store_true", help="先删除不在当前范围内的 Wikivoyage 种子页")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.reindex:
        print("index 已按磁盘种子页重建，共 %d 条" % reindex_from_disk())
        return 0
    if not args.dump:
        print("需要 --dump（或使用 --reindex）", file=sys.stderr)
        return 2

    dump_path = Path(args.dump)
    cn_set = None
    if args.scope == "china":
        cn_set, _ = china_titles(dump_path, args.root, args.depth)
        print("BFS 自「%s」depth=%d -> %d 个条目" % (args.root, args.depth, len(cn_set)))
        if args.prune:
            removed = []
            for path in (REPO / "wiki" / "cities").glob("*.md"):
                text = path.read_text(encoding="utf-8")
                fm = re.match(r"^---\n(.*?)\n---", text, re.S)
                if not fm or "upstream: wikivoyage" not in fm.group(1):
                    continue  # 只清理种子页，手写页面永不动
                m = re.search(r"^#\s+(.+)$", text, re.M)
                if m and m.group(1).strip() not in cn_set:
                    removed.append((path.name, m.group(1).strip()))
                    path.unlink()
            print("prune 删除 %d 个已不在范围内的种子页: %s" % (
                len(removed), ", ".join(t for _, t in removed[:15])))

    dump_date = "2026-09-01"
    valid_until = (datetime.strptime(dump_date, "%Y-%m-%d").date() + timedelta(days=90)).isoformat()
    excludes = {x.strip() for x in args.exclude.split(",") if x.strip()}

    stats = {"pages": 0, "with_listing": 0, "china": 0, "written": 0, "skipped_existing": 0, "skipped_excluded": 0}
    index_entries: list[tuple] = []
    by_loc: dict[str, int] = {}
    for title, ts, wikitext, _red in iter_pages(dump_path):
        stats["pages"] += 1
        items = parse_listings(wikitext)
        loc = located_in(wikitext)
        ageo = article_geo(wikitext)
        if len(items) < args.min_listings:
            continue
        stats["with_listing"] += 1
        if args.scope == "china":
            if title not in cn_set:
                continue
            # BFS 偶有跨境"下一站"带来的外国条目，用条目自身坐标做一次保守剔除；
            # 无 {{位于|国家}} 之外的取值不参与判断（它们是维基旅行的子区域名，如"北臺灣"）
            if ageo[0] is not None and not (
                CHINA_BBOX[0] <= ageo[0] <= CHINA_BBOX[1] and CHINA_BBOX[2] <= ageo[1] <= CHINA_BBOX[3]
            ):
                stats.setdefault("dropped_outside_bbox", 0)
                stats["dropped_outside_bbox"] += 1
                continue
        elif args.scope == "bbox" and not in_china(items, loc, ageo):
            continue
        stats["china"] += 1
        by_loc[loc or "(无位于)"] = by_loc.get(loc or "(无位于)", 0) + 1
        if args.stats:
            continue
        if title in excludes:
            stats["skipped_excluded"] += 1
            continue
        city_path = REPO / "wiki" / "cities" / (snapshot_name(title) + ".md")
        if city_path.exists():
            stats["skipped_existing"] += 1
            continue
        rel_snap = "raw/wikivoyage/%s_%s.md" % (dump_date, snapshot_name(title))
        snap_path = REPO / rel_snap
        body, summary, n_pois = render_city(title, items, section_map(wikitext), dump_date, rel_snap, valid_until)
        if args.dry_run:
            stats["written"] += 1
            print("[dry] %-24s listings=%-3d -> %s" % (title, len(items), city_path.name))
            if stats["written"] >= args.limit:
                break
            continue
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        city_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            "Source: https://zh.wikivoyage.org/wiki/%s\nDump: %s\nFetched: %s\nLicense: CC BY-SA 4.0\n\n%s"
            % (snapshot_name(title).replace("_", "%20"), dump_date, date.today().isoformat(), wikitext),
            encoding="utf-8")
        city_path.write_text(body, encoding="utf-8")
        index_entries.append((title, city_path.name, summary, n_pois))
        stats["written"] += 1
        if args.limit and stats["written"] >= args.limit:
            break

    if index_entries:
        update_index(index_entries)
        print("index.md 追加 %d 条种子页链接" % len(index_entries))
    if by_loc:
        print("按 {{位于}} 分布:", ", ".join(
            "%s=%d" % kv for kv in sorted(by_loc.items(), key=lambda x: -x[1])[:40]))
    print("统计:", stats)
    return 0


INDEX_ANCHOR = "## 种子页（Wikivoyage 批量导入）"


def reindex_from_disk() -> int:
    """扫描磁盘上的种子页重建 index 区块，用于修复"页面已写但索引漏项"的不一致。"""
    cities = REPO / "wiki" / "cities"
    entries = []
    for path in sorted(cities.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not fm or "upstream: wikivoyage" not in fm.group(1):
            continue
        summary = re.search(r"^summary:\s*(.+)$", fm.group(1), re.M)
        pois = re.search(r"## 内嵌 POI（(\d+) 条", text)
        entries.append((
            re.search(r"^#\s+(.+)$", text, re.M).group(1).strip(),
            path.name,
            (summary.group(1).strip() if summary else "")[:60],
            int(pois.group(1)) if pois else 0,
        ))
    update_index(entries)
    return len(entries)


def update_index(entries) -> None:
    """把新生成的种子页登记进 index.md，否则会被 lint 判为孤立页面。

    标题里带括号（如"全州 (桂林)"）会被转成文件名里的下划线，链接目标必须百分号编码，
    否则 Markdown 解析和 lint 的正则都会在括号处截断。
    """
    def link_of(fname: str) -> str:
        return "cities/" + fname.replace("(", "%28").replace(")", "%29")

    path = REPO / "wiki" / "index.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Wiki 索引\n"
    def row(t, f, s, n):
        return "- [%s](%s) — %s（内嵌 POI %d 条，坐标未核验）" % (t, link_of(f), s, n)
    block = "\n" + INDEX_ANCHOR + "\n\n" + "\n".join(row(t, f, s, n) for t, f, s, n in entries) + "\n"
    if INDEX_ANCHOR in text:
        head, _, tail = text.partition(INDEX_ANCHOR)
        keep_lines = [ln for ln in tail.splitlines() if ln.startswith("- [")]
        merged = {}
        for ln in keep_lines:
            m = re.match(r"- \[([^\]]*)\]\(([^)]*)\)", ln)
            if m:
                merged[m.group(1)] = ln
        for t, f, s, n in entries:
            merged[t] = row(t, f, s, n)
        block = "\n" + INDEX_ANCHOR + "\n\n" + "\n".join(merged[k] for k in sorted(merged)) + "\n"
        after = tail.split("\n## ", 1)
        text = head + block + ("\n## " + after[1] if len(after) > 1 else "")
    else:
        text = text.rstrip() + "\n" + block
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
