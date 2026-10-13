"""Niulai —— 本地优先的 A 股 AI 助手。

分层（自底向上）：

    datasource/  行情取数，按能力路由，可换源
    tools/       工具定义、参数校验、超时、结果格式化
    llm/         LLM 适配层（OpenAI 兼容协议、流式、重试、用量）
    agent/       手写 ReAct 内核：循环、预算、取消、事件流
    trace        运行轨迹落盘与回放

上层永远不反向依赖下层，`agent` 不允许 import 任何 UI 代码。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
