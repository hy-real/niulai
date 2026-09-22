"""异常分类。

核心设计：**不要把"重试"当成万能兜底。**
可重试（429 / 5xx / 超时）与终止（400 / 401 / 403）必须分开 ——
否则用户把模型名写错时，程序会挨个撞好几个模型才报错，又慢又费钱。
"""

from __future__ import annotations


class NiulaiError(Exception):
    """所有自研异常的基类。"""


class ConfigError(NiulaiError):
    """配置缺失或非法，例如没有设置 API Key。"""


class LLMError(NiulaiError):
    """LLM 调用失败。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RetryableLLMError(LLMError):
    """可重试：限流、服务端错误、网络超时。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, status=status, retryable=True)


class FatalLLMError(LLMError):
    """终止：请求本身有问题，重试没有意义。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, status=status, retryable=False)


class ToolError(NiulaiError):
    """工具执行失败。"""


class DataSourceError(NiulaiError):
    """所有数据源都取不到数据。"""


class BudgetExceeded(NiulaiError):
    """超出轮数或 token 预算。"""


def classify_status(status: int) -> type[LLMError]:
    """把 HTTP 状态码映射为可重试 / 终止两类。"""
    if status in (408, 409, 425, 429) or status >= 500:
        return RetryableLLMError
    return FatalLLMError
