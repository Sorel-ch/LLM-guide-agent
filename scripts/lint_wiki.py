"""Lint the wiki: frontmatter fields, expired valid_until, broken links, dangling footnotes.

用法: python scripts/lint_wiki.py
"""

import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
META = {WIKI / "index.md", WIKI / "log.md"}
REQUIRED = ("type", "summary", "sources", "updated", "valid_until")
TODAY = date.today()


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fields: dict[str, str] = {}
    current = None
    for line in m.group(1).splitlines():
        if re.match(r"^\S.*?:", line):
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
            current = key.strip()
        elif line.strip().startswith("- ") and current == "sources":
            fields.setdefault("sources_list", "")
            fields["sources_list"] += line.strip()[2:] + "\n"
    return fields


def main() -> int:
    problems: list[str] = []
    pages = sorted(WIKI.rglob("*.md"))
    linked: set[Path] = set()

    for page in pages:
        text = page.read_text(encoding="utf-8")
        rel = page.relative_to(ROOT).as_posix()

        # 链接收集遍历所有页面（index.md 是导航中心，其出链必须计入），
        # 但只对内容页面报断链。
        for target in re.findall(r"\]\(([^)#\s]+\.(?:md|json))", text):
            if target.startswith(("http://", "https://")):
                continue
            # 链接里的括号被百分号编码（文件名含 "(桂林)" 这类消歧后缀）
            target = unquote(target)
            resolved = (page.parent / target).resolve()
            if resolved.exists():
                linked.add(resolved)
            elif page not in META:
                problems.append(f"{rel}: 断链 -> {target}")

        if page in META:
            continue

        fm = frontmatter(text)

        missing = [k for k in REQUIRED if k not in fm]
        if missing:
            problems.append(f"{rel}: frontmatter 缺字段 {missing}")
            continue

        raw_value = fm.get("valid_until", "")
        try:
            valid_until = date.fromisoformat(raw_value)
        except ValueError:
            problems.append(f"{rel}: valid_until 非法值 {raw_value!r}")
        else:
            if valid_until < TODAY:
                problems.append(f"{rel}: 已过期 valid_until={valid_until}")

        for src in (fm.get("sources_list") or "").splitlines():
            src = src.strip()
            if src and not (ROOT / src).exists():
                problems.append(f"{rel}: sources 指向不存在的快照 {src}")

        for line in text.splitlines():
            m = re.match(r"^\[\^(\d+)\]:\s*(\S+)\s*$", line.strip())
            if m and not (ROOT / m.group(2)).exists():
                problems.append(f"{rel}: 脚注 [^{m.group(1)}] 指向不存在的文件 {m.group(2)}")

        used = set(re.findall(r"\[\^(\d+)\](?!:)", text))
        defined = set(re.findall(r"\[\^(\d+)\]:", text))
        for orphan in used ^ defined:
            problems.append(f"{rel}: 脚注 [^{orphan}] 引用与定义不匹配")

    for page in pages:
        if page in META:
            continue
        if page.resolve() not in linked:
            problems.append(f"{page.relative_to(ROOT).as_posix()}: 孤立页面（无入链）")

    print(f"检查 {len(pages)} 个页面")
    for p in problems:
        print("  !", p)
    if not problems:
        print("  全部通过")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
