"""只读审计既有 final_plan，不重新规划、不调用 LLM 或外部接口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from planning.audit import audit_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="JSON 文件或目录；默认 .cache/plans")
    parser.add_argument("-o", "--output", type=Path, default=Path(".cache/audits/plan-audit.json"))
    args = parser.parse_args()
    roots = args.paths or [Path(".cache/plans")]
    files = [p for root in roots for p in ([root] if root.is_file() else sorted(root.glob("*.json")))]
    reports = []
    for path in files:
        try:
            report = audit_plan(json.loads(path.read_text(encoding="utf-8")))
            report["file"] = str(path)
            reports.append(report)
        except (OSError, ValueError) as exc:
            reports.append({"file": str(path), "issues": [{"code": "invalid_plan", "message": str(exc)}]})
    result = {"plans": len(reports), "issue_counts": dict(Counter(
        i["code"] for r in reports for i in r["issues"])), "results": reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"审计 {len(reports)} 份，问题 {sum(result['issue_counts'].values())} 条 → {args.output}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
