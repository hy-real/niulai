"""niulai-mcp —— 把 Niulai 的 A 股数据源暴露成 MCP server。

**为什么单独成一个包**：Claude Code / Cursor 用户只想查个行情，
不应该被迫装上桌面应用那一整套依赖。独立 `pyproject.toml` 让它可以被单独安装：

    uvx niulai-mcp
    claude mcp add niulai -- uvx niulai-mcp

状态：**P9 才实现，当前仅占位。**
"""

__version__ = "0.1.0"
