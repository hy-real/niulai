# apps/desktop —— 桌面壳（P5 / P6）

**当前为空，属于计划中的工作。**

这里将来放两部分：

- `ui/` —— React 三栏界面：左侧会话列表 / 中间对话流（含工具调用卡片）/ 右侧自选股与报告
- `shell/` —— 桌面壳（PyWebView 优先，Tauri + Python sidecar 备选）

界面通过 **本地 HTTP + SSE** 与内核通信，事件类型定义在
`packages/niulai/src/niulai/events.py`。内核不 import 这里的任何代码。

开发顺序不可颠倒：

1. 先在浏览器里把界面调顺（`niulai serve`，P4）
2. 再套壳（P6）—— 前端代码完全复用，套壳只是加一层窗口

先做 Web 版不是绕路，而是把同样的活减去打包工序。
