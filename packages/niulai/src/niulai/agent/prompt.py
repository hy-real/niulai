"""Prompt 加载。

prompt 独立成文件、文件名带版本号：eval 能对比不同版本的差异，
改一次不丢一次历史，也能回滚。

版本（旧版本不删 —— 会话历史是按旧 prompt 产生的，A/B 对比还要用）：
- system_v1.md：只有一个 stock_quote 工具。
- system_v2.md：工具面扩到四个，补"历史被压缩 / 数据时效"两条规则；
  "连续失败就停"改为运行时强制的写法。
- system_v3.md：同花顺官方 API 接入后的九工具矩阵（单标的 + 市场视野），
  补"开放问题先用市场视野工具拿客观数据""榜单只概括不整段复述"
  "热度 ≠ 涨幅"等规则。
- system_v4.md：从"数据查询助手"转向"分析讨论伙伴"：加 stock_indicator；
  "不给投资建议"改写为观点纪律（事实来自工具、观点挂数据、必带反方
  证据）；新增"分析与观点"一节。
- system_v5.md：加 write_file 与报告生成流程；边界一节改为"只能读写
  数据目录之内"。
- system_report_v1.md：报告子代理专用，只管"把 brief 写成结构完整、
  数据可溯源的 Markdown 报告并落盘"。
"""

from __future__ import annotations

from importlib.resources import files

DEFAULT_PROMPT = "system_v5.md"

# 报告子代理的 prompt（agent/report.py 的委派目标）。与主 prompt 同目录、同
# 版本化机制，但服务对象不同：主代理面向用户，它面向"把 brief 写成报告"。
REPORT_PROMPT = "system_report_v1.md"


def load_prompt(name: str = DEFAULT_PROMPT) -> str:
    """从 `niulai/agent/prompts/` 读取 prompt 文本。"""
    resource = files(__package__) / "prompts" / name
    return resource.read_text(encoding="utf-8").strip()
