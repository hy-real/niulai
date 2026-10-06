"""Niulai 桌面壳：起本地服务，开一个原生窗口加载它。

壳本身只负责"有个窗口"，界面与逻辑全在本地服务（webapi.py）里——
浏览器访问 http://127.0.0.1:<port> 与本窗口体验完全一致。

跨平台说明：
- Windows 用系统自带的 WebView2（Edge 内核，Win10/11 预装）；
- macOS 用 WKWebView。注意 webview.start() 必须占用主线程（macOS 要求），
  所以服务放在守护线程里跑；窗口关闭 → start() 返回 → 进程退出，
  守护线程随之结束（等同关掉了服务，在跑的任务会被放弃——与 CLI 的
  Ctrl-C 行为一致；优雅挂起等 P4 的 run journal）。

运行：pip install pywebview 后 `python apps/desktop/main.py`
"""

from __future__ import annotations

import threading

import webview

from niulai.webapi import DEFAULT_PORT, start_server


def main() -> None:
    port = DEFAULT_PORT
    server = threading.Thread(
        target=start_server, kwargs={"host": "127.0.0.1", "port": port}, daemon=True
    )
    server.start()

    webview.create_window(
        "Niulai — 本地 A 股助手",
        f"http://127.0.0.1:{port}",
        width=1100,
        height=760,
        min_size=(860, 600),
        text_select=True,  # 默认 False（壳层注入 user-select:none）；这是窗口参数，不是 start() 的
    )
    webview.start()  # 阻塞主线程直到窗口关闭


if __name__ == "__main__":
    main()
