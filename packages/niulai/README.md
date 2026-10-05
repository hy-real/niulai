# niulai

本地优先的 A 股 AI 助手的**内核**。这个包不包含界面 —— 界面（CLI / 浏览器 / 桌面壳 / MCP server）
都是它的外壳，通过事件流对接。

## 分层

| 包 | 职责 | 关键约束 |
| --- | --- | --- |
| `niulai.datasource` | 行情 / K 线 / 财务取数、按能力路由、限流重试 | 工具不认识具体数据源 |
| `niulai.tools` | 工具定义、参数校验、超时、能力声明、大结果落盘 | 结果在**进上下文之前**就处理掉 |
| `niulai.llm` | **两套协议**（OpenAI 兼容 / Anthropic）、流式、重试、用量 | 内核只认 `LLMClient`，不认识厂商 |
| `niulai.agent` | 手写 ReAct 循环、预算、取消、熔断、事件流 | **不 import 任何 UI 代码** |
| `niulai.context` | 上下文裁剪（历史工具结果压缩） | **只换内容，不删消息** |
| `niulai.session` | 会话（多轮历史）持久化 | 存完整的，发裁剪的 |
| `niulai.artifacts` | 大结果落盘与安全读回 | 只能读自己管的目录 |
| `niulai.trace` | 运行轨迹落盘与回放 | trace 里绝不出现 API Key |

依赖是**严格单向**的：`cli → agent → tools → datasource`。

## 工具与能力声明

| 工具 | 能力 | 说明 |
| --- | --- | --- |
| `stock_quote` | `read` + `network` | 实时行情快照 |
| `stock_kline` | `read` + `network` | 历史 K 线（日 / 周 / 月，可指定复权） |
| `stock_financial` | `read` + `network` | 定期报告主要财务指标 |
| `read_file` | `read` | 读回上一步落盘的大结果，**不出网** |

能力是工具**自己声明**的，判定权不在工具手里 —— 被审对象不能当法官。
完整的权限模型见 `docs/architecture.md` 7.12；当前 `read_file` 的路径围栏是它的第一块落地。

**结果太大时不截断，而是落盘**：只把「摘要 + 路径 + 开头预览」送进上下文，
并在摘要里明确写出「这不是全部数据」。模型需要细节时用 `read_file` 分页取。
理由见 `docs/architecture.md` 7.3 —— 截断会让模型拿到残数据却不知道缺了什么，
于是给出一个看起来正确的结论。

## 两套协议，一套中间表示

|  | OpenAI 兼容 | Anthropic Messages |
| --- | --- | --- |
| 端点 | `/chat/completions` | `/messages` |
| 认证 | `Authorization: Bearer` | `x-api-key` + `anthropic-version` |
| system | 普通消息 | **顶层参数** |
| 工具 schema | `function.parameters` | `input_schema` |
| 工具结果 | `role="tool"` 消息 | user 消息里的 `tool_result` 块，**同轮多条要合并** |
| `max_tokens` | 可选 | **必填** |
| 私有块 | 无 | thinking / redacted_thinking，**必须原样回传** |
| 缓存 | 多数服务端自动 | 显式 `cache_control` 断点 |

覆盖的模型：DeepSeek / 通义 / 混元 / Kimi / OpenAI（走 openai），
Claude 系（走 anthropic）。差异全部在 `niulai/llm/` 内消化，
`AgentRuntime` 一行都不用改。

## 多轮会话

内核的单位是 **run**（一次任务），会话是**外层**概念：

```python
session = store.create()

result = await runtime.run("茅台现在多少钱", emitter=emitter, session_id=session.id)
session.messages = [m for m in result.messages if m.role != "system"]

# 下一轮：把历史带回去，模型就知道"它"指的是哪只股票
result = await runtime.run("它今天成交多少", emitter=emitter, messages=session.messages)
```

`run()` 接收已有 transcript 并在后面追加，而不是每次从零构建 ——
这一个入口同时支撑了多轮会话和断点恢复。

两点值得注意：

- **会话里不存 system 消息**，由 runtime 每次注入最新版本，这样改了 prompt 立刻对老会话生效。
- **`run()` 会先修复破损的 transcript**：如果上次是在工具调用途中被中断的（进程被杀 / Ctrl-C），
  尾部会留下"声明了调用但没有结果"的非法状态，两个协议都不收。
  补的内容判为**「结果未知」而不是「失败」**—— 工具可能已经把请求发出去了，
  盲目重试对将来的写类工具就是事故。

## 使用

```python
from niulai.agent import AgentRuntime
from niulai.artifacts import ArtifactStore
from niulai.config import Settings
from niulai.datasource import build_default_service
from niulai.events import EventEmitter
from niulai.llm import build_client
from niulai.tools import ReadFileTool, ToolRegistry, build_stock_tools

settings = Settings.from_env()
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
    print(result.text, result.usage.total_tokens)
finally:
    await llm.aclose()
    await service.aclose()
```

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `NIULAI_API_KEY` | API Key（必填） | — |
| `NIULAI_PROTOCOL` | `openai` / `anthropic` | 按接入点自动推断 |
| `NIULAI_BASE_URL` | 接入点 | 随协议变化 |
| `NIULAI_MODEL` | 模型名 | `deepseek-chat` / `claude-sonnet-5` |
| `NIULAI_HOME` | 数据目录覆盖（traces / sessions / artifacts） | 平台默认目录 |

也兼容 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` / `ANTHROPIC_API_KEY`。

**协议推断顺序**：显式 `NIULAI_PROTOCOL` > 接入点里含 `anthropic` >
只提供了 `ANTHROPIC_API_KEY` > 默认 `openai`。

填错协议的典型表现是 `HTTP 404` 或 `model not found`，而不是中文提示 ——
所以不确定时请显式指定。

## 可调参数（`Settings`）

`max_turns` / `tool_timeout_s` / `max_consecutive_failures` / `keep_recent_tool_results` /
`max_retries` —— 前三个是**硬约束**，对应"四件套"里的三项（第四项是取消，由 asyncio 任务取消承担）。

## 开发

```bash
pip install -e "packages/niulai[dev]"
pytest                 # 全部无网络，秒级跑完
ruff check .
```

## 合规

数据来自公开网络接口的抓取，**版权归原平台所有**。本项目仅用于学习研究，
请控制请求频率、勿商用。
