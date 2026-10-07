# Niulai

**本地优先的 A 股 AI 助手：装完就能用，自带 API Key，对话和数据全部留在你的电脑上。**

[English](#english) · [架构设计](docs/architecture.md) · [路线图](docs/architecture.md#8-路线图)

> 状态：早期开发中。内核 + 多轮会话 + Web 界面 + 评测集已跑通（198 个测试全绿），
> LLM 层支持 OpenAI 兼容与 Anthropic 两套协议。
> 尚未发布到 PyPI，目前从源码安装。

---

## 为什么又做了一个炒股工具

现在做 AI 炒股的工具，基本都要你注册账号、绑定手机、把持仓传到别人的服务器上。

Niulai 反着来：

| | 常见做法 | Niulai |
| --- | --- | --- |
| 账号 | 必须注册 | **零账号** |
| 数据 | 上传到云端 | **全在本机，不上传** |
| 模型 | 平台代付、按量收费 | **自带 Key，不经任何中转** |
| 数据源 | 锁定一家 | **插拔式，按能力路由** |

---

## 快速开始

需要 Python 3.11+。

```bash
git clone https://github.com/<you>/niulai.git && cd niulai

# Windows
python -m venv .venv && .venv\Scripts\activate
# macOS / Linux
python -m venv .venv && source .venv/bin/activate

pip install -e packages/niulai

# 配置模型（默认推荐国内可直连的 DeepSeek）
export NIULAI_API_KEY="sk-..."
export NIULAI_MODEL="deepseek-chat"                      # 可选
export NIULAI_BASE_URL="https://api.deepseek.com/v1"     # 可选
export NIULAI_THS_API_KEY="sk-fuyao-..."                 # 可选：同花顺数据源 Key，缺省退回东财
# 同花顺 Key 申请：登录 https://fuyao.aicubes.cn/admin/ → API Key 管理 → 创建
# 热榜 / 涨跌停池 / 龙虎榜 / 估值 / 异动等市场数据依赖它；也可以在 Web 界面的设置里填

niulai ask "茅台现在多少钱"        # 问一次
niulai chat                        # 或者进入多轮对话（会记住上下文）
```

多轮里可以说"它今天成交多少"——上一轮查的是哪只股票，它记得：

```
你 › 茅台现在多少钱
  ⚙ stock_quote({"symbol":"茅台"})
  ✓ 返回 118 字符
贵州茅台最新价 1253.80 元，涨 0.10%
· 正常结束　2 轮　tokens 1,284

你 › 它最近半年走势怎么样
  ⚙ stock_kline({"symbol":"600519","limit":150})
  📄 结果过大已落盘：.../stock_kline-1e13dcbe.txt（11,240 字符，可用 read_file 读取）
  ⚙ read_file({"path":"...","limit":8})
```

> 加 `--session` 可以把 `ask` 也存成会话：`niulai ask "它业绩怎么样" --session latest`。

想换成 Claude 就直接改协议，接入点和模型会自动跟着换：

```bash
export NIULAI_API_KEY="sk-ant-..."
export NIULAI_PROTOCOL="anthropic"
```

支持的模型：**DeepSeek / 通义 / 混元 / Kimi / OpenAI**（走 OpenAI 兼容协议）、
**Claude 系**（走 Anthropic Messages 协议）。两套协议的差异全部在内核之外的适配层消化，
换协议不需要改任何业务代码。

Windows PowerShell 用 `$env:NIULAI_API_KEY="sk-..."`。

> PyPI 发布后，上面这些会简化成一行：`uvx niulai ask "茅台现在多少钱"`。

### 其他命令

```bash
niulai chat --resume               # 续接最近的会话
niulai chat --resume <session_id>  # 续接指定会话
niulai sessions                    # 列出历史会话
niulai ask "..." --json            # 每行输出一个事件 JSON，便于管道处理
niulai replay latest               # 回放最近一次运行的完整轨迹
niulai resume latest               # 从被中断的 run 恢复出可续聊的会话
niulai tools                       # 列出工具、数据源与各自的能力声明
```

### 配置与数据目录

一切程序生成的文件都在 `~/.niulai/`（Windows 为 `C:\Users\你\.niulai\`），
可用 `NIULAI_HOME` 整体覆盖，删掉它 = 完全重置：

```
~/.niulai/
├─ config.toml   用户配置（唯一需要人动手的文件，可省略）
├─ traces/       每次运行一个 JSONL（每一步都记，可回放）
├─ niulai.db     会话（SQLite 单文件；将来自选 / 缓存也进这里）
├─ artifacts/    因体积过大而落盘的工具结果
└─ logs/         调试日志（DEBUG/INFO 时自动出现）
```

config.toml 示例（只写想改的，其余用默认值）：

```toml
[llm]
# protocol = "openai"          # openai（DeepSeek/通义/Kimi）或 anthropic（Claude）
# base_url = "https://api.deepseek.com/v1"
# api_key = "sk-..."           # 也可用环境变量 NIULAI_API_KEY（优先）
model = "deepseek-chat"
max_turns = 12                  # 单次提问最多几轮工具循环

[datasource]
# ths_api_key = "sk-fuyao-..."  # 同花顺数据源 Key；也可用环境变量 NIULAI_THS_API_KEY（优先）

[log]
# level = "debug"               # 等价于环境变量 NIULAI_LOG_LEVEL
```

优先级：**CLI 参数 > 环境变量 > config.toml > 默认值**（与 git / aws cli 同惯例）。
API Key 可以写进 config.toml（本地单机：文件就在你自己的机器上，不出本机，
界面与日志里只脱敏展示尾号）；环境变量优先于文件——共享环境建议只用环境变量。

### 调试日志

trace 记的是运行轨迹；HTTP 层的细节（每次请求、重试、错误响应体）走标准 logging，
只写 **stderr**（`--json` 的 stdout 事件流不受影响），默认关闭：

```bash
# PowerShell
$env:NIULAI_LOG_LEVEL="DEBUG"      # debug / info / warning / error，默认 warning
$env:NIULAI_LOG_FILE="d:\tmp\niulai.log"   # 可选：不设且级别开到 INFO/DEBUG 时自动落 ~/.niulai/logs/
```

---

## 现在能做什么

| 能力 | 状态 |
| --- | --- |
| 手写 ReAct 内核（轮数 / 工具超时 / 取消 / 熔断） | ✅ |
| 流式输出 | ✅ |
| 工具参数校验、错误结构化回灌 | ✅ |
| 两套协议：OpenAI 兼容 / Anthropic Messages | ✅ |
| 重试与错误分类（可重试 vs 终止，带抖动退避） | ✅ |
| Anthropic 提示缓存（省输入费用） | ✅ |
| **多轮会话**（历史持久化，可续接） | ✅ |
| **单标的工具**：行情 / K 线 / 财务 / 估值 / 当日异动原因 | ✅ |
| **市场视野**：热股榜 / 飙升榜 / 涨跌停池 / 连板天梯 / 龙虎榜 / 资金流 | ✅ |
| **大结果落盘**，只把摘要 + 路径送进上下文，配 `read_file` 分页读 | ✅ |
| **上下文压缩**（早期工具结果换成占位说明，结构不动） | ✅ |
| **运行时熔断**（重复调用 / 连续失败被拦下，不靠模型自觉） | ✅ |
| **被中断的对话可修复**（补回"结果未知"，不猜成败） | ✅ |
| 数据源限流自动重试 | ✅ |
| **会话存储 SQLite**（单文件 `niulai.db`） | ✅ |
| Token 统计（每轮 / 累计；不做费用换算——自带 Key 无需算钱） | ✅ |
| **同花顺官方 API 主源**（行情 / K 线 / 财务 / 估值 / 热榜 / 涨跌停 / 龙虎榜 / 异动） | ✅ 需 `NIULAI_THS_API_KEY` |
| 东财 / 腾讯兜底（资金流 + 行情降级） | ✅ |
| 本地 Web 界面（三栏） | ✅ |
| 桌面安装包（双击即用） | ⏳ P6（壳已就位，差打包流水线） |
| 自选股 / 条件预警 / 盘后复盘 | ⏳ P7 |
| 评测集（任务级断言，10 条确定性断言） | ✅ |
| 独立 MCP server（给 Claude Code / Cursor 用） | ⏳ P9 |

完整路线图与验收标准见 [docs/architecture.md](docs/architecture.md#8-路线图)，
设计与实现的缺口对照见 [docs/gap-review.md](docs/gap-review.md)。

---

## 设计要点

**一份内核，四种外壳。** CLI / 浏览器 / 桌面壳 / MCP server 共用同一个内核，内核不知道 UI 存在。

- **手写 ReAct 内核，不先上框架。** 先用 LangGraph 之外的路径把循环、预算、错误回灌做对，
  理解清楚再谈抽象。
- **两套协议，一套中间表示。** 模型厂商的差异（system 位置、工具 schema、流式事件形状、
  私有内容块）全部沉在适配层，内核只认一个 `LLMClient` 接口。
- **工具与数据源分离。** 工具不认识同花顺、东财或新浪，只调 `MarketDataService`，按能力路由。
  换源 = 加一个实现类。
- **大结果落盘，不做静默截断。** 截断会让模型拿到残数据却不知道缺了什么，
  于是给出一个"看起来对"的结论 —— 这比直接报错更糟。落盘 + 摘要 + 路径 + `read_file` 才是对的。
- **存储完整，发送裁剪。** 会话里存的是完整 transcript，发给模型的是一份视图。
  裁的时候只换内容不删消息 —— `tool_call` 和它的结果被拆散就是非法请求。
- **纪律不能只写在 prompt 里。** prompt 是概率性的，runtime 是确定性的。
  "同一工具连续失败两次就停"这句话，在运行时里是一条真会拦人的规则。
- **错误字符串本身就是 prompt。** 工具报错要写成可恢复的指引，而不是"失败了"。
- **被中断的运行，结果判定为「未知」而不是「失败」。** 工具可能已经把请求发出去了 ——
  对只读行情无所谓，对将来的写类工具，盲目重试就是事故。

完整设计与取舍见 [docs/architecture.md](docs/architecture.md)。

---

## 免责声明

⚠️ **本项目仅用于学习与研究。**

- 股票数据来自公开网络接口的抓取，**版权归原平台所有**，请勿用于商业用途。
- 请自行控制请求频率，不要对数据源造成压力。
- **本项目不构成任何投资建议。** 据此操作，风险自负。

## License

MIT

---

<a name="english"></a>

## English

**Niulai** is a local-first AI assistant for China A-share investing. No account, no server,
no data upload — your API key, your conversations and your data stay on your own machine.

```bash
pip install -e packages/niulai
niulai ask "How much is Moutai trading at?"
niulai chat                 # multi-turn conversation with persistent history
```

Current capabilities: a hand-written ReAct loop (turn / timeout / cancellation budgets,
quote + K-line + financial tools, oversized results written to disk with a `read_file` tool to
page them back, context trimming that never breaks tool-call pairing, and a runtime circuit
breaker so repeated or repeatedly-failing calls are stopped without relying on the model's
cooperation.

Design docs: [docs/architecture.md](docs/architecture.md) (Chinese).
