"""webapp 薄后端（docs/frontend-plan.md 第 2/5 节）。

只做三件事：跑管线出 JSON、下发地图 JS key、托管静态文件。
规划逻辑一律 import scripts/plan_agent.py 的 run_plan，这里不复制任何流程。

启动：
    E:\\code\\LLM-guide-agent\\.venv\\Scripts\\python.exe -m uvicorn webapp.server:app --port 8300
（在仓库根目录起；.env 由 plan_agent 导入时自行加载）
"""

import asyncio
import concurrent.futures as cf
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
PLANS_DIR = REPO_ROOT / ".cache" / "plans"
STATIC_DIR = Path(__file__).resolve().parent / "static"

import sys

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT / "mcp"))
import plan_agent as pa  # noqa: E402  （导入时已加载仓库根 .env）

# 网页用户手滑连点比 CLI 贵，这一条只作用于 web 入口，不改 CLI 默认（第 5 节）。
MAX_EXTERNAL_WEB = 24
LIST_LIMIT = 20
PLAN_ID_RE = re.compile(r"^[\w.\-一-鿿]{1,120}$")

app = FastAPI(title="LLM-guide-agent webapp", docs_url=None, redoc_url=None)

# 一次只让一个规划在跑：单次规划内部自己有并发，两个任务同时跑就会把高德个人 key
# 的并发额度（实测上限 3，见 plan_agent.run_calls 的注释）吃满。本地自用不需要并行。
EXECUTOR = cf.ThreadPoolExecutor(max_workers=1)
JOB_TIMEOUT = 240        # 秒；一次规划实测 8~20 秒，留足补查轮与网络抖动的余量
JOBS: dict[str, dict] = {}
JOB_LOCK = asyncio.Lock()


class PlanRequest(BaseModel):
    question: str = Field(min_length=4, max_length=600)
    date: str | None = None
    offline: bool = False


def job_key(question: str, date: str | None, offline: bool) -> str:
    """同一 question+date 的 in-flight 任务去重：第二个请求直接挂到同一个 job（第 5 节）。"""
    raw = f"{question.strip()}|{date or ''}|{int(offline)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def plan_path(plan_id: str) -> Path:
    if not PLAN_ID_RE.match(plan_id or ""):
        raise HTTPException(400, "plan_id 不合法")
    path = (PLANS_DIR / f"{plan_id}.json").resolve()
    if PLANS_DIR.resolve() not in path.parents:   # 兜住 ..\ 之类的穿越
        raise HTTPException(400, "plan_id 不合法")
    return path


def run_job(job: dict) -> None:
    """在 EXECUTOR 线程里跑一遍管线。plan_agent 内部自己开线程池取数，
    所以这里绝不能把它丢进事件循环线程（第 5 节说的不要 asyncio.run）。"""
    job["state"] = "running"
    job["started"] = time.perf_counter()
    try:
        res = pa.run_plan(
            job["question"],
            date=job["date"],
            json_mode=True,
            max_external=MAX_EXTERNAL_WEB,
            quiet=True,
            dry_run=job["offline"],
            on_stage=lambda line: job["stages"].append(line),
        )
    except SystemExit as exc:      # run_plan 缺 key 时用 SystemExit 说话
        job["state"] = "error"
        job["error"] = f"配置缺失：{exc}"
        return
    except Exception as exc:       # 管线崩了要把原因留给前端，不能只留个转圈
        job["state"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return

    job["seconds"] = round(time.perf_counter() - job["started"], 1)
    if job["offline"]:
        job["result"] = {"offline": True, "needs": res["needs"],
                         "start_date": res["start_date"],
                         "planned_calls": res["planned_calls"],
                         "wiki_pages": res["wiki_pages"], "stages": res["trace"],
                         "external_calls": 0, "llm_calls": res["llm_calls"]}
        job["state"] = "done"
        return

    plan = res["plan"]
    job["result"] = plan
    # degraded 的方案也落盘：前端要能按 Markdown 原文显示它，历史页据此也能复盘。
    job["plan_file"] = str(pa.save_plan(plan).relative_to(REPO_ROOT))
    job["state"] = "done"


@app.post("/api/plan")
async def api_plan(req: PlanRequest) -> dict:
    """响应体就是 final_plan（第 3 节的契约），同时把 job_id 带回去。
    规划要跑 10~30 秒，job_id 由 question+date 哈希得出，前端可以在等响应期间
    并发轮询 /api/plan/<job_id>/status 看阶段进度。
    """
    key = job_key(req.question, req.date, req.offline)
    async with JOB_LOCK:
        job = JOBS.get(key)
        # 只有还在跑的任务才复用；跑完过的重新跑一遍——天气与"班次待核实"只对当天有效（第 8 节）。
        if job and job["state"] not in ("queued", "running"):
            job = None
        if not job:
            job = {"id": key, "question": req.question.strip(), "date": req.date,
                   "offline": req.offline, "state": "queued", "stages": [],
                   "created": datetime.now().isoformat(timespec="seconds"),
                   "created_ts": time.perf_counter(), "started": None, "future": None,
                   "seconds": None, "result": None, "error": None, "plan_file": None}
            JOBS[key] = job
            while len(JOBS) > 60:   # 进程内任务表别无限涨，按插入顺序丢最旧的
                JOBS.pop(next(iter(JOBS)))
            job["future"] = asyncio.get_running_loop().run_in_executor(EXECUTOR, run_job, job)
        future = job["future"]

    try:
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=JOB_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"任务 {key} 超过 {JOB_TIMEOUT} s 没跑完，"
                                 f"进度请用 /api/plan/{key}/status（线程仍在跑，别重复提交）")
    if job["state"] == "error":
        raise HTTPException(500, job["error"] or "管线失败，原因没记下来")
    return {"job_id": key, **(job["result"] or {})}


@app.get("/api/plan/{job_id}/status")
async def api_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"没有这个 job_id：{job_id}（进程重启后 job 不保留，"
                                 f"已完成的方案请用 /api/plans/{{plan_id}}）")
    return _status_body(job)


def _status_body(job: dict) -> dict:
    elapsed = None
    if job["state"] in ("queued", "running"):
        elapsed = round(time.perf_counter() - job["created_ts"], 1)
    return {"job_id": job["id"], "state": job["state"], "stages": list(job["stages"]),
            "question": job["question"], "offline": job["offline"],
            "elapsed": elapsed, "seconds": job["seconds"], "error": job["error"],
            "result_url": f"/api/plan/{job['id']}" if job["state"] == "done" else None}


@app.get("/api/job/running")
async def api_running() -> dict:
    """POST /api/plan 是阻塞的（响应体就是 final_plan），前端要在此期间看进度就轮询这里——
    不用先知道 job_id，返回最近启动的那个还在跑的任务。"""
    live = [j for j in JOBS.values() if j["state"] in ("queued", "running")]
    if not live:
        return {"running": False, "stages": []}
    job = max(live, key=lambda j: j["created_ts"])
    return {"running": True, **_status_body(job)}


@app.get("/api/plan/{job_id}")
async def api_job_result(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"没有这个 job_id：{job_id}")
    if job["state"] == "error":
        raise HTTPException(500, job["error"] or "任务失败，原因没记下来")
    if job["state"] != "done":
        raise HTTPException(409, f"任务还在 {job['state']}，先轮询 /api/plan/{job_id}/status")
    return job["result"]


@app.get("/api/config")
async def api_config() -> dict:
    """JS key 与 Web服务 key 不通用，缺了就给空串，前端自己隐藏地图块（第 3 节）。"""
    return {"amap_js_key": os.getenv("AMAP_JS_KEY") or "",
            "amap_js_security_code": os.getenv("AMAP_JS_SECURITY_CODE") or "",
            "server_time": datetime.now().isoformat(timespec="seconds")}


def summarize(plan: dict, path: Path) -> dict:
    src = plan.get("sources") or {}
    return {"plan_id": plan.get("plan_id"),
            "question": plan.get("question"),
            "destination": plan.get("destination"),
            "days_count": plan.get("days_count"),
            "start_date": plan.get("start_date"),
            "end_date": plan.get("end_date"),
            "budget_yuan": plan.get("budget_yuan"),
            "degraded": bool(plan.get("degraded")),
            "external_calls": src.get("external_calls"),
            "llm_calls": src.get("llm_calls"),
            "seconds": src.get("seconds"),
            # plan_id 里带的是规划发生的时刻；文件 mtime 会被任何一次改写带偏，只作兜底。
            "created": plan_id_time(plan.get("plan_id") or "") or
                       datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}


def plan_id_time(plan_id: str) -> str | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})", plan_id)
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}" if m else None


@app.get("/api/plans")
async def api_plans() -> dict:
    """历史页用：按修改时间取最近 20 条，纯文件读，没有数据库。"""
    if not PLANS_DIR.exists():
        return {"plans": [], "note": "还没有落盘的方案（跑一次 POST /api/plan 就有了）"}
    files = sorted(PLANS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for f in files[:LIST_LIMIT]:
        try:
            rows.append(summarize(json.loads(f.read_text(encoding="utf-8")), f))
        except (json.JSONDecodeError, OSError) as exc:
            rows.append({"plan_id": f.stem, "error": f"读不动：{type(exc).__name__}"})
    return {"plans": rows, "total": len(files)}


@app.get("/api/plans/{plan_id}")
async def api_plan_detail(plan_id: str) -> dict:
    path = plan_path(plan_id)
    if not path.exists():
        raise HTTPException(404, f".cache/plans/{plan_id}.json 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:   # M3 才建前端目录；没有它也不能让 / 报 500
    @app.get("/")
    async def root_placeholder() -> dict:
        return {"ok": True, "webapp": "M2 薄后端已就绪",
                "endpoints": ["/api/plan", "/api/plan/{job_id}/status", "/api/plan/{job_id}",
                              "/api/config", "/api/plans", "/api/plans/{plan_id}"],
                "static_dir": str(STATIC_DIR) + "（还没建，M3 的前端放这里）"}
