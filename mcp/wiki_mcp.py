"""只读 wiki MCP server：把 `wiki/` 的查询能力暴露给外部 agent（如 VSCode Cline）。

设计约束（决定了工具为什么这么切）：
- `wiki/index.md` 有 13 万字符，其中 1739 行是批量种子页。整份返回会瞬间吃满上下文，
  所以 read_index 默认只给人工维护的导航区，种子页靠 search_pages 按需命中。
- 一律只读：任何写操作都不在本 server 的能力范围内，路径也被限制在 `wiki/` 内。
- 页面过了 `valid_until` 或坐标 `unverified` 时，返回值里带 explicitly 的提示，
  让调用方知道接下来该调高德/秘塔补数，而不是直接采信。
"""

import functools
import inspect
import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mcp import types
from mcp.server.fastmcp import FastMCP

WIKI_ROOT = Path(__file__).resolve().parent.parent / "wiki"
INDEX = WIKI_ROOT / "index.md"

# 导航区 = index.md 里人工维护的章节；其余全是批量导入的种子页
CURATED_HEADINGS = ("## 城市", "## POI", "## 路线", "## 待建页面")
SEED_HEADING = "## 种子页"

mcp = FastMCP("wiki", log_level="ERROR")


def wiki_tool(fn):
    """注册为 MCP 工具，返回单份紧凑正文。

    返回 dict 的话 FastMCP 会给两份：indent=2 的 text 正文 + 一份 structuredContent 重复体。
    本 server 的输出直接进模型上下文（read_pages 一次 11 KB），所以显式构造 CallToolResult。
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        payload = fn(*args, **kwargs)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], structuredContent=None
        )

    wrapper.__signature__ = sig.replace(return_annotation=types.CallToolResult)
    return mcp.tool()(wrapper)


def _ok(data: Any, query: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "error_code": None,
        "error_msg": None,
        "query": query or {},
        "data": data,
        "meta": {"source": "local wiki/", "read_only": True},
    }


def _fail(code: str, msg: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "error_msg": msg,
        "data": None,
        "meta": {"source": "local wiki/", "read_only": True},
    }


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if re.match(r"^\S.*?:", line):
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields


def _page_meta(path: Path, fm: dict[str, str]) -> dict[str, Any]:
    today = date.today()
    warns: list[str] = []

    raw_valid = fm.get("valid_until", "")
    stale = None
    try:
        valid_until = date.fromisoformat(raw_valid)
        stale = valid_until < today
    except ValueError:
        stale = None

    if stale:
        warns.append(
            f"页面已过 valid_until={raw_valid}，门票/营业时间/天气结论视为存疑，"
            "请调用 get_amap_* 或 metaso_web_search 现场核实后再采信"
        )
    if fm.get("coord_status") == "unverified":
        warns.append(
            "coord_status=unverified：页内坐标来自 Wikivoyage 批量导入，实测偏差可达数百米"
            "甚至 1km 以上，禁止直接用于路线规划，需 get_amap_poi_search 复核"
        )

    return {
        "path": path.relative_to(WIKI_ROOT.parent).as_posix(),
        "type": fm.get("type"),
        "summary": fm.get("summary"),
        "updated": fm.get("updated"),
        "valid_until": raw_valid or None,
        "stale": stale,
        "coord_status": fm.get("coord_status"),
        "warnings": warns,
    }


def _resolve(rel: str) -> Path | None:
    """把调用方给的路径解析到 wiki/ 之内，越界一律拒绝。"""
    rel = unquote((rel or "").strip().replace("\\", "/").lstrip("/"))
    if not rel:
        return None
    candidate = (WIKI_ROOT / rel) if not rel.startswith("wiki/") else (WIKI_ROOT.parent / rel)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_relative_to(WIKI_ROOT) else None


def _index_lines() -> list[str]:
    if not INDEX.exists():
        return []
    return INDEX.read_text(encoding="utf-8").splitlines()


@wiki_tool
def read_index(scope: str = "curated") -> dict[str, Any]:
    """读 wiki 导航索引，规划任务的第一步。

    scope:
      curated — 人工维护的城市/POI/路线清单（默认，很小，优先用这个）
      seeds   — Wikivoyage 批量导入的 1700+ 种子页清单（很大，慎用）
      all     — 整个 index.md（约 13 万字符，几乎一定会撑爆上下文）
    """
    lines = _index_lines()
    if not lines:
        return _fail("INDEX_NOT_FOUND", f"未找到 {INDEX}")

    picked: list[str] = []
    bucket: str | None = None
    for line in lines:
        if line.startswith("## "):
            if scope == "curated":
                bucket = "keep" if line.startswith(CURATED_HEADINGS) else "drop"
            elif scope == "seeds":
                bucket = "keep" if line.startswith(SEED_HEADING) else "drop"
            else:
                bucket = "keep"
            if bucket == "keep":
                picked.append(line)
            continue
        if bucket == "keep" and line.strip():
            picked.append(line)

    if scope == "all":
        picked = [ln for ln in lines if ln.strip()]

    curated_total = sum(1 for ln in picked if ln.startswith("- ["))
    return _ok(
        {
            "content": "\n".join(picked),
            "entry_count": curated_total,
            "hint": "命中具体页面后用 read_page 取正文；找不到就用 search_pages",
        },
        {"scope": scope},
    )


@wiki_tool
def search_pages(keyword: str, limit: int = 15) -> dict[str, Any]:
    """在 index.md 的条目行里检索关键词，返回页面路径与一句话摘要。

    只检索索引（不扫全库正文），因此一次调用即可完成、代价恒定；
    索引未收录的正文细节请用 read_page 打开候选页再看。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return _fail("EMPTY_KEYWORD", "keyword 不能为空")

    hits: list[dict[str, Any]] = []
    for line in _index_lines():
        if not line.startswith("- ["):
            continue
        m = re.match(r"- \[([^\]]+)\]\(([^)]+)\)", line)
        if not m:
            continue
        if kw not in line.lower():
            continue
        title, target = m.group(1), unquote(m.group(2))
        page = _resolve(target)
        fm = {}
        if page and page.exists():
            fm = _frontmatter(page.read_text(encoding="utf-8"))
        hits.append(
            {
                "title": title,
                "path": f"wiki/{target}",
                "exists": bool(page and page.exists()),
                "type": fm.get("type"),
                "summary": fm.get("summary") or line.split("—", 1)[-1].strip(),
                "stale": _page_meta(page, fm)["stale"] if page and page.exists() else None,
            }
        )
        if len(hits) >= max(1, min(limit, 50)):
            break

    return _ok({"keyword": keyword, "hit_count": len(hits), "results": hits},
               {"keyword": keyword})


@wiki_tool
def read_page(path: str) -> dict[str, Any]:
    """读单个 wiki 页面全文（相对 wiki/ 的路径，如 "pois/天一阁.md"，也接受 "wiki/pois/天一阁.md"）。

    返回的 meta.warnings 会告诉你该页是否已过期、坐标是否未核验；
    有 warnings 时不要直接采信相关字段，应转用高德/秘塔工具现场补数。
    """
    return _read_page_payload(path)


def _read_page_payload(path: str) -> dict[str, Any]:
    page = _resolve(path)
    if page is None:
        return _fail("PATH_OUT_OF_WIKI", f"只允许读 wiki/ 下的文件，收到 {path!r}")
    if page.suffix != ".md":
        return _fail("NOT_A_PAGE", f"只支持 .md 页面，收到 {path!r}")
    if not page.exists():
        return _fail("PAGE_NOT_FOUND", f"页面不存在：{path}")

    text = page.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    meta = _page_meta(page, fm)
    meta["chars"] = len(text)
    meta["outlinks"] = sorted(
        {unquote(t) for t in re.findall(r"\]\(([^)#\s]+\.md)", text) if not t.startswith("http")}
    )
    return _ok({"meta": meta, "content": text}, {"path": path})


@wiki_tool
def read_pages(paths: list[str]) -> dict[str, Any]:
    """一次读多页（2–5 个），用于在有限工具调用预算里换页面。"""
    pages = []
    for p in paths[:8]:
        res = _read_page_payload(p)
        if res["ok"]:
            pages.append(res["data"])
        else:
            pages.append({"meta": {"path": p, "error": res["error_code"], "error_msg": res["error_msg"]},
                          "content": ""})
    return _ok({"count": len(pages), "pages": pages}, {"paths": paths})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
