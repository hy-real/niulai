# apps/desktop —— 桌面壳（P5 / P6）

**P5（Web 界面）与 P6 的壳已就位，差打包流水线。**

现有两部分：

- `web/` —— 三栏界面（单文件 `index.html`，无构建步骤）：左侧会话列表 /
  中间对话流（含工具调用卡片）/ 右侧留白；带断线恢复提示条
  （检测到未收尾 run 可一键恢复）。事件协议与 `events.py` 对齐。
- `main.py` —— PyWebView 桌面壳：起本地服务（`webapi.py`）+ 原生窗口。
  Windows 用系统自带 WebView2，macOS 用 WKWebView。

界面通过 **本地 HTTP + SSE（127.0.0.1）** 与内核通信，事件类型定义在
`packages/niulai/src/niulai/events.py`。内核不 import 这里的任何代码。

## 剩余工作（P6 收尾）

- GitHub Actions 出三平台安装包（Windows 优先）；
- 首次启动引导填 Key（`webview` 窗口内的引导页，Key 只进环境变量 / 凭据管理器）。

开发顺序不可颠倒：先在浏览器里把界面调顺（`niulai serve`，P4），
再套壳（P6）—— 前端代码完全复用，套壳只是加一层窗口。

运行：`pip install pywebview` 后 `python apps/desktop/main.py`；
不开窗口直接 `niulai serve` 用浏览器访问同样可用。
