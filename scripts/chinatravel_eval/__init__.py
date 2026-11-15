"""ChinaTravel 评测适配层（只在外部 3.12 工作区运行，产品路径不 import）。

代码位置在仓库内便于评审与维护；运行依赖：
- `CHINATRAVEL_HOME`（默认 E:/code/chinatravel-eval/ChinaTravel）
- `CHINATRAVEL_PYTHON`（默认 E:/code/chinatravel-eval/.venv/Scripts/python.exe）
- 该 venv 同时装了 ChinaTravel 与本仓库 requirements。

边界：不写 `wiki/`、`raw/`；不把 `hard_logic_py` 喂给规划器；只读 benchmark 与其 sandbox。
"""

from .sandbox import Sandbox  # noqa: F401
from .queries import Task, load_tasks, build_needs, export_local_split  # noqa: F401
