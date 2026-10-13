"""Agent 内核。

对外只暴露 `AgentRuntime` 与 `RunResult`。
`scheduler`（自选股 / 预警 / 盘后复盘）会作为同级模块加入。
"""

from .loop import AgentRuntime, RunResult
from .prompt import DEFAULT_PROMPT, load_prompt

__all__ = ["DEFAULT_PROMPT", "AgentRuntime", "RunResult", "load_prompt"]
