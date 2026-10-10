"""Prompt 加载。

prompt 独立成文件、文件名带版本号，是为了让 eval 能对比不同版本的差异（P8 跑 A/B）。
不要把 prompt 硬编码进代码 —— 那样改一次就丢一次历史，也没法回滚。

版本：
- `system_v1.md` —— P0，只有一个 `stock_quote` 工具。
- `system_v2.md` —— 工具面扩到四个，并补上"历史被压缩""数据时效"
  两条规则；"连续失败就停"也从"请求"改成了"运行时强制"的写法。
- `system_v3.md` —— 同花顺官方 API 接入后的九工具矩阵：
  单标的（quote / kline / financial / valuation / anomaly / moneyflow）
  + 市场视野（hot / limitup / dragontiger）；补充"开放问题先用市场视野
  工具拿客观数据""榜单只概括不整段复述""热度 ≠ 涨幅"等规则。
- `system_v4.md` —— 当前。从"数据查询助手"转向"分析讨论伙伴"：
  加 `stock_indicator`（技术指标由本地确定性计算）；"不给投资建议"
  改写为**观点纪律**（事实来自工具、观点挂数据、必带反方证据）；
  新增"分析与观点"一节，定义分析类问题的取数策略与多视角输出结构。

旧版本**不删**：会话里存的历史是按旧 prompt 产生的，做 A/B 对比时还要用。

旧版本**不删**：会话里存的历史是按旧 prompt 产生的，做 A/B 对比时还要用。
"""

from __future__ import annotations

from importlib.resources import files

DEFAULT_PROMPT = "system_v4.md"


def load_prompt(name: str = DEFAULT_PROMPT) -> str:
    """从 `niulai/agent/prompts/` 读取 prompt 文本。"""
    resource = files(__package__) / "prompts" / name
    return resource.read_text(encoding="utf-8").strip()
