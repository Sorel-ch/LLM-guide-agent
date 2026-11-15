"""ChinaTravel 评测入口（用外部 3.12 venv 运行）。

示例：
  E:/code/chinatravel-eval/.venv/Scripts/python.exe scripts/eval_chinatravel.py \
      --mode rule --split easy --days 1,2 --limit 10 --method rule_v0

不进入产品管线；不写 wiki/raw；不把 hard_logic_py 喂给规划器。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_HOME = Path(os.getenv("CHINATRAVEL_HOME", r"E:/code/chinatravel-eval/ChinaTravel"))
for path in (REPO_ROOT / "scripts", BENCHMARK_HOME):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from chinatravel_eval.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
