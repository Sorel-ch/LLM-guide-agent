# raw/ — 不可变原始快照

LLM 只写入、不修改、不删除本目录内容。子目录按来源划分：

- `amap/` — 高德 MCP 工具返回的 JSON（天气/路径/POI/提示）
- `web/` — 秘塔 `metaso_web_reader` 抓取的网页正文（Markdown）
- `chat/` — `metaso_chat` 等问答结果（谨慎使用，需二次溯源）

命名：`<YYYY-MM-DD>_<主题>_<参数摘要>.<json|md>`，
例如 `2026-09-28_weather_110000_base.json`、`2026-09-28_杭州_三日游攻略.md`。

wiki 页面脚注引用这里的相对路径，保证每个结论可溯源。
