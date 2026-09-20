# Niulai

一个本地优先的股票分析 Agent 练习项目。

## 产品定位

- 用户安装后在本机使用。
- 用户配置自己的 LLM API Key，即 BYOK。
- 会话、工具调用记录、股票数据、生成的分析报告都保存在本机。
- 项目不提供业务后端，不保存用户数据。
- 首阶段用 CLI 打通 Agent 核心，桌面 UI 后置。

## 当前设计结论

主技术栈使用 TypeScript / Node.js，不沿用 `AgentForge` 的 FastAPI + PostgreSQL + Redis 服务端形态。

原因：

- 本地 BYOK 应用不需要多用户认证、计费、服务端部署。
- Agent 的核心难点在运行循环、工具调用、上下文管理、错误回灌、流式与取消，而不是服务框架。
- TS 生态更适合后续扩展桌面客户端和本地 UI。
- `AgentForge` 中的股票数据实现可以复用，但 Agent 内核建议重写。

## 技术栈

| 层 | 选择 |
| --- | --- |
| 语言 | TypeScript / Node.js >= 22 |
| 运行形态 | 本地 CLI，后续可加桌面壳 |
| Agent 内核 | 自研 ReAct loop，不引入业务编排框架 |
| 工具协议 | 本地函数工具 + JSON Schema + Zod 校验 |
| 模型适配 | OpenAI-compatible 与 Anthropic |
| 本地存储 | SQLite + Drizzle ORM |
| 股票数据 | 开发期 Python sidecar，封装 akshare |
| 测试 | Vitest |
| 构建脚本 | pnpm + tsx |

# 架构设计

## 设计目标

Niulai 是一个学习型的本地 Agent 项目，股票分析是第一个真实场景。

这个项目要优先回答这些问题：

1. Agent 如何循环调用 LLM 和工具？
2. 工具参数错了怎么办？
3. 工具返回数据太大怎么办？
4. 长会话上下文如何压缩？
5. 用户如何打断一次正在执行的 Agent 任务？
6. 本地数据如何持久化？
7. 一个股票分析报告如何从数据、推理过程变成可保存的本地文件？

不优先解决的问题：

1. 多用户。
2. 云端部署。
3. 业务后端。
4. 通用编排框架。
5. 一开始就做完整桌面 UI。

## 总体形态

```text
┌────────────────────────────────────┐
│           Local CLI / UI           │
│  输入会话、展示事件、展示最终报告    │
└──────────────┬─────────────────────┘
               │ 进程内调用
┌──────────────▼─────────────────────┐
│           Agent Runtime            │
│  ReAct Loop / Context / Events     │
│  Tool Dispatcher / Approval        │
└───┬───────────────┬────────────────┘
    │               │
┌───▼─────────┐ ┌───▼──────────────┐
│ LLM Adapter │ │   Local Tools    │
│ OpenAI/Claude│ │ file / report /  │
│ BYOK API    │ │ stock / shell?   │
└─────────────┘ └───────┬──────────┘
                        │
                ┌───────▼──────────┐
                │ Stock Data Bridge │
                │ Python sidecar    │
                │ akshare / iFinD   │
                └──────────────────┘

┌────────────────────────────────────┐
│           Local Storage            │
│  SQLite: sessions / messages /     │
│  tool calls / reports / settings   │
└────────────────────────────────────┘
```

首阶段可以简化为单个 CLI 进程：

1. CLI 收集用户输入。
2. Agent Runtime 在同一进程内运行。
3. SQLite 读写本地数据。
4. LLM 请求走用户配置的 API。
5. 股票数据通过本地 Python sidecar 获取。

后续加桌面 UI 时，Agent Runtime 不应该直接依赖 UI 层。

## 目录结构

先使用单包结构，不提前拆 monorepo。

```text
niulai/
├─ README.md
├─ package.json
├─ tsconfig.json
├─ drizzle.config.ts
├─ src/
│  ├─ main.ts
│  ├─ cli/
│  │  ├─ commands/
│  │  └─ ui.ts
│  ├─ agent/
│  │  ├─ loop.ts
│  │  ├─ types.ts
│  │  ├─ context.ts
│  │  └─ events.ts
│  ├─ llm/
│  │  ├─ provider.ts
│  │  ├─ openai-compatible.ts
│  │  ├─ anthropic.ts
│  │  └─ errors.ts
│  ├─ tools/
│  │  ├─ registry.ts
│  │  ├─ executor.ts
│  │  ├─ builtin/
│  │  │  ├─ read-file.ts
│  │  │  ├─ write-file.ts
│  │  │  └─ list-files.ts
│  │  └─ stock/
│  │     ├─ get-financial.ts
│  │     ├─ get-kline.ts
│  │     └─ stock-bridge.ts
│  ├─ storage/
│  │  ├─ db.ts
│  │  ├─ schema.ts
│  │  └─ repositories/
│  ├─ config/
│  │  └─ settings.ts
│  └─ util/
└─ python/
   └─ stock_bridge.py
```

重要边界：

1. `agent/` 不 import `cli/`。
2. `agent/` 不直接知道具体数据源，只认识 `Tool`。
3. `tools/stock/` 负责调用股票数据桥。
4. `storage/` 不依赖 Agent 或 CLI。
5. UI 或 CLI 只消费事件流，不操作 Agent 内部状态。

## 核心模型

### Agent Task

一次用户请求会创建一个 `AgentTask`。

```ts
type AgentTask = {
  id: string;
  sessionId: string;
  status: "pending" | "running" | "waiting_approval" | "done" | "cancelled" | "failed";
  input: string;
  createdAt: string;
  updatedAt: string;
};
```

### Agent Event

Agent 执行过程中发出结构化事件，不直接打印日志。

```ts
type AgentEvent =
  | { type: "task_started"; taskId: string }
  | { type: "llm_request_started"; taskId: string; model: string }
  | { type: "llm_token"; taskId: string; text: string }
  | { type: "llm_request_completed"; taskId: string; usage: LlmUsage }
  | { type: "tool_call_started"; taskId: string; toolCallId: string; toolName: string; args: unknown }
  | { type: "tool_call_completed"; taskId: string; toolCallId: string; ok: boolean; summary: string }
  | { type: "approval_required"; taskId: string; reason: string }
  | { type: "task_completed"; taskId: string; output: string }
  | { type: "task_failed"; taskId: string; error: AgentError }
  | { type: "task_cancelled"; taskId: string };
```

CLI 可以直接展示事件，未来 UI 可以订阅同一套事件。

### Agent Loop

最小循环不追求复杂，先保持可读：

```text
1. 组装 system prompt + 会话历史 + 用户输入。
2. 调用 LLM。
3. 如果没有 tool call，输出最终结果。
4. 如果有 tool call，执行工具。
5. 把 tool result 作为消息追加到上下文。
6. 回到步骤 2。
```

必须内置的边界条件：

1. 最大循环次数，例如 30 次。
2. 单工具执行超时。
3. 工具输出大小上限。
4. 上下文 token 预算。
5. 用户取消信号。
6. 不可重试错误立即终止。

## LLM Adapter

首阶段只需要两个 Provider：

1. OpenAI-compatible。
2. Anthropic。

统一接口：

```ts
interface LlmProvider {
  id: string;
  complete(request: LlmRequest, signal?: AbortSignal): Promise<LlmResponse>;
  stream(request: LlmRequest, signal?: AbortSignal): AsyncIterable<LlmEvent>;
}
```

`LlmRequest` 至少包含：

```ts
type LlmRequest = {
  model: string;
  messages: LlmMessage[];
  tools?: LlmToolSpec[];
  temperature?: number;
  maxOutputTokens?: number;
};
```

Provider 层职责：

1. 把内部消息格式转换成厂商格式。
2. 把厂商 tool call 转成内部统一格式。
3. 统一错误分类。
4. 统一 token usage。
5. 支持 AbortSignal 取消。

不建议第一天就做复杂的模型路由、熔断、降级。本地单用户场景下，这些收益很低。

## Tool System

### Tool 定义

工具定义留在 TS 侧，实现可以是 TS 函数或 Python sidecar。

```ts
interface Tool<TInput = unknown, TOutput = unknown> {
  name: string;
  description: string;
  schema: z.ZodType<TInput>;
  risk: "safe" | "external" | "dangerous";
  execute(input: TInput, context: ToolContext): Promise<TOutput>;
}
```

`ToolContext` 包含：

```ts
type ToolContext = {
  taskId: string;
  sessionId: string;
  signal: AbortSignal;
  dataDir: string;
};
```

### 执行流程

```text
LLM tool call
  → 查找工具
  → Zod 参数校验
  → 权限确认
  → 执行
  → 超时与取消处理
  → 结果截断
  → 结构化成功/失败包装
  → 返回给 LLM
```

错误返回不要把原始异常或长堆栈直接丢给模型。

推荐格式：

```ts
type ToolResult<T> =
  | { ok: true; data: T }
  | {
      ok: false;
      error: {
        code:
          | "invalid_args"
          | "not_found"
          | "timeout"
          | "cancelled"
          | "permission_denied"
          | "upstream_error"
          | "internal_error";
        message: string;
        retriable?: boolean;
      };
    };
```

### 第一批工具

通用工具优先于股票工具：

| 工具 | 用途 | 风险 |
| --- | --- | --- |
| `list_files` | 查看本地目录 | safe |
| `read_file` | 读取文本文件 | safe |
| `write_file` | 写 Markdown 报告 | external |
| `get_financial` | 查询财务数据 | external |
| `get_kline` | 查询 K 线数据 | external |

`shell` 工具暂缓。等基础循环稳定后，再单独设计审批和沙箱。

## 上下文管理

上下文管理不是后期优化，它是 Agent 内核的一部分。

最小策略：

1. 每条消息计算 token 数。
2. 给一次 Agent task 设置总预算。
3. 工具结果默认截断，保留结构化摘要。
4. 旧消息超过窗口时，先保留 system prompt、最近消息和活跃工具结果。
5. 对更早的历史生成摘要。

首阶段可以只做三件事：

1. token 计数。
2. 工具结果截断。
3. 简单历史裁剪。

摘要压缩可以放到第四阶段。

## 本地存储

使用 SQLite，不引入 PostgreSQL 和 Redis。

### 存储位置

开发期使用项目内 `data/niulai.db`。

正式发布时应使用系统用户数据目录，例如：

```text
macOS:   ~/Library/Application Support/niulai
Windows: %APPDATA%/niulai
Linux:   ~/.local/share/niulai
```

### 基础表

| 表 | 用途 |
| --- | --- |
| `settings` | 用户可见设置；API Key 不放这里 |
| `sessions` | 会话 |
| `messages` | 用户消息、模型消息、工具结果 |
| `tool_calls` | 工具调用记录 |
| `reports` | 分析报告元信息 |
| `watchlist` | 自选股 |

### API Key

开发期可以从 `.env` 读取。

桌面发布时必须使用操作系统 keychain：

1. macOS: Keychain。
2. Windows: Credential Manager。
3. Linux: Secret Service，例如 gnome-keyring。

不要把 API Key 明文写入 SQLite。

## 股票数据桥

akshare 和 iFinD 都是 Python 生态库，不适合直接在 Node 里调用。

开发期推荐方案：常驻 Python sidecar。

```text
Node Tool
  → NDJSON over stdio
  → Python stock_bridge.py
  → akshare / iFinD
  → 结构化 JSON
  → Node Tool
```

协议示例：

请求：

```json
{
  "id": "req-001",
  "method": "get_financial",
  "params": {
    "symbol": "600519",
    "statement": "income"
  }
}
```

响应：

```json
{
  "id": "req-001",
  "ok": true,
  "data": {
    "symbol": "600519",
    "rows": []
  }
}
```

错误：

```json
{
  "id": "req-001",
  "ok": false,
  "error": {
    "code": "upstream_error",
    "message": "akshare request failed"
  }
}
```

sidecar 规则：

1. 应用启动时拉起，退出时关闭。
2. 不要每次工具调用都新建 Python 进程。
3. 所有错误必须结构化。
4. 不把 Python traceback 原样交给模型。
5. 输出要裁剪，不能把整张财务表塞进上下文。

发布期是否继续使用 Python sidecar，可以等真实接口稳定后再决定。如果最终只需要少量行情和财务接口，用 TS 直接实现 HTTP 数据源可能更干净。

## 权限与安全

本地 Agent 会接触用户文件，所以不能默认信任所有工具。

风险分级：

| 等级 | 行为 | 策略 |
| --- | --- | --- |
| `safe` | 只读、无外部副作用 | 直接执行 |
| `external` | 出网或写本地文件 | 可配置自动执行或确认 |
| `dangerous` | 执行 shell、删除、覆盖大量文件 | 默认确认，暂缓实现 |

首阶段只实现一个简单确认机制：

1. `write_file` 默认显示目标路径。
2. CLI 提示用户确认。
3. 支持一次会话内记住选择。

## 多 Agent 的演进方式

不要一开始做多 Agent。

先用单 Agent + 多工具完成一个股票分析闭环。

等出现真实需求后再拆：

1. 数据收集 Agent。
2. 财务分析 Agent。
3. 技术面分析 Agent。
4. 风险审查 Agent。
5. 报告汇总 Agent。

届时有两种路线：

1. 先手写简单 orchestrator。
2. 再评估是否使用 LangGraph.js。

不建议第一阶段引入 LangGraph，因为当前最重要的是理解 Agent loop、工具调用和上下文控制。框架会把这些学习点藏起来。

## 测试策略

| 层 | 测试重点 |
| --- | --- |
| Agent loop | 多轮 tool call、超限终止、取消 |
| LLM adapter | mock provider、tool call 格式转换、错误分类 |
| tools | 参数校验、成功/失败、超时、截断 |
| storage | 会话与消息持久化、重启恢复 |
| stock bridge | NDJSON 请求响应、错误结构化、超时 |
| e2e | mock LLM + mock stock data 跑完一次报告生成 |

先用 mock LLM 做测试，不要依赖真实模型 API。

## 首个可交付目标

第一个可验证版本：

```text
pnpm niulai ask "分析贵州茅台最近财务情况"
```

预期行为：

1. CLI 创建会话。
2. Agent 调用 `get_financial`。
3. 工具结果进入上下文。
4. LLM 生成分析结论。
5. CLI 流式展示输出。
6. 会话、消息和工具调用写入 SQLite。
7. 用户可以选择把结果写成 Markdown 报告。

完成这个闭环后，再考虑桌面 UI 和多 Agent。

# 分阶段实现路线

## 原则

1. 每个 Phase 都要有可运行的验收结果。
2. 先打通 Agent 核心，不做 UI 和部署。
3. 不提前抽象多 Agent、DAG、编排框架。
4. 每个阶段只解决一两个真实痛点。
5. Python demo 只作为参考，不直接迁移服务端代码。

## Phase 0：项目初始化

目标：建立干净可运行的 TypeScript 项目骨架。

### 内容

1. 初始化 pnpm + TypeScript + ESM。
2. 配置 ESLint、Prettier、Vitest。
3. 配置 `src/` 目录结构。
4. 建立基础配置读取。
5. 添加 CLI 入口 `niulai`。
6. 初始化 Drizzle + SQLite。

### 技术选型

| 项 | 选择 |
| --- | --- |
| 包管理 | pnpm |
| Node | >= 22 |
| TypeScript | latest stable |
| 校验 | zod |
| ORM | Drizzle ORM |
| SQLite | better-sqlite3 |
| CLI | commander |
| 测试 | vitest |

### 验收

```bash
pnpm install
pnpm typecheck
pnpm test
pnpm niulai --help
```

全部通过，并且本地能生成 SQLite 数据库文件。

## Phase 1：最小 Agent Loop

目标：不依赖 UI 和数据库，先让“用户输入 → LLM → 输出”跑通。

### 内容

1. 定义 `AgentTask`、`AgentEvent`、`LlmMessage`。
2. 实现 OpenAI-compatible Provider。
3. 实现 Anthropic Provider。
4. 支持通过环境变量或本地设置读取 API Key。
5. CLI 命令：

```bash
pnpm niulai ask "你好"
```

6. 支持流式输出。
7. 支持 Ctrl+C 取消。

### 明确不做

1. 不做多 Agent。
2. 不做工具。
3. 不做历史压缩。
4. 不做会话持久化。

### 验收

1. 配置 Key 后可以流式输出。
2. Ctrl+C 能中断请求。
3. 换 Provider 不需要改 Agent loop。
4. 有 mock provider 测试。

## Phase 2：工具系统和本地存储

目标：让 Agent 具备真正能力，并保证重启后数据不丢。

### 内容

1. 实现 `ToolRegistry`。
2. 实现 `ToolExecutor`。
3. 支持 Zod 参数校验。
4. 统一工具成功与失败返回结构。
5. 实现基础本地工具：
   - `list_files`
   - `read_file`
   - `write_file`
6. SQLite 建表：
   - `sessions`
   - `messages`
   - `tool_calls`
   - `reports`
7. CLI 支持创建会话、继续会话。
8. 每次 LLM 请求和工具调用持久化。

### 设计重点

1. 工具错误必须结构化。
2. 工具输出必须截断。
3. Agent loop 必须有最大轮数。
4. `write_file` 需要简单确认。

### 验收

用户输入：

```text
读取 docs/architecture.md，然后用三句话总结
```

Agent 能：

1. 调用 `read_file`。
2. 返回摘要。
3. 把消息写入 SQLite。
4. 重启 CLI 后可以继续这个会话。

再输入：

```text
把总结保存到 output/summary.md
```

Agent 能调用 `write_file`，生成 Markdown 文件。

## Phase 3：股票数据工具

目标：接入第一个真实领域场景。

### 内容

1. 实现本地 Python sidecar。
2. Node 与 Python 之间用 NDJSON over stdio 通信。
3. 实现两个股票工具：
   - `get_financial`
   - `get_kline`
4. 工具结果做裁剪和摘要。
5. CLI 展示工具调用过程。
6. 生成 Markdown 报告。

### Python sidecar

```text
python/stock_bridge.py
```

第一版接口：

```text
get_financial(symbol, statement)
get_kline(symbol, period, limit)
```

### 设计重点

1. 工具定义在 TS。
2. 数据实现在 Python。
3. Python 错误不能直接给模型。
4. 大表数据返回前必须裁剪。

### 验收

```bash
pnpm niulai ask "分析贵州茅台最近几个报告期的财务数据，并保存报告"
```

预期结果：

1. Agent 调用股票工具。
2. 数据结构化进入上下文。
3. 生成分析文本。
4. 写入本地 Markdown 报告。
5. 报告记录保存到 `reports` 表。

## Phase 4：上下文管理

目标：让长任务和长会话可靠。

### 内容

1. 增加 token 计数器。
2. 每个任务设置 token 预算。
3. 工具结果按大小和历史价值截断。
4. 保留最近消息。
5. 对更早历史生成摘要。
6. 上下文快照记录。
7. 支持任务中断后恢复基础状态。

### 验收

构造一个会返回大量数据的工具响应，Agent 能：

1. 不把全部数据塞进上下文。
2. 继续完成任务。
3. 明确记录哪些内容被裁剪。
4. 超过最大轮数或 token 预算时优雅终止。

## Phase 5：报告体验与会话质量

目标：把“能跑”变成“好用”。

### 内容

1. 报告模板。
2. 财务分析报告结构：
   - 结论摘要
   - 数据表
   - 财务分析
   - 风险提示
   - 后续观察点
3. 支持用户追加提问。
4. 支持引用之前的工具结果。
5. 支持重新生成报告。
6. CLI 交互体验优化。

### 验收

同一个会话内完成：

```text
分析贵州茅台
再对比五粮液
把两个公司的对比报告保存下来
```

Agent 能复用上下文，不要求用户重新输入全部信息。

## Phase 6：本地客户端形态

目标：从 CLI 演进为本地桌面应用。

### 内容

1. 抽出本地 Runtime API 或使用 Electron `utilityProcess`。
2. 引入 React UI。
3. 会话列表。
4. 消息流。
5. 工具调用时间线。
6. 报告页面。
7. 设置页。
8. API Key 存操作系统 keychain。
9. 打包和签名。

### 关键约束

Agent Runtime 不 import Electron。

Electron Main 只负责窗口、菜单和 IPC 生命周期，不承载 Agent 任务。

### 验收

1. 用户可以安装应用。
2. 用户在设置页配置自己的 API Key。
3. 会话数据保存在本机。
4. 报告可以在应用内打开。
5. 应用关闭后数据仍存在。

## Phase 7：多 Agent 与编排

目标：只在单 Agent 明显不够用时进入这个阶段。

### 先手写

先实现一个很小的 orchestrator：

```text
coordinator
  → data collector
  → financial analyst
  → technical analyst
  → risk reviewer
  → report writer
```

先不追求并行复杂调度，重点是：

1. 每个子任务有清晰输入输出。
2. 子 Agent 只拿到必要上下文。
3. 总上下文不爆炸。
4. 每一步可观测。
5. 风险审查可以打回重做。

### 什么时候考虑 LangGraph.js

LangGraph.js 是可选方案，不是必需方案。

可以考虑使用的时机：

1. 已经手写过一次简单 orchestrator。
2. 明确需要状态图。
3. 需要分支、回退、并行子任务。
4. 需要断点恢复。
5. 手写调度代码开始变得难维护。

如果只是一串顺序调用，或者一个 coordinator 轮询几个子 Agent，手写更清楚。

## 里程碑

| 里程碑 | 内容 | 意义 |
| --- | --- | --- |
| M1 | Phase 0 + 1 | Agent loop 跑通 |
| M2 | Phase 2 | 有工具、有本地持久化 |
| M3 | Phase 3 | 股票场景闭环 |
| M4 | Phase 4 + 5 | 长会话可靠，报告可用 |
| M5 | Phase 6 | 本地客户端 |
| M6 | Phase 7 | 多 Agent 实验 |

## 第一阶段不建议引入的依赖

| 依赖 | 原因 |
| --- | --- |
| LangChain | 会隐藏 Agent loop 和上下文管理的学习点 |
| LangGraph | 后期多 Agent 再评估 |
| PostgreSQL | 本地应用不需要 |
| Redis | 本地应用不需要 |
| Docker | 开发期不需要 |
| Kubernetes | 明显过度设计 |
| Electron | 最后再做 UI 壳 |

## 当前下一步

进入 Phase 0：

1. 初始化 `niulai` 项目。
2. 安装基础依赖。
3. 建立 `src/agent/loop.ts` 的类型和最小实现。
4. 添加 `pnpm niulai --help`。
5. 用 mock provider 写第一个 Agent loop 测试。
