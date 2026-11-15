"""批量 runner：rule_v0 / LLM 选点 → 官方 plan → 官方 eval → 报告。

运行环境：外部 Python 3.12 venv（同时装了 ChinaTravel 与本仓库 requirements）。
不进入产品 import 链；不写 wiki/raw；不把 hard_logic_py 喂给规划器。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .queries import Task, build_needs, export_local_split, load_tasks
from .rule_plan import PlanError, build_rule_plan
from .sandbox import CITY_SLUGS, Sandbox

DEFAULT_QUERIES = Path(
    os.getenv("CHINATRAVEL_QUERIES", r"E:/code/chinatravel-eval/downloads/easy.csv")
)
DEFAULT_PY = Path(
    os.getenv("CHINATRAVEL_PYTHON", r"E:/code/chinatravel-eval/.venv/Scripts/python.exe")
)
DEFAULT_OUT = Path(
    os.getenv("CHINATRAVEL_RUNS", r"E:/code/chinatravel-eval/runs")
)
DEFAULT_HOME = Path(
    os.getenv("CHINATRAVEL_HOME", r"E:/code/chinatravel-eval/ChinaTravel")
)


@dataclass
class RunConfig:
    queries: Path = DEFAULT_QUERIES
    split: str = "easy"          # 源 split（CSV/本地目录的标签）
    local_split: str = ""        # 官方 eval 用的本地 split 名，不能是 HF split
    method: str = "rule_v0"
    days: tuple[int, ...] = (1, 2, 3)
    limit: int = 10
    uids: list[str] = field(default_factory=list)
    out: Path = DEFAULT_OUT
    home: Path = DEFAULT_HOME
    python: Path = DEFAULT_PY
    resume: bool = False
    run_eval: bool = True
    mode: str = "rule"  # rule | llm | native


def _local_split(cfg: RunConfig, n: int) -> str:
    if cfg.local_split:
        return cfg.local_split
    return f"{cfg.method}_{cfg.split}_{n}"


def _run_dir(cfg: RunConfig) -> Path:
    return cfg.out / f"{cfg.method}_{cfg.split}_{cfg.limit}"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def select_tasks(cfg: RunConfig) -> list[Task]:
    tasks = load_tasks(cfg.queries, split=cfg.split, uids=cfg.uids or None)
    wanted = set(cfg.uids or [])
    if wanted:
        return [t for t in tasks if t.uid in wanted]
    filtered = [
        t for t in tasks
        if t.days in cfg.days
        and t.start_city != t.target_city
        and t.target_city in CITY_SLUGS
    ]
    # 多天数时轮转取样，避免“前 10 条全是 1 天题”
    if len(cfg.days) == 1:
        return filtered[: cfg.limit]
    buckets = {d: [] for d in cfg.days}
    for t in filtered:
        buckets.setdefault(t.days, []).append(t)
    picked: list[Task] = []
    while len(picked) < cfg.limit and any(buckets.values()):
        for d in cfg.days:
            if buckets.get(d) and len(picked) < cfg.limit:
                picked.append(buckets[d].pop(0))
    return picked


def _local_validation(task: Task, plan: dict, home: Path) -> dict:
    """冻结方案后的只读诊断：schema + commonsense + hard（hard 只评分，不回灌）。"""
    out = {"schema": None, "commonsense": {}, "hard": None, "errors": []}
    try:
        from jsonschema import Draft7Validator

        schema = json.loads(
            (home / "chinatravel/evaluation/output_schema.json").read_text(encoding="utf-8")
        )
        errs = sorted(Draft7Validator(schema).iter_errors(plan), key=lambda e: list(e.path))
        out["schema"] = not errs
        out["errors"] += [f"schema:{list(e.path)}:{e.message}" for e in errs[:5]]
    except Exception as exc:
        out["schema"] = False
        out["errors"].append(f"schema_exception:{type(exc).__name__}:{exc}")
    try:
        from chinatravel.symbol_verification.commonsense_constraint import (
            Is_activity_grounded,
            Is_attractions_correct,
            Is_hotels_correct,
            Is_intercity_transport_correct,
            Is_restaurants_correct,
            Is_space_correct,
            Is_time_correct,
            Is_transport_correct,
        )

        checks = [
            Is_activity_grounded,
            Is_intercity_transport_correct,
            Is_attractions_correct,
            Is_hotels_correct,
            Is_restaurants_correct,
            Is_transport_correct,
            Is_time_correct,
            Is_space_correct,
        ]
        for fn in checks:
            try:
                table, info = fn(task.raw, plan, verbose=False)
                bad = [c for c in table.columns if c != "data_id" and table.iloc[0][c] != 0]
                out["commonsense"][fn.__name__] = {"ok": not bad, "violations": bad, "info": str(info)[:500]}
                if bad:
                    out["errors"].append(f"commonsense:{fn.__name__}:{bad}")
            except Exception as exc:
                out["commonsense"][fn.__name__] = {"ok": False, "exception": f"{type(exc).__name__}:{exc}"}
                out["errors"].append(f"commonsense_exception:{fn.__name__}:{exc}")
    except Exception as exc:
        out["errors"].append(f"commonsense_import:{type(exc).__name__}:{exc}")
    try:
        from chinatravel.symbol_verification.hard_constraint import evaluate_constraints_py

        results = evaluate_constraints_py(task.hard_logic_py, plan)
        out["hard"] = {"passed": sum(1 for x in results if x), "total": len(results),
                       "results": [bool(x) for x in results]}
        if not all(results):
            out["errors"].append("hard_constraint_failed")
    except Exception as exc:
        out["errors"].append(f"hard_exception:{type(exc).__name__}:{exc}")
    return out


def run_one(task: Task, cfg: RunConfig, sandbox: Sandbox, run_dir: Path) -> dict:
    uid_dir = run_dir / "uids" / task.uid
    uid_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "uid": task.uid,
        "split": task.split,
        "days": task.days,
        "people": task.people,
        "start_city": task.start_city,
        "target_city": task.target_city,
        "nature_language": task.nature_language,
        "status": "started",
        "seconds": 0.0,
        "llm_calls": 0,
        "external_calls": 0,
    }
    t0 = time.perf_counter()
    try:
        _write_json(uid_dir / "task.json", task.raw)
        needs = build_needs(task, sandbox)
        _write_json(uid_dir / "needs.json", needs)
        if cfg.mode == "rule":
            plan, plan_meta = build_rule_plan(task, needs, sandbox)
        elif cfg.mode == "native":
            from .native import build_native_plan

            plan, plan_meta = build_native_plan(task, needs, sandbox)
            meta["llm_calls"] = (plan_meta.get("native") or {}).get("llm_calls") or 0
            meta["external_calls"] = (plan_meta.get("native") or {}).get("external_calls") or 0
        else:
            from .llm_plan import build_llm_plan

            plan, plan_meta = build_llm_plan(task, needs, sandbox)
            meta["llm_calls"] = plan_meta.pop("llm_calls", 0)
        _write_json(uid_dir / "plan.json", plan)
        _write_json(uid_dir / "selection.json", plan_meta)
        validation = _local_validation(task, plan, cfg.home)
        _write_json(uid_dir / "validation.json", validation)
        results_dir = cfg.home / "results" / cfg.method
        results_dir.mkdir(parents=True, exist_ok=True)
        _write_json(results_dir / f"{task.uid}.json", plan)
        meta.update(
            status="done",
            cuisine=needs.get("cuisine_pref"),
            schema=validation["schema"],
            hard=validation.get("hard"),
            validation_errors=validation["errors"],
            plan_meta={k: v for k, v in plan_meta.items() if k != "llm"},
        )
    except PlanError as exc:
        meta.update(status="plan_error", error=f"{exc.code}:{exc.detail}")
        results_dir = cfg.home / "results" / cfg.method
        results_dir.mkdir(parents=True, exist_ok=True)
        _write_json(results_dir / f"{task.uid}.json", {})
    except Exception as exc:
        meta.update(status="exception", error=f"{type(exc).__name__}:{exc}")
        _write_json(uid_dir / "traceback.txt", traceback.format_exc())
        results_dir = cfg.home / "results" / cfg.method
        results_dir.mkdir(parents=True, exist_ok=True)
        _write_json(results_dir / f"{task.uid}.json", {})
    meta["seconds"] = round(time.perf_counter() - t0, 2)
    _write_json(uid_dir / "meta.json", meta)
    return meta


def run_official_eval(cfg: RunConfig, run_dir: Path) -> dict:
    scores_path = cfg.home / "your_tpc_scores.json"
    if scores_path.exists():
        shutil.move(str(scores_path), str(run_dir / "your_tpc_scores.prev.json"))
    cmd = [str(cfg.python), "eval_tpc.py", "--splits", cfg.local_split, "--method", cfg.method, "--lang", "zh"]
    proc = subprocess.run(cmd, cwd=cfg.home, text=True, encoding="utf-8",
                          capture_output=True, timeout=3600)
    (run_dir / "eval.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (run_dir / "eval.stderr.txt").write_text(proc.stderr, encoding="utf-8")
    scores = {}
    if scores_path.exists():
        try:
            scores = json.loads(scores_path.read_text(encoding="utf-8"))
        except Exception:
            scores = {"raw": scores_path.read_text(encoding="utf-8", errors="replace")}
        shutil.copy2(scores_path, run_dir / "scores.json")
    return {
        "returncode": proc.returncode,
        "scores": scores,
        "stdout_tail": proc.stdout[-3000:],
        "stderr_tail": proc.stderr[-1500:],
    }


def write_report(run_dir: Path, cfg: RunConfig, metas: list[dict], eval_result: dict) -> None:
    scores = eval_result.get("scores", {})
    lines = [
        f"# ChinaTravel smoke · {cfg.method}",
        "",
        f"- source split: `{cfg.split}`",
        f"- local split: `{cfg.local_split}`",
        f"- queries: {len(metas)}",
        f"- mode: `{cfg.mode}`",
        f"- method: `{cfg.method}`",
        "",
        "## Scores",
        "",
    ]
    for key in ("schema", "MicEPR", "MacEPR", "C-LPR", "FPR", "DAV", "ATT", "DDR", "overall"):
        if key in scores:
            lines.append(f"- **{key}**: {scores[key]}")
    lines += ["", "## Per query", "", "| uid | days | route | status | cuisine | schema | hard | seconds |", "|---|---|---|---|---|---|---|---|"]
    for m in metas:
        route = f"{m['start_city']}→{m['target_city']}"
        hard = m.get("hard") or {}
        hard_txt = f"{hard.get('passed','')}/{hard.get('total','')}" if hard else ""
        lines.append(
            f"| {m['uid']} | {m['days']} | {route} | {m['status']} | {m.get('cuisine') or ''} | "
            f"{m.get('schema')} | {hard_txt} | {m.get('seconds')} |"
        )
    lines += ["", "## Eval stdout tail", "", "```", eval_result.get("stdout_tail", ""), "```", ""]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    _write_json(run_dir / "summary.json", {
        "config": {k: str(v) for k, v in cfg.__dict__.items()},
        "metas": metas,
        "scores": scores,
        "eval_returncode": eval_result.get("returncode"),
    })


def run_batch(cfg: RunConfig) -> dict:
    if str(cfg.home) not in sys.path:
        sys.path.insert(0, str(cfg.home))
    run_dir = _run_dir(cfg)
    run_dir.mkdir(parents=True, exist_ok=True)
    tasks = select_tasks(cfg)
    if not tasks:
        raise SystemExit("no tasks selected")
    if not cfg.local_split:
        cfg.local_split = _local_split(cfg, len(tasks))
    export_local_split(cfg.home, cfg.local_split, tasks)
    _write_json(run_dir / "queries.json", [t.raw for t in tasks])
    sandbox = Sandbox(cfg.home)
    metas = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task.uid} {task.start_city}->{task.target_city} days={task.days}")
        if cfg.resume and (run_dir / "uids" / task.uid / "meta.json").exists():
            old = json.loads((run_dir / "uids" / task.uid / "meta.json").read_text(encoding="utf-8"))
            if old.get("status") == "done":
                print("  resume: done")
                metas.append(old)
                continue
        metas.append(run_one(task, cfg, sandbox, run_dir))
        print(f"  {metas[-1]['status']} {metas[-1].get('error','')}")
    eval_result = run_official_eval(cfg, run_dir) if cfg.run_eval else {"scores": {}, "returncode": None}
    write_report(run_dir, cfg, metas, eval_result)
    print("scores:", json.dumps(eval_result.get("scores", {}), ensure_ascii=False))
    print("report:", run_dir / "report.md")
    return {"run_dir": str(run_dir), "scores": eval_result.get("scores", {}), "metas": metas}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ChinaTravel smoke runner")
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--split", default="easy")
    p.add_argument("--local-split", default="")
    p.add_argument("--method", default="rule_v0")
    p.add_argument("--days", default="1,2,3")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--uids", nargs="*", default=[])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--home", type=Path, default=DEFAULT_HOME)
    p.add_argument("--python", type=Path, default=DEFAULT_PY)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--no-eval", action="store_true")
    p.add_argument("--mode", choices=["rule", "llm", "native"], default="rule")
    args = p.parse_args(argv)
    cfg = RunConfig(
        queries=args.queries,
        split=args.split,
        local_split=args.local_split,
        method=args.method,
        days=tuple(int(x) for x in args.days.split(",") if x.strip()),
        limit=args.limit,
        uids=list(args.uids),
        out=args.out,
        home=args.home,
        python=args.python,
        resume=args.resume,
        run_eval=not args.no_eval,
        mode=args.mode,
    )
    result = run_batch(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
