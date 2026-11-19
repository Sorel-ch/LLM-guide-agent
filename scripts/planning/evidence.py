"""从网页摘要提取可归属的价格证据；歧义时返回 unknown。"""

from __future__ import annotations

import re
from typing import Any


PRICE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:元|¥)\s*(?P<scope>每人|人均|/人|每晚|/晚|元/晚)?")


def entity_matches(name: str, text: str) -> bool:
    if name in text:
        return True
    # 只在主体名称较长时允许去掉城市前缀/分店后缀，避免“汉庭”匹配别的分店。
    stem = re.sub(r"[(（].*?[)）]", "", name).strip()
    return len(stem) >= 5 and stem in text and ("店" not in name or "店" in text)


def extract_price(name: str, kind: str, results: list[tuple[str, dict, dict]]) -> dict[str, Any] | None:
    for tool, args, payload in results:
        if tool != "metaso_web_search" or not payload.get("ok"):
            continue
        query = str(args.get("q") or "")
        if name not in query and not entity_matches(name, query):
            continue
        for page in payload.get("data", {}).get("webpages", []):
            text = " ".join(str(page.get(k) or "") for k in ("title", "snippet", "summary"))
            if not entity_matches(name, text):
                continue
            if kind == "attraction" and re.search(r"免费|免票|不收门票", text):
                return {"value": 0, "scope": "person", "text": text[:200]}
            for match in PRICE_RE.finditer(text):
                scope = match.group("scope") or ""
                if kind == "hotel" and not re.search(r"每晚|/晚|元/晚|房价", text):
                    continue
                if kind == "attraction" and not re.search(r"门票|票价|免费|免票", text):
                    continue
                if re.search(r"起|约|参考|均价", text):
                    continue
                return {"value": int(float(match.group("value"))),
                        "scope": "night" if kind == "hotel" else "person", "text": text[:200]}
    return None
