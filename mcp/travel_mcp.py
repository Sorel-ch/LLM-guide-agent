import functools
import inspect
import json
import os
import re
import time
from typing import Any
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from pathlib import Path


def _load_local_env() -> None:
    """从仓库根 .env 补全缺失的环境变量，使 Key 不依赖启动方式。"""
    for env_file in (
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent / ".env",
    ):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

APP_NAME = "travel"
DEFAULT_TIMEOUT = float(os.getenv("AMAP_HTTP_TIMEOUT", "10"))
# cost 必带：v5 只有请求了 cost 才返回 paths[].time 与 transits[].cost.duration
DEFAULT_SHOW_FIELDS = "cost,polyline,navi,tmc"
LOG_LEVEL = os.getenv("MCP_LOG_LEVEL", "ERROR")
METASO_MCP_URL = os.getenv("METASO_MCP_URL", "https://metaso.cn/api/mcp")
METASO_API_KEY = os.getenv("METASO_API_KEY")
METASO_TIMEOUT = float(os.getenv("METASO_HTTP_TIMEOUT", "30"))
METASO_SSE_READ_TIMEOUT = float(os.getenv("METASO_SSE_READ_TIMEOUT", "300"))

mcp = FastMCP(APP_NAME, log_level=LOG_LEVEL)


_KEY_IN_TEXT = re.compile(r"(?i)(key=)[^&\s'\"]+")


def mask_secrets(text: str) -> str:
    """抹掉错误信息里可能出现的明文 Key（httpx 异常会把完整 URL 带进来）。"""
    return _KEY_IN_TEXT.sub(r"\1***", text)


def ok(data: Any, query: dict[str, Any] | None = None) -> dict[str, Any]:
	return {
		"ok": True,
		"error_code": None,
		"error_msg": None,
		"query": query or {},
		"data": data,
		"meta": {"source": "AMap API"},
	}


def fail(code: str, msg: str, raw: Any = None) -> dict[str, Any]:
	payload = {
		"ok": False,
		"error_code": code,
		"error_msg": mask_secrets(msg),
		"data": None,
		"meta": {"source": "AMap API"},
	}
	if raw is not None:
		payload["raw"] = raw
	return payload


def get_api_key(api_key: str | None) -> str | None:
	return api_key or os.getenv("AMAP_API_KEY")


def metaso_ok(tool_name: str, data: Any, query: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "error_code": None,
        "error_msg": None,
        "query": query or {},
        "data": data,
        "meta": {
            "source": "Metaso MCP",
            "tool": tool_name,
        },
    }


def metaso_fail(code: str, msg: str, raw: Any = None, query: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error_code": code,
        "error_msg": msg,
        "data": None,
        "query": query or {},
        "meta": {
            "source": "Metaso MCP",
        },
    }
    if raw is not None:
        payload["raw"] = raw
    return payload


# ---------------------------------------------------------------------------
# 对外返回层：紧凑序列化 + 会话内去重
#
# 实测一次规划 140 秒里有 123 秒花在模型往返上，而往返耗时基本由上下文长度决定；
# FastMCP 对 dict 返回值一律按 indent=2 序列化，白白多占约四成体积。同参数再打一次
# 外部接口则纯烧配额（上一轮就为"拿耗时"整条重发过 4 次）。两件事都在这一层解决，
# 所以七个工具函数本体保持返回 dict，只在注册时包一层。
# ---------------------------------------------------------------------------

DEDUP_TTL = float(os.getenv("TRAVEL_DEDUP_TTL", "900"))
_CALL_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}  # 参数指纹 -> (时间, 第几次, 返回体)
_POI_SEEN: dict[str, int] = {}  # POI/tip 的 id -> 首次出现在第几次调用
_STATE = {"seq": 0}


def _compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _text_result(payload: dict[str, Any]) -> types.CallToolResult:
    """只留一份正文。

    工具函数直接返回 dict 时，FastMCP 会同时给出 indent=2 的 text 正文和一份
    structuredContent；实测一次天气调用 3637 B 里有 1281 B 是同一份数据的重复。
    显式构造 CallToolResult 并注解为它，才会完全不走结构化那条路。
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_compact(payload))],
        structuredContent=None,
    )


def _fingerprint(name: str, args: dict[str, Any]) -> str:
    kept = {k: v for k, v in args.items() if k != "api_key" and v not in (None, "", [], {})}
    return f"{name}|{json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


def _mark_seen_entities(payload: dict[str, Any]) -> None:
    """换关键词搜到同一个 POI 时（"杭州东站" / "火车东站"），标出来让模型看得见。"""
    data = payload.get("data") or {}
    rows = data.get("pois") or data.get("tips") or []
    if not isinstance(rows, list):
        return
    with_id = [r for r in rows if isinstance(r, dict) and r.get("id")]
    dupes = 0
    for row in with_id:
        seen = _POI_SEEN.get(row["id"])
        if seen:
            row["seen_in_call"] = seen
            dupes += 1
        else:
            _POI_SEEN[row["id"]] = _STATE["seq"]
    if with_id and dupes == len(with_id):
        payload.setdefault("meta", {})["note"] = (
            "这些 POI 之前的调用已全部返回过（seen_in_call 标明是第几次），"
            "坐标等信息在上文里，不必再换关键词重复检索"
        )


def travel_tool(fn):
    sig = inspect.signature(fn)

    def finish(args: dict[str, Any], payload: dict[str, Any]) -> types.CallToolResult:
        key = _fingerprint(fn.__name__, args)
        hit = _CALL_CACHE.get(key)
        if hit and time.time() - hit[0] < DEDUP_TTL:
            _ts, seq, cached = hit
            # 只改顶层 meta，浅拷贝即可，不动缓存里的对象
            meta = {**(cached.get("meta") or {}), "reused_call": seq}
            meta["note"] = f"参数与第 {seq} 次调用完全相同，已复用其结果，本次未请求外部接口"
            return _text_result({**cached, "meta": meta})

        _STATE["seq"] += 1
        payload.setdefault("meta", {})["call_seq"] = _STATE["seq"]
        _mark_seen_entities(payload)
        if payload.get("ok"):  # 失败不缓存，否则瞬时错误会把后续重试一起挡掉
            _CALL_CACHE[key] = (time.time(), _STATE["seq"], payload)
        return _text_result(payload)

    def args_of(call_args, call_kwargs):
        return dict(sig.bind(*call_args, **call_kwargs).arguments)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*call_args, **call_kwargs):
            return finish(args_of(call_args, call_kwargs), await fn(*call_args, **call_kwargs))
    else:

        @functools.wraps(fn)
        def wrapper(*call_args, **call_kwargs):
            return finish(args_of(call_args, call_kwargs), fn(*call_args, **call_kwargs))

    # 注解成 CallToolResult 才能让 FastMCP 不再生成 output schema（否则返回 dict 会被双份序列化）
    wrapper.__signature__ = sig.replace(return_annotation=types.CallToolResult)
    return mcp.tool()(wrapper)


async def call_metaso_tool(tool_name: str, arguments: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    if not METASO_API_KEY:
        return metaso_fail(
            "MISSING_METASO_API_KEY",
            "缺少秘塔 API Key，请设置 METASO_API_KEY。",
            query=query,
        )

    headers = {"Authorization": f"Bearer {METASO_API_KEY}"}
    timeout = httpx.Timeout(METASO_TIMEOUT, read=METASO_SSE_READ_TIMEOUT)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            async with streamable_http_client(METASO_MCP_URL, http_client=client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
    except httpx.HTTPError as exc:
            return metaso_fail(
                "UPSTREAM_HTTP_ERROR",
                f"连接秘塔 MCP 失败: {exc}",
                query=query,
            )
    except Exception as exc:
            return metaso_fail(
                "UPSTREAM_RUNTIME_ERROR",
                f"调用秘塔 MCP 工具失败: {exc}",
                query=query,
            )

    if result.isError:
        return metaso_fail(
            "UPSTREAM_BUSINESS_ERROR",
            f"秘塔工具 {tool_name} 返回失败。",
            raw=result.model_dump(),
            query=query,
        )

    return metaso_ok(tool_name, result.model_dump(), query=query)


@travel_tool
async def metaso_web_search(
    q: str,
    scope: str | None = None,
    includeSummary: bool = False,
    includeRawContent: bool = False,
    size: int = 10,
) -> dict[str, Any]:
    """封装秘塔网络搜索能力到本地 travel-mcp。"""
    if size < 1 or size > 50:
        return metaso_fail("INVALID_ARGUMENT", "size 必须在 1-50 之间。", query={"q": q})

    arguments: dict[str, Any] = {
        "q": q,
        "includeSummary": includeSummary,
        "includeRawContent": includeRawContent,
        "size": size,
    }
    if scope:
        arguments["scope"] = scope

    return await call_metaso_tool(
        "metaso_web_search",
        arguments,
        {
            "q": q,
            "scope": scope,
            "includeSummary": includeSummary,
            "includeRawContent": includeRawContent,
            "size": size,
        },
    )


@travel_tool
async def metaso_web_reader(url: str, format: str = "markdown") -> dict[str, Any]:
    """封装秘塔网页内容读取能力到本地 travel-mcp。"""
    if format not in {"json", "markdown"}:
        return metaso_fail("INVALID_ARGUMENT", "format 只能是 json 或 markdown。", query={"url": url})

    arguments = {
        "url": url,
        "format": format,
    }
    return await call_metaso_tool("metaso_web_reader", arguments, {"url": url, "format": format})


@travel_tool
async def metaso_chat(message: str, model: str = "fast") -> dict[str, Any]:
    """封装秘塔智能问答能力到本地 travel-mcp。"""
    arguments = {
        "message": message,
        "model": model,
    }
    return await call_metaso_tool("metaso_chat", arguments, {"message": message, "model": model})

@travel_tool
def get_amap_input_tips(
    keywords: str,
    city: str | None = None,
    type: str | None = None,
    location: str | None = None, # 格式 lon,lat
    datatype: str = "all", # 返回数据类型，all / poi / bus / admin
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    高德地图输入提示 (Auto-complete/Input Tips)。
    
    用于根据用户输入的关键词，提供地点、商圈、公交线路等的补全建议。
    
    Args:
        keywords: 用户输入的关键词，例如 "天安门"。
        city: 城市编码 (adcode) 或中文城市名，用于限制搜索范围。
        type: 限制返回结果的类型，例如 "汽车站" 或 "美食"。
        location: 用于周边推荐的中心点，格式 `lon,lat`。如果设置了 keywords，该参数作为中心点影响排序。
        datatype: 返回数据的种类，可选：
                  - all: 返回所有类型 (默认)
                  - poi: 返回兴趣点
                  - bus: 返回公交线路/站点
                  - admin: 返回行政区划
        api_key: 可选，未传时使用环境变量 AMAP_API_KEY。
        
    Returns:
        高德输入提示接口返回的 JSON。
    """
    key = get_api_key(api_key)
    if not key:
        return fail("MISSING_API_KEY", "缺少高德 API Key，请设置 AMAP_API_KEY 或传入 api_key。")
    
    if datatype not in {"all", "poi", "bus", "admin"}:
        return fail("INVALID_ARGUMENT", "datatype 只能是 all, poi, bus 或 admin。")
    
    url = "https://restapi.amap.com/v3/assistant/inputtips"
    params = {
        "key": key,
        "keywords": keywords,
        "output": "JSON"
    }
    
    # 添加可选参数
    if city:
        params["city"] = city
    if type:
        params["type"] = type
    if location:
        params["location"] = location
    params["datatype"] = datatype

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return fail("UPSTREAM_HTTP_ERROR", f"请求高德输入提示接口失败: {exc}")

    # 注意：InputTips 的状态码逻辑与路径规划略有不同
    # status 为 1 表示请求成功，info 字段可能包含更详细的业务信息
    if data.get("status") != "1":
        return fail(
            "UPSTREAM_BUSINESS_ERROR", 
            data.get("info", "高德接口返回失败"), 
            raw=data
        )

    return ok(
        data=data,
        query={
            "keywords": keywords,
            "city": city,
            "type": type,
            "location": location,
            "datatype": datatype
        }
    )

@travel_tool
def get_amap_weather(
	city: str,
	extensions: str = "base",
	api_key: str | None = None,
) -> dict[str, Any]:
	"""查询高德天气。
    - 当需要获取当前实时天气状况时，使用 extensions="base"
    - 当需要获取未来天气预报时，使用 extensions="all"
    - 如果不确定需要哪种数据，默认使用"base"获取实时天气

	Args:
		city: 城市 adcode 或 citycode，例如 110000。
		extensions: base(实况) 或 all(预报)。
		api_key: 可选，未传时使用环境变量 AMAP_API_KEY。

	Returns:
		高德天气接口返回的 JSON。
	"""
	key = get_api_key(api_key)
	if not key:
		return fail("MISSING_API_KEY", "缺少高德 API Key，请设置 AMAP_API_KEY 或传入 api_key。")

	if extensions not in {"base", "all"}:
		return fail("INVALID_ARGUMENT", "extensions 只能是 base 或 all。")

	url = "https://restapi.amap.com/v3/weather/weatherInfo"
	params = {
		"city": city,
		"key": key,
		"extensions": extensions,
		"output": "JSON",
	}

	try:
		with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
			resp = client.get(url, params=params)
			resp.raise_for_status()
			data = resp.json()
	except httpx.HTTPError as exc:
		return fail("UPSTREAM_HTTP_ERROR", f"请求高德天气接口失败: {exc}")

	if data.get("status") != "1":
		return fail("UPSTREAM_BUSINESS_ERROR", data.get("info", "高德接口返回失败"), raw=data)

	return ok(data=data, query={"city": city, "extensions": extensions})


@travel_tool
def get_amap_direction(
    origin: str,
    destination: str,
    mode: str = "driving",
    # --- 驾车 (driving) 专用参数 ---
    strategy: int | None = None,
    waypoints: str | None = None, # 途经点
    avoidpolygons: str | None = None, # 避让区域
    plate: str | None = None, # 车牌号
    cartype: int = 0, # 车辆类型 (0-7座, 1-8-19座, 2-20-40座)
    # --- 公交 (transit) 专用参数 ---
    city1: str | None = None,
    city2: str | None = None,
    nightflag: int | None = None,
    # --- 骑行 (bicycling) 专用参数 ---
    alternative_route: int | None = None,
    # --- 通用参数 ---
    show_fields: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    查询高德路径规划 (新版 v5 API)，统一支持多种出行方式。
    
    Args:
        origin: 起点经纬度，格式 `lon,lat`。
        destination: 终点经纬度，格式 `lon,lat`。
        mode: 出行方式。支持：
              - driving: 驾车
              - walking: 步行
              - bicycling: 骑行 (新增)
              - transit: 公交
              - electrobike: 电动车
        strategy: 驾车策略 (0-速度优先, 32-费用优先等)。
        waypoints: 驾车途经点，格式 "lon,lat;lon,lat"。
        alternative_route: 骑行备选路线数量 (1-3)。
        city1/city2: 公交起终点城市编码。
        show_fields: 返回的详细字段，默认 "cost,polyline,navi,tmc"；
              不显式传参即可拿到耗时（v5 只有带 cost 才返回 paths[].time）。
    """
    key = get_api_key(api_key)
    if not key:
        return fail("MISSING_API_KEY", "缺少高德 API Key，请设置 AMAP_API_KEY")

    # --- 1. 模式映射与 URL 定义 (V5 版本) ---
    mode_map = {
        "driving": "https://restapi.amap.com/v5/direction/driving",
        "walking": "https://restapi.amap.com/v5/direction/walking",
        "bicycling": "https://restapi.amap.com/v5/direction/bicycling",
        "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
        "electrobike": "https://restapi.amap.com/v5/direction/electrobike",
    }
    
    if mode not in mode_map:
        return fail("INVALID_ARGUMENT", f"不支持的 mode。请选择: {list(mode_map.keys())}")

    url = mode_map[mode]
    params = {
        "key": key,
        "origin": origin,
        "destination": destination,
        "output": "JSON"
    }

    # --- 2. 根据不同模式动态处理参数 ---
    try:
        if mode == "driving":
            params["show_fields"] = show_fields or DEFAULT_SHOW_FIELDS
            if strategy is not None: params["strategy"] = strategy
            if waypoints: params["waypoints"] = waypoints
            if avoidpolygons: params["avoidpolygons"] = avoidpolygons
            if plate: params["plate"] = plate
            if cartype in [0, 1, 2]: params["cartype"] = cartype

        elif mode == "walking":
            params["show_fields"] = show_fields or DEFAULT_SHOW_FIELDS

        elif mode == "bicycling":
            # 骑行参数
            params["show_fields"] = show_fields or DEFAULT_SHOW_FIELDS
            if alternative_route is not None:
                if 1 <= alternative_route <= 3:
                    params["alternative_route"] = alternative_route
                else:
                    return fail("INVALID_ARGUMENT", "骑行 alternative_route 必须在 1-3 之间")

        elif mode == "transit":
            # 公交强制校验
            if not city1 or not city2:
                return fail("INVALID_ARGUMENT", "公交模式必须提供 city1 和 city2")
            params["city1"] = city1
            params["city2"] = city2
            if nightflag in [0, 1]: params["nightflag"] = nightflag
            params["show_fields"] = show_fields or DEFAULT_SHOW_FIELDS

        elif mode == "electrobike":
            params["show_fields"] = show_fields or DEFAULT_SHOW_FIELDS

    except Exception as e:
        return fail("INVALID_ARGUMENT", f"参数处理异常: {str(e)}")

    # --- 3. 发送请求 ---
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return fail("UPSTREAM_HTTP_ERROR", f"请求失败 ({mode}): {exc}")

    # --- 4. 结果处理 ---
    if data.get("status") != "1":
        return fail("UPSTREAM_BUSINESS_ERROR", data.get("info", "接口返回失败"), raw=data)

    return ok(
        data=data,
        query={
            "mode": mode,
            "origin": origin,
            "destination": destination,
            "strategy": strategy if mode == "driving" else None,
            "alternative_route": alternative_route if mode == "bicycling" else None,
        },
    )

@travel_tool
def get_amap_poi_search(
    keywords: str | None = None,
    types: str | None = None,
    city: str | None = None,
    location: str | None = None,  # 格式 lon,lat
    radius: int | None = None,    # 单位：米
    offset: int = 20,             # 每页结果数量，默认20，最大50
    page: int = 1,                # 页码
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    高德地图POI搜索工具，支持关键字搜索和周边搜索。
    
    根据参数自动选择搜索模式：
    - 如果提供 location 参数，则进行周边搜索（在指定点周围搜索POI）
    - 否则进行关键字搜索（根据关键字和城市搜索POI）
    
    Args:
        keywords: 搜索关键字，例如 "咖啡厅"、"医院"。周边搜索时可选，关键字搜索时必填。
        types: POI类型，例如 "010000"（美食）、"020000"（酒店）。多个类型用|分隔。
        city: 城市编码 (adcode) 或中文城市名，例如 "110000" 或 "北京"。关键字搜索时建议提供。
        location: 中心点坐标，格式 `lon,lat`，例如 "116.481499,39.990475"。提供时进行周边搜索。
        radius: 搜索半径，单位米，范围0-50000。周边搜索时有效，默认3000米。
        offset: 每页返回结果数量，1-50，默认20。
        page: 页码，从1开始。
        api_key: 可选，未传时使用环境变量 AMAP_API_KEY。
        
    Returns:
        高德POI搜索接口返回的JSON数据，包含搜索结果列表。
    """
    key = get_api_key(api_key)
    if not key:
        return fail("MISSING_API_KEY", "缺少高德 API Key，请设置 AMAP_API_KEY 或传入 api_key。")
    
    # 参数校验
    if offset < 1 or offset > 50:
        return fail("INVALID_ARGUMENT", "offset 必须在 1-50 之间")
    if page < 1:
        return fail("INVALID_ARGUMENT", "page 必须大于等于 1")
    
    if location:
        # 周边搜索模式
        if not radius:
            radius = 3000  # 默认3000米
        if radius < 0 or radius > 50000:
            return fail("INVALID_ARGUMENT", "radius 必须在 0-50000 米之间")
        
        url = "https://restapi.amap.com/v5/place/around"
        params = {
            "key": key,
            "location": location,
            "radius": radius,
            "offset": offset,
            "page": page,
            "output": "JSON"
        }
        
        # 周边搜索的关键字是可选的
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types
        if city:
            params["city"] = city
            
        mode = "around"
        
    else:
        # 关键字搜索模式
        if not keywords:
            return fail("INVALID_ARGUMENT", "关键字搜索时 keywords 参数不能为空")
        
        url = "https://restapi.amap.com/v5/place/text"
        params = {
            "key": key,
            "keywords": keywords,
            "offset": offset,
            "page": page,
            "output": "JSON"
        }
        
        if types:
            params["types"] = types
        if city:
            params["city"] = city
            
        mode = "text"
    
    # 发送请求
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return fail("UPSTREAM_HTTP_ERROR", f"请求高德POI搜索接口失败: {exc}")
    
    # 处理响应
    if data.get("status") != "1":
        return fail(
            "UPSTREAM_BUSINESS_ERROR", 
            data.get("info", "高德接口返回失败"), 
            raw=data
        )
    
    return ok(
        data=data,
        query={
            "mode": mode,
            "keywords": keywords,
            "types": types,
            "city": city,
            "location": location,
            "radius": radius,
            "offset": offset,
            "page": page
        }
    )

def main() -> None:
	mcp.run()


if __name__ == "__main__":
	main()