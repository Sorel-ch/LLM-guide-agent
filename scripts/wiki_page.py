"""城市页的共享组装层：frontmatter/章节解析、章节归属合并、简体化与维基语法清洗。

章节归属是硬约定，两个刷新脚本各自只换自己的段落：

- Wikipedia：背景、交通
- Wikivoyage：抵达、当地交通、住宿、购物、内嵌 POI
- 系统：待办

其他未知章节一律原样保留，避免刷新时误删人工内容。
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from datetime import date
from urllib.parse import quote

SECTION_ORDER = ("背景", "交通", "抵达", "当地交通", "住宿", "购物", "内嵌 POI")
WIKIPEDIA_SECTIONS = frozenset({"背景", "交通"})
WIKIVOYAGE_SECTIONS = frozenset({"抵达", "当地交通", "住宿", "购物", "内嵌 POI"})
WIKIPEDIA_FM_PREFIXES = ("wikipedia_", "prose_")

WIKIVOYAGE_ATTRIBUTION_MARK = "Wikivoyage"
WIKIPEDIA_ATTRIBUTION_MARK = "中文维基百科"
ATTRIBUTION_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

try:  # 简体化是软依赖：装不上时保留原文，不让刷新整条链路失败
    from opencc import OpenCC

    _OPENCC = OpenCC("t2s")
except Exception:  # pragma: no cover - 只在缺依赖的环境触发
    _OPENCC = None


def to_simplified(text: str) -> str:
    if not text or _OPENCC is None:
        return text
    return _OPENCC.convert(text)


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- frontmatter


def parse_frontmatter(text: str) -> tuple[OrderedDict, str]:
    """返回 (字段 -> 原始行列表, 正文)。列表值保留 `  - ...` 行。"""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return OrderedDict(), text
    body = text[m.end():]
    if body.startswith("\n"):
        body = body[1:]
    fields: OrderedDict = OrderedDict()
    current = None
    for line in m.group(1).splitlines():
        hit = re.match(r"^([A-Za-z0-9_\-]+):(?:\s*(.*))?$", line)
        if hit and not line.startswith(" "):
            current = hit.group(1)
            rest = hit.group(2) or ""
            fields[current] = [rest] if rest else []
        elif current is not None:
            fields[current].append(line)
    return fields, body


def render_frontmatter(fields: OrderedDict) -> str:
    lines = ["---"]
    for key, values in fields.items():
        if not values:
            lines.append("%s:" % key)
        elif len(values) == 1 and not values[0].startswith("  - "):
            lines.append("%s: %s" % (key, values[0]))
        else:
            lines.append("%s:" % key)
            lines.extend(values)
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def fm_get(fields: OrderedDict, key: str, default: str = "") -> str:
    values = fields.get(key) or []
    if not values:
        return default
    if len(values) == 1:
        return values[0].strip()
    return "\n".join(v.strip() for v in values if v.strip())


def fm_set(fields: OrderedDict, key: str, value: str) -> None:
    fields[key] = [value]


def fm_sources(fields: OrderedDict) -> list[str]:
    out = []
    for line in fields.get("sources") or []:
        hit = re.match(r"^\s*-\s*(\S+)", line)
        if hit:
            out.append(hit.group(1))
    return out


def fm_add_source(fields: OrderedDict, path: str) -> bool:
    if path in fm_sources(fields):
        return False
    fields.setdefault("sources", []).append("  - " + path)
    return True


# ------------------------------------------------------------------- sections


def canonical_heading(heading: str) -> str:
    heading = re.sub(r"\s+", " ", heading).strip()
    if heading.startswith("内嵌 POI"):
        return "内嵌 POI"
    return heading


def parse_sections(body: str) -> tuple[str, OrderedDict]:
    """按二级标题切分，返回 (标题与署名等前导内容, 章节)。章节值含 `## 标题` 行。"""
    preamble: list[str] = []
    sections: OrderedDict = OrderedDict()
    current = None
    buf: list[str] = []
    for line in body.splitlines(keepends=True):
        hit = re.match(r"^##\s+(.+?)\s*$", line)
        if hit:
            if current is not None:
                sections[current] = "".join(buf)
            current = canonical_heading(hit.group(1))
            buf = [line]
        elif current is None:
            preamble.append(line)
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "".join(buf)
    return "".join(preamble), sections


def section_body(section_text: str) -> str:
    lines = section_text.splitlines()
    if lines and lines[0].startswith("##"):
        return "\n".join(lines[1:]).strip()
    return section_text.strip()


def set_section(sections: OrderedDict, key: str, body: str, heading: str | None = None) -> None:
    body = body.strip("\n")
    if key in sections:
        old = sections[key].splitlines()
        head = old[0] if old and old[0].startswith("##") else "## " + (heading or key)
        sections[key] = head + "\n\n" + body + "\n\n"
    else:
        sections[key] = "## " + (heading or key) + "\n\n" + body + "\n\n"


def _order_index(key: str) -> int:
    if key == "待办":
        return 99
    if key in SECTION_ORDER:
        return SECTION_ORDER.index(key)
    return 50  # 未知/人工章节排在已知章节之后、待办之前


def _put_section(sections: OrderedDict, key: str, body: str, heading: str | None = None) -> None:
    """已有章节原位替换；新章节按固定顺序插入，不整体重排旧页。"""
    if key in sections:
        set_section(sections, key, body, heading)
        return
    head = heading or key
    value = "## " + head + "\n\n" + body.strip("\n") + "\n\n"
    idx = _order_index(key)
    items = list(sections.items())
    pos = len(items)
    for i, (existing, _value) in enumerate(items):
        if _order_index(existing) > idx:
            pos = i
            break
    items.insert(pos, (key, value))
    sections.clear()
    sections.update(items)


def compose_page(
    old_text: str,
    updates: dict[str, str] | None = None,
    removals: set[str] | None = None,
    fm_updates: dict[str, str] | None = None,
    sources: list[str] | None = None,
    preamble: str | None = None,
) -> str:
    """在现有页上原地替换指定章节，其他章节与未知章节保持原样。"""
    fields, body = parse_frontmatter(old_text)
    old_preamble, sections = parse_sections(body)
    for key, value in (updates or {}).items():
        _put_section(sections, canonical_heading(key), value)
    for key in removals or ():
        sections.pop(canonical_heading(key), None)
    for key, value in (fm_updates or {}).items():
        fm_set(fields, key, value)
    for path in sources or ():
        fm_add_source(fields, path)
    return render_frontmatter(fields) + (preamble if preamble is not None else old_preamble) + "".join(sections.values())


# ------------------------------------------------------------------ summaries


def first_paragraph(section_text: str) -> str:
    body = section_body(section_text)
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        if re.fullmatch(r"\*\*[^*]{1,16}\*\*", block):
            continue
        return block
    return ""


BACKGROUND_LIMIT = 3000


def _bg_blocks(text: str) -> list[tuple[str, str]]:
    """按 `**小节名**` 切背景块；返回 [(标签, 原块文本)]，前导文字标签为空。"""
    blocks: list[tuple[str, str]] = []
    label = ""
    buf: list[str] = []
    for line in (text or "").splitlines():
        hit = re.match(r"^\*\*(.+?)\*\*\s*$", line)
        if hit:
            if any(x.strip() for x in buf):
                blocks.append((label, "\n".join(buf).strip()))
            label = hit.group(1).strip()
            buf = [line]
        else:
            buf.append(line)
    if any(x.strip() for x in buf):
        blocks.append((label, "\n".join(buf).strip()))
    return blocks


def _bg_group(label: str) -> str | None:
    if "历史" in label:
        return "历史"
    if "气候" in label:
        return "气候"
    if label == "自然" or any(key in label for key in ("地理", "地形", "自然环境")):
        return "地理"
    return None


def background_text(page_text: str) -> str:
    _fm, body = parse_frontmatter(page_text)
    _preamble, sections = parse_sections(body)
    return section_body(sections.get("背景", ""))


def has_labeled_background(page_text: str) -> bool:
    return any(label for label, _text in _bg_blocks(background_text(page_text)))


def merge_background(old_bg: str, new_bg: str, limit: int = BACKGROUND_LIMIT) -> str:
    """历史/地理用维基百科替换，旧页独有的风景/气候/旅行时间等段落保留。"""
    old_blocks = _bg_blocks(old_bg)
    if not any(label for label, _text in old_blocks):
        return new_bg[:limit].strip()

    new_blocks = _bg_blocks(new_bg)
    new_intro = next((text for label, text in new_blocks if not label), "")
    new_labeled = [(label, text) for label, text in new_blocks if label]
    new_groups = {_bg_group(label) for label, _text in new_labeled} - {None}

    out: list[str] = []
    old_intro = next((text for label, text in old_blocks if not label), "")
    if new_intro:
        out.append(new_intro)
    elif old_intro:
        out.append(old_intro)
    # 维基百科的历史/地理放前面，旧页独有的风景/气候等段落接在后面
    out.extend(text for _label, text in new_labeled)
    for label, text in old_blocks:
        if not label:
            continue
        if _bg_group(label) in new_groups:
            continue
        out.append(text)

    merged = "\n\n".join(text.strip() for text in out if text.strip())
    if len(merged) > limit:
        cut = merged[:limit]
        pos = cut.rfind("\n\n")
        merged = cut[:pos] if pos > limit * 0.6 else cut
    return merged.strip()


def page_summary(section_text: str, title: str, limit: int = 70) -> str:
    text = first_paragraph(section_text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"'{2,5}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:limit].rstrip()
    return text.replace(":", "：") or (to_simplified(title) + "（维基百科正文）")


# ---------------------------------------------------------------- wikitext


def _split_wikitext_sections(wikitext: str) -> dict[str, str]:
    heads = []
    for m in re.finditer(r"^(={2,4})\s*(.+?)\s*\1\s*$", wikitext, re.M):
        if len(m.group(1)) != 2:
            continue
        head = re.sub(r"\{\{([^{}|]+)(?:\|[^{}]*)?\}\}", r"\1", m.group(2)).strip()
        if head:
            heads.append((m.start(), m.end(), head))
    out: dict[str, str] = {}
    for i, (_start, body_start, head) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(wikitext)
        out.setdefault(head, wikitext[body_start:end])
    return out


def clean_wiki(text: str, limit: int | None = None) -> str:
    """把维基语法清成可读段落；保留列表项，丢掉模板/表格/引用/图片。"""
    try:
        import mwparserfromhell

        code = mwparserfromhell.parse(text)
        drop_tags = {"ref", "references", "gallery", "imagemap", "table", "math", "score", "mapframe"}
        for tag in list(code.filter_tags(matches=lambda t: str(t.tag).lower() in drop_tags)):
            try:
                code.remove(tag, recursive=True)
            except Exception:
                pass
        for tmpl in list(code.filter_templates()):
            try:
                code.remove(tmpl, recursive=True)
            except Exception:
                pass
        for heading in list(code.filter_headings()):
            try:
                code.remove(heading, recursive=True)
            except Exception:
                pass
        file_prefixes = ("file:", "image:", "文件:", "图像:", "category:", "分类:")
        for link in list(code.filter_wikilinks()):
            try:
                if str(link.title).strip().lower().startswith(file_prefixes):
                    code.remove(link, recursive=True)
                else:
                    code.replace(link, link.text or link.title)
            except Exception:
                pass
        for link in list(code.filter_external_links()):
            try:
                code.replace(link, link.title or "")
            except Exception:
                pass
        text = str(code)
    except Exception:
        text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
        text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)

    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"-\{[^{}]*\}-", "", text)  # 繁简转换标记 -{zh-hans:...;zh-hant:...}-
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'{2,5}", "", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"^\s*\|.*$", "", text, flags=re.M)  # 残留表格行
    text = re.sub(r"^\s*[\[{].*[\]}]\s*$", "", text, flags=re.M)  # 残留模板/链接行
    text = re.sub(r"[（(]\s*[)）]", "", text)  # 模板被剥掉后留下的空括号
    text = re.sub(r"^[ \t]*[-*][ \t]*$", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 维基列表转 Markdown，交通章节大量使用
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("*"):
            line = "- " + line.lstrip("*").strip()
        elif line.startswith("#"):
            line = "1. " + line.lstrip("#").strip()
        lines.append(line)
    text = "\n".join(lines).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        pos = cut.rfind("\n\n")
        text = cut[:pos] if pos > limit * 0.6 else cut
    return text


def _pick(sections: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if name in sections:
            return sections[name]
    return ""


def extract_wikipedia_sections(wikitext: str) -> dict[str, str]:
    """只取写进城市页的两段：背景（概况/历史/地理）与交通。"""
    sections = _split_wikitext_sections(wikitext)
    parts: list[str] = []
    first_head = re.search(r"^(={2,4})\s*.+?\s*\1\s*$", wikitext, re.M)
    lead = clean_wiki(wikitext[:first_head.start()] if first_head else wikitext, 600)
    if lead:
        parts.append(lead)
    else:
        overview = clean_wiki(_pick(sections, ("概况", "简介", "城市概况", "名称")), 500)
        if overview:
            parts.append(overview)
    history = clean_wiki(_pick(sections, ("历史", "历史沿革", "建置沿革")), 1000)
    if history:
        parts.append("**历史**\n\n" + history)
    geography = clean_wiki(_pick(sections, ("地理", "地理环境", "自然地理", "地形地貌")), 700)
    if geography:
        parts.append("**地理**\n\n" + geography)
    background = "\n\n".join(parts)[:2000].strip()
    transport = clean_wiki(_pick(sections, ("交通", "交通运输", "对外交通", "交通状况")), 1200)
    out = {"背景": to_simplified(background), "交通": to_simplified(transport)}
    return {k: v for k, v in out.items() if v and len(v) >= (80 if k == "交通" else 1)}


def wikipedia_attribution(wp_title: str, revid: str | int) -> str:
    url = "https://zh.wikipedia.org/wiki/%s" % quote(wp_title.replace(" ", "_"))
    if revid:
        url += "?oldid=%s" % revid
    display = to_simplified(wp_title)
    return (
        "> 本页面正文部分由中文维基百科转化而来，依据 [CC BY-SA 4.0](%s) 授权；"
        "原文与修订历史见 [%s](%s)（oldid=%s）。" % (ATTRIBUTION_URL, display, url, revid or "?")
    )


def ensure_wikipedia_attribution(preamble: str, wp_title: str, revid: str | int) -> str:
    line = wikipedia_attribution(wp_title, revid)
    trailing = "\n" * (len(preamble) - len(preamble.rstrip("\n")))
    lines = preamble.rstrip("\n").splitlines()
    for i, old in enumerate(lines):
        if WIKIPEDIA_ATTRIBUTION_MARK in old and old.lstrip().startswith(">"):
            lines[i] = line
            return "\n".join(lines) + trailing
    insert_at = 1
    for i, old in enumerate(lines):
        if WIKIVOYAGE_ATTRIBUTION_MARK in old and old.lstrip().startswith(">"):
            insert_at = i + 1
            break
    lines.insert(insert_at, line)
    return "\n".join(lines) + trailing


# ------------------------------------------------------------------- merge


def _date_or_none(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def merge_wikivoyage_page(old_text: str, new_text: str) -> str:
    """Wikivoyage 刷新：以新渲染页为底，保留 Wikipedia 拥有的章节与未知章节。"""
    old_fm, old_body = parse_frontmatter(old_text)
    new_fm, new_body = parse_frontmatter(new_text)
    old_preamble, old_sections = parse_sections(old_body)
    _new_preamble, new_sections = parse_sections(new_body)

    merged = OrderedDict()
    for key, old_value in old_sections.items():
        if key in WIKIPEDIA_SECTIONS:
            merged[key] = old_value
        elif key in new_sections:
            merged[key] = new_sections[key]
        elif key in WIKIVOYAGE_SECTIONS:
            continue  # 上游删掉了这个旅行章节
        else:
            merged[key] = old_value  # 未知/人工章节原样保留
    for key, new_value in new_sections.items():
        if key not in merged:
            head = new_value.splitlines()[0][2:].strip() if new_value.startswith("##") else key
            _put_section(merged, key, section_body(new_value), head)
    new_sections = merged

    for key in WIKIPEDIA_FM_PREFIXES:
        for field in old_fm:
            if field.startswith(key):
                new_fm[field] = old_fm[field]

    merged_sources = []
    for fm in (new_fm, old_fm):
        merged_sources.extend(fm_sources(fm))
    if merged_sources:
        seen = set()
        new_fm["sources"] = []
        for src in merged_sources:
            if src not in seen:
                seen.add(src)
                new_fm["sources"].append("  - " + src)

    old_wp = "背景" in old_sections and fm_get(old_fm, "prose_upstream") == "wikipedia"
    if old_wp and fm_get(old_fm, "summary"):
        new_fm["summary"] = old_fm["summary"]

    old_valid = _date_or_none(fm_get(old_fm, "valid_until"))
    new_valid = _date_or_none(fm_get(new_fm, "valid_until"))
    if old_valid and new_valid and old_valid < new_valid:
        new_fm["valid_until"] = old_valid.isoformat()

    preamble = _new_preamble
    if WIKIPEDIA_ATTRIBUTION_MARK in old_preamble and WIKIPEDIA_ATTRIBUTION_MARK not in preamble:
        wp_line = next(
            (ln for ln in old_preamble.splitlines() if WIKIPEDIA_ATTRIBUTION_MARK in ln),
            "",
        )
        if wp_line:
            preamble = preamble.rstrip() + "\n" + wp_line + "\n\n"

    ordered = "".join(new_sections.values())
    return render_frontmatter(new_fm) + preamble + ordered


def merge_wikipedia_page(
    old_text: str,
    prose: dict[str, str],
    wp_title: str,
    revid: str | int,
    dump_date: str,
    fetched: str,
    source_path: str,
) -> str:
    """Wikipedia 刷新：只替换 背景/交通，更新 frontmatter、摘要与署名。"""
    fields, body = parse_frontmatter(old_text)
    preamble, _sections = parse_sections(body)
    updates = {k: v for k, v in prose.items() if v}
    if not updates:
        return old_text
    if "背景" in updates:
        old_bg = section_body(_sections.get("背景", ""))
        updates["背景"] = merge_background(old_bg, updates["背景"])

    removals = set()
    if fm_get(fields, "prose_upstream") == "wikipedia" and "交通" not in updates:
        removals.add("交通")  # 上游删掉了交通章节，页面里也不留旧版

    summary = page_summary(updates["背景"], wp_title) if "背景" in updates else fm_get(fields, "summary")
    today = date.today().isoformat()
    valid = (date.fromisoformat(dump_date).toordinal() + 90)
    valid_until = date.fromordinal(valid).isoformat()
    old_valid = _date_or_none(fm_get(fields, "valid_until"))
    if old_valid and old_valid.isoformat() < valid_until:
        valid_until = old_valid.isoformat()

    fm_updates = {
        "summary": summary,
        "prose_upstream": "wikipedia",
        "wikipedia_title": wp_title,
        "wikipedia_revid": str(revid),
        "wikipedia_dump": dump_date,
        "wikipedia_fetched": fetched,
        "prose_hash": sha1_text("\n\n".join(updates[k] for k in sorted(updates))),
        "updated": today,
        "valid_until": valid_until,
    }
    new_preamble = ensure_wikipedia_attribution(preamble, wp_title, revid)
    return compose_page(
        old_text,
        updates=updates,
        removals=removals,
        fm_updates=fm_updates,
        sources=[source_path],
        preamble=new_preamble,
    )


# ---------------------------------------------------------------- simplify


def simplify_page(text: str) -> str:
    """全页转简体；保留 H1 原文，避免破坏与上游标题的匹配。"""
    if _OPENCC is None:
        return text
    fields, body = parse_frontmatter(text)
    summary = fm_get(fields, "summary")
    if summary:
        fm_set(fields, "summary", to_simplified(summary))
    lines = body.splitlines(keepends=True)
    out = []
    seen_h1 = False
    for line in lines:
        if line.startswith("# ") and not seen_h1:
            out.append(line)
            seen_h1 = True
        else:
            out.append(to_simplified(line))
    return render_frontmatter(fields) + "".join(out)
