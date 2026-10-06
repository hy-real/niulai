"""任务级 eval 回归集（P3.5）。

与 packages/niulai/tests 的分工：
- tests/        mock LLM，无网络，验证**内核机制**（回灌、熔断、裁剪……）
- evals/        真 LLM，按需跑，验证**端到端行为**（选没选对工具、有没有越界）

用法（仓库根目录）：
    .venv\\Scripts\\python -m evals.run            # 跑全部
    .venv\\Scripts\\python -m evals.run quote      # 只跑 id 含 "quote" 的
    .venv\\Scripts\\python -m evals.run --list     # 列出任务清单
"""
