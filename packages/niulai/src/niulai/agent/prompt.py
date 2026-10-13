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
- `system_v4.md` —— 从"数据查询助手"转向"分析讨论伙伴"：
  加 `stock_indicator`（技术指标由本地确定性计算）；"不给投资建议"
  改写为**观点纪律**（事实来自工具、观点挂数据、必带反方证据）；
  新增"分析与观点"一节，定义分析类问题的取数策略与多视角输出结构。
- `system_v5.md` —— 加入 `write_file` 与报告生成流程：
  用户明确要报告直接写、分析结束后主动问一句；报告结构 / 命名 /
  落地路径规范；写文件被审批拒绝后的处理（不要再试，贴给用户）。
  边界一节从"不能读写任意文件"改写为"只能读写数据目录之内"。
- `system_report_v1.md` —— 报告子代理专用（`agent/report.py` 委派）。
  不含行情工具选择指南——那是主代理的事；它只管"把 brief 写成一份
  结构完整、数据可溯源的 Markdown 报告并落盘"。

旧版本**不删**：会话里存的历史是按旧 prompt 产生的，做 A/B 对比时还要用。
"""

from __future__ import annotations

from importlib.resources import files

DEFAULT_PROMPT = "system_v5.md"

# 报告子代理的 prompt（agent/report.py 的委派目标）。与主 prompt 同目录、
# 同版本化机制，但服务对象不同：主代理面向用户，它面向"把 brief 写成报告"。
REPORT_PROMPT = "system_report_v1.md"


def load_prompt(name: str = DEFAULT_PROMPT) -> str:
    """从 `niulai/agent/prompts/` 读取 prompt 文本。"""
    resource = files(__package__) / "prompts" / name
    return resource.read_text(encoding="utf-8").strip()
