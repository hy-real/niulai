"""运行时配置。

## 优先级（高 → 低），与 git / aws cli / gh 同一惯例

    CLI 参数  >  环境变量  >  ~/.niulai/config.toml  >  内置默认值

作用域匹配生命周期：config.toml 是"这台机器的常态"，env 是"这个终端的
临时意图"——`NIULAI_PROTOCOL=anthropic niulai ask "..."` 这种临时切换
必须赢过文件，否则试个协议还得改文件。

## API Key 的来源与优先级

    环境变量  >  config.toml 里的 api_key

单机本地使用，文件就在用户自己机器上，读文件没什么危险——
所以文件里的 api_key 也认，只是排 env 后面（env 是"这个终端的
临时意图"，应当赢过"这台机器的常态"）。将来如需多用户/共享
环境再接系统凭据管理器 keyring，见 architecture.md 12。

协议怎么选（按顺序取第一个命中的）：
1. CLI `--protocol` / 环境变量 NIULAI_PROTOCOL / 文件里的 protocol；
2. 没写，但 base_url（env 或文件）里含 `anthropic` → anthropic；
3. 没写，但只提供了 ANTHROPIC_API_KEY → anthropic；
4. 其余 → openai（兼容面最广，DeepSeek / 通义 / 混元 / Kimi / OpenAI 都走它）。

不确定协议时别硬填：填错的表现是"HTTP 404"或"model not found"，
而不是一句能看懂的中文提示。
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .errors import ConfigError
from .logs import get_logger
from .paths import data_dir

_log = get_logger("config")

ProtocolName = Literal["openai", "anthropic"]

CONFIG_FILE_NAME = "config.toml"

DEFAULT_BASE_URLS: dict[str, str] = {
    # 默认走 DeepSeek：国内直连、便宜、工具调用够用
    "openai": "https://api.deepseek.com/v1",
    "anthropic": "https://api.deepseek.com/anthropic",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "deepseek-flash",
    "anthropic": "deepseek-flash",
}

KEY_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("NIULAI_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("NIULAI_API_KEY", "ANTHROPIC_API_KEY"),
}

# 同花顺数据源 Key（与 LLM Key 同一套优先级：env > config.toml）
THS_KEY_ENV_VAR = "NIULAI_THS_API_KEY"


def read_ths_api_key(config_file: Path | None = None) -> str:
    """读同花顺数据源 API Key：env NIULAI_THS_API_KEY > config.toml [datasource] ths_api_key。"""
    datasource = _section(read_config_file(config_file), "datasource")
    return _env(THS_KEY_ENV_VAR) or _clean(datasource.get("ths_api_key")) or ""

DEFAULT_MAX_TURNS = 12
DEFAULT_TOOL_TIMEOUT_S = 15.0
DEFAULT_MAX_RETRIES = 2
# 同一工具连续失败几次就熔断。见 agent/guard.py —— 这是把 prompt 里的
# "连续失败两次就停"变成 runtime 强制的那个开关。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 2
# 发给模型时保留最近几条工具结果的原样内容（更早的换成占位说明）。
DEFAULT_KEEP_RECENT_TOOL_RESULTS = 15
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


def _clean(value: object) -> str | None:
    """配置文件里的字符串值：None / 非字符串 / 空白都归一成 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def config_file_path() -> Path:
    return data_dir() / CONFIG_FILE_NAME


def read_config_file(path: Path | None = None) -> dict:
    """读 config.toml。读不到 / 读坏了都返回 {}——配置错误不拦住运行。

    文件坏了要喊一声（stderr），但绝不能抛：用户手编 TOML 打错一个引号，
    惩罚不应该是"程序起不来"，而是"这次按默认值跑 + 告诉他为什么"。
    """
    target = path or config_file_path()
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"警告：{target} 解析失败，本次忽略配置文件（{exc}）", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _section(data: dict, name: str) -> dict:
    """取一个节，且保证它是字典——`[llm]` 写成字符串这种错别字别把程序带崩。"""
    section = data.get(name)
    return section if isinstance(section, dict) else {}


def infer_protocol(explicit: str | None, base_url: str | None) -> ProtocolName:
    """按模块 docstring 里的四条规则推协议。"""
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

    `max_turns` / `tool_timeout_s` 两个是硬约束，
    对应"四件套"里的两个（另外两个是取消与运行时熔断）。
    """

    base_url: str
    api_key: str
    model: str
    protocol: ProtocolName = "openai"
    max_turns: int = DEFAULT_MAX_TURNS
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
    def load(
        cls,
        *,
        require_key: bool = True,
        protocol: str | None = None,
        config_file: Path | None = None,
    ) -> Settings:
        """按优先级合并：CLI 参数 > 环境变量 > config.toml > 默认值。

        `protocol` 是 CLI 的 `--protocol`（最高优先级）；`config_file`
        是给测试的注入点，缺省用 `~/.niulai/config.toml`。
        """
        llm = _section(read_config_file(config_file), "llm")

        # ---- protocol：CLI > env > 文件；base_url 参与推断时同样 env 优先
        file_protocol = _clean(llm.get("protocol"))
        explicit = protocol or _env("NIULAI_PROTOCOL") or file_protocol
        base_url_candidate = _env("NIULAI_BASE_URL", "OPENAI_BASE_URL") or _clean(llm.get("base_url"))
        resolved = infer_protocol(explicit, base_url_candidate)

        base_url = base_url_candidate or DEFAULT_BASE_URLS[resolved]
        model = _env("NIULAI_MODEL", "OPENAI_MODEL") or _clean(llm.get("model")) or DEFAULT_MODELS[resolved]
        # Key：env 优先，其次 config.toml 里的 api_key（本地单用户场景够用）
        api_key = _env(*KEY_ENV_VARS[resolved]) or _clean(llm.get("api_key")) or ""

        if require_key and not api_key:
            raise ConfigError(
                "没有找到 API Key。\n"
                '  Windows PowerShell:  $env:NIULAI_API_KEY="sk-..."\n'
                "  macOS / Linux:       export NIULAI_API_KEY=\"sk-...\"\n"
                "  或者写入 ~/.niulai/config.toml 的 [llm] 节：api_key = \"sk-...\"\n"
                "  （env 优先于文件；共享环境下建议只用 env，机密不落盘）\n"
                f"  当前协议：{resolved}（可用 NIULAI_PROTOCOL 指定 openai / anthropic）\n"
                f"  当前接入点：{base_url}（可用 NIULAI_BASE_URL 覆盖）"
            )

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=resolved,
            max_turns=_int_from(llm, "max_turns", DEFAULT_MAX_TURNS),
            tool_timeout_s=_float_from(llm, "tool_timeout_s", DEFAULT_TOOL_TIMEOUT_S),
            max_retries=_int_from(llm, "max_retries", DEFAULT_MAX_RETRIES),
            max_tokens=_int_from(llm, "max_tokens", DEFAULT_MAX_TOKENS),
            max_consecutive_failures=_int_from(
                llm, "max_consecutive_failures", DEFAULT_MAX_CONSECUTIVE_FAILURES
            ),
            keep_recent_tool_results=_int_from(
                llm, "keep_recent_tool_results", DEFAULT_KEEP_RECENT_TOOL_RESULTS
            ),
            prompt_cache=_bool_from(llm, "prompt_cache", True),
        )

    # 兼容旧名（内部测试与老脚本可能还在用）；语义已变为"文件+env 合并加载"
    @classmethod
    def from_env(cls, **kwargs: object) -> Settings:
        return cls.load(**kwargs)  # type: ignore[arg-type]


def _int_from(section: dict, key: str, default: int) -> int:
    value = section.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        _log.warning("config.toml 的 %s = %r 不是整数，按默认值 %d 处理", key, value, default)
        return default


def _float_from(section: dict, key: str, default: float) -> float:
    value = section.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        _log.warning("config.toml 的 %s = %r 不是数字，按默认值 %g 处理", key, value, default)
        return default


def _bool_from(section: dict, key: str, default: bool) -> bool:
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    _log.warning("config.toml 的 %s = %r 不是布尔值，按默认值 %s 处理", key, value, default)
    return default
