"""运行时配置。

P0/P1 只从环境变量读取。后续会补上系统凭据管理器（keyring）与本地配置文件。

**API Key 永远不进 SQLite、不进 trace、不进日志。**

协议怎么选（`NIULAI_PROTOCOL`）：
1. 显式写了 `openai` / `anthropic` → 就用它；
2. 没写，但 `NIULAI_BASE_URL` 里含 `anthropic` → anthropic；
3. 没写，但只提供了 `ANTHROPIC_API_KEY` → anthropic；
4. 其余 → openai（兼容面最广，DeepSeek / 通义 / 混元 / Kimi / OpenAI 都走它）。

不确定协议时别硬填：填错的表现是"HTTP 404"或"model not found"，
而不是一句能看懂的中文提示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

from .errors import ConfigError

ProtocolName = Literal["openai", "anthropic"]

DEFAULT_BASE_URLS: dict[str, str] = {
    # 默认走 DeepSeek：国内直连、便宜、工具调用够用
    "openai": "https://api.deepseek.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "deepseek-chat",
    "anthropic": "claude-sonnet-5",
}

KEY_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("NIULAI_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("NIULAI_API_KEY", "ANTHROPIC_API_KEY"),
}

DEFAULT_MAX_TURNS = 12
DEFAULT_TOKEN_BUDGET = 60_000
DEFAULT_TOOL_TIMEOUT_S = 15.0
DEFAULT_MAX_RETRIES = 2
# 同一工具连续失败几次就熔断。见 agent/guard.py —— 这是把 prompt 里的
# "连续失败两次就停"变成 runtime 强制的那个开关。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 2
# 发给模型时保留最近几条工具结果的原样内容（更早的换成占位说明）。
DEFAULT_KEEP_RECENT_TOOL_RESULTS = 4
# Anthropic 的 max_tokens 是必填项，而且**包含思考预算** ——
# 给太小会在长报告里被截断。OpenAI 兼容那边不传这个字段，用服务端默认。
DEFAULT_MAX_TOKENS = 8_192


def _env(*names: str) -> str | None:
    """按顺序返回第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def infer_protocol(explicit: str | None, base_url: str | None) -> ProtocolName:
    """按上面的四条规则推协议。"""
    if explicit in ("openai", "anthropic"):
        return explicit  # type: ignore[return-value]
    if base_url and "anthropic" in base_url.lower():
        return "anthropic"
    if _env("ANTHROPIC_API_KEY") and not _env("OPENAI_API_KEY", "NIULAI_API_KEY"):
        return "anthropic"
    return "openai"


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行的全部可调参数。

    `max_turns` / `token_budget` / `tool_timeout_s` 三个是硬约束，
    对应"四件套"里的三个（第四个是取消，由 asyncio 任务取消承担）。
    """

    base_url: str
    api_key: str
    model: str
    protocol: ProtocolName = "openai"
    max_turns: int = DEFAULT_MAX_TURNS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    keep_recent_tool_results: int = DEFAULT_KEEP_RECENT_TOOL_RESULTS
    # 仅 Anthropic 生效：给 system 挂缓存断点（服务端低于最小长度会静默跳过）
    prompt_cache: bool = True

    def with_overrides(self, **kwargs: object) -> Settings:
        """返回替换了部分字段的副本（用于 CLI 参数覆盖）。"""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, *, require_key: bool = True, protocol: str | None = None) -> Settings:
        """从环境变量构造。

        `protocol` 是给 CLI 的 `--protocol` 用的显式覆盖 —— 走这里而不是
        `with_overrides`，是为了让 base_url / model 的默认值跟着协议一起换。
        否则 `--protocol anthropic` 会拿 DeepSeek 的地址去打 Claude，必错。
        """
        base_url_env = _env("NIULAI_BASE_URL", "OPENAI_BASE_URL")
        resolved = infer_protocol(protocol or _env("NIULAI_PROTOCOL"), base_url_env)

        base_url = base_url_env or DEFAULT_BASE_URLS[resolved]
        model = _env("NIULAI_MODEL", "OPENAI_MODEL") or DEFAULT_MODELS[resolved]
        api_key = _env(*KEY_ENV_VARS[resolved]) or ""

        if require_key and not api_key:
            raise ConfigError(
                "没有找到 API Key。\n"
                '  Windows PowerShell:  $env:NIULAI_API_KEY="sk-..."\n'
                '  macOS / Linux:       export NIULAI_API_KEY="sk-..."\n'
                f"  当前协议：{resolved}（可用 NIULAI_PROTOCOL 指定 openai / anthropic）\n"
                f"  当前接入点：{base_url}（可用 NIULAI_BASE_URL 覆盖）"
            )

        return cls(base_url=base_url, api_key=api_key, model=model, protocol=resolved)
