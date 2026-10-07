"""把 travel-mcp 的工具返回落盘为 raw/ 不可变快照。

用法:
    python scripts/raw_snapshot.py <tool> <out_rel_path> '<kwargs json>'
    python scripts/raw_snapshot.py --list
"""

import asyncio
import inspect
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "mcp"


def main() -> int:
    sys.path.insert(0, str(MCP_DIR))
    import travel_mcp

    tools = {
        "get_amap_weather": travel_mcp.get_amap_weather,
        "get_amap_poi_search": travel_mcp.get_amap_poi_search,
        "get_amap_input_tips": travel_mcp.get_amap_input_tips,
        "get_amap_direction": travel_mcp.get_amap_direction,
        "metaso_web_search": travel_mcp.metaso_web_search,
        "metaso_web_reader": travel_mcp.metaso_web_reader,
    }

    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        print("\n".join(sorted(tools)))
        return 0

    name, out_rel = sys.argv[1], sys.argv[2]
    kwargs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    if name not in tools:
        print(f"unknown tool: {name}", file=sys.stderr)
        return 2

    result = tools[name](**kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)

    # 工具层现在返回 CallToolResult（只有一份紧凑正文，那是为模型上下文省的）；
    # 落盘时还原成缩进版，与 raw/ 里既有快照格式一致——省的是空白，不截断任何字段。
    if hasattr(result, "content"):
        payload = json.loads(result.content[0].text)
    elif isinstance(result, str):
        payload = json.loads(result)
    else:
        payload = result

    out_path = REPO_ROOT / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok = payload.get("ok")
    try:
        shown = out_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        shown = out_path.as_posix()
    print(f"ok={ok} -> {shown}")
    if not ok:
        print(f"error_code={payload.get('error_code')} error_msg={payload.get('error_msg')}")
        return 1
    if name.startswith("metaso"):
        text = json.dumps(payload, ensure_ascii=False)
        print(f"payload_chars={len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
