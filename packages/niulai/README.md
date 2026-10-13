# niulai

本地优先的 A 股 AI 助手。这个包装的是内核、CLI 与本地 Web 服务层：
自带 API Key，对话、会话与取数全部留在本机。

## 安装

需要 Python 3.11+。

```bash
pip install niulai                  # 或从源码：pip install -e packages/niulai
pip install "niulai[web]"           # Web 界面（niulai serve）需要
```

## 命令行

```bash
export NIULAI_API_KEY="sk-..."
niulai ask "茅台现在多少钱"        # 问一次
niulai chat                        # 多轮对话（会记住上下文）
niulai serve                       # 本地 Web 界面（http://127.0.0.1:8710）
niulai tools                       # 列出工具、数据源与各自的能力声明
```

## 作为库使用

```python
import asyncio

from niulai.agent import AgentRuntime
from niulai.artifacts import ArtifactStore
from niulai.config import Settings
from niulai.datasource import build_default_service
from niulai.events import EventEmitter
from niulai.llm import build_client
from niulai.tools import ReadFileTool, ToolRegistry, build_stock_tools


async def main() -> None:
    settings = Settings.load()
    service = build_default_service()
    store = ArtifactStore()
    tools = ToolRegistry(
        [*build_stock_tools(service), ReadFileTool(store)],
        artifact_store=store,          # 不给它，大结果就退回截断
    )

    emitter = EventEmitter(run_id="demo")
    emitter.subscribe(lambda ev: print(ev.to_dict()))

    llm = build_client(settings)
    try:
        runtime = AgentRuntime(settings=settings, llm=llm, tools=tools)
        result = await runtime.run("茅台现在多少钱", emitter=emitter)
        print(result.text)
    finally:
        await llm.aclose()
        await service.aclose()


asyncio.run(main())
```

多轮对话把上一次的 `result.messages` 传回 `run()` 即可，模型就知道"它"指的是哪只股票。

## 工具

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `stock_quote` / `stock_kline` / `stock_financial` / `stock_valuation` / `stock_anomaly` / `stock_auction` / `stock_moneyflow` | `read` + `network` | 单标的：行情 / K 线 / 财务 / 估值 / 当日异动 / 集合竞价 / 资金流 |
| `market_hot` / `market_limitup` / `market_dragontiger` / `market_auction_benchmark` / `market_index` | `read` + `network` | 市场视野：热股榜 / 涨跌停池与连板天梯 / 龙虎榜 / 短线风向标 / 指数与板块 |
| `stock_indicator` | `read` + `network` | 技术指标（MA / MACD / KDJ / RSI / 布林 / 量能） |
| `read_file` | `read` | 读回上一步落盘的大结果，不出网 |
| `write_file` | `write_local` | 写报告 / 产出到当前会话目录（越权与覆盖先过权限门） |

工具按数据源能力门控：某个能力没有可用数据源时，对应工具不注册，模型看不到它。

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `NIULAI_API_KEY` | API Key（必填） | — |
| `NIULAI_PROTOCOL` | `openai` / `anthropic` | 按接入点自动推断 |
| `NIULAI_BASE_URL` | 接入点 | 随协议变化 |
| `NIULAI_MODEL` | 模型名 | `deepseek-flash` |
| `NIULAI_THS_API_KEY` | 同花顺数据源 Key（主源）；缺省退回东财 / 腾讯兜底 | — |
| `NIULAI_HOME` | 数据目录（traces / sessions / artifacts / logs / niulai.db） | `~/.niulai` |
| `NIULAI_LOG_LEVEL` | 日志级别（debug / info / warning / error） | `warning` |
| `NIULAI_LOG_FILE` | 日志文件路径（不设且级别开到 INFO/DEBUG 时自动落数据目录） | — |

也兼容 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` / `ANTHROPIC_API_KEY`。

**协议推断顺序**：显式 `NIULAI_PROTOCOL` > 接入点里含 `anthropic` >
只提供了 `ANTHROPIC_API_KEY` > 默认 `openai`。

## 可调参数（`Settings`）

`max_turns` / `tool_timeout_s` / `max_retries` / `max_consecutive_failures` /
`keep_recent_tool_results` / `report_max_turns` / `approval_timeout_s` /
`max_tokens` / `prompt_cache`，均可经环境变量或 `~/.niulai/config.toml` 覆盖。

## 开发

```bash
pip install -e "packages/niulai[dev]"
pytest                 # 全部无网络，秒级跑完
ruff check .
```

## 合规

数据来自公开网络接口的抓取，**版权归原平台所有**。本项目仅用于学习研究，
请控制请求频率、勿商用。
